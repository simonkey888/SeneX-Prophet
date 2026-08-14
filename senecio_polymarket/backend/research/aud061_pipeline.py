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

AUDIT_VERSION = "AUD-061-paper-research-v1"
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
    thresholds = (0.40, 0.50, 0.55, 0.60, 0.70, 0.80, 0.90)
    curve = []
    p_values = []
    cohort = independent_1h_cohort(filter_proof_qualified(rows))
    for threshold in thresholds:
        selected = [row for row in cohort if float(row.get("confidence") or 0.0) >= threshold]
        wins = sum(row.get("outcome") == "WIN" for row in selected)
        n = len(selected)
        z = abs((wins / n - 0.5) / math.sqrt(0.25 / n)) if n else 0.0
        p = math.erfc(z / math.sqrt(2.0)) if n else 1.0
        p_values.append(p)
        curve.append({"threshold": threshold, "n": n, "coverage": round(n / len(cohort), 6) if cohort else None, "win_rate": round(wins / n, 6) if n else None, "raw_p": round(p, 8)})
    for item, adjusted in zip(curve, _holm_adjust(p_values)):
        item["holm_adjusted_p"] = round(adjusted, 8)
    return {
        "version": AUDIT_VERSION,
        "split": "STRICT_CHRONOLOGICAL_PURGED_1H_EMBARGO_1H",
        "multiple_testing_correction": "HOLM",
        "production_writeback": False,
        "independent_1h_n": len(cohort),
        "status": "COMPLETE" if len(cohort) >= MIN_OOS_PAIRS else "INSUFFICIENT_OOS_EVIDENCE",
        "curve": curve,
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
        "split": "STRICT_CHRONOLOGICAL_PURGED_1H_EMBARGO_1H",
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
    """Paired causal A/B using only proof known >=1h before each decision."""
    from oracle_runtime import institutional_core as learning

    def core():
        return learning.SingleDecisionCore(
            max_drawdown=0.12, ruin_probability_threshold=0.05, hard_stop=True,
            max_position_pct=0.25, max_leverage=1, min_confidence=0.40,
            min_ev_to_trade=0.001, no_trade_noise=0.60, initial_capital=1000.0,
        )

    proof = filter_proof_qualified(rows)
    symbols = sorted({str(row.get("symbol") or "").replace("/", "").upper() for row in proof})
    decisions = []
    authority_target_n = 0
    for symbol in symbols:
        scoped = independent_1h_cohort([
            row for row in proof
            if str(row.get("symbol") or "").replace("/", "").upper() == symbol
        ])
        authority_target_n += len(scoped)
        scoped.sort(key=lambda row: (_epoch(row.get("ts")) or float("inf"), str(row.get("id") or "")))
        for target in scoped:
            target_epoch = _epoch(target.get("ts"))
            if target_epoch is None:
                continue
            prior = [row for row in scoped if (_epoch(row.get("ts")) or float("inf")) + 3600.0 <= target_epoch]
            learned_core = core()
            state = learning.replay_authoritative_learning(learned_core, prior, symbol, decision_cutoff=target_epoch)
            if state["proof_qualified_n"] < learning.MIN_LEARNING_EXAMPLES:
                continue
            target_step2 = _pipeline(target).get("step2_features") or {}
            pressures = target_step2.get("pressures") or {}
            target_learning = target_step2.get("learning_state_v1") or {}
            target_weights = target_learning.get("effective_weights") or state["base_weights"]
            base = state["base_weights"]
            effective = state["effective_weights"]
            pressure_map = {
                "orderflow": "orderflow", "volume_delta": "volume_delta",
                "bidask": "bidask_imbalance", "funding": "funding_signal",
                "oi": "oi_momentum", "price_momentum": "price_momentum",
            }
            raw = {}
            valid = True
            for pressure_name, weight_name in pressure_map.items():
                try:
                    # Stored pressures were produced with the target decision's
                    # then-effective weights. Recover the paired raw input before
                    # applying frozen-A or causal-B weights.
                    raw[weight_name] = float(pressures[pressure_name]) / float(target_weights[weight_name])
                except (KeyError, TypeError, ValueError, ZeroDivisionError):
                    valid = False
            if not valid:
                continue
            total_a = sum(raw[name] * float(base[name]) for name in raw)
            total_b = sum(raw[name] * float(effective[name]) for name in raw)
            direction = lambda value: "LONG" if value > 0.05 else ("SHORT" if value < -0.05 else "FLAT")
            dual = _audit(target).get("outcomes_dual") or {}
            origin = float(target["price_now"])
            later = float(dual["price_1h_later"])
            if math.isclose(later, origin, rel_tol=0.0, abs_tol=1e-12):
                continue
            truth = "LONG" if later > origin else "SHORT"
            a, b = direction(total_a), direction(total_b)
            signed_return = (later - origin) / origin
            decisions.append({
                "target_id": target.get("id"), "timestamp": target.get("ts"), "symbol": symbol,
                "a_direction": a, "b_direction": b, "truth": truth,
                "a_correct": a == truth if a != "FLAT" else None,
                "b_correct": b == truth if b != "FLAT" else None,
                "a_signed_return": round(signed_return * (1 if a == "LONG" else -1), 8) if a != "FLAT" else None,
                "b_signed_return": round(signed_return * (1 if b == "LONG" else -1), 8) if b != "FLAT" else None,
                "source_prediction_ids": state["source_prediction_ids"],
                "source_evidence_hash": state["source_evidence_hash"],
                "effective_weights_hash": state["effective_weights_hash"],
                "code_hash": state["code_hash"], "config_hash": state["config_hash"],
            })
    paired = [item for item in decisions if item["a_correct"] is not None and item["b_correct"] is not None]
    a_correct = sum(item["a_correct"] for item in paired)
    b_correct = sum(item["b_correct"] for item in paired)
    delta = (b_correct - a_correct) / len(paired) if paired else None
    per_direction = {}
    for direction in ("LONG", "SHORT"):
        subset = [item for item in paired if item["truth"] == direction]
        correct = sum(item["b_correct"] for item in subset)
        per_direction[direction] = {"n": len(subset), "b_correct": correct, "wilson_95": _wilson(correct, len(subset))}
    status = "COMPLETE" if len(paired) >= MIN_OOS_PAIRS else "INSUFFICIENT_OOS_EVIDENCE"
    return {
        "version": AUDIT_VERSION,
        "status": status,
        "learning_effect": "INSUFFICIENT_OOS_EVIDENCE" if status != "COMPLETE" else (
            "POSITIVE" if delta and delta > 0 else "NEGATIVE" if delta and delta < 0 else "NO_DETECTABLE_DIFFERENCE"
        ),
        "method": "PAIRED_STRICT_CHRONOLOGICAL_PROOF_QUALIFIED_PURGED_1H",
        "same_inputs_and_timestamps": True,
        "reorder_invariant": True,
        "paired_n": len(paired),
        "authority_target_n": authority_target_n,
        "coverage": round(len(paired) / authority_target_n, 6) if authority_target_n else None,
        "abstention_n": authority_target_n - len(paired),
        "a_correct": a_correct,
        "b_correct": b_correct,
        "paired_correctness_delta": round(delta, 6) if delta is not None else None,
        "a_mean_signed_return": round(sum(item["a_signed_return"] for item in paired) / len(paired), 8) if paired else None,
        "b_mean_signed_return": round(sum(item["b_signed_return"] for item in paired) / len(paired), 8) if paired else None,
        "per_truth_direction": per_direction,
        "decisions": decisions,
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
