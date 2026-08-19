#!/usr/bin/env python3
"""AUD-068 prospective probability validation, offline/read-only.

Validates the frozen hypothesis that persisted decision-time
``audit.pipeline.step2_features.up_prob`` may represent a probability usable
for the canonical BTCUSDT 1h proof-qualified directional outcome.

This module does not calibrate, tune, mutate production, or grant probability
authority. ``confidence`` remains RAW_CONVICTION / UNVALIDATED.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

COHORT_START_TS = "2026-08-19T18:44:53Z"
TARGET_SYMBOL = "BTCUSDT"
HORIZON_SECONDS = 3600
MIN_GLOBAL_N = 100
MIN_LONG_N = 30
MIN_SHORT_N = 30
MAX_BRIER = 0.25
MAX_ECE = 0.10
MIN_WILSON_LOWER = 0.50
ALLOWED_EXCHANGES = {"okx", "kraken", "gate", "mexc", "bitget"}


class ValidationError(ValueError):
    pass


def canon(obj: Any) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_utc(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_symbol(value: Any) -> str:
    return str(value or "").upper().replace("/", "").replace("-", "").strip()


def contract_symbol(value: Any) -> str:
    s = str(value or "").upper().strip()
    if "/" in s:
        return s
    if s.endswith("USDT") and len(s) > 4:
        return f"{s[:-4]}/USDT"
    return s


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def normalize_exchange(value: Any) -> str | None:
    exchange = str(value or "").strip().lower()
    return exchange if exchange in ALLOWED_EXCHANGES else None


def target_epoch_ms(ts: Any, seconds: int) -> int | None:
    dt = parse_utc(ts)
    if dt is None or seconds not in (900, 3600):
        return None
    return int((dt + timedelta(seconds=seconds)).timestamp() * 1000)


def directional_outcome(direction: str, origin: Any, later: Any) -> str | None:
    origin_n, later_n = finite_number(origin), finite_number(later)
    if origin_n is None or later_n is None or origin_n <= 0 or later_n <= 0:
        return None
    direction = str(direction or "").upper()
    if direction == "LONG":
        return "WIN" if later_n > origin_n else "LOSS"
    if direction == "SHORT":
        return "WIN" if later_n < origin_n else "LOSS"
    return None


def price_match(left: Any, right: Any) -> bool:
    a, b = finite_number(left), finite_number(right)
    return a is not None and b is not None and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def valid_price_evidence(evidence: Any, *, exchange: str, symbol: str, ts: Any, window: int) -> bool:
    if not isinstance(evidence, dict) or evidence.get("version") != "historical-price-evidence-v1":
        return False
    expected_exchange = normalize_exchange(exchange)
    expected_symbol = contract_symbol(symbol)
    expected_target = target_epoch_ms(ts, window)
    if expected_exchange is None or not expected_symbol or expected_target is None:
        return False
    if evidence.get("source") != expected_exchange or evidence.get("symbol") != expected_symbol:
        return False
    try:
        evidence_window = int(evidence.get("window_seconds"))
        target = int(evidence.get("target_epoch_ms"))
        candle_open = int(evidence.get("candle_open_epoch_ms"))
        candle_close = int(evidence.get("candle_close_epoch_ms"))
        interval = int(evidence.get("candle_interval_ms"))
        price = float(evidence.get("price"))
    except Exception:
        return False
    if evidence_window != window or target != expected_target or interval != 60000:
        return False
    if candle_close != candle_open + interval or not (candle_open <= target < candle_close):
        return False
    if not math.isfinite(price) or price <= 0:
        return False
    observed_at = parse_utc(evidence.get("observed_at"))
    if observed_at is None:
        return False
    return int(observed_at.timestamp() * 1000) >= candle_close


def proof_qualified(row: Any) -> bool:
    """Independent AUD-063 1h proof qualification; no production scorer import."""
    if not isinstance(row, dict) or row.get("outcome") not in {"WIN", "LOSS"}:
        return False
    direction = str(row.get("prediction") or "").upper()
    if direction not in {"LONG", "SHORT"}:
        return False
    audit = row.get("audit") or {}
    if not isinstance(audit, dict):
        return False
    origin = audit.get("origin_price_v1")
    dual = audit.get("outcomes_dual")
    if not isinstance(origin, dict) or not isinstance(dual, dict):
        return False
    if origin.get("version") != "origin-price-v1":
        return False
    row_ts, origin_ts = parse_utc(row.get("ts")), parse_utc(origin.get("timestamp"))
    if row_ts is None or origin_ts is None or row_ts != origin_ts:
        return False
    exchange = normalize_exchange(row.get("exchange_used"))
    if exchange is None or normalize_exchange(origin.get("source")) != exchange:
        return False
    symbol = normalize_symbol(row.get("symbol"))
    if not symbol:
        return False
    values = [
        finite_number(origin.get("price")),
        finite_number(row.get("price_now")),
        finite_number(dual.get("price_15m_later")),
        finite_number(dual.get("price_1h_later")),
    ]
    if any(v is None or v <= 0 for v in values):
        return False
    origin_price, row_price, p15, p60 = values
    if not price_match(origin_price, row_price):
        return False
    if dual.get("primary_window") != "1h" or dual.get("settlement_contract_version") != "aud063-v1":
        return False
    if dual.get("outcome_15m") not in {"WIN", "LOSS"} or dual.get("outcome_1h") not in {"WIN", "LOSS"}:
        return False
    if dual.get("outcome_1h") != row.get("outcome"):
        return False
    evidence = dual.get("price_evidence_v1")
    if not isinstance(evidence, dict):
        return False
    e15, e60 = evidence.get("15m"), evidence.get("1h")
    if not valid_price_evidence(e15, exchange=exchange, symbol=symbol, ts=row.get("ts"), window=900):
        return False
    if not valid_price_evidence(e60, exchange=exchange, symbol=symbol, ts=row.get("ts"), window=3600):
        return False
    if not price_match(p15, e15.get("price")) or not price_match(p60, e60.get("price")):
        return False
    observation = dual.get("settlement_observation_v1")
    if not isinstance(observation, dict) or observation.get("version") != "settlement-observation-v1":
        return False
    observed_at = parse_utc(observation.get("observed_at"))
    if observed_at is None or observed_at < row_ts + timedelta(seconds=HORIZON_SECONDS):
        return False
    if target_epoch_ms(row.get("ts"), 3600) != int(e60.get("target_epoch_ms")):
        return False
    return (
        dual.get("outcome_15m") == directional_outcome(direction, origin_price, p15)
        and dual.get("outcome_1h") == directional_outcome(direction, origin_price, p60)
    )


def persisted_up_prob(row: dict[str, Any]) -> tuple[float | None, str | None]:
    audit = row.get("audit") or {}
    pipeline = audit.get("pipeline") or {} if isinstance(audit, dict) else {}
    step2 = pipeline.get("step2_features") or {} if isinstance(pipeline, dict) else {}
    if not isinstance(step2, dict) or "up_prob" not in step2:
        return None, "MISSING_PERSISTED_UP_PROB"
    value = finite_number(step2.get("up_prob"))
    if value is None:
        return None, "MALFORMED_PERSISTED_UP_PROB"
    if not 0.0 <= value <= 1.0:
        return None, "PERSISTED_UP_PROB_OUT_OF_RANGE"
    return value, None


def candidate_probability(row: dict[str, Any]) -> tuple[float | None, str | None]:
    direction = str(row.get("prediction") or "").upper()
    if direction not in {"LONG", "SHORT"}:
        return None, "PREDICTION_NOT_DIRECTIONAL"
    up_prob, error = persisted_up_prob(row)
    if error:
        return None, error
    assert up_prob is not None
    return (up_prob if direction == "LONG" else 1.0 - up_prob), None


def stable_key(row: dict[str, Any]) -> tuple[float, str]:
    dt = parse_utc(row.get("ts"))
    epoch = dt.timestamp() if dt is not None else math.inf
    return epoch, str(row.get("id") if row.get("id") is not None else "")


def proof_digest(row: dict[str, Any]) -> str:
    audit = row.get("audit") or {}
    payload = {
        "id": row.get("id"),
        "ts": row.get("ts"),
        "symbol": row.get("symbol"),
        "prediction": row.get("prediction"),
        "outcome": row.get("outcome"),
        "origin_price_v1": audit.get("origin_price_v1") if isinstance(audit, dict) else None,
        "outcomes_dual": audit.get("outcomes_dual") if isinstance(audit, dict) else None,
    }
    return sha256_bytes(canon(payload))


def eligibility(row: dict[str, Any], cutoff: datetime) -> tuple[bool, str | None, float | None]:
    ts = parse_utc(row.get("ts"))
    if ts is None:
        return False, "INVALID_TS", None
    if ts <= cutoff:
        return False, "AT_OR_BEFORE_PROSPECTIVE_CUTOFF", None
    if normalize_symbol(row.get("symbol")) != TARGET_SYMBOL:
        return False, "WRONG_SYMBOL", None
    direction = str(row.get("prediction") or "").upper()
    if direction not in {"LONG", "SHORT"}:
        return False, "PREDICTION_NOT_DIRECTIONAL", None
    p_correct, error = candidate_probability(row)
    if error:
        return False, error, None
    if row.get("outcome") not in {"WIN", "LOSS"}:
        return False, "OUTCOME_NOT_SETTLED", None
    if not proof_qualified(row):
        return False, "AUD063_PROOF_NOT_QUALIFIED", None
    return True, None, p_correct


def independent_nonoverlap(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    last_ts: datetime | None = None
    for row in sorted(rows, key=stable_key):
        ts = parse_utc(row.get("ts"))
        if ts is None:
            continue
        if last_ts is None or ts >= last_ts + timedelta(seconds=HORIZON_SECONDS):
            selected.append(row)
            last_ts = ts
    return selected


def row_evidence(row: dict[str, Any], position: int) -> dict[str, Any]:
    p_correct, error = candidate_probability(row)
    if error or p_correct is None:
        raise ValidationError(error or "INVALID_CANDIDATE_PROBABILITY")
    audit = row.get("audit") or {}
    step2 = ((audit.get("pipeline") or {}).get("step2_features") or {}) if isinstance(audit, dict) else {}
    return {
        "id": row.get("id"),
        "ts": row.get("ts"),
        "prediction": str(row.get("prediction") or "").upper(),
        "persisted_up_prob": float(step2.get("up_prob")),
        "p_correct_candidate": p_correct,
        "outcome": row.get("outcome"),
        "proof_digest_sha256": proof_digest(row),
        "selection_position": position,
    }


def maturity_prefix(selected: list[dict[str, Any]]) -> int | None:
    long_n = short_n = 0
    for idx, row in enumerate(selected, start=1):
        direction = str(row.get("prediction") or "").upper()
        if direction == "LONG":
            long_n += 1
        elif direction == "SHORT":
            short_n += 1
        if idx >= MIN_GLOBAL_N and long_n >= MIN_LONG_N and short_n >= MIN_SHORT_N:
            return idx
    return None


def wilson_lower(wins: int, n: int, z: float = 1.959963984540054) -> float | None:
    if n <= 0:
        return None
    p = wins / n
    denominator = 1 + z * z / n
    centre = p + z * z / (2 * n)
    radius = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - radius) / denominator)


def ece_10(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    bins: list[list[tuple[float, int]]] = [[] for _ in range(10)]
    for row in rows:
        p = float(row["p_correct_candidate"])
        y = 1 if row["outcome"] == "WIN" else 0
        index = min(9, int(p * 10))
        bins[index].append((p, y))
    total = len(rows)
    ece = 0.0
    for bucket in bins:
        if not bucket:
            continue
        mean_p = sum(p for p, _ in bucket) / len(bucket)
        mean_y = sum(y for _, y in bucket) / len(bucket)
        ece += (len(bucket) / total) * abs(mean_p - mean_y)
    return ece


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise ValidationError("METRICS_REQUIRE_NONEMPTY_ROWS")
    def bucket(part: list[dict[str, Any]]) -> dict[str, Any]:
        wins = sum(row["outcome"] == "WIN" for row in part)
        losses = sum(row["outcome"] == "LOSS" for row in part)
        n = wins + losses
        return {
            "n": n,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wins / n * 100, 6) if n else None,
        }
    global_bucket = bucket(rows)
    long_bucket = bucket([row for row in rows if row["prediction"] == "LONG"])
    short_bucket = bucket([row for row in rows if row["prediction"] == "SHORT"])
    brier = sum((float(row["p_correct_candidate"]) - (1 if row["outcome"] == "WIN" else 0)) ** 2 for row in rows) / len(rows)
    mean_p = sum(float(row["p_correct_candidate"]) for row in rows) / len(rows)
    ece = ece_10(rows)
    wilson = wilson_lower(global_bucket["wins"], global_bucket["n"])
    return {
        "global": global_bucket,
        "LONG": long_bucket,
        "SHORT": short_bucket,
        "mean_p_correct_candidate": round(mean_p, 12),
        "brier": round(brier, 12),
        "ece_10_equal_width": round(ece, 12) if ece is not None else None,
        "wilson_lower_95": round(wilson, 12) if wilson is not None else None,
        "thresholds_reused_not_tuned": {
            "MAX_BRIER": MAX_BRIER,
            "MAX_ECE": MAX_ECE,
            "MIN_WILSON_LOWER": MIN_WILSON_LOWER,
        },
        "diagnostic_threshold_checks": {
            "brier_le_max": brier <= MAX_BRIER,
            "ece_le_max": ece is not None and ece <= MAX_ECE,
            "wilson_gt_min": wilson is not None and wilson > MIN_WILSON_LOWER,
        },
    }


def validate_capture(capture: dict[str, Any], cutoff_text: str = COHORT_START_TS) -> dict[str, Any]:
    cutoff = parse_utc(cutoff_text)
    if cutoff is None:
        raise ValidationError("INVALID_COHORT_CUTOFF")
    rows = capture.get("rows")
    if not isinstance(rows, list):
        raise ValidationError("CAPTURE_ROWS_NOT_LIST")
    eligible: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            exclusions.append({"id": None, "reason": "ROW_NOT_OBJECT"})
            continue
        ok, reason, _ = eligibility(row, cutoff)
        if ok:
            eligible.append(row)
        else:
            exclusions.append({"id": row.get("id"), "ts": row.get("ts"), "reason": reason})
    selected_rows = independent_nonoverlap(eligible)
    selected_evidence = [row_evidence(row, idx) for idx, row in enumerate(selected_rows, start=1)]
    cohort_sha = sha256_bytes(canon(selected_evidence))
    mature_at = maturity_prefix(selected_evidence)
    status = "MATURE" if mature_at is not None else "WARMUP"
    terminal_rows = selected_evidence[:mature_at] if mature_at is not None else []
    return {
        "order": "AUD-068",
        "name": "PROSPECTIVE_PROBABILITY_VALIDATION_V1",
        "authority_comment": 5346442886,
        "cohort_start_ts": cutoff_text,
        "cohort_start_rule": "prediction.ts > THIS_COMMENT.created_at",
        "symbol": TARGET_SYMBOL,
        "horizon": "1h",
        "candidate_mapping": {
            "LONG": "p_correct_candidate = persisted_up_prob",
            "SHORT": "p_correct_candidate = 1 - persisted_up_prob",
            "FLAT": "EXCLUDED",
            "source_field": "audit.pipeline.step2_features.up_prob",
        },
        "captured_row_count": len(rows),
        "eligible_pre_nonoverlap_n": len(eligible),
        "current_prospective_n": len(selected_evidence),
        "selected_long_n": sum(row["prediction"] == "LONG" for row in selected_evidence),
        "selected_short_n": sum(row["prediction"] == "SHORT" for row in selected_evidence),
        "selected_rows": selected_evidence,
        "excluded_rows": exclusions,
        "selected_cohort_sha256": cohort_sha,
        "maturity_rule": {"GLOBAL_N": MIN_GLOBAL_N, "LONG_N": MIN_LONG_N, "SHORT_N": MIN_SHORT_N},
        "mature_prefix_n": mature_at,
        "prospective_eval_status": status,
        "probability_semantics_validated": "NO",
        "confidence_semantics_unchanged": "RAW_CONVICTION / UNVALIDATED",
        "terminal_metrics": metrics(terminal_rows) if terminal_rows else None,
        "calibration_or_tuning_performed": False,
        "production_mutation": 0,
        "runtime017_mutation": 0,
        "threshold_tuning": 0,
        "base_weight_tuning": 0,
        "real_trading": 0,
        "orders_enabled": False,
    }


def write_output(input_path: Path, output_path: Path) -> dict[str, Any]:
    capture = json.loads(input_path.read_text(encoding="utf-8"))
    result = validate_capture(capture)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        assert parse_utc(COHORT_START_TS) is not None
        assert candidate_probability({"prediction": "LONG", "audit": {"pipeline": {"step2_features": {"up_prob": 0.7}}}})[0] == 0.7
        assert math.isclose(candidate_probability({"prediction": "SHORT", "audit": {"pipeline": {"step2_features": {"up_prob": 0.7}}}})[0], 0.3)
        print(json.dumps({"status": "PASS", "order": "AUD-068", "probability_semantics_validated": "NO"}, sort_keys=True))
        return 0
    if args.input is None or args.out is None:
        parser.error("--input and --out are required unless --self-test is used")
    result = write_output(args.input, args.out)
    print(f"CURRENT_PROSPECTIVE_N={result['current_prospective_n']}")
    print(f"PROSPECTIVE_EVAL_STATUS={result['prospective_eval_status']}")
    print("PROBABILITY_SEMANTICS_VALIDATED=NO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
