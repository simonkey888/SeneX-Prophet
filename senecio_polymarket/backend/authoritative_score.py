"""AUD-059 authoritative statistical-truth contract.

Settlement truth is delegated exclusively to SCORE-002's proof gate. Raw
proof-qualified rows remain available for diagnostics, but statistical authority
uses a deterministic, symbol-scoped, non-overlapping 1h cohort. Persisted
``confidence`` is raw model conviction, not a validated P(correct), therefore
Brier/ECE derived from it are diagnostic-only and authority fails closed until
prospective/OOS probability semantics are validated.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable

from .settlement_proof import filter_proof_qualified

MIN_GLOBAL_N = 100
MIN_DIRECTION_N = 30
LONG_MIN_WIN_RATE_PCT = 50.0
SHORT_MIN_WIN_RATE_PCT = 55.0
GLOBAL_MIN_WIN_RATE_PCT = 52.0
MIN_WILSON_LOWER = 0.50
MAX_BRIER = 0.25
MAX_ECE = 0.10
INDEPENDENT_HORIZON_S = 3600.0
CONFIDENCE_SEMANTICS = "RAW_CONVICTION"
CONFIDENCE_PROBABILITY_SEMANTICS = "UNVALIDATED"


def _normalize_symbol(value: Any) -> str:
    return str(value or "").upper().replace("/", "").replace("-", "").strip()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _confidence(row: dict[str, Any]) -> float | None:
    value = _number(row.get("confidence"))
    if value is None or not 0.0 <= value <= 1.0:
        return None
    return value


def _ts_seconds(row: dict[str, Any]) -> float:
    value = row.get("ts")
    if not value:
        return math.inf
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return math.inf


def _stable_row_key(row: dict[str, Any]) -> tuple[float, str, str]:
    """Stable ordering independent of caller input order, including ts ties."""
    row_id = row.get("id")
    canonical = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
    return (_ts_seconds(row), str(row_id) if row_id is not None else "", canonical)


def independent_1h_cohort(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select deterministic non-overlapping 1h observations from one symbol.

    Callers must pass proof-qualified rows for a single symbol. The first row in
    timestamp order is selected, then another row is selected only when its
    timestamp is at least 3600 seconds after the last selected timestamp.
    """
    selected: list[dict[str, Any]] = []
    last_ts: float | None = None
    for row in sorted(list(rows), key=_stable_row_key):
        ts = _ts_seconds(row)
        if not math.isfinite(ts):
            continue
        if last_ts is None or ts >= last_ts + INDEPENDENT_HORIZON_S:
            selected.append(row)
            last_ts = ts
    return selected


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
    """Diagnostic ECE treating raw conviction *as if* P(correct); not authority."""
    if not rows:
        return None
    confidence_rows: list[tuple[float, bool]] = []
    for row in rows:
        confidence = _confidence(row)
        if confidence is None:
            return None
        confidence_rows.append((confidence, row.get("outcome") == "WIN"))
    total = len(confidence_rows)
    error = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        bucket = [
            item for item in confidence_rows
            if lower <= item[0] < upper or (index == bins - 1 and item[0] == 1.0)
        ]
        if not bucket:
            continue
        mean_confidence = sum(item[0] for item in bucket) / len(bucket)
        accuracy = sum(item[1] for item in bucket) / len(bucket)
        error += len(bucket) / total * abs(accuracy - mean_confidence)
    return error


def _bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(1 for row in rows if row.get("outcome") == "WIN")
    losses = sum(1 for row in rows if row.get("outcome") == "LOSS")
    n = wins + losses
    return {
        "verified": n,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round((wins / n * 100.0) if n else 0.0, 2),
    }


def _window_outcome(row: dict[str, Any], window: str) -> str | None:
    if window == "1h":
        return row.get("outcome")
    audit = row.get("audit") or {}
    dual = audit.get("outcomes_dual") if isinstance(audit, dict) else {}
    return dual.get("outcome_15m") if isinstance(dual, dict) else None


def _by_window(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Raw proof-qualified dual-window diagnostics. Never authority."""
    result: dict[str, dict[str, Any]] = {}
    for window in ("15m", "1h"):
        result[window] = {"diagnostic_only": True}
        total_wins = total_losses = 0
        for direction in ("LONG", "SHORT", "FLAT"):
            direction_rows = [
                row for row in rows
                if str(row.get("prediction") or "").upper() == direction
            ]
            wins = sum(1 for row in direction_rows if _window_outcome(row, window) == "WIN")
            losses = sum(1 for row in direction_rows if _window_outcome(row, window) == "LOSS")
            verified = wins + losses
            total_wins += wins
            total_losses += losses
            result[window][direction] = {
                "verified": verified,
                "wins": wins,
                "losses": losses,
                "win_rate_pct": round((wins / verified * 100.0) if verified else 0.0, 2),
                "diagnostic_only": True,
            }
        total = total_wins + total_losses
        result[window]["global"] = {
            "verified": total,
            "wins": total_wins,
            "losses": total_losses,
            "win_rate_pct": round((total_wins / total * 100.0) if total else 0.0, 2),
            "diagnostic_only": True,
        }
    return result


def _gate(bucket: dict[str, Any], *, min_n: int, threshold_pct: float) -> dict[str, Any]:
    n = int(bucket.get("verified") or 0)
    win_rate_pct = float(bucket.get("win_rate_pct") or 0.0)
    return {
        "pass": bool(n >= min_n and win_rate_pct >= threshold_pct),
        "win_rate_pct": win_rate_pct,
        "n": n,
        "threshold_pct": threshold_pct,
        "min_n": min_n,
        "n_source": "INDEPENDENT_NONOVERLAP_1H",
    }


def _authority_1h_payload(score: dict[str, Any]) -> dict[str, Any]:
    """Expose the sole control-safe 1h buckets derived from the cohort."""
    by_direction = score.get("by_direction") or {}

    def authority_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
        return {
            "verified": int(bucket.get("verified") or 0),
            "wins": int(bucket.get("wins") or 0),
            "losses": int(bucket.get("losses") or 0),
            "win_rate_pct": float(bucket.get("win_rate_pct") or 0.0),
            "n_source": "INDEPENDENT_NONOVERLAP_1H",
        }

    verified = int(score.get("verified") or 0)
    wins = int(score.get("wins") or 0)
    losses = int(score.get("losses") or 0)
    return {
        "symbol": score.get("symbol"),
        "cohort": "INDEPENDENT_NONOVERLAP_1H",
        "n_source": "INDEPENDENT_NONOVERLAP_1H",
        "LONG": authority_bucket(by_direction.get("LONG") or {}),
        "SHORT": authority_bucket(by_direction.get("SHORT") or {}),
        "FLAT": authority_bucket(by_direction.get("FLAT") or {}),
        "global": {
            "verified": verified,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round((wins / verified * 100.0) if verified else 0.0, 2),
            "n_source": "INDEPENDENT_NONOVERLAP_1H",
        },
    }


def _quality_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Authority-safe quality metrics over the independent cohort.

    Wilson is a valid authority statistic for Bernoulli outcomes. Brier/ECE are
    retained only as raw-conviction diagnostics. Their authority gates are hard
    false until a separate, prospectively/OOS validated probability is supplied.
    """
    n = len(rows)
    wins = sum(1 for row in rows if row.get("outcome") == "WIN")
    wilson = _wilson_lower(wins, n) if n else None
    confidences = [_confidence(row) for row in rows]
    confidence_complete = bool(n and all(value is not None for value in confidences))
    brier = None
    ece = None
    if confidence_complete:
        brier = sum(
            (float(confidences[index]) - (1.0 if row.get("outcome") == "WIN" else 0.0)) ** 2
            for index, row in enumerate(rows)
        ) / n
        ece = _ece(rows)

    rounded_wilson = round(wilson, 6) if wilson is not None else None
    rounded_brier = round(brier, 6) if brier is not None else None
    rounded_ece = round(ece, 6) if ece is not None else None
    return {
        "authority_rows": n,
        "authority_n_source": "INDEPENDENT_NONOVERLAP_1H",
        "confidence_semantics": CONFIDENCE_SEMANTICS,
        "confidence_probability_semantics": CONFIDENCE_PROBABILITY_SEMANTICS,
        "confidence_complete": confidence_complete,
        "valid_confidence_n": sum(value is not None for value in confidences),
        "wilson_lower_95": rounded_wilson,
        # Backward-compatible names retained, explicitly diagnostic-only.
        "raw_confidence_brier": rounded_brier,
        "raw_confidence_ece": rounded_ece,
        "raw_confidence_brier_diagnostic_only": True,
        "raw_confidence_ece_diagnostic_only": True,
        "gates": {
            "confidence_complete": {
                "pass": confidence_complete,
                "required": True,
            },
            "wilson_lower_95": {
                "pass": bool(wilson is not None and wilson > MIN_WILSON_LOWER),
                "value": rounded_wilson,
                "threshold": MIN_WILSON_LOWER,
                "operator": ">",
                "n_source": "INDEPENDENT_NONOVERLAP_1H",
            },
            "brier": {
                "pass": False,
                "value": rounded_brier,
                "threshold": MAX_BRIER,
                "operator": "<",
                "diagnostic_only": True,
                "blocked_reason": "UNVALIDATED_PROBABILITY_SEMANTICS",
            },
            "ece": {
                "pass": False,
                "value": rounded_ece,
                "threshold": MAX_ECE,
                "operator": "<=",
                "diagnostic_only": True,
                "blocked_reason": "UNVALIDATED_PROBABILITY_SEMANTICS",
            },
        },
    }


def _empty_symbol_score(symbol: str | None) -> dict[str, Any]:
    empty = _bucket([])
    return {
        "symbol": symbol,
        "score_status": "UNKNOWN",
        "authoritative_score_pct": None,
        "observed_win_rate_pct": None,
        "observed_win_rate_diagnostic_only": True,
        "win_rate_pct": 0.0,
        "win_rate_pct_is_authoritative": False,
        "proof_qualified_rows_raw": 0,
        "independent_1h_rows": 0,
        "authority_cohort": "INDEPENDENT_NONOVERLAP_1H",
        "verified": 0,
        "wins": 0,
        "losses": 0,
        "posterior_accuracy": None,
        "by_direction": {direction: _bucket([]) for direction in ("LONG", "SHORT", "FLAT")},
        "gates": {
            "long_1h": _gate(empty, min_n=MIN_DIRECTION_N, threshold_pct=LONG_MIN_WIN_RATE_PCT),
            "short_1h": _gate(empty, min_n=MIN_DIRECTION_N, threshold_pct=SHORT_MIN_WIN_RATE_PCT),
            "global_1h": _gate(empty, min_n=MIN_GLOBAL_N, threshold_pct=GLOBAL_MIN_WIN_RATE_PCT),
        },
        "quality": _quality_metrics([]),
        "reasons": ["NO_PROOF_QUALIFIED_EVIDENCE"],
        "short_only_paper_mode": False,
    }


def _score_symbol(symbol: str, raw_rows: list[dict[str, Any]]) -> dict[str, Any]:
    independent = independent_1h_cohort(raw_rows)
    by_direction = {
        direction: _bucket([
            row for row in independent
            if str(row.get("prediction") or "").upper() == direction
        ])
        for direction in ("LONG", "SHORT", "FLAT")
    }
    authority_bucket = _bucket(independent)
    raw_bucket = _bucket(raw_rows)
    observed = raw_bucket["win_rate_pct"] if raw_bucket["verified"] else None
    posterior = (
        (authority_bucket["wins"] + 1.0) / (authority_bucket["verified"] + 2.0)
        if authority_bucket["verified"] else None
    )
    gates = {
        "long_1h": _gate(by_direction["LONG"], min_n=MIN_DIRECTION_N, threshold_pct=LONG_MIN_WIN_RATE_PCT),
        "short_1h": _gate(by_direction["SHORT"], min_n=MIN_DIRECTION_N, threshold_pct=SHORT_MIN_WIN_RATE_PCT),
        "global_1h": _gate(authority_bucket, min_n=MIN_GLOBAL_N, threshold_pct=GLOBAL_MIN_WIN_RATE_PCT),
    }
    quality = _quality_metrics(independent)
    global_n = authority_bucket["verified"]
    enough_samples = bool(
        global_n >= MIN_GLOBAL_N
        and by_direction["LONG"]["verified"] >= MIN_DIRECTION_N
        and by_direction["SHORT"]["verified"] >= MIN_DIRECTION_N
    )
    directional_gates_pass = all(gate["pass"] for gate in gates.values())
    authority_quality_gates_pass = all(gate["pass"] for gate in quality["gates"].values())

    reasons: list[str] = []
    if global_n == 0:
        score_status = "UNKNOWN"
        reasons.append("NO_INDEPENDENT_PROOF_QUALIFIED_EVIDENCE")
    elif not enough_samples:
        score_status = "INSUFFICIENT_EVIDENCE"
        reasons.append("INSUFFICIENT_INDEPENDENT_1H_SAMPLE")
    else:
        if not directional_gates_pass:
            reasons.append("DIRECTIONAL_OR_GLOBAL_EDGE_GATE_FAILED")
        if not quality["confidence_complete"]:
            reasons.append("INVALID_OR_MISSING_CONFIDENCE")
        if quality["gates"]["wilson_lower_95"]["pass"] is False:
            reasons.append("EDGE_NOT_DEMONSTRATED_AT_95PCT")
        reasons.append("CONFIDENCE_PROBABILITY_SEMANTICS_UNVALIDATED")
        score_status = "REJECTED"

    # Fail closed: raw conviction cannot authorize a calibrated score.
    authoritative_score_pct = (
        round(float(posterior) * 100.0, 2)
        if score_status == "CALIBRATED" and authority_quality_gates_pass and posterior is not None
        else None
    )
    short_only_paper_mode = bool(gates["short_1h"]["pass"] and not gates["long_1h"]["pass"])
    return {
        "symbol": symbol,
        "score_status": score_status,
        "authoritative_score_pct": authoritative_score_pct,
        "observed_win_rate_pct": observed,
        "observed_win_rate_diagnostic_only": True,
        "win_rate_pct": observed if observed is not None else 0.0,
        "win_rate_pct_is_authoritative": False,
        "proof_qualified_rows_raw": len(raw_rows),
        "independent_1h_rows": len(independent),
        "authority_cohort": "INDEPENDENT_NONOVERLAP_1H",
        "verified": global_n,
        "wins": authority_bucket["wins"],
        "losses": authority_bucket["losses"],
        "posterior_accuracy": round(posterior, 6) if posterior is not None else None,
        "raw_diagnostic": {
            "verified": raw_bucket["verified"],
            "wins": raw_bucket["wins"],
            "losses": raw_bucket["losses"],
            "win_rate_pct": observed,
            "diagnostic_only": True,
        },
        "by_direction": by_direction,
        "gates": gates,
        "quality": quality,
        "reasons": reasons,
        "short_only_paper_mode": short_only_paper_mode,
    }


def build_authoritative_score(
    rows: Iterable[dict[str, Any]],
    runner_state: dict[str, Any] | None = None,
    *,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Build a per-symbol score with raw diagnostics and independent authority."""
    _ = runner_state or {}  # API compatibility; runtime aggregates cannot inject authority.
    input_rows = list(rows)
    proof_qualified = filter_proof_qualified(input_rows)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in proof_qualified:
        row_symbol = _normalize_symbol(row.get("symbol"))
        if row_symbol:
            grouped[row_symbol].append(row)

    by_symbol = {
        row_symbol: _score_symbol(row_symbol, symbol_rows)
        for row_symbol, symbol_rows in sorted(grouped.items())
    }
    requested_symbol = _normalize_symbol(symbol) if symbol else None
    selected_rows: list[dict[str, Any]] = []
    if requested_symbol:
        selected_rows = grouped.get(requested_symbol, [])
        selected = by_symbol.get(requested_symbol) or _empty_symbol_score(requested_symbol)
        report_status = selected["score_status"]
        authoritative_score_pct = selected["authoritative_score_pct"]
    elif len(by_symbol) == 1:
        single_symbol = next(iter(by_symbol))
        selected_rows = grouped.get(single_symbol, [])
        selected = by_symbol[single_symbol]
        report_status = selected["score_status"]
        authoritative_score_pct = selected["authoritative_score_pct"]
    else:
        selected = None
        report_status = "MULTI_INSTRUMENT_REPORT" if by_symbol else "UNKNOWN"
        authoritative_score_pct = None

    by_window = _by_window(selected_rows) if selected is not None else {}
    selected_payload = selected or _empty_symbol_score(requested_symbol)
    authority_1h = _authority_1h_payload(selected_payload)
    selected_raw_n = len(selected_rows) if selected is not None else len(proof_qualified)
    selected_independent_n = (
        selected_payload["independent_1h_rows"] if selected is not None else 0
    )
    return {
        "version": "AUD-059-score-truth-v4",
        "score_scope": "PER_SYMBOL",
        "requested_symbol": requested_symbol,
        "score_status": report_status,
        "authoritative_score_pct": authoritative_score_pct,
        "authority_cohort": "INDEPENDENT_NONOVERLAP_1H",
        "authority_horizon_seconds": int(INDEPENDENT_HORIZON_S),
        "authority_n_field": "independent_1h_rows",
        "proof_qualified_rows_raw": selected_raw_n,
        "independent_1h_rows": selected_independent_n,
        # Backward compatible raw proof count; authority uses independent_1h_rows.
        "proof_qualified_rows": selected_raw_n,
        "observed_win_rate_pct": selected_payload["observed_win_rate_pct"] if selected else None,
        "observed_win_rate_diagnostic_only": True,
        "win_rate_pct": selected_payload["win_rate_pct"] if selected else 0.0,
        "win_rate_pct_is_authoritative": False,
        "confidence_semantics": CONFIDENCE_SEMANTICS,
        "confidence_probability_semantics": CONFIDENCE_PROBABILITY_SEMANTICS,
        "total_predictions": sum(
            1 for row in input_rows
            if requested_symbol is None or _normalize_symbol(row.get("symbol")) == requested_symbol
        ),
        "input_rows": len(input_rows),
        "verified": selected_payload["verified"] if selected else 0,
        "wins": selected_payload["wins"] if selected else 0,
        "losses": selected_payload["losses"] if selected else 0,
        "posterior_accuracy": selected_payload["posterior_accuracy"] if selected else None,
        "by_direction": selected_payload["by_direction"] if selected else {},
        "authority_1h": authority_1h,
        "by_window": by_window,
        "by_window_diagnostic_only": True,
        "gates": selected_payload["gates"] if selected else {},
        "quality": selected_payload["quality"] if selected else {},
        "reasons": selected_payload["reasons"] if selected else ["SYMBOL_REQUIRED_FOR_AUTHORITATIVE_SCORE"],
        "by_symbol": by_symbol,
        "minimum_evidence": {
            "global_n": MIN_GLOBAL_N,
            "direction_n": MIN_DIRECTION_N,
            "n_source": "INDEPENDENT_NONOVERLAP_1H",
        },
        "quality_thresholds": {
            "min_wilson_lower_95": MIN_WILSON_LOWER,
            "max_brier": MAX_BRIER,
            "max_ece": MAX_ECE,
            "brier_ece_authority_enabled": False,
            "blocked_reason": "UNVALIDATED_PROBABILITY_SEMANTICS",
        },
        "short_only_paper_mode": selected_payload["short_only_paper_mode"] if selected else False,
        "trade_mode": "PAPER",
        "orders_enabled": False,
        "live_capital_locked": True,
    }
