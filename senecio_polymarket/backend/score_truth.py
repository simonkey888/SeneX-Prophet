"""Truth contract for public Oracle scores.

Raw model conviction is not a probability and a WIN/LOSS label is not trusted
unless its 1-hour settlement evidence can be recomputed.  This module keeps
observed diagnostics separate from the nullable, authoritative score.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable


MIN_GLOBAL_N = 100
MIN_DIRECTION_N = 30
MIN_WILSON_LOWER = 0.50
MAX_BRIER = 0.25
MAX_ECE = 0.10


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _wilson_lower(wins: int, total: int, z: float = 1.959963984540054) -> float:
    if total <= 0:
        return 0.0
    observed = wins / total
    denominator = 1.0 + z * z / total
    centre = observed + z * z / (2.0 * total)
    radius = z * math.sqrt(
        (observed * (1.0 - observed) + z * z / (4.0 * total)) / total
    )
    return max(0.0, (centre - radius) / denominator)


def _ece(rows: list[dict[str, Any]], bins: int = 10) -> float | None:
    if not rows:
        return None
    total = len(rows)
    error = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        bucket = [
            row for row in rows
            if lower <= row["confidence"] < upper
            or (index == bins - 1 and row["confidence"] == 1.0)
        ]
        if not bucket:
            continue
        mean_confidence = sum(row["confidence"] for row in bucket) / len(bucket)
        accuracy = sum(row["outcome"] == "WIN" for row in bucket) / len(bucket)
        error += len(bucket) / total * abs(accuracy - mean_confidence)
    return error


def validate_1h_outcome(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Return a normalized row only when the stored 1h outcome is provable."""
    symbol = str(row.get("symbol") or "").upper().replace("/", "")
    direction = str(row.get("prediction") or "").upper()
    outcome = str(row.get("outcome") or "").upper()
    confidence = _number(row.get("confidence"))
    price_now = _number(row.get("price_now"))
    audit = row.get("audit") or row.get("_audit") or {}
    dual = audit.get("outcomes_dual") if isinstance(audit, dict) else None

    if not symbol:
        return None, "MISSING_SYMBOL"
    if direction not in {"LONG", "SHORT"}:
        return None, "NOT_DIRECTIONAL"
    if confidence is None or not 0.0 <= confidence <= 1.0:
        return None, "INVALID_RAW_CONFIDENCE"
    prediction_at = _timestamp(row.get("ts") or row.get("timestamp"))
    if prediction_at is None:
        return None, "INVALID_TIMESTAMP"
    if not isinstance(dual, dict):
        return None, "MISSING_DUAL_WINDOW_PROOF"
    origin_proof = audit.get("origin_price_proof")
    if not isinstance(origin_proof, dict):
        return None, "MISSING_ORIGIN_PRICE_PROOF"
    origin_observed_at = _timestamp(origin_proof.get("observed_at"))
    origin_proof_price = _number(origin_proof.get("price"))
    if (
        origin_proof.get("proof_schema") != "oracle-origin-price-v1"
        or str(row.get("exchange_used") or "").lower() != "okx"
        or str(origin_proof.get("exchange") or "").lower() != "okx"
        or str(origin_proof.get("instrument") or "").upper().replace("/", "").replace("-", "") != symbol
        or origin_proof.get("price_source") != "public_ticker_best_bid"
        or origin_observed_at is None
        or abs((origin_observed_at - prediction_at).total_seconds()) > 1.0
        or origin_proof_price is None
        or price_now is None
        or not math.isclose(origin_proof_price, price_now, rel_tol=0.0, abs_tol=1e-9)
    ):
        return None, "INVALID_ORIGIN_PRICE_PROOF"
    if (
        dual.get("proof_schema") != "oracle-settlement-proof-v1"
        or dual.get("price_source") != "okx_public_ohlcv"
        or dual.get("settlement_method") != "historical_1m_close_containing_target"
    ):
        return None, "UNVERIFIED_SETTLEMENT_METHOD"
    if dual.get("primary_window") != "1h":
        return None, "PRIMARY_WINDOW_NOT_1H"
    settled_at = _timestamp(dual.get("settled_at"))
    if settled_at is None or (settled_at - prediction_at).total_seconds() < 3600:
        return None, "SETTLED_BEFORE_HORIZON_OR_INVALID"

    proved_outcome = str(dual.get("outcome_1h") or "").upper()
    if outcome not in {"WIN", "LOSS"} or proved_outcome != outcome:
        return None, "OUTCOME_PROOF_MISMATCH"
    price_1h = _number(dual.get("price_1h_later"))
    if price_now is None or price_now <= 0 or price_1h is None or price_1h <= 0:
        return None, "INVALID_SETTLEMENT_PRICE"

    observations = dual.get("settlement_observations")
    observation_1h = observations.get("1h") if isinstance(observations, dict) else None
    if not isinstance(observation_1h, dict):
        return None, "MISSING_SETTLEMENT_OBSERVATION"
    target_ts = _timestamp(observation_1h.get("target_ts"))
    target_ms = _number(observation_1h.get("target_ts_ms"))
    candle_open_ms = _number(observation_1h.get("candle_open_ts_ms"))
    candle_close_ms = _number(observation_1h.get("candle_close_ts_ms"))
    observed_price = _number(observation_1h.get("price"))
    expected_target = prediction_at.timestamp() + 3600.0
    if (
        observation_1h.get("proof_schema") != "oracle-settlement-observation-v1"
        or str(observation_1h.get("exchange") or "").lower() != "okx"
        or str(observation_1h.get("instrument") or "").upper().replace("/", "").replace("-", "") != symbol
        or observation_1h.get("timeframe") != "1m"
        or observation_1h.get("price_field") != "close"
        or target_ts is None
        or abs(target_ts.timestamp() - expected_target) > 0.001
        or target_ms is None
        or abs(target_ms - expected_target * 1000.0) > 1.0
        or candle_open_ms is None
        or candle_close_ms is None
        or abs((candle_close_ms - candle_open_ms) - 60_000.0) > 0.001
        or not candle_open_ms <= target_ms < candle_close_ms
        or settled_at.timestamp() * 1000.0 < candle_close_ms
        or observed_price is None
        or not math.isclose(observed_price, price_1h, rel_tol=0.0, abs_tol=1e-9)
    ):
        return None, "INVALID_SETTLEMENT_OBSERVATION"

    expected = (
        "WIN"
        if (direction == "LONG" and price_1h > price_now)
        or (direction == "SHORT" and price_1h < price_now)
        else "LOSS"
    )
    if expected != outcome:
        return None, "RECOMPUTED_OUTCOME_MISMATCH"

    return {
        "id": row.get("id"),
        "ts": (row.get("ts") or row.get("timestamp")),
        "symbol": symbol,
        "prediction": direction,
        "outcome": outcome,
        "confidence": confidence,
        "price_now": price_now,
        "price_1h_later": price_1h,
    }, None


def _metrics(rows: list[dict[str, Any]], *, minimum_n: int) -> dict[str, Any]:
    n = len(rows)
    wins = sum(row["outcome"] == "WIN" for row in rows)
    observed = wins / n if n else None
    posterior = (wins + 1.0) / (n + 2.0) if n else None
    wilson = _wilson_lower(wins, n) if n else None
    brier = (
        sum((row["confidence"] - (1.0 if row["outcome"] == "WIN" else 0.0)) ** 2 for row in rows) / n
        if n else None
    )
    ece = _ece(rows)

    reasons: list[str] = []
    if n < minimum_n:
        reasons.append("INSUFFICIENT_PROOF_QUALIFIED_SAMPLE")
    if n and wilson is not None and wilson <= MIN_WILSON_LOWER:
        reasons.append("EDGE_NOT_DEMONSTRATED_AT_95PCT")
    if n and brier is not None and brier >= MAX_BRIER:
        reasons.append("RAW_CONFIDENCE_NOT_BETTER_THAN_NEUTRAL_BRIER")
    if n and ece is not None and ece > MAX_ECE:
        reasons.append("RAW_CONFIDENCE_MISCALIBRATED")

    if not n:
        status = "UNKNOWN"
    elif n < minimum_n:
        status = "INSUFFICIENT_EVIDENCE"
    elif reasons:
        status = "REJECTED"
    else:
        status = "CALIBRATED"

    return {
        "score_status": status,
        "authoritative_score_pct": round(posterior * 100, 2) if status == "CALIBRATED" else None,
        "observed_win_rate_pct": round(observed * 100, 2) if observed is not None else None,
        "verified": n,
        "wins": wins,
        "losses": n - wins,
        "posterior_accuracy": round(posterior, 6) if posterior is not None else None,
        "wilson_lower_95": round(wilson, 6) if wilson is not None else None,
        "raw_confidence_brier": round(brier, 6) if brier is not None else None,
        "raw_confidence_ece": round(ece, 6) if ece is not None else None,
        "minimum_n": minimum_n,
        "reasons": reasons,
    }


def build_score_report(
    rows: Iterable[dict[str, Any]], *, requested_symbol: str | None = None
) -> dict[str, Any]:
    """Build a score report that never promotes unproved observations."""
    input_rows = list(rows)
    accepted: list[dict[str, Any]] = []
    rejected = Counter()
    seen: set[tuple[Any, ...]] = set()
    for row in input_rows:
        clean, reason = validate_1h_outcome(row)
        if clean is None:
            rejected[reason or "UNKNOWN_REJECTION"] += 1
        else:
            evidence_key = (
                clean["symbol"],
                clean["id"] if clean["id"] is not None else clean["ts"],
                clean["prediction"],
            )
            if evidence_key in seen:
                rejected["DUPLICATE_EVIDENCE"] += 1
                continue
            seen.add(evidence_key)
            accepted.append(clean)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in accepted:
        grouped[row["symbol"]].append(row)

    by_symbol: dict[str, Any] = {}
    for symbol, symbol_rows in sorted(grouped.items()):
        by_symbol[symbol] = {
            **_metrics(symbol_rows, minimum_n=MIN_GLOBAL_N),
            "by_direction": {
                direction: _metrics(
                    [row for row in symbol_rows if row["prediction"] == direction],
                    minimum_n=MIN_DIRECTION_N,
                )
                for direction in ("LONG", "SHORT")
            },
        }

    normalized_request = requested_symbol.upper().replace("/", "") if requested_symbol else None
    if normalized_request:
        selected = by_symbol.get(normalized_request) or _metrics([], minimum_n=MIN_GLOBAL_N)
        report_status = selected["score_status"]
        authoritative_score = selected["authoritative_score_pct"]
    else:
        selected = None
        report_status = "MULTI_INSTRUMENT_REPORT" if len(by_symbol) > 1 else (
            next(iter(by_symbol.values()))["score_status"] if by_symbol else "UNKNOWN"
        )
        authoritative_score = (
            next(iter(by_symbol.values()))["authoritative_score_pct"] if len(by_symbol) == 1 else None
        )

    return {
        "version": "oracle-score-truth-v1",
        "mode": "PAPER_ONLY",
        "orders_enabled": False,
        "live_capital_locked": True,
        "horizon_s": 3600,
        "score_semantics": "posterior probability of correct direction; populated only after proof and calibration gates",
        "score_status": report_status,
        "authoritative_score_pct": authoritative_score,
        "requested_symbol": normalized_request,
        "input_rows": len(input_rows),
        "proof_qualified_rows": len(accepted),
        "excluded_rows": len(input_rows) - len(accepted),
        "exclusion_reasons": dict(sorted(rejected.items())),
        "selected": selected,
        "by_symbol": by_symbol,
    }


def decorate_prediction(row: dict[str, Any]) -> dict[str, Any]:
    """Make raw prediction semantics explicit without rewriting old records."""
    result = dict(row)
    direction = str(result.get("prediction") or "FLAT").upper()
    result.update({
        "mode": "PAPER_ONLY",
        "orders_enabled": False,
        "live_capital_locked": True,
        "horizon_s": 3600,
        "publication_status": "RAW_UNCALIBRATED" if direction in {"LONG", "SHORT"} else "ABSTAIN",
        "confidence_semantics": "raw model conviction; not a calibrated probability",
        "authoritative_score_pct": None,
    })
    return result
