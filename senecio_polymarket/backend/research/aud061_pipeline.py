"""Deterministic, read-only research gates for AUD-061.

The module consumes exported prediction rows and emits evidence only.  It has
no database client, no write-back path, and no production tuning hook.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from ..authoritative_score import independent_1h_cohort
from ..settlement_proof import filter_proof_qualified

AUDIT_VERSION = "AUD-061-R1-paper-research-v2"
HORIZONS = ("15m", "30m", "1h", "2h", "4h")
MIN_OOS_PAIRS = 30
FLAT_REASONS = (
    "NO_DIRECTION/NEUTRAL",
    "LOW_CONVICTION_GATE",
    "REGIME/HIGH_VOL_SHIELD",
    "LONG_BEAR_SUPPRESSION",
    "HIGH_NOISE",
    "NEGATIVE_OR_INSUFFICIENT_EV",
    "EXECUTION_INFEASIBLE",
    "LATENCY_COOLDOWN",
    "SIGNAL_DENSITY",
    "SIZE_TOO_SMALL",
    "MISSING_INPUT/DEGRADED_SOURCE",
    "OTHER_EXPLICIT_REASON",
)


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _epoch(value: Any) -> float | None:
    try:
        if isinstance(value, (int, float)):
            number = float(value)
            return number / 1000.0 if number > 10_000_000_000 else number
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _audit(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("audit", row.get("_audit", {}))
    return value if isinstance(value, dict) else {}


def _pipeline(row: dict[str, Any]) -> dict[str, Any]:
    value = _audit(row).get("pipeline", {})
    return value if isinstance(value, dict) else {}


def settlement_observed_epoch(row: dict[str, Any]) -> float | None:
    """Return only an explicit settlement observation timestamp.

    Horizon expiry is deliberately not accepted as proof that an outcome was
    already persisted and visible to a historical decision.
    """
    dual = _audit(row).get("outcomes_dual") or {}
    if not isinstance(dual, dict):
        return None
    provenance = dual.get("settlement_observation_v1") or {}
    candidates = (
        provenance.get("observed_at") if isinstance(provenance, dict) else None,
        dual.get("settled_at"), dual.get("verified_at"), dual.get("reconciled_at"),
    )
    for value in candidates:
        parsed = _epoch(value)
        if parsed is not None:
            return parsed
    return None


def temporal_purged_split(
    rows: list[dict[str, Any]], *, train_fraction: float = 0.60,
    purge_seconds: int = 3600, embargo_seconds: int = 3600,
) -> dict[str, Any]:
    """Create a deterministic chronological split with a real time gap."""
    ordered = [
        row for row in sorted(
            rows, key=lambda item: (_epoch(item.get("ts")) or float("inf"), str(item.get("id") or "")),
        ) if _epoch(row.get("ts")) is not None
    ]
    if len(ordered) < 2:
        return {
            "status": "INSUFFICIENT_OOS_EVIDENCE", "train_ids": [],
            "purged_embargoed_ids": [], "evaluation_ids": [],
            "purge_seconds": purge_seconds, "embargo_seconds": embargo_seconds,
            "mechanically_disjoint": True, "minimum_gap_seconds": None,
        }
    split_index = max(1, min(len(ordered) - 1, int(len(ordered) * train_fraction)))
    train = ordered[:split_index]
    boundary = _epoch(train[-1].get("ts"))
    evaluation_start = float(boundary) + purge_seconds + embargo_seconds
    remainder = ordered[split_index:]
    evaluation = [row for row in remainder if float(_epoch(row.get("ts"))) >= evaluation_start]
    purged = [row for row in remainder if float(_epoch(row.get("ts"))) < evaluation_start]
    gap = (
        float(_epoch(evaluation[0].get("ts"))) - float(boundary)
        if evaluation else None
    )
    return {
        "status": "COMPLETE" if evaluation else "INSUFFICIENT_OOS_EVIDENCE",
        "train_ids": [row.get("id") for row in train],
        "purged_embargoed_ids": [row.get("id") for row in purged],
        "evaluation_ids": [row.get("id") for row in evaluation],
        "train_end_epoch": boundary,
        "evaluation_start_epoch": _epoch(evaluation[0].get("ts")) if evaluation else None,
        "purge_seconds": purge_seconds,
        "embargo_seconds": embargo_seconds,
        "mechanically_disjoint": not bool(
            set(row.get("id") for row in train) & set(row.get("id") for row in evaluation)
        ),
        "minimum_gap_seconds": gap,
    }


def classify_flat_reason(row: dict[str, Any]) -> str:
    if str(row.get("prediction") or "").upper() != "FLAT":
        return "DIRECTIONAL_EXECUTE"
    pipeline = _pipeline(row)
    step1 = pipeline.get("step1_market") or {}
    step2 = pipeline.get("step2_features") or {}
    action = _audit(row).get("action_vector") or {}
    reason = str(action.get("reason") or "").lower()
    if step2.get("long_suppressed_by_regime"):
        return "LONG_BEAR_SUPPRESSION"
    if "no_direction" in reason or step2.get("direction") == "NEUTRAL":
        return "NO_DIRECTION/NEUTRAL"
    if "low_conviction" in reason:
        return "LOW_CONVICTION_GATE"
    if "volatile_shield" in reason or "regime_guard" in reason:
        return "REGIME/HIGH_VOL_SHIELD"
    if "high_noise" in reason:
        return "HIGH_NOISE"
    if "negative_ev" in reason:
        return "NEGATIVE_OR_INSUFFICIENT_EV"
    if "not_feasible" in reason:
        return "EXECUTION_INFEASIBLE"
    if "cooldown" in reason:
        return "LATENCY_COOLDOWN"
    if "signal_density" in reason:
        return "SIGNAL_DENSITY"
    if "size_too_small" in reason:
        return "SIZE_TOO_SMALL"
    availability = step1.get("feature_availability_v1") or {}
    if any((item or {}).get("status") in {"MISSING", "SOURCE_ERROR"} for item in availability.values()):
        return "MISSING_INPUT/DEGRADED_SOURCE"
    return "OTHER_EXPLICIT_REASON"


def flat_waterfall(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get("symbol") or "UNKNOWN").replace("/", "").upper()].append(row)

    def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
        counts = Counter(classify_flat_reason(row) for row in items)
        flat_n = sum(counts[key] for key in FLAT_REASONS)
        total = len(items)
        return {
            "total": total,
            "flat_n": flat_n,
            "directional_n": counts["DIRECTIONAL_EXECUTE"],
            "flat_rate": round(flat_n / total, 6) if total else None,
            "counts": {key: counts[key] for key in FLAT_REASONS},
            "rates": {key: round(counts[key] / total, 6) if total else None for key in FLAT_REASONS},
            "transition_loss": {key: counts[key] for key in FLAT_REASONS},
        }

    all_rows = [row for values in buckets.values() for row in values]
    return {
        "version": AUDIT_VERSION,
        "status": "COMPLETE",
        "per_symbol": {symbol: summarize(items) for symbol, items in sorted(buckets.items())},
        "aggregate_diagnostic_only": summarize(all_rows),
    }


def feature_availability(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    features = ("orderflow", "volume_delta", "bidask_imbalance", "funding_signal", "oi_momentum", "price_momentum")
    grouped: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        symbol = str(row.get("symbol") or "UNKNOWN").replace("/", "").upper()
        exchange = str(row.get("exchange_used") or _audit(row).get("exchange_used") or "UNKNOWN")
        step1 = _pipeline(row).get("step1_market") or {}
        explicit = step1.get("feature_availability_v1") or {}
        for feature in features:
            item = explicit.get(feature) if isinstance(explicit, dict) else None
            if isinstance(item, dict) and item.get("status"):
                status = str(item["status"])
            else:
                value = step1.get(feature)
                if value is None:
                    status = "MISSING"
                elif abs(float(value)) <= 1e-12:
                    status = "UNKNOWN_ZERO_CONFLATED"
                else:
                    status = "REAL_NONZERO_UNPROVEN_LEGACY"
            grouped[f"{exchange}|{symbol}|{feature}"][status] += 1
    return {
        "version": AUDIT_VERSION,
        "status": "COMPLETE",
        "grouping": "exchange|symbol|feature",
        "counts": {key: dict(sorted(counts.items())) for key, counts in sorted(grouped.items())},
        "legacy_zero_is_not_observed_zero": True,
    }


def _holm_adjust(p_values: list[float]) -> list[float]:
    order = sorted(range(len(p_values)), key=lambda i: p_values[i])
    adjusted = [1.0] * len(p_values)
    running = 0.0
    for rank, index in enumerate(order):
        value = min(1.0, (len(p_values) - rank) * p_values[index])
        running = max(running, value)
        adjusted[index] = running
    return adjusted


def horizon_research(rows: list[dict[str, Any]]) -> dict[str, Any]:
    # Use the conservative 1h non-overlap cohort for every horizon. Raw
    # overlapping prices remain descriptive only and never increase test N.
    qualified = independent_1h_cohort(filter_proof_qualified(rows))
    results = []
    p_values = []
    for horizon in HORIZONS:
        values = []
        for row in qualified:
            dual = _audit(row).get("outcomes_dual") or {}
            outcome = dual.get(f"outcome_{horizon}")
            price = dual.get(f"price_{horizon}_later")
            if outcome in {"WIN", "LOSS"} and price is not None:
                values.append(outcome == "WIN")
        wins = sum(values)
        n = len(values)
        # Conservative normal diagnostic; it is never authoritative here.
        if n:
            z = abs((wins / n - 0.5) / math.sqrt(0.25 / n))
            p_value = math.erfc(z / math.sqrt(2.0))
        else:
            p_value = 1.0
        p_values.append(p_value)
        results.append({"horizon": horizon, "n": n, "wins": wins, "win_rate": round(wins / n, 6) if n else None, "raw_p": round(p_value, 8)})
    adjusted = _holm_adjust(p_values)
    for item, value in zip(results, adjusted):
        item["holm_adjusted_p"] = round(value, 8)
        item["status"] = "COMPLETE" if item["n"] >= MIN_OOS_PAIRS else "INSUFFICIENT_OOS_EVIDENCE"
    return {
        "version": AUDIT_VERSION,
        "primary_authority_horizon": "1h",
        "multiple_horizon_correction": "HOLM",
        "status": "COMPLETE" if all(item["n"] >= MIN_OOS_PAIRS for item in results) else "INSUFFICIENT_OOS_EVIDENCE",
        "horizons": results,
    }


def threshold_research(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fail-closed OOS counterfactual plus a clearly in-sample diagnostic.

    Legacy rows do not preserve a replayable decision-time snapshot for FLAT
    decisions, so they cannot support a production-threshold counterfactual.
    """
    thresholds = (0.40, 0.50, 0.55, 0.60, 0.70, 0.80, 0.90)
    diagnostic_curve = []
    p_values = []
    cohort = independent_1h_cohort(filter_proof_qualified(rows))
    for threshold in thresholds:
        selected = [row for row in cohort if float(row.get("confidence") or 0.0) >= threshold]
        wins = sum(row.get("outcome") == "WIN" for row in selected)
        n = len(selected)
        z = abs((wins / n - 0.5) / math.sqrt(0.25 / n)) if n else 0.0
        p = math.erfc(z / math.sqrt(2.0)) if n else 1.0
        p_values.append(p)
        diagnostic_curve.append({"threshold": threshold, "n": n, "coverage": round(n / len(cohort), 6) if cohort else None, "win_rate": round(wins / n, 6) if n else None, "raw_p": round(p, 8)})
    for item, adjusted in zip(diagnostic_curve, _holm_adjust(p_values)):
        item["holm_adjusted_p"] = round(adjusted, 8)
    split = temporal_purged_split(rows)
    by_id = {row.get("id"): row for row in rows}
    evaluation = [by_id[item] for item in split["evaluation_ids"] if item in by_id]
    replay_ready = [
        row for row in evaluation
        if isinstance(_audit(row).get("decision_replay_v1"), dict)
        and (_audit(row).get("outcomes_dual") or {}).get("price_1h_later") is not None
    ]
    return {
        "version": AUDIT_VERSION,
        "analysis_type": "THRESHOLD_COUNTERFACTUAL",
        "oos_split": split,
        "all_decision_snapshot_n": len(rows),
        "evaluation_snapshot_n": len(evaluation),
        "evaluation_flat_n": sum(str(row.get("prediction") or "").upper() == "FLAT" for row in evaluation),
        "replay_ready_evaluation_n": len(replay_ready),
        "oos_curve": [],
        "multiple_testing_correction": "HOLM",
        "production_writeback": False,
        "independent_1h_n": len(cohort),
        "status": "INSUFFICIENT_OOS_EVIDENCE",
        "reason": "legacy_FLAT_and_directional_rows_lack_complete_decision_replay_snapshots",
        "diagnostic_in_sample_directional_only": {
            "label": "DESCRIPTIVE_IN_SAMPLE_DIRECTIONAL_ONLY_NON_OOS",
            "multiple_testing_correction": "HOLM",
            "curve": diagnostic_curve,
        },
    }


def signal_ablation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cohort = independent_1h_cohort(filter_proof_qualified(rows))
    features = ("orderflow", "volume_delta", "bidask", "funding", "oi", "price_momentum")
    availability = Counter()
    for row in cohort:
        pressures = ((_pipeline(row).get("step2_features") or {}).get("pressures") or {})
        for feature in features:
            if pressures.get(feature) is not None:
                availability[feature] += 1
    return {
        "version": AUDIT_VERSION,
        "split": "NO_TEMPORAL_SPLIT_DESCRIPTIVE_AVAILABILITY_ONLY",
        "production_feature_change": False,
        "independent_1h_n": len(cohort),
        "status": "COMPLETE" if len(cohort) >= MIN_OOS_PAIRS else "INSUFFICIENT_OOS_EVIDENCE",
        "feature_rows": {feature: availability[feature] for feature in features},
        "reason": None if len(cohort) >= MIN_OOS_PAIRS else f"requires_at_least_{MIN_OOS_PAIRS}_independent_rows",
    }


def _wilson(correct: int, n: int) -> list[float] | None:
    if n <= 0:
        return None
    z = 1.959963984540054
    phat = correct / n
    den = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / den
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / den
    return [round(max(0.0, center - half), 6), round(min(1.0, center + half), 6)]


def learning_ab(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Assess whether the export can support a full causal model A/B.

    AUD-061 legacy exports have Step-2 outputs but neither complete replay
    inputs nor historical settlement-observation timestamps. Reweighting those
    outputs is only component sensitivity, not a model A/B, so this gate fails
    closed instead of manufacturing paired performance.
    """
    proof = filter_proof_qualified(rows)
    symbols = sorted({str(row.get("symbol") or "").replace("/", "").upper() for row in proof})
    authority_target_n = 0
    for symbol in symbols:
        scoped = independent_1h_cohort([
            row for row in proof
            if str(row.get("symbol") or "").replace("/", "").upper() == symbol
        ])
        authority_target_n += len(scoped)
    replay_snapshot_n = sum(isinstance(_audit(row).get("decision_replay_v1"), dict) for row in proof)
    observed_settlement_n = sum(settlement_observed_epoch(row) is not None for row in proof)
    return {
        "version": AUDIT_VERSION,
        "status": "INSUFFICIENT_CAUSAL_PROVENANCE",
        "learning_effect": "NOT_ESTIMABLE",
        "analysis_type": "COMPONENT_LEVEL_WEIGHT_SENSITIVITY_NOT_MODEL_AB",
        "full_model_ab": False,
        "method": "FAIL_CLOSED_PROVENANCE_GATE",
        "same_inputs_and_timestamps": False,
        "reorder_invariant": True,
        "paired_n": 0,
        "authority_target_n": authority_target_n,
        "decision_replay_snapshot_n": replay_snapshot_n,
        "settlement_observation_provenance_n": observed_settlement_n,
        "coverage": 0.0 if authority_target_n else None,
        "abstention_n": authority_target_n,
        "paired_correctness_delta": None,
        "a_mean_signed_return": None,
        "b_mean_signed_return": None,
        "reason": "legacy_rows_do_not_prove_decision_time_settlement_availability_or_complete_pipeline_replay_inputs",
        "decisions": [],
    }


def run_all(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (_epoch(row.get("ts")) or float("inf"), str(row.get("id") or "")))
    learning = learning_ab(ordered)
    symbols = sorted({str(row.get("symbol") or "UNKNOWN").replace("/", "").upper() for row in ordered})
    scoped = {
        symbol: [row for row in ordered if str(row.get("symbol") or "UNKNOWN").replace("/", "").upper() == symbol]
        for symbol in symbols
    }
    return {
        "version": AUDIT_VERSION,
        "input_rows": len(ordered),
        "input_hash": canonical_hash(ordered),
        "learning_ab": learning,
        "flat_waterfall": flat_waterfall(ordered),
        "threshold_research": {symbol: threshold_research(items) for symbol, items in scoped.items()},
        "horizon_research": {symbol: horizon_research(items) for symbol, items in scoped.items()},
        "signal_ablation": {symbol: signal_ablation(items) for symbol, items in scoped.items()},
        "feature_availability": feature_availability(ordered),
        "edge_claim_supported": learning["status"] == "COMPLETE" and learning["learning_effect"] == "POSITIVE",
        "production_writeback": False,
    }
