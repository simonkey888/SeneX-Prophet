"""BTC V2 shadow calibrator.

Read-only, fail-closed overlay for the existing oracle.  It never changes the
V1 prediction and never places orders; it only checks whether recent verified
BTC outcomes support the reported direction/confidence.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable

from .score_truth import validate_1h_outcome


MIN_COHORT_N = 30
MIN_POSTERIOR_ACCURACY = 0.52
MIN_WILSON_LOWER = 0.50
CONFIDENCE_BIN_WIDTH = 0.10
MAX_SOURCE_AGE_S = 2100


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _wilson_lower(wins: int, total: int, z: float = 1.959963984540054) -> float:
    if total <= 0:
        return 0.0
    p = wins / total
    denominator = 1.0 + z * z / total
    centre = p + z * z / (2.0 * total)
    radius = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total)
    return max(0.0, (centre - radius) / denominator)


def _clean_verified(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    clean: list[dict[str, Any]] = []
    for row in rows:
        normalized, _ = validate_1h_outcome(row)
        if normalized is not None and normalized["symbol"] == "BTCUSDT":
            clean.append(normalized)
    return sorted(clean, key=lambda row: str(row.get("ts") or ""), reverse=True)


def _utc_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    wins = sum(row["outcome"] == "WIN" for row in rows)
    posterior = (wins + 1.0) / (n + 2.0)  # Beta(1, 1), explicit shrinkage.
    brier = (
        sum((row["confidence"] - (1.0 if row["outcome"] == "WIN" else 0.0)) ** 2 for row in rows) / n
        if n else None
    )
    return {
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "observed_accuracy": wins / n if n else None,
        "posterior_accuracy": posterior,
        "wilson_lower_95": _wilson_lower(wins, n),
        "reported_confidence_brier": brier,
    }


def evaluate_btc_shadow(
    current: dict[str, Any] | None,
    history: Iterable[dict[str, Any]],
    *,
    recent_limit: int = 100,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate a V1 BTC prediction against recent verified BTC outcomes."""
    current = current or {}
    direction = str(current.get("prediction") or "FLAT").upper()
    confidence = _number(current.get("confidence"))
    source_ts = current.get("ts") or current.get("timestamp")
    source_at = _utc_timestamp(source_ts)
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    source_age_s = (observed_at - source_at).total_seconds() if source_at else None
    verified = _clean_verified(history)[:recent_limit]

    base = {
        "version": "btc-shadow-v2.0",
        "mode": "PAPER_ONLY",
        "orders_enabled": False,
        "live_capital_locked": True,
        "source_prediction": direction,
        "source_confidence": confidence,
        "source_ts": source_ts,
        "source_age_s": round(source_age_s, 3) if source_age_s is not None else None,
        "confidence_semantics": "raw model conviction; not a calibrated probability",
        "authoritative_score_pct": None,
        "shadow_action": "FLAT",
        "gate_status": "UNKNOWN",
        "cohort": None,
        "thresholds": {
            "recent_limit": recent_limit,
            "min_cohort_n": MIN_COHORT_N,
            "min_posterior_accuracy": MIN_POSTERIOR_ACCURACY,
            "min_wilson_lower_95": MIN_WILSON_LOWER,
        },
        "reasons": [],
    }
    if direction not in {"LONG", "SHORT"} or confidence is None:
        base["reasons"] = ["NO_DIRECTIONAL_SOURCE_PREDICTION"]
        return base
    if str(current.get("symbol") or "").upper().replace("/", "") != "BTCUSDT":
        base["reasons"] = ["SOURCE_INSTRUMENT_MISMATCH"]
        return base
    if source_age_s is None or source_age_s < 0 or source_age_s > MAX_SOURCE_AGE_S:
        base["reasons"] = ["SOURCE_PREDICTION_STALE_OR_INVALID"]
        return base
    if confidence < 0.0 or confidence > 1.0:
        base["reasons"] = ["INVALID_RAW_CONFIDENCE"]
        return base

    bin_low = math.floor(confidence / CONFIDENCE_BIN_WIDTH) * CONFIDENCE_BIN_WIDTH
    if confidence == 1.0:
        bin_low = 0.9
    bin_high = min(1.0, bin_low + CONFIDENCE_BIN_WIDTH)
    exact = [
        row for row in verified
        if row["prediction"] == direction
        and (
            bin_low <= row["confidence"] < bin_high
            or (bin_high == 1.0 and row["confidence"] == 1.0)
        )
    ]
    directional = [row for row in verified if row["prediction"] == direction]
    if len(exact) >= MIN_COHORT_N:
        cohort_name, cohort = f"{direction}_{bin_low:.1f}_{bin_high:.1f}", exact
    elif len(directional) >= MIN_COHORT_N:
        cohort_name, cohort = f"{direction}_ALL_CONFIDENCE", directional
    else:
        cohort_name, cohort = f"{direction}_INSUFFICIENT", directional

    stats = _stats(cohort)
    base["cohort"] = {"name": cohort_name, **stats}
    if stats["n"] < MIN_COHORT_N:
        base["reasons"] = ["INSUFFICIENT_RECENT_VERIFIED_COHORT"]
        return base

    reasons: list[str] = []
    if stats["posterior_accuracy"] < MIN_POSTERIOR_ACCURACY:
        reasons.append("POSTERIOR_ACCURACY_BELOW_GATE")
    if stats["wilson_lower_95"] <= MIN_WILSON_LOWER:
        reasons.append("EDGE_NOT_DEMONSTRATED_AT_95PCT")
    if reasons:
        base["gate_status"] = "REJECT"
        base["reasons"] = reasons
        return base

    base["gate_status"] = "PASS"
    base["shadow_action"] = direction
    base["authoritative_score_pct"] = round(stats["posterior_accuracy"] * 100, 2)
    base["reasons"] = ["RECENT_CALIBRATION_GATE_PASSED"]
    return base
