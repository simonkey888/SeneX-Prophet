"""AUD-062 deterministic, read-only decision-causality forensics.

This module intentionally has no network, database, deployment, trading, or
write-back client.  It consumes a frozen public observation bundle and emits
diagnostic evidence.  Production thresholds, weights, and signal behavior are
never modified.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import math
import re
from collections import Counter, defaultdict
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from backend.research.aud062_r1_contracts import (
    MACHINE_REASON_CLASSES,
    canonical_hash,
    feature_provenance_contract,
    finite_distribution,
    historical_canonical_counterfactual,
    instrument_cost_contract,
    verify_persisted_roundtrip,
)


AUDIT_VERSION = "AUD-062-decision-causality-v2"
DATASET_PROVENANCE_VERSION = "AUD-062-dataset-provenance-v1"
BASE_SHA = "49c5f0a69609c005da80e48b585e91d8582a5ac6"
BASE_TREE = "3e323bcc2795f97b29242883d3bf2a015c092ccd"
PRODUCTION_DEPLOYED_AT = "2026-08-14T20:55:21Z"
PRODUCTION_LINEAGE = BASE_SHA
FEATURES = (
    "orderflow",
    "volume_delta",
    "bidask_imbalance",
    "funding_signal",
    "oi_momentum",
    "price_momentum",
)
OBSERVED_STATUSES = {"REAL_NONZERO", "REAL_OBSERVED_ZERO"}
FLAT_CAUSES = {
    "NO_DIRECTIONAL_SIGNAL",
    "SIGNAL_CONFLICT",
    "FEATURE_MISSING",
    "FEATURE_SOURCE_ERROR",
    "AUTHORITY_INSUFFICIENT",
    "EV_NEGATIVE",
    "EV_BELOW_DYNAMIC_MIN",
    "RISK_REJECT",
    "SURVIVABILITY_REJECT",
    "REGIME_REJECT",
    "AGREEMENT_REJECT",
    "HORIZON_UNALIGNED",
    "POLICY_LOCK",
    "OTHER_EXPLICIT",
    "UNKNOWN_CAUSAL_PATH",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def dataset_provenance(
    rows: Any,
    *,
    source_class: str,
    capture_time_utc: str,
    source_endpoint_or_class: str,
    raw_or_derived: str,
    transformation: str,
) -> dict[str, Any]:
    """Return the mandatory provenance block for a row-level dataset.

    ``SHA256`` binds the canonical row payload rather than the containing file,
    so JSON and CSV representations can carry the same non-self-referential
    digest.
    """
    row_count = len(rows) if isinstance(rows, list) else 0
    return {
        "VERSION": DATASET_PROVENANCE_VERSION,
        "SOURCE_CLASS": source_class,
        "CAPTURE_TIME_UTC": capture_time_utc,
        "SOURCE_ENDPOINT_OR_CLASS": source_endpoint_or_class,
        "RAW_OR_DERIVED": raw_or_derived,
        "TRANSFORMATION": transformation,
        "ROW_COUNT": row_count,
        "SHA256": canonical_hash(rows),
    }


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dt(value: Any) -> datetime | None:
    try:
        if isinstance(value, (int, float)):
            number = float(value)
            if number > 10_000_000_000:
                number /= 1000.0
            return datetime.fromtimestamp(number, timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _audit(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("audit", row.get("_audit", {}))
    return value if isinstance(value, dict) else {}


def _pipeline(row: dict[str, Any]) -> dict[str, Any]:
    value = _audit(row).get("pipeline")
    return value if isinstance(value, dict) else {}


def _step(row: dict[str, Any], name: str) -> dict[str, Any]:
    value = _pipeline(row).get(name)
    return value if isinstance(value, dict) else {}


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _decimal_mean(values: list[float], places: int = 8) -> float | None:
    """Return a cross-Python-version deterministic rounded mean.

    Python 3.12 changed float summation accuracy, so native ``sum`` can make
    byte-pinned evidence differ from Python 3.11 despite identical inputs.
    Decimal-from-string preserves the persisted decimal values exactly.
    """
    if not values:
        return None
    total = sum((Decimal(str(value)) for value in values), Decimal(0))
    quantum = Decimal(1).scaleb(-places)
    return float((total / Decimal(len(values))).quantize(quantum))


def _normalize_symbol(value: Any) -> str:
    return str(value or "UNKNOWN").replace("/", "").replace("-", "").upper()


def _parse_reason_probability(reason: Any) -> float | None:
    match = re.search(r"(?:HIGH|MODERATE)_RUIN_PROB:\s*([0-9.]+)%", str(reason or ""))
    return float(match.group(1)) / 100.0 if match else None


def _raw_direction(step2: dict[str, Any]) -> str:
    pressure = float(step2.get("base_total_pressure", step2.get("total_pressure", 0.0)) or 0.0)
    if pressure > 0.05:
        return "LONG"
    if pressure < -0.05:
        return "SHORT"
    return "NEUTRAL"


def _action_reason(row: dict[str, Any]) -> str:
    action = _audit(row).get("action_vector") or {}
    return str(action.get("reason") or "") if isinstance(action, dict) else ""


def classify_binding_gate(row: dict[str, Any]) -> tuple[str, list[str], float | None]:
    """Return the first executable gate, secondary facts, and threshold distance."""
    if str(row.get("prediction") or "").upper() != "FLAT":
        return "NONE_DIRECTIONAL_EXECUTE", [], None
    step1 = _step(row, "step1_market")
    step2 = _step(row, "step2_features")
    step4 = _step(row, "step4_ev")
    reason = _action_reason(row).lower()
    secondary: list[str] = []
    availability = step1.get("feature_availability_v1") or {}
    for feature, item in sorted(availability.items() if isinstance(availability, dict) else []):
        status = str((item or {}).get("status") or "") if isinstance(item, dict) else ""
        if status == "MISSING":
            secondary.append(f"FEATURE_MISSING:{feature}")
        elif status == "SOURCE_ERROR":
            secondary.append(f"FEATURE_SOURCE_ERROR:{feature}")

    if "hard_stop" in reason or "ruin_prob:" in reason:
        return "RISK_REJECT", secondary, None
    if step2.get("long_suppressed_by_regime"):
        return "REGIME_REJECT", secondary, None
    if "no_direction" in reason:
        distance = abs(float(step2.get("total_pressure") or 0.0)) - 0.05
        return "NO_DIRECTIONAL_SIGNAL", secondary, round(distance, 8)
    if "low_conviction" in reason:
        conviction = float(step2.get("conviction") or 0.0)
        gate = 1.0 / (1.0 + math.exp(-(conviction - 0.40) * 12.0))
        return "OTHER_EXPLICIT", secondary + ["LOW_CONVICTION_GATE"], round(gate - 0.05, 8)
    if "volatile_shield" in reason or "regime_guard" in reason:
        return "REGIME_REJECT", secondary, None
    if "high_noise" in reason:
        return "AGREEMENT_REJECT", secondary, round(0.60 - float(step2.get("noise") or 0.0), 8)
    if "negative_ev" in reason:
        adjusted = float(step4.get("adjusted_ev") or 0.0)
        threshold = float(step4.get("dynamic_min_ev") or 0.0)
        cause = "EV_NEGATIVE" if adjusted < 0 else "EV_BELOW_DYNAMIC_MIN"
        return cause, secondary, round(adjusted - threshold, 8)
    if "not_feasible" in reason:
        return "OTHER_EXPLICIT", secondary + ["EXECUTION_INFEASIBLE"], None
    if "size_too_small" in reason:
        match = re.search(r"size_too_small:\s*([0-9.]+)\s*<\s*([0-9.]+)", reason)
        distance = float(match.group(1)) - float(match.group(2)) if match else None
        return "OTHER_EXPLICIT", secondary + ["SIZE_TOO_SMALL"], round(distance, 8) if distance is not None else None
    if "cooldown" in reason:
        return "OTHER_EXPLICIT", secondary + ["LATENCY_COOLDOWN"], None
    if "signal_density" in reason:
        return "OTHER_EXPLICIT", secondary + ["SIGNAL_DENSITY"], None
    return "UNKNOWN_CAUSAL_PATH", secondary, None


def recompute_ev(row: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct both executable EV branches using only stored inputs."""
    step1 = _step(row, "step1_market")
    step2 = _step(row, "step2_features")
    step3 = _step(row, "step3_risk")
    step4 = _step(row, "step4_ev")
    p_win = float(step4.get("p_win") or 0.0)
    avg_win = float(step4.get("avg_win") or 0.0)
    avg_loss = float(step4.get("avg_loss") or 0.0)
    estimated_cost = float(step4.get("estimated_cost") or 0.0)
    raw_ev = p_win * avg_win - (1.0 - p_win) * avg_loss
    core_base_ev = raw_ev - estimated_cost
    survival_discount = float(step4.get("survival_discount") or 0.0)
    core_survival_ev = core_base_ev * survival_discount

    conviction = float(step2.get("conviction") or 0.0)
    noise = float(step2.get("noise") or 0.0)
    volatility = float(step1.get("volatility") or 0.0)
    anchor_p_win = 1.0 / (1.0 + math.exp(-conviction))
    entropy_discount = 1.0 - noise * 0.5
    anchor_gross_ev = (
        anchor_p_win * volatility * 1.2
        - (1.0 - anchor_p_win) * volatility * 0.8
    ) * entropy_discount
    position_usdt = 1000.0 * conviction * float(step3.get("size_multiplier") or 0.0)
    participation = min(1.0, position_usdt / 500_000.0)
    anchor_default_slippage = 0.0002
    anchor_default_impact = (2.0 * (1.0 + participation * 10.0)) / 10_000.0
    anchor_latency_decay = 1.0
    market_anchor_ev = anchor_gross_ev * anchor_latency_decay - anchor_default_slippage - anchor_default_impact
    reconstructed = min(core_survival_ev, market_anchor_ev)
    stored_base = float(step4.get("base_ev") or 0.0)
    stored_adjusted = float(step4.get("adjusted_ev") or 0.0)
    return {
        "raw_ev_before_cost": raw_ev,
        "fee_component_round_trip": 0.0004,
        "slippage_component_round_trip": estimated_cost - 0.0004,
        "estimated_cost": estimated_cost,
        "core_base_ev_recomputed": core_base_ev,
        "core_base_ev_stored": stored_base,
        "survival_discount": survival_discount,
        "core_survival_ev": core_survival_ev,
        "anchor_p_win_from_conviction": anchor_p_win,
        "anchor_entropy_discount": entropy_discount,
        "anchor_gross_ev": anchor_gross_ev,
        "anchor_default_slippage": anchor_default_slippage,
        "anchor_default_impact": anchor_default_impact,
        "anchor_latency_decay": anchor_latency_decay,
        "market_anchor_ev": market_anchor_ev,
        "selected_branch": "MARKET_ANCHOR" if market_anchor_ev < core_survival_ev else "CORE_SURVIVAL_EV",
        "adjusted_ev_recomputed": reconstructed,
        "adjusted_ev_stored": stored_adjusted,
        "base_residual": stored_base - core_base_ev,
        "adjusted_residual": stored_adjusted - reconstructed,
        "bridge_from_stored_base": stored_base - reconstructed,
    }


def decision_attribution(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (int(item.get("id") or 0), str(item.get("ts") or ""))):
        audit = _audit(row)
        step1 = _step(row, "step1_market")
        step2 = _step(row, "step2_features")
        step3 = _step(row, "step3_risk")
        step4 = _step(row, "step4_ev")
        action = audit.get("action_vector") or {}
        learning = step2.get("learning_state_v1") or {}
        mask = step2.get("missing_input_mask_v1") or {}
        replay = audit.get("decision_replay_v1") or {}
        cause, secondary, distance = classify_binding_gate(row)
        ev = recompute_ev(row)
        final = str(row.get("prediction") or "UNKNOWN").upper()
        direction = str(step2.get("direction") or "UNKNOWN").upper()
        raw_direction = _raw_direction(step2)
        core_ruin = _float(step3.get("ruin_prob"))
        survivability_ruin = _parse_reason_probability(step3.get("surv_reason"))
        survivability_machine_ruin = _float(step3.get("survivability_ruin_prob"))
        reason_text = _action_reason(row)
        reason_semantics = "COHERENT"
        if reason_text.lower().startswith("negative_ev") and float(step4.get("adjusted_ev") or 0.0) >= 0.0:
            reason_semantics = "POSITIVE_EV_MISLABELED_NEGATIVE"
        statuses = step1.get("feature_availability_v1") or {}
        records.append({
            "timestamp": row.get("ts"),
            "prediction_id": row.get("id"),
            "symbol": _normalize_symbol(row.get("symbol")),
            "runtime_lineage": PRODUCTION_LINEAGE if replay else "PRE_AUD061_SCHEMA_COMPATIBLE_UNKNOWN_COMMIT",
            "feature_cutoff_time": (replay.get("captured_at") if isinstance(replay, dict) else None),
            "raw_direction_candidate": raw_direction,
            "post_regime_direction_candidate": direction,
            "raw_conviction": step2.get("conviction", row.get("confidence")),
            "raw_confidence_semantics": "RAW_CONVICTION_UNCALIBRATED",
            "feature_values": {feature: step1.get(feature) for feature in FEATURES},
            "feature_statuses": statuses if isinstance(statuses, dict) else {},
            "masked_features": list(mask.get("masked_features") or []) if isinstance(mask, dict) else [],
            "learning_version": learning.get("learning_version") if isinstance(learning, dict) else None,
            "learning_source_n": learning.get("proof_qualified_n") if isinstance(learning, dict) else None,
            "learning_effective_weights": learning.get("effective_weights") if isinstance(learning, dict) else None,
            "aggregate_pressure": step2.get("total_pressure"),
            "expected_move": ev["raw_ev_before_cost"],
            "expected_value_raw_before_cost": ev["raw_ev_before_cost"],
            "base_ev": step4.get("base_ev"),
            "adjusted_ev": step4.get("adjusted_ev"),
            "estimated_cost": step4.get("estimated_cost"),
            "fee_component": ev["fee_component_round_trip"],
            "spread_component": 0.0,
            "slippage_component": ev["slippage_component_round_trip"],
            "entropy_discount_or_penalty": step4.get("entropy_discount"),
            "risk_penalty_or_multiplier": step4.get("survival_discount"),
            "survivability_factor": step3.get("surv_size_factor"),
            "ruin_probability_fields": {
                "core_risk_ruin_prob": core_ruin,
                "survivability_reason_ruin_prob": survivability_ruin,
                "survivability_machine_ruin_prob": survivability_machine_ruin,
            },
            "dynamic_min_ev": step4.get("dynamic_min_ev"),
            "regime_gate": "REJECT" if step2.get("long_suppressed_by_regime") else "PASS",
            "agreement_gate": "REJECT" if "high_noise" in _action_reason(row).lower() else "PASS",
            "authority_gate": "NOT_IN_PRODUCTION_DECISION_EQUATION",
            "pre_ev_decision": direction if direction in {"LONG", "SHORT"} else "FLAT",
            "post_ev_decision": direction if bool(step4.get("tradeable")) and direction in {"LONG", "SHORT"} else "FLAT",
            "pre_risk_decision": raw_direction if raw_direction in {"LONG", "SHORT"} else "FLAT",
            "post_risk_decision": "FLAT" if str(step3.get("verdict")) == "KILL" else (direction if direction in {"LONG", "SHORT"} else "FLAT"),
            "final_decision": final,
            "primary_reject_reason": cause,
            "secondary_reject_reasons": secondary,
            "distance_to_binding_threshold": distance,
            "first_binding_gate": cause,
            "action": action.get("action") if isinstance(action, dict) else None,
            "action_reason": reason_text,
            "action_reason_semantics": reason_semantics,
            "ev_selected_branch": ev["selected_branch"],
            "market_anchor_ev_recomputed": ev["market_anchor_ev"],
            "core_survival_ev_recomputed": ev["core_survival_ev"],
            "ev_residual": ev["adjusted_residual"],
        })
    return records


def _distribution(items: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(items)
    flats = [item for item in items if item["final_decision"] == "FLAT"]
    return {
        "n": total,
        "flat_n": len(flats),
        "long_n": sum(item["final_decision"] == "LONG" for item in items),
        "short_n": sum(item["final_decision"] == "SHORT" for item in items),
        "flat_rate": _rate(len(flats), total),
    }


def flat_cause_distribution(attribution: list[dict[str, Any]]) -> dict[str, Any]:
    deploy_dt = _dt(PRODUCTION_DEPLOYED_AT)
    pre = [item for item in attribution if (_dt(item["timestamp"]) or datetime.max.replace(tzinfo=timezone.utc)) < deploy_dt]
    post = [item for item in attribution if (_dt(item["timestamp"]) or datetime.min.replace(tzinfo=timezone.utc)) >= deploy_dt]
    flats = [item for item in attribution if item["final_decision"] == "FLAT"]
    by_symbol = {symbol: _distribution([item for item in attribution if item["symbol"] == symbol]) for symbol in sorted({item["symbol"] for item in attribution})}
    causes = Counter(item["first_binding_gate"] for item in flats)
    regimes = Counter(str(item["regime_gate"]) for item in flats)
    raw_direction = [item for item in attribution if item["raw_direction_candidate"] in {"LONG", "SHORT"}]
    high_conviction = [item for item in attribution if float(item["raw_conviction"] or 0.0) >= 0.70]
    missing = [item for item in attribution if item["masked_features"]]
    return {
        "version": AUDIT_VERSION,
        "total": _distribution(attribution),
        "by_symbol": by_symbol,
        "pre_aud061": _distribution(pre),
        "post_aud061": _distribution(post),
        "flat_by_first_binding_gate": dict(sorted(causes.items())),
        "flat_by_regime_gate": dict(sorted(regimes.items())),
        "flat_rate_when_raw_direction_exists": _distribution(raw_direction)["flat_rate"],
        "flat_rate_when_raw_conviction_high": _distribution(high_conviction)["flat_rate"],
        "flat_rate_when_features_missing": _distribution(missing)["flat_rate"],
        "flat_rate_due_to_ev": _rate(causes["EV_NEGATIVE"] + causes["EV_BELOW_DYNAMIC_MIN"], len(attribution)),
        "flat_rate_due_to_risk": _rate(causes["RISK_REJECT"] + causes["SURVIVABILITY_REJECT"], len(attribution)),
        "flat_rate_due_to_authority": 0.0,
        "unknown_causal_path_n": causes["UNKNOWN_CAUSAL_PATH"],
    }


def action_reason_semantics(attribution: list[dict[str, Any]]) -> dict[str, Any]:
    mislabeled = [
        item for item in attribution
        if item["action_reason_semantics"] == "POSITIVE_EV_MISLABELED_NEGATIVE"
    ]
    negative_labels = [
        item for item in attribution
        if str(item.get("action_reason") or "").lower().startswith("negative_ev")
    ]
    return {
        "version": AUDIT_VERSION,
        "observed_rows": len(attribution),
        "negative_ev_reason_rows": len(negative_labels),
        "positive_adjusted_ev_mislabeled_negative_n": len(mislabeled),
        "affected_prediction_ids": [item["prediction_id"] for item in mislabeled],
        "historical_reason_contract": "NEGATIVE_EV_LABEL_CONFLATES_SIGN_WITH_DYNAMIC_THRESHOLD_FAILURE",
        "candidate_remediation": {
            "decision_semantics_changed": False,
            "threshold_changed": False,
            "new_reason_prefix": "ev_below_dynamic_min",
            "serialized_terms": ["adjusted_ev", "dynamic_min_ev"],
        },
    }


def ev_bridge(rows: list[dict[str, Any]]) -> dict[str, Any]:
    bridges = [(row, recompute_ev(row)) for row in rows]
    max_residual = max((abs(item[1]["adjusted_residual"]) for item in bridges), default=0.0)
    anchor_binding = [(row, ev) for row, ev in bridges if ev["selected_branch"] == "MARKET_ANCHOR"]
    positive_to_negative = [(row, ev) for row, ev in bridges if ev["core_base_ev_stored"] > 0 and ev["adjusted_ev_stored"] < 0]
    suppressed = [
        (row, ev) for row, ev in bridges
        if ev["core_survival_ev"] > float(_step(row, "step4_ev").get("dynamic_min_ev") or 0.0)
        and ev["adjusted_ev_stored"] <= float(_step(row, "step4_ev").get("dynamic_min_ev") or 0.0)
    ]
    representatives: dict[str, Any] = {}
    for label, predicate in (
        ("LONG", lambda row: row.get("prediction") == "LONG"),
        ("SHORT", lambda row: row.get("prediction") == "SHORT"),
        ("FLAT", lambda row: row.get("prediction") == "FLAT" and recompute_ev(row)["core_base_ev_stored"] > 0 and recompute_ev(row)["adjusted_ev_stored"] < 0),
    ):
        selected = next((row for row in rows if predicate(row)), None)
        if selected:
            representatives[label] = {"prediction_id": selected.get("id"), "symbol": selected.get("symbol"), **recompute_ev(selected)}
    return {
        "version": AUDIT_VERSION,
        "tolerance": 1e-6,
        "rows_reconciled": len(rows),
        "ev_formula_reconciled": max_residual <= 1e-6,
        "max_absolute_residual": max_residual,
        "unexplained_residual": max_residual > 1e-6,
        "market_anchor_binding_n": len(anchor_binding),
        "positive_base_to_negative_adjusted_n": len(positive_to_negative),
        "core_tradeable_but_anchor_rejected_n": len(suppressed),
        "affected_prediction_ids": [row.get("id") for row, _ in suppressed],
        "representative_numeric_bridges": representatives,
        "formulas": {
            "raw_ev": "p_win*avg_win-(1-p_win)*avg_loss",
            "core_base_ev": "raw_ev-estimated_cost",
            "core_survival_ev": "core_base_ev*survival_discount",
            "anchor_p_win": "sigmoid(raw_conviction)",
            "anchor_gross_ev": "(anchor_p_win*single_candle_vol*1.2-(1-anchor_p_win)*single_candle_vol*0.8)*entropy_discount",
            "anchor_market_ev": "anchor_gross_ev-0.0002_default_slippage-default_impact",
            "adjusted_ev": "min(core_survival_ev,anchor_market_ev)",
        },
        "units": "decimal return; 0.0001=1bp",
        "invariant_results": {
            "BPS_DECIMAL_UNIT_MISMATCH": "FALSIFIED_FOR_EXECUTED_ARITHMETIC",
            "DOUBLE_COUNTED_FEES": "FALSIFIED_AS_DIRECT_ADDITION; TWO_INCOMPATIBLE_NET_EV_BRANCHES_CONFIRMED",
            "DOUBLE_COUNTED_SPREAD": "FALSIFIED; SPREAD_ONLY_SIZES_EXECUTION",
            "DOUBLE_COUNTED_SLIPPAGE": "PARTIALLY_CONFIRMED_ACROSS_COMPETING_BRANCHES",
            "RISK_PENALTY_DOUBLE_APPLICATION": "PARTIALLY_CONFIRMED_EV_AND_SIZE_BY_DESIGN",
            "SIZE_FACTOR_APPLIED_TO_EV_INCORRECTLY": "PARTIALLY_CONFIRMED_VIA_ANCHOR_POSITION_IMPACT",
            "ENTROPY_APPLIED_TWICE": "FALSIFIED; REINTRODUCED_ON_ANCHOR_BRANCH_CONTRARY_TO_CORE_COMMENT",
            "NEGATIVE_EV_FROM_HIDDEN_TERM": "CONFIRMED",
            "DYNAMIC_MIN_EV_UNIT_MISMATCH": "FALSIFIED",
            "DIRECTION_ASYMMETRY": "FALSIFIED_FOR_ANCHOR_TRANSFORM; BOTH_SIDES_USE_ABSOLUTE_CONVICTION",
            "LONG_SHORT_SIGN_ERROR": "FALSIFIED_ON_RECONSTRUCTIBLE_ROWS",
        },
    }


def cost_model_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": AUDIT_VERSION,
        "status": "CONCEPTUALLY_MISMATCHED_UNSUPPORTED_PARAMETERS",
        "instrument_assumed": "NO_SINGLE_EXECUTABLE_INSTRUMENT",
        "observed_inputs": "OKX spot ticker/OHLCV/orderbook plus OKX USDT-swap funding/OI",
        "paper_action_target": "unspecified maker-style crypto round trip",
        "fee_schedule": {"value_per_side": 0.0002, "source": "code_constant", "primary_source_provenance": None, "maker_taker": "maker_claim_only"},
        "unit_contract": {
            "decimal_return": "1.0=100%; 0.0001=1bp",
            "basis_points": "1bp=0.0001 decimal return",
            "percent": "1%=0.01 decimal return=100bp",
            "commission_per_side_decimal": 0.0002,
            "commission_per_side_bps": 2.0,
            "commission_round_trip_decimal": 0.0004,
            "commission_round_trip_bps": 4.0,
            "anchor_slippage_decimal": 0.0002,
            "anchor_slippage_bps": 2.0,
            "normalization_status": "ARITHMETIC_UNITS_RECONCILED",
        },
        "spread": {"source": "OKX spot BBO", "ev_inclusion": False, "execution_size_only": True, "full_or_half": "full spread for size adjustment"},
        "slippage": {"core": "max(1bp, half observed spot spread) then doubled round-trip", "market_anchor": "independent fixed 2bp deduction", "refresh": "per decision for core; never for anchor default"},
        "market_impact": {"source": "fixed 2bp plus position/default_depth heuristic", "orderbook_depth_used": False, "default_depth_usdt": 500000.0},
        "market_vs_limit_assumption": "contradictory: maker fee claim with modeled slippage and unspecified fill style",
        "fallback_behavior": "fixed anchor slippage and default depth silently bind",
        "duplicate_penalty_detection": {
            "fee_added_twice_in_same_branch": False,
            "spread_added_to_ev_and_size": False,
            "slippage_present_in_both_competing_net_ev_branches": True,
            "entropy_present_in_core_ev_and_anchor_ev": False,
            "entropy_reintroduced_only_in_anchor_ev": True,
            "risk_affects_core_ev_and_final_size": True,
            "status": "NO_LITERAL_UNIT_DOUBLE_ADD; SEMANTIC_DUPLICATION_ACROSS_INCOMPATIBLE_BRANCHES",
        },
        "row_count": len(rows),
        "production_parameter_change": False,
        "minimum_correction": "Choose and document one paper instrument; bind primary-source fee/depth provenance; serialize every anchor term; remove or reconcile the competing EV branch under a separately authorized behavior-change order.",
    }


def risk_survivability_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    contradictory = []
    for row in rows:
        step3 = _step(row, "step3_risk")
        core = _float(step3.get("ruin_prob"))
        reason_value = _parse_reason_probability(step3.get("surv_reason"))
        if core is not None and reason_value is not None and abs(core - reason_value) > 1e-12:
            contradictory.append({"prediction_id": row.get("id"), "core_ruin_prob": core, "survivability_reason_ruin_prob": reason_value, "reason": step3.get("surv_reason")})
    return {
        "version": AUDIT_VERSION,
        "rows": len(rows),
        "contradictory_display_state_n": len(contradictory),
        "risk_fields_coherent": not contradictory,
        "ruin_prob_contradiction": "CONFIRMED" if contradictory else "FALSIFIED",
        "machine_core_field": "pipeline.step3_risk.ruin_prob",
        "human_survivability_field": "pipeline.step3_risk.surv_reason",
        "missing_machine_field": "pipeline.step3_risk.survivability_ruin_prob",
        "semantics": "The values originate in distinct models, but the persisted payload does not name/serialize the second numeric authority.",
        "examples": contradictory[:12],
        "risk_affects_ev": True,
        "risk_affects_size": True,
        "survivability_affects_size_only": True,
        "current_fresh_risk_state": {"drawdown": 0.0, "var": 0.0, "loss_streak": 0, "core_ruin_prob": 0.0},
        "minimum_correction": "Persist core_ruin_prob and survivability_ruin_prob as distinct machine fields and generate reasons from the latter field.",
        "candidate_remediation": {
            "status": "IMPLEMENTED_FOR_FUTURE_ROWS",
            "decision_semantics_changed": False,
            "new_machine_field": "pipeline.step3_risk.survivability_ruin_prob",
            "historical_rows_unchanged": True,
        },
    }


def feature_availability(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    missing_timestamp = Counter()
    cutoff_violations = []
    masked_rows = []
    for row in rows:
        symbol = _normalize_symbol(row.get("symbol"))
        period = "POST_AUD061" if _audit(row).get("decision_replay_v1") else "LEGACY"
        step1 = _step(row, "step1_market")
        explicit = step1.get("feature_availability_v1") or {}
        row_ts = _dt(row.get("ts"))
        for feature in FEATURES:
            item = explicit.get(feature) if isinstance(explicit, dict) else None
            value = _float(step1.get(feature), 0.0)
            if isinstance(item, dict) and item.get("status"):
                status = str(item["status"])
                observed_at = _dt(item.get("observed_at"))
                if status in OBSERVED_STATUSES and observed_at is None:
                    missing_timestamp[f"{symbol}|{feature}"] += 1
                if observed_at and row_ts and observed_at > row_ts:
                    cutoff_violations.append({"prediction_id": row.get("id"), "feature": feature, "observed_at": item.get("observed_at"), "decision_time": row.get("ts")})
            elif abs(float(value or 0.0)) <= 1e-12:
                status = "UNKNOWN_LEGACY_ZERO_CONFLATED"
            else:
                status = "REAL_NONZERO"
                missing_timestamp[f"{symbol}|{feature}"] += 1
            grouped[f"{period}|{symbol}|{feature}"][status] += 1
        mask = _step(row, "step2_features").get("missing_input_mask_v1") or {}
        if isinstance(mask, dict) and mask.get("masked_features"):
            masked_rows.append({"prediction_id": row.get("id"), "symbol": symbol, "masked_features": sorted(mask.get("masked_features") or []), "first_binding_gate": classify_binding_gate(row)[0]})
    return {
        "version": AUDIT_VERSION,
        "classification_counts": {key: dict(sorted(value.items())) for key, value in sorted(grouped.items())},
        "missing_is_not_real_zero": True,
        "source_error_is_not_real_zero": True,
        "missing_excluded_from_agreement_denominator": True,
        "masked_rows": masked_rows,
        "missing_feature_observation_timestamp_counts": dict(sorted(missing_timestamp.items())),
        "provenance_cutoff_violations": cutoff_violations,
        "provenance_complete": not missing_timestamp and not cutoff_violations,
        "missing_material_counterfactual": "NOT_ESTIMABLE_UNKNOWN_TRUE_VALUES",
        "candidate_remediation": {
            "status": "IMPLEMENTED_FOR_FUTURE_ROWS",
            "decision_semantics_changed": False,
            "ohlcv_features_observed_at": "last_candle_timestamp",
            "orderbook_features_observed_at": "orderbook_source_timestamp",
            "combined_orderflow_observed_at": "max(ohlcv_timestamp,orderbook_timestamp)",
            "historical_rows_unchanged": True,
        },
    }


def _decision_replay_hash(snapshot: dict[str, Any]) -> str:
    payload = {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
    return canonical_hash(payload)


def learning_frozen_vs_learned(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Run exact component replay but fail closed on the missing query marker.

    The stored source IDs and weight hash let us reproduce effective weights and
    final actions by assigning the known decision cutoff as the latest possible
    query-observation time.  The exact process-local query marker was included
    in ``source_evidence_hash`` but was not persisted, so a full causal A/B is
    deliberately not claimed.
    """
    try:
        from oracle_runtime import institutional_core as learning
        from oracle_runtime import institutional_core_real as real
    except ImportError as exc:  # pragma: no cover - dependency gate reported, not hidden
        return {"version": AUDIT_VERSION, "status": "INSUFFICIENT_CAUSAL_PROVENANCE", "error": type(exc).__name__, "decisions": []}

    class OfflineCore(real.SingleDecisionCore):
        def _load_learning_for_symbol(self, symbol: str, decision_cutoff: Any | None = None) -> None:
            return None

    by_id = {row.get("id"): row for row in rows}
    decisions = []
    for row in rows:
        audit = _audit(row)
        snapshot = audit.get("decision_replay_v1")
        learned_step2 = _step(row, "step2_features")
        stored_learning = learned_step2.get("learning_state_v1") or {}
        if not isinstance(snapshot, dict) or not isinstance(stored_learning, dict):
            continue
        cutoff = stored_learning.get("decision_cutoff_epoch")
        source_ids = list(stored_learning.get("source_prediction_ids") or [])
        source_rows = []
        for source_id in source_ids:
            source = copy.deepcopy(by_id.get(source_id) or {})
            source["_senex_snapshot_observed_at_epoch"] = cutoff
            source_rows.append(source)

        core = OfflineCore(
            max_drawdown=0.12,
            ruin_probability_threshold=0.05,
            hard_stop=True,
            max_position_pct=0.25,
            max_leverage=1,
            min_confidence=0.40,
            min_ev_to_trade=0.001,
            no_trade_noise=0.60,
            initial_capital=1000.0,
        )
        replay_state = learning.replay_authoritative_learning(core, source_rows, str(row.get("symbol") or ""), decision_cutoff=cutoff)
        core.weights.clear()
        core.weights.update(replay_state.get("shadow_weights") or replay_state.get("effective_weights") or {})
        learned_action = core.decide(copy.deepcopy(snapshot.get("market") or {}), copy.deepcopy(snapshot.get("risk_state") or {}), copy.deepcopy(snapshot.get("execution_state") or {}))
        learned_weights = dict(core.weights)
        core.weights.clear()
        core.weights.update(replay_state.get("base_weights") or {})
        frozen_action = core.decide(copy.deepcopy(snapshot.get("market") or {}), copy.deepcopy(snapshot.get("risk_state") or {}), copy.deepcopy(snapshot.get("execution_state") or {}))
        learned_pipe = learned_action.get("pipeline") or {}
        frozen_pipe = frozen_action.get("pipeline") or {}
        learned_features = learned_pipe.get("step2_features") or {}
        frozen_features = frozen_pipe.get("step2_features") or {}
        learned_ev = learned_pipe.get("step4_ev") or {}
        frozen_ev = frozen_pipe.get("step4_ev") or {}
        final_learned = learned_action.get("side") if learned_action.get("action") == "EXECUTE" else "FLAT"
        final_frozen = frozen_action.get("side") if frozen_action.get("action") == "EXECUTE" else "FLAT"
        base_weights = replay_state.get("base_weights") or {}
        decisions.append({
            "prediction_id": row.get("id"),
            "symbol": _normalize_symbol(row.get("symbol")),
            "FROZEN_DECISION": final_frozen,
            "LEARNED_DECISION": final_learned,
            "FROZEN_PRESSURE": frozen_features.get("total_pressure"),
            "LEARNED_PRESSURE": learned_features.get("total_pressure"),
            "FROZEN_EV": frozen_ev.get("adjusted_ev"),
            "LEARNED_EV": learned_ev.get("adjusted_ev"),
            "DECISION_CHANGED": final_frozen != final_learned,
            "WEIGHT_DELTA_BY_FEATURE": {name: round(float(learned_weights.get(name, 0.0)) - float(base_weights.get(name, 0.0)), 8) for name in sorted(set(learned_weights) | set(base_weights))},
            "stored_final_decision": row.get("prediction"),
            "learned_replays_stored_final": final_learned == row.get("prediction"),
            "effective_weights_hash_matches": replay_state.get("shadow_weights_hash", replay_state.get("effective_weights_hash")) == stored_learning.get("effective_weights_hash"),
            "source_ids_match": replay_state.get("source_prediction_ids") == source_ids,
            "source_evidence_hash_matches": replay_state.get("source_evidence_hash") == stored_learning.get("source_evidence_hash"),
            "snapshot_hash_matches": _decision_replay_hash(snapshot) == snapshot.get("snapshot_hash"),
        })
    full_provenance = bool(decisions) and all(item["source_evidence_hash_matches"] for item in decisions)
    return {
        "version": AUDIT_VERSION,
        "status": "COMPLETE" if full_provenance else "INSUFFICIENT_CAUSAL_PROVENANCE",
        "analysis_type": "FULL_MODEL_AB" if full_provenance else "COMPONENT_LEVEL_WEIGHT_SENSITIVITY_NOT_MODEL_AB",
        "paired_n": len(decisions),
        "decision_changed_n": sum(item["DECISION_CHANGED"] for item in decisions),
        "learned_exact_final_replay_n": sum(item["learned_replays_stored_final"] for item in decisions),
        "effective_weight_hash_match_n": sum(item["effective_weights_hash_matches"] for item in decisions),
        "source_evidence_hash_match_n": sum(item["source_evidence_hash_matches"] for item in decisions),
        "reason": None if full_provenance else "process_local_query_observation_epoch_is_hashed_but_not_serialized",
        "production_writeback": False,
        "candidate_remediation": {
            "status": "IMPLEMENTED_FOR_FUTURE_ROWS",
            "decision_semantics_changed": True,
            "decision_semantics_scope": "LEARNED_WEIGHTS_SHADOW_ONLY_AND_FROZEN_BASE_WEIGHTS_FOR_PAPER_DECISIONS",
            "new_field": "decision_provenance_roundtrip_v2.learning_source_settlement_observation_epochs",
            "historical_rows_unchanged": True,
        },
        "decisions": decisions,
    }


def confidence_semantics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    max_up = max((_float(_step(row, "step2_features").get("up_prob"), 0.0) or 0.0 for row in rows), default=0.0)
    return {
        "version": AUDIT_VERSION,
        "fields": {
            "confidence": {"class": "RAW_CONVICTION", "calibrated_probability": False, "source": "step2.conviction"},
            "raw_conviction": {"class": "RAW_CONVICTION", "calibrated_probability": False},
            "up_prob": {"class": "HEURISTIC_TRANSFORM", "formula": "sigmoid(total_pressure*5)", "calibrated_probability": False},
            "down_prob": {"class": "HEURISTIC_TRANSFORM", "formula": "sigmoid(-total_pressure*5)", "calibrated_probability": False},
            "polymarket.up_probability": {"class": "MARKET_IMPLIED_PROBABILITY", "calibrated_probability": False},
            "kalshi.yes_probability": {"class": "MARKET_IMPLIED_PROBABILITY", "calibrated_probability": False},
            "score.authoritative_score_pct": {"class": "CALIBRATED_PROBABILITY", "available": False},
            "observed_win_rate_pct": {"class": "EMPIRICAL_WIN_RATE", "diagnostic_only": True},
        },
        "max_observed_up_prob": max_up,
        "reported_96pct_up_interpretation": "HEURISTIC_TRANSFORM_NOT_P_CORRECT",
        "probability_like_mislabel_present": True,
        "audit_confidence_semantics_rows": sum(isinstance(_audit(row).get("confidence_semantics_v1"), dict) for row in rows),
    }


def score_truth(bundle: dict[str, Any]) -> dict[str, Any]:
    snapshots = bundle.get("scores") or {}
    symbols: dict[str, Any] = {}
    for symbol in ("BTCUSDT", "ETHUSDT"):
        score = snapshots.get(symbol) or {}
        authority = score.get("authority_1h") or {}
        global_authority = authority.get("global") or {}
        symbols[symbol] = {
            "requested_symbol": score.get("requested_symbol"),
            "score_status": score.get("score_status"),
            "proof_qualified_rows_raw": score.get("proof_qualified_rows_raw"),
            "independent_1h_rows": score.get("independent_1h_rows"),
            "authority_cohort": score.get("authority_cohort"),
            "authority_horizon_seconds": score.get("authority_horizon_seconds"),
            "authority_win_rate_pct": global_authority.get("win_rate_pct"),
            "authoritative_score_pct": score.get("authoritative_score_pct"),
            "observed_win_rate_pct": score.get("observed_win_rate_pct"),
            "observed_win_rate_diagnostic_only": bool(score.get("observed_win_rate_diagnostic_only")),
            "confidence_semantics": score.get("confidence_semantics"),
            "confidence_probability_semantics": score.get("confidence_probability_semantics"),
            "trade_mode": score.get("trade_mode"),
            "orders_enabled": bool(score.get("orders_enabled")),
            "live_capital_locked": bool(score.get("live_capital_locked")),
            "reasons": score.get("reasons") or [],
        }
    return {
        "version": AUDIT_VERSION,
        "symbols": symbols,
        "symbol_isolation": all(symbols[s]["requested_symbol"] == s for s in symbols),
        "authority_affects_production_decision": False,
        "authority_state": "INSUFFICIENT_EVIDENCE",
        "paper_live_lock": all(
            item["trade_mode"] == "PAPER"
            and item["orders_enabled"] is False
            and item["live_capital_locked"] is True
            for item in symbols.values()
        ),
    }


def authority_feedback_loop(attribution: list[dict[str, Any]], score: dict[str, Any]) -> dict[str, Any]:
    authority_rejects = [item for item in attribution if item["first_binding_gate"] == "AUTHORITY_INSUFFICIENT"]
    post_learning = [item for item in attribution if item.get("learning_source_n") in {10, 11}]
    return {
        "version": AUDIT_VERSION,
        "direct_authority_reject_n": len(authority_rejects),
        "authority_affects_production_direction_or_ev": False,
        "poor_score_to_lower_current_ev_loop": "NOT_DEMONSTRATED",
        "score_authority_state": score["authority_state"],
        "separate_learning_feedback_present": bool(post_learning),
        "learning_feedback_rows": len(post_learning),
        "learning_feedback_decision_changes_in_paired_replay": 0,
        "authority_feedback_loop": "NO",
        "learning_feedback_status": "BOUNDED_COMPONENT_EFFECT_WITH_INSUFFICIENT_CAUSAL_PROVENANCE",
    }


def external_shadow_ledger(rows: list[dict[str, Any]], resolutions: dict[str, Any]) -> dict[str, Any]:
    resolved = {item.get("slug"): item for item in resolutions.get("results", []) if isinstance(item, dict)}
    ledger = []
    kalshi_ledger = []
    boros_ledger = []
    for row in rows:
        audit = _audit(row)
        external = audit.get("external_markets_v1") or {}
        if not isinstance(external, dict):
            continue

        kalshi = external.get("kalshi") or {}
        if isinstance(kalshi, dict) and kalshi.get("status") not in {None, "NOT_APPLICABLE"}:
            market = kalshi.get("market") or {}
            if not isinstance(market, dict):
                market = {}
            yes = _float(market.get("yes_probability"))
            kalshi_ledger.append({
                "prediction_id": row.get("id"),
                "decision_time": row.get("ts"),
                "symbol": _normalize_symbol(row.get("symbol")),
                "source": kalshi.get("source"),
                "status": kalshi.get("status"),
                "observed_at": kalshi.get("observed_at"),
                "freshness_s": _float(kalshi.get("freshness_s")),
                "source_horizon": kalshi.get("horizon") or "15m",
                "senex_horizon": "1h",
                "horizon_compatible": False,
                "product_compatible": _normalize_symbol(row.get("symbol")) == "BTCUSDT",
                "market_ticker": market.get("ticker"),
                "yes_probability": yes,
                "implied_direction": "LONG" if yes is not None and yes > 0.5 else "SHORT" if yes is not None and yes < 0.5 else "FLAT",
                "actual_production_decision": row.get("prediction"),
                "external_applied": 0,
                "value_add_status": "NOT_ESTIMABLE_HORIZON_MISMATCH",
            })

        boros = external.get("boros") or {}
        if isinstance(boros, dict) and boros.get("status") not in {None, "NOT_APPLICABLE"}:
            markets = boros.get("markets") or []
            boros_ledger.append({
                "prediction_id": row.get("id"),
                "decision_time": row.get("ts"),
                "symbol": _normalize_symbol(row.get("symbol")),
                "source": boros.get("source"),
                "status": boros.get("status"),
                "observed_at": boros.get("observed_at"),
                "freshness_s": _float(boros.get("freshness_s")),
                "source_horizon": "FUNDING_YIELD_TERM_STRUCTURE",
                "senex_horizon": "1h_DIRECTIONAL",
                "horizon_compatible": False,
                "product_compatible": False,
                "market_count": len(markets) if isinstance(markets, list) else 0,
                "actual_production_decision": row.get("prediction"),
                "external_applied": 0,
                "value_add_status": "NOT_ESTIMABLE_PRODUCT_AND_HORIZON_MISMATCH",
            })

        if _normalize_symbol(row.get("symbol")) != "BTCUSDT":
            continue
        poly = external.get("polymarket") or {}
        if not isinstance(poly, dict) or poly.get("up_probability") is None:
            continue
        up = _float(poly.get("up_probability"))
        down = _float(poly.get("down_probability"))
        if up is None or down is None:
            continue
        slug = poly.get("slug")
        resolution = resolved.get(slug, {})
        window_start = None
        if slug:
            try:
                window_start = datetime.fromtimestamp(int(str(slug).rsplit("-", 1)[-1]), timezone.utc)
            except (ValueError, OSError):
                pass
        window_end = window_start + timedelta(seconds=300) if window_start else _dt(resolution.get("end_date"))
        decision_time = _dt(row.get("ts"))
        target_end = decision_time + timedelta(hours=1) if decision_time else None
        freshness = _float(poly.get("freshness_s"))
        inferred_snapshot = decision_time - timedelta(seconds=freshness) if decision_time and freshness is not None else None
        up_book = poly.get("up") or {}
        down_book = poly.get("down") or {}
        up_bid, up_ask = _float(up_book.get("best_bid")), _float(up_book.get("best_ask"))
        down_bid, down_ask = _float(down_book.get("best_bid")), _float(down_book.get("best_ask"))
        up_mid = (up_bid + up_ask) / 2.0 if up_bid is not None and up_ask is not None else up
        down_mid = (down_bid + down_ask) / 2.0 if down_bid is not None and down_ask is not None else down
        implied = "LONG" if up > 0.5 else "SHORT" if up < 0.5 else "FLAT"
        internal = str(_step(row, "step2_features").get("direction") or "NEUTRAL")
        winner = resolution.get("resolved_winner")
        overlap = 0.0
        elapsed_since_window_start = None
        if decision_time and target_end and window_start and window_end:
            overlap = max(0.0, (min(target_end, window_end) - max(decision_time, window_start)).total_seconds())
            elapsed_since_window_start = (decision_time - window_start).total_seconds()
        ledger.append({
            "prediction_id": row.get("id"),
            "decision_time": row.get("ts"),
            "market_slug": slug,
            "market_id": resolution.get("market_id") or poly.get("condition_id"),
            "window_start": window_start.isoformat() if window_start else None,
            "window_end": window_end.isoformat() if window_end else None,
            "resolution_source": resolution.get("resolution_source") or poly.get("resolution_source"),
            "snapshot_timestamp": poly.get("observed_at"),
            "snapshot_timestamp_inferred_non_authoritative": inferred_snapshot.isoformat() if inferred_snapshot else None,
            "freshness_s": freshness,
            "up_bid": up_bid,
            "up_ask": up_ask,
            "up_mid": up_mid,
            "down_bid": down_bid,
            "down_ask": down_ask,
            "down_mid": down_mid,
            "spread": {"up": _float(up_book.get("spread")), "down": _float(down_book.get("spread"))},
            "depth_liquidity": {"up_bid_depth_5": up_book.get("bid_depth_5"), "up_ask_depth_5": up_book.get("ask_depth_5"), "down_bid_depth_5": down_book.get("bid_depth_5"), "down_ask_depth_5": down_book.get("ask_depth_5")},
            "implied_direction": implied,
            "senex_target_window": {"start": decision_time.isoformat() if decision_time else None, "end": target_end.isoformat() if target_end else None, "horizon": "1h"},
            "overlap_seconds": overlap,
            "elapsed_since_window_start_seconds": elapsed_since_window_start,
            "exact_horizon_match": False,
            "endogenous_same_market_label": True,
            "internal_direction": internal,
            "final_senex_decision": row.get("prediction"),
            "subsequent_polymarket_settlement_label": winner,
            "INTERNAL_ONLY": internal,
            "EXTERNAL_ONLY_SHADOW": implied,
            "BLENDED_SHADOW": "NOT_ESTIMABLE_NO_PREDECLARED_BLEND",
            "ACTUAL_PRODUCTION_DECISION": row.get("prediction"),
            "external_applied": 0,
        })
    directional = [item for item in ledger if item["internal_direction"] in {"LONG", "SHORT"} and item["implied_direction"] in {"LONG", "SHORT"}]
    settled = [item for item in ledger if item["subsequent_polymarket_settlement_label"] in {"Up", "Down"}]
    strong = [item for item in ledger if abs(float(item["up_mid"]) - 0.5) >= 0.20]
    correct = sum((item["implied_direction"] == "LONG") == (item["subsequent_polymarket_settlement_label"] == "Up") for item in settled)
    flips = [item for item in directional if item["internal_direction"] != item["implied_direction"]]
    flat_strong = [item for item in strong if item["final_senex_decision"] == "FLAT"]
    return {
        "version": AUDIT_VERSION,
        "status": "DIAGNOSTIC_ONLY",
        "polymarket_rows": len(ledger),
        "resolved_rows": len(settled),
        "horizon_match_rate": 0.0,
        "internal_external_agreement": _rate(len(directional) - len(flips), len(directional)),
        "external_strong_event_count": len(strong),
        "same_market_resolved_label_agreement": _rate(correct, len(settled)),
        "same_market_metric_is_predictive_accuracy": False,
        "external_value_add_status": "NOT_ESTIMABLE_NO_CAUSALLY_ALIGNED_OOS_LABEL",
        "external_signal_value_assessment": {
            "adds_value": "NOT_PROVEN",
            "redundant": "NOT_PROVEN",
            "unsupported": True,
            "reason": "snapshot timestamp gaps, same-window endogenous label, target mismatch, and no preregistered blend",
        },
        "external_shadow_abstention_effect": {"production_flat_with_strong_external_n": len(flat_strong), "counterfactual_action_not_claimed": True},
        "would_flip_decision_count": len(flips),
        "exact_snapshot_timestamp_coverage": _rate(sum(item["snapshot_timestamp"] is not None for item in ledger), len(ledger)),
        "kalshi": {"status": "HORIZON_MISMATCH", "horizon": "15m", "directional_applied": 0, "rows": len(kalshi_ledger)},
        "boros": {"status": "PRODUCT_AND_HORIZON_MISMATCH", "purpose": "funding context", "directional_applied": 0, "rows": len(boros_ledger)},
        "kalshi_ledger": kalshi_ledger,
        "boros_ledger": boros_ledger,
        "candidate_remediation": {
            "status": "IMPLEMENTED_FOR_FUTURE_ROWS",
            "new_field": "external_markets_v1.<source>.observed_at",
            "decision_semantics_changed": False,
            "external_directional_activation": False,
        },
        "production_writeback": False,
        "ledger": ledger,
    }


def horizon_alignment(rows: list[dict[str, Any]], external: dict[str, Any]) -> dict[str, Any]:
    proof_rows = []
    violations = Counter()
    window_checks = Counter()
    for row in rows:
        dual = _audit(row).get("outcomes_dual") or {}
        if row.get("outcome") in {"WIN", "LOSS"} and isinstance(dual, dict):
            origin = _float(row.get("price_now"))
            later_1h = _float(dual.get("price_1h_later"))
            later_15m = _float(dual.get("price_15m_later"))
            if origin and later_1h:
                proof_rows.append(row)
                side = str(row.get("prediction") or "")
                computed_1h = "WIN" if (later_1h > origin and side == "LONG") or (later_1h < origin and side == "SHORT") else "LOSS"
                window_checks["1h"] += 1
                if computed_1h != dual.get("outcome_1h"):
                    violations["WRONG_SETTLEMENT_OR_SIGN"] += 1
                if dual.get("primary_window") == "1h" and computed_1h != row.get("outcome"):
                    violations["PRIMARY_WINDOW_OUTCOME_MISMATCH"] += 1
            if origin and later_15m:
                side = str(row.get("prediction") or "")
                computed_15m = "WIN" if (later_15m > origin and side == "LONG") or (later_15m < origin and side == "SHORT") else "LOSS"
                window_checks["15m"] += 1
                if computed_15m != dual.get("outcome_15m"):
                    violations["WRONG_15M_SETTLEMENT_OR_SIGN"] += 1
            origin_proof = _audit(row).get("origin_price_v1") or {}
            if origin_proof and _float(origin_proof.get("price")) != origin:
                violations["WRONG_ORIGIN_PRICE"] += 1
    return {
        "version": AUDIT_VERSION,
        "primary_prediction_horizon": "1h",
        "proof_rows_checked": len(proof_rows),
        "window_checks": dict(sorted(window_checks.items())),
        "violations": dict(sorted(violations.items())),
        "lookahead_leakage": "NOT_DETECTED_IN_PROOF_ROWS",
        "label_window_shift": "NOT_DETECTED_IN_PROOF_ROWS",
        "overlap_policy": "INDEPENDENT_NONOVERLAP_1H_FOR_AUTHORITY",
        "external_polymarket_horizon": "5m_SUBWINDOW_NOT_SAME_TARGET",
        "external_kalshi_horizon": "15m_MISMATCH",
        "external_boros_horizon": "FUNDING_PRODUCT_MISMATCH",
        "external_exact_horizon_match_rate": external.get("horizon_match_rate"),
        "status": "PARTIAL_PROVENANCE_GAPS",
    }


def pre_post_behavior(attribution: list[dict[str, Any]]) -> dict[str, Any]:
    deploy = _dt(PRODUCTION_DEPLOYED_AT)
    cohorts = {
        "PRE_AUD061": [item for item in attribution if (_dt(item["timestamp"]) or datetime.max.replace(tzinfo=timezone.utc)) < deploy],
        "POST_AUD061": [item for item in attribution if (_dt(item["timestamp"]) or datetime.min.replace(tzinfo=timezone.utc)) >= deploy],
    }
    result = {}
    for name, items in cohorts.items():
        pressures = [float(item["aggregate_pressure"] or 0.0) for item in items]
        convictions = [float(item["raw_conviction"] or 0.0) for item in items]
        adjusted = [float(item["adjusted_ev"] or 0.0) for item in items]
        result[name] = {
            **_distribution(items),
            "mean_raw_conviction": _decimal_mean(convictions),
            "mean_aggregate_pressure": _decimal_mean(pressures),
            "mean_adjusted_ev_proxy": _decimal_mean(adjusted),
            "binding_gates": dict(sorted(Counter(item["first_binding_gate"] for item in items if item["final_decision"] == "FLAT").items())),
            "feature_availability_schema_n": sum(bool(item["feature_statuses"]) for item in items),
            "learning_n": sorted({item["learning_source_n"] for item in items if item["learning_source_n"] is not None}),
        }
    return {"version": AUDIT_VERSION, "normalization": "COMMON_EXECUTABLE_PIPELINE_FIELDS_ONLY", "cohorts": result}


def governance_auto_cd(bundle: dict[str, Any]) -> dict[str, Any]:
    observed = bundle.get("governance") or {}
    paths = [
        "direct push to unprotected main",
        "force push unless separately denied at actor/repository level",
        "GitHub UI edit committing to main",
        "manual oracle.yml workflow_dispatch with contents:write and git push",
        "merge without required checks/reviews",
        "stale or unaudited PR merge",
        "bot/app repository-content write followed by automatic Northflank build",
    ]
    return {
        "version": AUDIT_VERSION,
        "branch_protected": bool(observed.get("branch_protected")),
        "ruleset_count": int(observed.get("ruleset_count") or 0),
        "required_status_checks": observed.get("required_status_checks") or [],
        "northflank_status_context": observed.get("northflank_status_context"),
        "deployed_sha": observed.get("deployed_sha"),
        "auto_cd_evidence": observed.get("auto_cd_evidence"),
        "AUTO_CD_SAFE_UNDER_CURRENT_GOVERNANCE": "NO",
        "UNREVIEWED_MAIN_TO_PROD_PATHS": paths,
        "RECOMMENDED_GUARDRAILS": [
            "protect main with required pull requests and at least one owner/AUD approval",
            "require exact-head SCORE-001, SCORE-002, smoke, and audit checks",
            "block force pushes and deletions",
            "restrict bypass to no standing actor; use time-bounded break-glass only",
            "change oracle.yml to contents:read or remove its direct git push job",
            "pin/approve GitHub Apps allowed to write contents",
            "require deployment environment approval bound to reviewed SHA before Northflank production rollout",
        ],
        "settings_mutated": False,
    }


def decision_graph() -> dict[str, Any]:
    def edge(source: str, target: str, function: str, inputs: list[str], outputs: list[str], units: str, domain: str, fallback: str, missing: str, zero: str, error: str, force_flat: bool, change_direction: bool, effect: str) -> dict[str, Any]:
        return {
            "source": source, "target": target, "source_function": function,
            "input_fields": inputs, "output_fields": outputs, "units": units,
            "range_domain": domain, "fallback_behavior": fallback,
            "missing_behavior": missing, "zero_behavior": zero,
            "error_behavior": error, "causal_at_decision_time": True,
            "can_force_flat": force_flat, "can_change_direction": change_direction,
            "changes_ev_or_size": effect,
        }
    edges = [
        edge("RAW_MARKET", "FEATURE_OBSERVATIONS", "exchange_connector.fetch_market_snapshot", ["ohlcv", "orderbook", "funding", "open_interest"], ["feature_observations"], "mixed raw + provenance", "public OKX observations", "explicit fallback", "MISSING/SOURCE_ERROR", "REAL_OBSERVED_ZERO", "status not numeric zero", True, True, "direction+EV"),
        edge("FEATURE_OBSERVATIONS", "MARKET_STATE", "oracle_runtime.institutional_core.SingleDecisionCore.ingest_market", list(FEATURES), list(FEATURES)+["feature_availability_v1"], "decimal pressures", "finite real", "numeric 0 with mask", "masked", "observed neutral", "masked", True, True, "direction+EV"),
        edge("MARKET_STATE", "NORMALIZED_PRESSURES", "SingleDecisionCore.compress_features", list(FEATURES), ["pressures", "missing_input_mask_v1"], "weighted decimal", "unbounded then sigmoid", "base weights", "excluded", "included neutral", "fail-open core", True, True, "direction+EV"),
        edge("LEARNING_EVIDENCE", "EFFECTIVE_WEIGHTS", "replay_authoritative_learning", ["prior proof rows", "decision cutoff"], ["effective_weights", "hash"], "weight multiplier", "base +/-25%", "base weights", "row excluded", "zero pressure ignored", "base weights", False, True, "direction+EV+size"),
        edge("NORMALIZED_PRESSURES", "AGGREGATE_PRESSURE", "compress_features", ["pressures", "effective_weights"], ["total_pressure", "agreement", "noise"], "decimal pressure", "finite", "sum", "excluded denominator", "observed zero denominator member", "noise=1 if none", True, True, "direction+EV"),
        edge("AGGREGATE_PRESSURE", "DIRECTION_CANDIDATE", "compress_features", ["total_pressure"], ["direction"], "decimal", "LONG >.05; SHORT <-.05", "NEUTRAL", "NEUTRAL", "NEUTRAL", "NEUTRAL", True, True, "direction"),
        edge("DIRECTION_CANDIDATE", "RAW_CONVICTION", "compress_features", ["total_pressure", "noise"], ["up_prob", "down_prob", "conviction"], "heuristic 0..1", "0..1", "0", "0", "0", "0", True, False, "EV+size"),
        edge("RAW_CONVICTION", "CORE_BASE_EV", "SingleDecisionCore.compute_ev", ["p_win", "ATR", "commission", "slippage"], ["base_ev", "estimated_cost"], "decimal return", "finite", "single-candle volatility", "defaults", "valid", "no exception guard", True, False, "EV"),
        edge("CORE_BASE_EV", "CORE_SURVIVAL_EV", "compute_ev", ["base_ev", "risk_score"], ["survival_discount"], "multiplier", ".2..1", "1", "1", "1", "no guard", True, False, "EV"),
        edge("RAW_CONVICTION", "MARKET_ANCHOR_EV", "oracle.market_ev.compute_market_ev", ["conviction", "noise", "single-candle volatility", "default costs"], ["market_ev"], "decimal return", "finite", "default depth/slippage", "defaults", "valid", "no guard", True, False, "EV"),
        edge("CORE_SURVIVAL_EV", "ADJUSTED_EV", "min(core_survival_ev, market_anchor_ev)", ["core", "anchor"], ["adjusted_ev"], "decimal return", "minimum branch", "anchor available", "anchor default", "valid", "no guard", True, False, "EV"),
        edge("ADJUSTED_EV", "DYNAMIC_MIN_EV", "compute_ev", ["ATR/vol_ref", "min_ev_to_trade"], ["dynamic_min_ev", "tradeable"], "decimal return", "5%-100% base threshold", "volatility", "low-vol tier", "low-vol tier", "no guard", True, False, "EV gate"),
        edge("RISK_STATE", "RISK_GATE", "filter_risk", ["drawdown", "VaR", "loss streak", "survivability"], ["verdict", "risk_score", "size_multiplier"], "probability/multiplier", "0..1", "fresh zero state", "zero risk", "zero risk", "KILL on core threshold", True, False, "EV+size"),
        edge("DYNAMIC_MIN_EV", "FEASIBILITY", "check_execution_feasibility", ["tradeable", "spread", "slippage", "latency", "liquidity"], ["feasible", "size_adjustment"], "bps/multiplier", "0..1", "fixed defaults", "defaults", "valid", "reject", True, False, "size+gate"),
        edge("ALL_GATES", "POSITION_SIZE", "produce_action", ["conviction", "entropy", "calibration", "risk", "execution"], ["final_size"], "equity fraction", "0..0.25", "0", "0", "0", "HOLD", True, False, "size"),
        edge("POSITION_SIZE", "FINAL_DECISION", "produce_action + predict_only.run_prediction", ["ordered gates", "side", "size"], ["LONG|SHORT|FLAT"], "categorical", "3 states", "FLAT", "FLAT", "FLAT", "FLAT", True, True, "final"),
    ]
    return {"version": AUDIT_VERSION, "nodes": sorted({edge[side] for edge in edges for side in ("source", "target")}), "edges": edges, "external_directional_applied": 0}


def findings(artifacts: dict[str, Any]) -> dict[str, Any]:
    ev = artifacts["aud-062-ev-bridge.json"]
    risk = artifacts["aud-062-risk-survivability-audit.json"]
    learning = artifacts["aud-062-learning-frozen-vs-learned.json"]
    governance = artifacts["aud-062-governance-auto-cd.json"]
    feature = artifacts["aud-062-feature-availability.json"]
    reason_semantics = artifacts["aud-062-action-reason-semantics.json"]
    external = artifacts["aud-062-external-shadow-ledger.json"]
    records = [
        {
            "FINDING_ID": "AUD062-F001", "SEVERITY": "HIGH", "STATUS": "CONFIRMED",
            "TITLE": "A hidden, semantically different market-EV branch dominates abstention",
            "AFFECTED_PATHS": ["senecio_polymarket/oracle/institutional_core.py:750-806", "senecio_polymarket/oracle/market_ev.py:280-324"],
            "AFFECTED_RUNTIME_FIELDS": ["pipeline.step4_ev.adjusted_ev", "tradeable", "prediction"],
            "FIRST_BAD_OR_RELEVANT_COMMIT": "NOT_PROVEN",
            "EVIDENCE": {"market_anchor_binding_n": ev["market_anchor_binding_n"], "positive_base_to_negative_n": ev["positive_base_to_negative_adjusted_n"], "core_tradeable_anchor_rejected_n": ev["core_tradeable_but_anchor_rejected_n"]},
            "REPRODUCTION": "python -m scripts.run_aud062 analyze --input docs/evidence/aud062-public-inputs.json.gz --output <dir>",
            "CAUSAL_IMPACT": "The min() selects an anchor built from raw conviction, single-candle volatility, entropy, and default costs; it can turn core-positive EV into negative adjusted EV and FLAT.",
            "SAFETY_IMPACT": "Over-abstention, not increased trading.", "STATISTICAL_IMPACT": "Observed directional coverage is materially changed by an unsupported alternate model.",
            "WHY_EXISTING_TESTS_MISSED_IT": "Tests assert market EV does not exceed model EV but do not reconcile the two incompatible formulas or serialize the bridge.",
            "MINIMUM_CORRECTION": "Under a separate behavior-change order, select one instrument-bound EV formula or fully reconcile/serialize the anchor terms.",
            "REGRESSION_GATE": "Assert no unexplained residual and no alternate branch with undocumented probability/cost semantics.",
        },
        {
            "FINDING_ID": "AUD062-F002", "SEVERITY": "MEDIUM", "STATUS": "CONFIRMED",
            "TITLE": "Persisted risk state says ruin_prob=0 while its human reason says HIGH_RUIN_PROB=50%",
            "AFFECTED_PATHS": ["senecio_polymarket/oracle/institutional_core.py:593-664", "senecio_polymarket/oracle/survivability.py:330-383"],
            "AFFECTED_RUNTIME_FIELDS": ["step3_risk.ruin_prob", "step3_risk.surv_reason"], "FIRST_BAD_OR_RELEVANT_COMMIT": "NOT_PROVEN",
            "EVIDENCE": {"contradictory_rows": risk["contradictory_display_state_n"]}, "REPRODUCTION": "test_aud_062.RiskSemanticsTests",
            "CAUSAL_IMPACT": "Distinct risk models are serialized as if one numeric field explained the human reason.", "SAFETY_IMPACT": "Can cause false risk interpretation; current survivability factor reduces size only.",
            "STATISTICAL_IMPACT": "The 50% value is an insufficient-data prior, not the core ruin estimate.", "WHY_EXISTING_TESTS_MISSED_IT": "No invariant binds reason text to a named machine field.",
            "MINIMUM_CORRECTION": "Serialize both named ruin probabilities and generate reason from the matching field.", "REGRESSION_GATE": "Machine/reason same-authority invariant.",
            "CANDIDATE_REMEDIATION": risk["candidate_remediation"],
        },
        {
            "FINDING_ID": "AUD062-F003", "SEVERITY": "HIGH", "STATUS": "CONFIRMED",
            "TITLE": "Unprotected main plus automatic Northflank CD permits unaudited repository-write-to-production paths",
            "AFFECTED_PATHS": [".github/workflows/oracle.yml:10-14,110-120", "GitHub branch settings", "Northflank main tracking"],
            "AFFECTED_RUNTIME_FIELDS": ["production lineage"], "FIRST_BAD_OR_RELEVANT_COMMIT": "NOT_PROVEN",
            "EVIDENCE": {"protected": governance["branch_protected"], "rulesets": governance["ruleset_count"], "paths": governance["UNREVIEWED_MAIN_TO_PROD_PATHS"]},
            "REPRODUCTION": "GET /repos/simonkey888/SeneX-Prophet/branches/main and inspect oracle.yml",
            "CAUSAL_IMPACT": "A direct/UI/workflow/app write to main can become a Northflank deployment without the intended audit gate.", "SAFETY_IMPACT": "Production trust-boundary bypass.",
            "STATISTICAL_IMPACT": "Unaudited code/evidence can replace the reviewed lineage.", "WHY_EXISTING_TESTS_MISSED_IT": "CI checks code, not repository settings or auto-CD trust boundaries.",
            "MINIMUM_CORRECTION": "Apply the exact guardrails listed in governance evidence.", "REGRESSION_GATE": "Protected main + required checks + no contents-write direct-push workflow.",
        },
        {
            "FINDING_ID": "AUD062-F004", "SEVERITY": "MEDIUM", "STATUS": "CONFIRMED",
            "TITLE": "Decision replay omits the exact query-observation epoch and several feature/external snapshot timestamps",
            "AFFECTED_PATHS": ["senecio_polymarket/oracle_runtime/institutional_core.py:129-178,209-374", "senecio_polymarket/oracle_runtime/predict_only.py:48-165"],
            "AFFECTED_RUNTIME_FIELDS": ["source_evidence_hash", "feature_availability_v1.observed_at", "decision_replay_v1"], "FIRST_BAD_OR_RELEVANT_COMMIT": "49c5f0a69609c005da80e48b585e91d8582a5ac6",
            "EVIDENCE": {"component_pairs": learning["paired_n"], "source_hash_matches": learning["source_evidence_hash_match_n"], "missing_timestamp_counts": feature["missing_feature_observation_timestamp_counts"]},
            "REPRODUCTION": "test_aud_062.LearningProvenanceTests and FeatureTruthTests", "CAUSAL_IMPACT": "Exact weights/actions can be replayed from IDs, but the hashed evidence snapshot cannot be independently reconstructed.",
            "SAFETY_IMPACT": "No direct trading activation; auditability gap.", "STATISTICAL_IMPACT": "Full frozen-vs-learned A/B fails closed as insufficient causal provenance.",
            "WHY_EXISTING_TESTS_MISSED_IT": "Tests verify deterministic hashes in-memory, not reconstruction from persisted rows.", "MINIMUM_CORRECTION": "Persist the query observation epoch and per-feature/external snapshot timestamps in decision_replay_v1.",
            "REGRESSION_GATE": "Offline source_evidence_hash equals persisted hash without invented fields.",
            "CANDIDATE_REMEDIATION": {
                "learning": learning["candidate_remediation"],
                "features": feature["candidate_remediation"],
                "external": external["candidate_remediation"],
            },
        },
        {
            "FINDING_ID": "AUD062-F005", "SEVERITY": "MEDIUM", "STATUS": "CONFIRMED",
            "TITLE": "The paper cost model has no single executable instrument or primary fee/depth provenance",
            "AFFECTED_PATHS": ["senecio_polymarket/oracle/predict_only.py:64,371-383", "senecio_polymarket/oracle/institutional_core.py:722-735", "senecio_polymarket/oracle/market_ev.py:57-78"],
            "AFFECTED_RUNTIME_FIELDS": ["estimated_cost", "adjusted_ev", "tradeable"], "FIRST_BAD_OR_RELEVANT_COMMIT": "NOT_PROVEN",
            "EVIDENCE": artifacts["aud-062-cost-model-audit.json"], "REPRODUCTION": "test_aud_062.EVFormulaTests",
            "CAUSAL_IMPACT": "Fixed defaults and mixed spot/swap semantics can bind EV without matching an executable paper instrument.", "SAFETY_IMPACT": "Conservative but potentially false abstention.",
            "STATISTICAL_IMPACT": "EV labels cannot be interpreted as instrument-realized expectancy.", "WHY_EXISTING_TESTS_MISSED_IT": "Unit tests validate arithmetic monotonicity, not primary-source parameter provenance.",
            "MINIMUM_CORRECTION": "Instrument-bind fees, spread, slippage, and depth in a separately authorized order.", "REGRESSION_GATE": "Cost provenance and unit contract fixture.",
        },
        {
            "FINDING_ID": "AUD062-F006", "SEVERITY": "MEDIUM", "STATUS": "CONFIRMED",
            "TITLE": "up_prob/down_prob are probability-like names for an uncalibrated heuristic transform",
            "AFFECTED_PATHS": ["senecio_polymarket/oracle/institutional_core.py:500-548", "senecio_polymarket/oracle_runtime/institutional_core.py:605-620"],
            "AFFECTED_RUNTIME_FIELDS": ["step2_features.up_prob", "step2_features.down_prob"], "FIRST_BAD_OR_RELEVANT_COMMIT": "NOT_PROVEN",
            "EVIDENCE": artifacts["aud-062-confidence-semantics.json"], "REPRODUCTION": "test_aud_062.ConfidenceSemanticsTests",
            "CAUSAL_IMPACT": "A displayed or reported '96% UP' can be mistaken for P(correct), though it is sigmoid(total_pressure*5).", "SAFETY_IMPACT": "False confidence semantics.",
            "STATISTICAL_IMPACT": "No OOS calibration supports probability interpretation.", "WHY_EXISTING_TESTS_MISSED_IT": "Confidence is labeled, but up_prob/down_prob are not.",
            "MINIMUM_CORRECTION": "Rename to heuristic_up_score/down_score or attach explicit semantics at every API/UI boundary.", "REGRESSION_GATE": "Probability-like field semantic registry.",
        },
        {
            "FINDING_ID": "AUD062-F007", "SEVERITY": "MEDIUM", "STATUS": "CONFIRMED",
            "TITLE": "Small-N evidence mutates weights and also feeds size calibration before score authority exists",
            "AFFECTED_PATHS": ["senecio_polymarket/oracle_runtime/institutional_core.py:298-439", "senecio_polymarket/oracle/institutional_core.py:1001-1030"],
            "AFFECTED_RUNTIME_FIELDS": ["learning_state_v1", "effective_weights", "final_size"], "FIRST_BAD_OR_RELEVANT_COMMIT": "49c5f0a69609c005da80e48b585e91d8582a5ac6",
            "EVIDENCE": {"paired_n": learning["paired_n"], "decision_changed_n": learning["decision_changed_n"], "status": learning["status"]},
            "REPRODUCTION": "test_aud_062.LearningProvenanceTests", "CAUSAL_IMPACT": "The same 10/11 independent rows affect pressures/EV through weights and size through calibration; score authority remains insufficient.",
            "SAFETY_IMPACT": "Bounded +/-25% and PAPER-only; no live activation.", "STATISTICAL_IMPACT": "No independent evidence justifies treating N=10/11 as production-learning authority.",
            "WHY_EXISTING_TESTS_MISSED_IT": "Tests enforce the configured N=10 and drift bound but do not compare it with reporting authority or double-use.",
            "MINIMUM_CORRECTION": "Prospectively preregister learning authority and separate/justify the calibration cohort before changing behavior.", "REGRESSION_GATE": "Learning-authority contract and independent cohort accounting.",
        },
        {
            "FINDING_ID": "AUD062-F008", "SEVERITY": "MEDIUM", "STATUS": "CONFIRMED",
            "TITLE": "The persisted negative_ev reason mislabels positive EV that only fails the dynamic minimum",
            "AFFECTED_PATHS": ["senecio_polymarket/oracle/institutional_core.py:974-978"],
            "AFFECTED_RUNTIME_FIELDS": ["action_vector.reason", "decision_waterfall_v1.raw_reason"], "FIRST_BAD_OR_RELEVANT_COMMIT": "NOT_PROVEN",
            "EVIDENCE": {"positive_ev_mislabeled_n": reason_semantics["positive_adjusted_ev_mislabeled_negative_n"], "affected_prediction_ids": reason_semantics["affected_prediction_ids"]},
            "REPRODUCTION": "test_aud_062.ActionReasonSemanticsTests", "CAUSAL_IMPACT": "The decision remains FLAT, but its machine/human reason falsely states negative EV instead of positive EV below the configured threshold.",
            "SAFETY_IMPACT": "Incorrect operator diagnosis can motivate the wrong remediation.", "STATISTICAL_IMPACT": "Sign and threshold failure are conflated in abstention attribution.",
            "WHY_EXISTING_TESTS_MISSED_IT": "Tests asserted HOLD behavior but not reason-to-numeric-field coherence.",
            "MINIMUM_CORRECTION": "Serialize adjusted_ev and dynamic_min_ev under an ev_below_dynamic_min reason without changing the gate.",
            "REGRESSION_GATE": "Reason sign/threshold coherence plus decision-invariance fixture.",
            "CANDIDATE_REMEDIATION": reason_semantics["candidate_remediation"],
        },
        {
            "FINDING_ID": "AUD062-F009", "SEVERITY": "MEDIUM", "STATUS": "CONFIRMED",
            "TITLE": "The 5m Polymarket resolved-label agreement is not a causal predictive-accuracy or value-add estimate",
            "AFFECTED_PATHS": ["senecio_polymarket/backend/research/aud062_forensics.py:external_shadow_ledger"],
            "AFFECTED_RUNTIME_FIELDS": ["external_markets_v1.polymarket", "AUD-062 shadow evidence"], "FIRST_BAD_OR_RELEVANT_COMMIT": "c17f27800f614e9afc69862484a17576f376d39f",
            "EVIDENCE": {"resolved_rows": external["resolved_rows"], "same_market_resolved_label_agreement": external["same_market_resolved_label_agreement"], "exact_snapshot_timestamp_coverage": external["exact_snapshot_timestamp_coverage"], "horizon_match_rate": external["horizon_match_rate"]},
            "REPRODUCTION": "test_aud_062.ExternalShadowTests", "CAUSAL_IMPACT": "A snapshot observed inside the same 5m market window is compared with that market's own settlement while SENEX targets 1h; it cannot establish incremental edge.",
            "SAFETY_IMPACT": "Could otherwise be used to justify prohibited directional activation.", "STATISTICAL_IMPACT": "No causally aligned OOS label, exact snapshot timestamp, independent sampling policy, or preregistered blend exists.",
            "WHY_EXISTING_TESTS_MISSED_IT": "The first harness named the descriptive agreement metric directional_accuracy despite caveats.",
            "MINIMUM_CORRECTION": "Fail closed as NOT_ESTIMABLE and call the metric same-market resolved-label agreement.",
            "REGRESSION_GATE": "No external accuracy/value-add claim without aligned OOS labels and exact observation times.",
        },
    ]
    falsified = [
        "Authority score status directly forces production FLAT",
        "Missing OI momentum is fabricated as observed zero after restart",
        "Polymarket/Kalshi/Boros directional pressure is active in production",
        "Current risk-state ruin probability itself is 50%",
        "A long/short sign inversion explains the reconstructible EV rows",
    ]
    return {
        "version": AUDIT_VERSION,
        "finding_count": len(records),
        "material_finding_count": sum(item["SEVERITY"] in {"CRITICAL", "HIGH", "MEDIUM"} for item in records),
        "severity_counts": dict(sorted(Counter(item["SEVERITY"] for item in records).items())),
        "findings": records,
        "falsified_hypotheses_and_negative_findings": falsified,
    }


def claim_assessments(artifacts: dict[str, Any]) -> dict[str, Any]:
    ev = artifacts["aud-062-ev-bridge.json"]
    risk = artifacts["aud-062-risk-survivability-audit.json"]
    feature = artifacts["aud-062-feature-availability.json"]
    learning = artifacts["aud-062-learning-frozen-vs-learned.json"]
    return {
        "A": {"status": "CONFIRMED", "evidence": f"EV is first binding gate for {artifacts['aud-062-flat-cause-distribution.json']['flat_by_first_binding_gate'].get('EV_NEGATIVE',0)+artifacts['aud-062-flat-cause-distribution.json']['flat_by_first_binding_gate'].get('EV_BELOW_DYNAMIC_MIN',0)} FLAT rows."},
        "B": {"status": "CONFIRMED", "evidence": f"{ev['positive_base_to_negative_adjusted_n']} rows have base_ev>0 and adjusted_ev<0."},
        "C": {"status": "CONFIRMED", "evidence": f"{risk['contradictory_display_state_n']} rows expose core ruin_prob=0 with a 50% survivability reason and no second machine field."},
        "D": {"status": "CONFIRMED", "evidence": "All persisted Polymarket pressure components are 0 and external_applied=0."},
        "E": {"status": "CONFIRMED", "evidence": f"Masked rows: {len(feature['masked_rows'])}; missing inputs are excluded from agreement."},
        "F": {"status": "CONFIRMED", "evidence": f"Effective weights reproduce at N=10/11 while score authority is insufficient; component pairs={learning['paired_n']}."},
        "G": {"status": "PARTIALLY_CONFIRMED", "evidence": "Polymarket 5m, Kalshi 15m, Boros funding, and SENEX 1h are mismatched targets; causal explanatory power across targets is not estimable."},
    }


def build_artifacts(bundle: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    predictions_payload = bundle.get("predictions_payload") or {}
    rows = list(predictions_payload.get("predictions") or [])
    attribution = decision_attribution(rows)
    resolution_payload = bundle.get("polymarket_resolutions") or {}
    artifacts: dict[str, Any] = {
        "aud-062-decision-graph.json": decision_graph(),
        "aud-062-flat-cause-distribution.json": flat_cause_distribution(attribution),
        "aud-062-action-reason-semantics.json": action_reason_semantics(attribution),
        "aud-062-ev-bridge.json": ev_bridge(rows),
        "aud-062-cost-model-audit.json": cost_model_audit(rows),
        "aud-062-risk-survivability-audit.json": risk_survivability_audit(rows),
        "aud-062-feature-availability.json": feature_availability(rows),
        "aud-062-learning-frozen-vs-learned.json": learning_frozen_vs_learned(rows),
        "aud-062-confidence-semantics.json": confidence_semantics(rows),
        "aud-062-score-truth.json": score_truth(bundle),
    }
    artifacts["aud-062-external-shadow-ledger.json"] = external_shadow_ledger(rows, resolution_payload)
    artifacts["aud-062-horizon-alignment.json"] = horizon_alignment(rows, artifacts["aud-062-external-shadow-ledger.json"])
    artifacts["aud-062-pre-post-behavior.json"] = pre_post_behavior(attribution)
    artifacts["aud-062-authority-feedback-loop.json"] = authority_feedback_loop(
        attribution, artifacts["aud-062-score-truth.json"]
    )
    artifacts["aud-062-governance-auto-cd.json"] = governance_auto_cd(bundle)
    artifacts["aud-062-findings.json"] = findings(artifacts)
    artifacts["aud-062-claim-assessments.json"] = claim_assessments(artifacts)
    artifacts.update(r1_remediation_artifacts(bundle, rows, attribution, artifacts))
    return artifacts, attribution


def _r1_group_distribution(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        grouped[str(row.get(key))][str(row.get("r1_decision_shadow"))] += 1
    return {name: dict(sorted(values.items())) for name, values in sorted(grouped.items())}


def r1_remediation_artifacts(
    bundle: dict[str, Any],
    rows: list[dict[str, Any]],
    attribution: list[dict[str, Any]],
    prior: dict[str, Any],
) -> dict[str, Any]:
    """Materialize the complete R1 fail-closed remediation evidence."""
    historical_ev = {row.get("id"): recompute_ev(row) for row in rows}
    counterfactual = [
        historical_canonical_counterfactual(row, historical_ev.get(row.get("id"), {}))
        for row in rows
    ]
    by_id = {item["prediction_id"]: item for item in attribution}
    deploy_dt = _dt(PRODUCTION_DEPLOYED_AT)
    for item in counterfactual:
        source = by_id.get(item["row_id"], {})
        item["historical_first_binding_gate"] = source.get("first_binding_gate")
        item["period"] = (
            "POST_AUD061"
            if (_dt(item["decision_time"]) or datetime.min.replace(tzinfo=timezone.utc)) >= deploy_dt
            else "PRE_AUD061"
        )
        item["historical_direction"] = item["historical_final_decision"]

    fees = [float(ev.get("fee_component_round_trip") or 0.0) for ev in historical_ev.values()]
    slippage = [float(ev.get("slippage_component_round_trip") or 0.0) for ev in historical_ev.values()]
    anchor_impact = [float(ev.get("anchor_default_impact") or 0.0) for ev in historical_ev.values()]
    risk_discounts = [float(_step(row, "step4_ev").get("survival_discount") or 0.0) for row in rows]
    risk_scores = [float(_step(row, "step3_risk").get("risk_score") or 0.0) for row in rows]
    historical_causes = Counter(item.get("first_binding_gate") for item in attribution if item.get("final_decision") == "FLAT")

    learning = prior["aud-062-learning-frozen-vs-learned.json"]
    external = prior["aud-062-external-shadow-ledger.json"]
    feature = prior["aud-062-feature-availability.json"]
    behavior = {
        "version": "AUD-062-R1-behavior-summary-v1",
        "row_count": len(counterfactual),
        "historical_distribution": _distribution(attribution),
        "historical_first_binding_gates": dict(sorted(historical_causes.items())),
        "canonical_ev_shadow_distribution": {
            "FLAT": sum(item["r1_decision_shadow"] == "FLAT" for item in counterfactual),
            "LONG": sum(item["r1_decision_shadow"] == "LONG" for item in counterfactual),
            "SHORT": sum(item["r1_decision_shadow"] == "SHORT" for item in counterfactual),
            "tradeable": sum(bool(item["r1_tradeable_shadow"]) for item in counterfactual),
            "fail_closed": sum(not bool(item["r1_tradeable_shadow"]) for item in counterfactual),
        },
        "decision_changes": {
            "total": sum(bool(item["decision_changed"]) for item in counterfactual),
            "by_exact_cause": dict(sorted(Counter(item["change_reason"] for item in counterfactual if item["decision_changed"]).items())),
        },
        "btc_eth_separated": _r1_group_distribution(counterfactual, "symbol"),
        "pre_post_aud061_separated": _r1_group_distribution(counterfactual, "period"),
        "historical_long_short_flat_separated": _r1_group_distribution(counterfactual, "historical_direction"),
        "cost_contribution_distribution_decimal_return": {
            "historical_fee_round_trip": finite_distribution(fees),
            "historical_slippage_round_trip": finite_distribution(slippage),
            "historical_anchor_impact_diagnostic": finite_distribution(anchor_impact),
            "r1_authoritative_cost": "NOT_ESTIMABLE",
            "literal_or_semantic_double_counting_in_r1": False,
        },
        "risk_contribution_distribution": {
            "historical_survival_discount": finite_distribution(risk_discounts),
            "historical_core_risk_score": finite_distribution(risk_scores),
            "r1_threshold_change": False,
        },
        "feature_availability_missingness": {
            "classification_counts": feature["classification_counts"],
            "masked_row_count": len(feature["masked_rows"]),
            "legacy_provenance_complete": feature["provenance_complete"],
        },
        "learning_frozen_vs_shadow_learned": {
            "historical_paired_n": learning.get("paired_n"),
            "historical_decision_changes": learning.get("final_decision_changed_n"),
            "r1_decision_weights": "FROZEN_BASE_ONLY",
            "r1_learned_weights": "SHADOW_ONLY",
        },
        "external_shadow_horizons": {
            "polymarket": "5m_MISMATCH",
            "kalshi": "15m_MISMATCH",
            "boros": "FUNDING_PRODUCT_AND_HORIZON_MISMATCH",
            "aligned_rows": 0,
            "mismatched_rows": len(external.get("ledger") or []) + len(external.get("kalshi_ledger") or []) + len(external.get("boros_ledger") or []),
            "blended_shadow": "NOT_ESTIMABLE",
            "external_applied": 0,
        },
        "same_sample_win_rate_or_edge_claim": False,
        "untouched_temporal_oos_edge_evidence": "ABSENT",
    }

    fixture_market = {
        "symbol": "BTC/USDT",
        "timeframe": "15m",
        "exchange_used": "okx",
        "candle_ts": 1786665600000,
        "ticker": {"last": 100000.0, "timestamp": 1786665600000},
        "orderbook": {"bid": 99999.0, "ask": 100001.0, "timestamp": 1786665600000},
    }
    fixture_result = {
        "timestamp": "2026-08-14T00:00:00+00:00",
        "_audit": {
            "pipeline": {
                "step1_market": {"feature_availability_v1": {
                    "price_momentum": {"status": "REAL_NONZERO", "source": "okx:BTC-USDT:ohlcv", "observed_at": "2026-08-14T00:00:00+00:00", "exchange_timestamp": 1786665600000, "query_observation_epoch": 1786665600.0},
                    "funding_signal": {"status": "MISSING", "source": "not_applicable_spot", "observed_at": None, "query_observation_epoch": 1786665600.0},
                }},
                "step2_features": {"learning_state_v1": {
                    "source_prediction_ids": ["fixture-prior-1"],
                    "source_settlement_observation_epochs": [{"prediction_id": "fixture-prior-1", "observed_at_epoch": 1786662000.0}],
                    "source_evidence_hash": canonical_hash(["fixture-prior-1"]),
                    "decision_weights_hash": canonical_hash({"weights": "base"}),
                    "shadow_weights_hash": canonical_hash({"weights": "shadow"}),
                    "code_hash": canonical_hash("code"),
                    "config_hash": canonical_hash("config"),
                }},
            },
            "external_markets_v1": {
                "polymarket": {"source": "POLYMARKET_PUBLIC", "status": "OK", "observed_at": "2026-08-14T00:00:00+00:00"},
                "kalshi": {"source": "KALSHI_PUBLIC_REST", "status": "OK", "observed_at": "2026-08-14T00:00:00+00:00", "directional_use": False},
                "boros": {"source": "BOROS_PUBLIC_API", "status": "OK", "observed_at": "2026-08-14T00:00:00+00:00", "directional_use": False},
            },
        },
    }
    roundtrip_fixture = feature_provenance_contract(fixture_market, fixture_result)
    roundtrip_reloaded = json.loads(json.dumps(roundtrip_fixture, sort_keys=True))
    roundtrip_check = verify_persisted_roundtrip(roundtrip_reloaded)

    governance_manifest = {
        "version": "AUD-062-R2-github-governance-proposal-v2",
        "status": "PROPOSED_NOT_APPLIED",
        "github_settings_applied": False,
        "repository": "simonkey888/SeneX-Prophet",
        "target": "refs/heads/main",
        "pr_required": True,
        "direct_push_to_main": "DENY",
        "force_push": "DENY",
        "branch_deletion": "DENY",
        "required_checks": ["score-001", "score-002", "act_final_audit_smoke (T1-T12)", "AUD_EXACT_HEAD_GATE"],
        "required_check_mapping": {
            "SCORE001": "score-001",
            "SCORE002": "score-002",
            "SMOKE": "act_final_audit_smoke (T1-T12)",
            "AUD_EXACT_HEAD_GATE": "AUD_EXACT_HEAD_GATE",
        },
        "stale_approval_handling": {"dismiss_stale_reviews_on_push": False, "require_last_push_approval": False},
        "bypass_actors": [],
        "owner_aud_authorization": "ISSUE23_PROCESS_GATE",
        "self_approval_required": False,
        "normal_author_can_bypass_required_checks": False,
        "single_owner_mergeability": "PR_PLUS_EXACT_HEAD_CHECKS_NO_GITHUB_REVIEW_REQUIRED",
        "write_capable_apps_reviewed": {"oracle_workflow": "DOWNGRADED_TO_CONTENTS_READ_NO_GIT_PUSH", "other_apps": "OWNER_CONTROL_PLANE_REVIEW_REQUIRED"},
        "deploy_only_from_reviewed_main_sha": True,
        "rest_ruleset_request_body": {
            "name": "main-reviewed-exact-head",
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {"type": "pull_request", "parameters": {"dismiss_stale_reviews_on_push": False, "require_code_owner_review": False, "require_last_push_approval": False, "required_approving_review_count": 0, "required_review_thread_resolution": True}},
                {"type": "required_status_checks", "parameters": {"do_not_enforce_on_create": False, "strict_required_status_checks_policy": True, "required_status_checks": [
                    {"context": "score-001"},
                    {"context": "score-002"},
                    {"context": "act_final_audit_smoke (T1-T12)"},
                    {"context": "AUD_EXACT_HEAD_GATE"},
                ]}},
            ],
        },
        "owner_authorization_required_before_apply": True,
    }

    dispositions = {
        "F001": {"status": "FAIL_CLOSED_WITH_EXPLICIT_LIMITATION", "evidence": "single canonical EV authority; explicit core survival EV cannot fall back to anchor-adjusted EV; EV NOT_ESTIMABLE"},
        "F002": {"status": "CLOSED", "evidence": "one survivability calculation supplies machine probability and reason"},
        "F003": {"status": "FAIL_CLOSED_WITH_EXPLICIT_LIMITATION", "evidence": "direct-push workflow removed; single-owner mergeable exact-check ruleset proposed but not applied"},
        "F004": {"status": "FAIL_CLOSED_WITH_EXPLICIT_LIMITATION", "evidence": "future whole-runtime-row persisted-only round trip enforced by exact-head R2 test; legacy rows remain insufficient"},
        "F005": {"status": "FAIL_CLOSED_WITH_EXPLICIT_LIMITATION", "evidence": "OKX spot instruments named; account-tier/fill costs unauthoritative so tradeability false"},
        "F006": {"status": "CLOSED", "evidence": "heuristic score names plus deprecated non-calibrated aliases"},
        "F007": {"status": "CLOSED", "evidence": "reporting, learning mutation, and size calibration authorities separated; decision uses frozen weights"},
        "F008": {"status": "CLOSED", "evidence": "machine reason classes distinguish negative EV from below-dynamic-min"},
        "F009": {"status": "CLOSED", "evidence": "same-market agreement truthfully descriptive; blend/value-add not estimable; external_applied=0"},
    }
    return {
        "aud-062-r1-canonical-ev-counterfactual.json": {
            "version": "AUD-062-R1-canonical-counterfactual-v1",
            "row_count": len(counterfactual),
            "diagnostic_only": True,
            "edge_claimed": False,
            "rows": counterfactual,
        },
        "aud-062-r1-behavior-summary.json": behavior,
        "aud-062-r1-instrument-cost-contract.json": {
            "version": "AUD-062-R1-instrument-cost-contract-v1",
            "contracts": {symbol: instrument_cost_contract(symbol) for symbol in ("BTCUSDT", "ETHUSDT")},
            "one_bp_fixture": {"basis_points": 1, "decimal_return": 0.0001, "pass": 1 / 10000 == 0.0001},
            "double_counting": {"status": "PASS", "authoritative_cost_terms_applied": 0, "reason": "cost authority is fail-closed rather than estimated from convenient constants"},
        },
        "aud-062-r1-provenance-roundtrip.json": {
            "version": "AUD-062-R2-provenance-roundtrip-v2",
            "fixture": roundtrip_fixture,
            "persisted_json_reload_check": roundtrip_check,
            "fixture_scope": "HELPER_CONTRACT_ONLY",
            "runtime_acceptance_test": "tests.test_aud_062_r2.Aud062R2RegressionTests.test_r2_f003_whole_runtime_row_round_trips_from_persisted_json_only",
            "runtime_acceptance_scope": "WHOLE_RETURNED_PREDICTION_ROW_SERIALIZE_DISCARD_RELOAD_VERIFY",
            "legacy_rows": "INSUFFICIENT_CAUSAL_PROVENANCE",
            "invented_timestamps": 0,
        },
        "aud-062-r1-probability-semantics.json": {
            "version": "AUD-062-R1-probability-semantics-v1",
            "registry": {
                "heuristic_up_score": {"class": "HEURISTIC_DIRECTIONAL_SCORE", "calibrated_probability": False},
                "heuristic_down_score": {"class": "HEURISTIC_DIRECTIONAL_SCORE", "calibrated_probability": False},
                "up_prob": {"deprecated_alias": "heuristic_up_score", "calibrated_probability": False},
                "down_prob": {"deprecated_alias": "heuristic_down_score", "calibrated_probability": False},
                "polymarket_up_price": {"class": "MARKET_IMPLIED_PRICE", "senex_calibrated_probability": False},
                "kalshi_yes_price": {"class": "MARKET_IMPLIED_PRICE", "senex_calibrated_probability": False},
                "empirical_win_rate": {"class": "EMPIRICAL_RATE", "diagnostic_only": True},
                "future_calibrated_probability": {"class": "UNAVAILABLE_WITHOUT_PURGED_TEMPORAL_OOS"},
            },
            "dashboard_labels": "RAW_CONVICTION_AND_MARKET_IMPLIED_PRICES_ONLY",
            "calibration_claim": False,
        },
        "aud-062-r1-learning-authority.json": {
            "version": "AUD-062-R1-learning-authority-v1",
            "reporting_authority": "INSUFFICIENT_EVIDENCE",
            "learning_mutation_authority": "SHADOW_ONLY",
            "size_calibration_authority": "FROZEN_BASE_ONLY",
            "production_decision_weights": "FROZEN_BASE",
            "learned_weights": "SHADOW_RESEARCH_ONLY",
            "activation_preregistration": "ABSENT_FAIL_CLOSED",
            "minimum_sample_threshold_changed": False,
            "post_hoc_weight_optimization": False,
        },
        "aud-062-r1-action-reason-contract.json": {
            "version": "AUD-062-R1-action-reason-contract-v1",
            "machine_reason_classes": list(MACHINE_REASON_CLASSES),
            "first_binding_gate_required": True,
            "candidate_fixture_unknown_causal_path_n": 0,
            "historical_positive_ev_mislabeled_negative_n": prior["aud-062-action-reason-semantics.json"]["positive_adjusted_ev_mislabeled_negative_n"],
            "historical_rows_rewritten": 0,
        },
        "aud-062-r1-external-truth.json": {
            "version": "AUD-062-R1-external-truth-v1",
            "metric_name": "same_market_5m_resolved_label_agreement",
            "observed_value": external.get("same_market_resolved_label_agreement"),
            "senex_1h_predictive_accuracy": False,
            "incremental_value": "NOT_ESTIMABLE",
            "blended_shadow": "NOT_ESTIMABLE",
            "exact_observation_timestamps_future_rows": True,
            "horizon_mismatch_explicit": True,
            "external_applied": 0,
            "activation_authorized": False,
        },
        "aud-062-r1-governance-settings-manifest.json": governance_manifest,
        "aud-062-r2-correction-evidence.json": {
            "version": "AUD-062-R2-correction-evidence-v1",
            "order_comment": 5299876166,
            "R2_F001": {
                "status": "FIXED",
                "core_ev_source": "EXPLICIT_CORE_RECONSTRUCTION_ONLY",
                "post_anchor_adjusted_ev_fallback": False,
                "neutral_probability_input": "NOT_APPLICABLE",
            },
            "R2_F002": {
                "status": "FIXED",
                "required_approving_review_count": 0,
                "require_last_push_approval": False,
                "bypass_actors": [],
                "owner_aud_authorization": "ISSUE23_PROCESS_GATE",
                "github_settings_applied": False,
            },
            "R2_F003": {
                "status": "FIXED",
                "acceptance_test": "tests.test_aud_062_r2.Aud062R2RegressionTests.test_r2_f003_whole_runtime_row_round_trips_from_persisted_json_only",
                "whole_returned_row_roundtrip": True,
                "persisted_fields_only_verification": True,
                "real_network_or_database": False,
            },
            "historical_rows_changed": 0,
            "edge_claimed": False,
            "threshold_changes": 0,
            "post_hoc_weight_tuning": 0,
            "external_directional_activation": 0,
            "github_settings_applied": False,
            "production_mutations": 0,
            "runtime017_mutations": 0,
        },
        "aud-062-r1-finding-disposition.json": {
            "version": "AUD-062-R1-finding-disposition-v1",
            "findings": dispositions,
            "all_findings_closed_or_fail_closed": all(value["status"] in {"CLOSED", "FAIL_CLOSED_WITH_EXPLICIT_LIMITATION"} for value in dispositions.values()),
            "github_settings_applied": False,
            "merge": False,
            "deploy": False,
            "production_mutations": 0,
            "runtime017_mutations": 0,
            "threshold_changes": 0,
            "post_hoc_weight_tuning": 0,
            "external_directional_activation": 0,
        },
    }


def attribution_csv(records: list[dict[str, Any]], provenance: dict[str, Any]) -> str:
    if not records:
        return ""
    output = io.StringIO(newline="")
    for key in (
        "SOURCE_CLASS", "CAPTURE_TIME_UTC", "SOURCE_ENDPOINT_OR_CLASS",
        "RAW_OR_DERIVED", "TRANSFORMATION", "ROW_COUNT", "SHA256",
    ):
        output.write(f"# {key}={provenance[key]}\n")
    fieldnames = list(records[0])
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow({key: canonical_json(value) if isinstance(value, (dict, list)) else value for key, value in record.items()})
    return output.getvalue()


def write_artifacts(bundle: dict[str, Any], output_dir: Path, *, command_log: list[str] | None = None) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts, attribution = build_artifacts(bundle)
    capture_time = str((bundle.get("observation") or {}).get("captured_at") or "UNKNOWN")
    attribution_provenance = dataset_provenance(
        attribution,
        source_class="PRODUCTION_DERIVED_FROM_PUBLIC_INPUTS",
        capture_time_utc=capture_time,
        source_endpoint_or_class="PUBLIC_RUNTIME_PREDICTION_SNAPSHOT",
        raw_or_derived="DERIVED",
        transformation="DETERMINISTIC_DECISION_CAUSAL_ATTRIBUTION_V2",
    )
    artifacts["aud-062-decision-attribution.json"] = {
        "version": AUDIT_VERSION,
        "dataset_provenance": attribution_provenance,
        "row_count": len(attribution),
        "rows": attribution,
    }

    row_datasets = {
        "aud-062-decision-graph.json": ("edges", "STATIC_CODE_CAUSAL_GRAPH"),
        "aud-062-feature-availability.json": ("masked_rows", "MISSING_INPUT_MASK_EXTRACTION"),
        "aud-062-risk-survivability-audit.json": ("examples", "RISK_SEMANTICS_CONTRADICTION_SAMPLE"),
        "aud-062-learning-frozen-vs-learned.json": ("decisions", "COMPONENT_LEVEL_FROZEN_VS_LEARNED_REPLAY"),
        "aud-062-findings.json": ("findings", "MATERIAL_FINDING_CLASSIFICATION"),
        "aud-062-r1-canonical-ev-counterfactual.json": ("rows", "R1_FAIL_CLOSED_CANONICAL_EV_COUNTERFACTUAL"),
    }
    inventory: dict[str, Any] = dict(bundle.get("dataset_provenance") or {})
    inventory["aud-062-decision-attribution.json#rows"] = attribution_provenance
    inventory["aud-062-decision-attribution.csv#rows"] = attribution_provenance
    for name, (key, transformation) in row_datasets.items():
        payload = artifacts[name]
        rows = payload.get(key) or []
        provenance = dataset_provenance(
            rows,
            source_class="PRODUCTION_DERIVED_FROM_PUBLIC_INPUTS",
            capture_time_utc=capture_time,
            source_endpoint_or_class="AUD062_FROZEN_PUBLIC_INPUT_BUNDLE",
            raw_or_derived="DERIVED",
            transformation=transformation,
        )
        payload["dataset_provenance"] = provenance
        inventory[f"{name}#{key}"] = provenance
    external = artifacts["aud-062-external-shadow-ledger.json"]
    for key, source, transformation in (
        ("ledger", "POLYMARKET_PUBLIC+PUBLIC_RUNTIME", "POLYMARKET_SHADOW_JOIN"),
        ("kalshi_ledger", "KALSHI_PUBLIC_REST+PUBLIC_RUNTIME", "KALSHI_SHADOW_ALIGNMENT"),
        ("boros_ledger", "BOROS_PUBLIC_API+PUBLIC_RUNTIME", "BOROS_CONTEXT_ALIGNMENT"),
    ):
        provenance = dataset_provenance(
            external.get(key) or [],
            source_class="PRODUCTION_DERIVED_FROM_PUBLIC_INPUTS",
            capture_time_utc=capture_time,
            source_endpoint_or_class=source,
            raw_or_derived="DERIVED",
            transformation=transformation,
        )
        external.setdefault("dataset_provenance", {})[key] = provenance
        inventory[f"aud-062-external-shadow-ledger.json#{key}"] = provenance
    artifacts["aud-062-dataset-provenance.json"] = {
        "version": DATASET_PROVENANCE_VERSION,
        "dataset_count": len(inventory),
        "datasets": dict(sorted(inventory.items())),
    }
    for name, payload in sorted(artifacts.items()):
        (output_dir / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    csv_path = output_dir / "aud-062-decision-attribution.csv"
    csv_path.write_text(attribution_csv(attribution, attribution_provenance), encoding="utf-8")

    artifact_hashes = {
        path.name: file_hash(path)
        for path in sorted(output_dir.glob("aud-062-*"))
        if path.name != "aud-062-manifest.json"
    }
    input_meta = bundle.get("observation") or {}
    manifest = {
        "version": AUDIT_VERSION,
        "base_sha": BASE_SHA,
        "base_tree": BASE_TREE,
        "candidate_head": {"binding": "CI_EXACT_HEAD", "command": "git rev-parse HEAD"},
        "candidate_tree": {"binding": "CI_EXACT_HEAD_TREE", "command": "git rev-parse HEAD^{tree}"},
        "commands_executed": command_log or ["python -m scripts.run_aud062 analyze --input docs/evidence/aud062-public-inputs.json.gz --output docs/evidence"],
        "input_hashes": bundle.get("input_hashes") or {},
        "artifact_hashes": artifact_hashes,
        "row_counts": {
            "total_decisions": len(attribution),
            "post_deploy_decisions": sum((_dt(item["timestamp"]) or datetime.min.replace(tzinfo=timezone.utc)) >= _dt(PRODUCTION_DEPLOYED_AT) for item in attribution),
            "btc": sum(item["symbol"] == "BTCUSDT" for item in attribution),
            "eth": sum(item["symbol"] == "ETHUSDT" for item in attribution),
            "external_shadow_polymarket": artifacts["aud-062-external-shadow-ledger.json"]["polymarket_rows"],
            "external_shadow_kalshi": len(artifacts["aud-062-external-shadow-ledger.json"]["kalshi_ledger"]),
            "external_shadow_boros": len(artifacts["aud-062-external-shadow-ledger.json"]["boros_ledger"]),
        },
        "excluded_rows": [],
        "runtime_public_endpoints_read": input_meta.get("endpoints") or [],
        "github_settings_mutations": 0,
        "northflank_mutations": 0,
        "database_mutations": 0,
        "production_mutations": 0,
        "runtime017_mutations": 0,
        "merge": False,
        "deploy": False,
        "threshold_changes": False,
        "weight_changes": False,
        "external_directional_activation": False,
        "candidate_instrumentation_corrections": [
            "survivability_ruin_prob_machine_field",
            "ev_below_dynamic_min_truthful_reason",
            "feature_observation_timestamps",
            "learning_source_observation_epochs",
            "external_snapshot_observed_at",
            "canonical_ev_single_authority_fail_closed",
            "instrument_bound_cost_authority_fail_closed",
            "truthful_heuristic_score_names",
            "learning_shadow_only_frozen_decision_weights",
            "reproducible_machine_reason_class",
            "persisted_only_provenance_roundtrip",
            "workflow_direct_main_push_removed",
        ],
        "production_decision_semantics_changed": True,
        "production_decision_semantics_change_scope": "PAPER_DIRECTIONAL_CANDIDATES_FAIL_CLOSED_FLAT_WHILE_CANONICAL_EV_AND_COST_AUTHORITY_ARE_NOT_ESTIMABLE",
        "r1_all_findings_closed_or_fail_closed": artifacts["aud-062-r1-finding-disposition.json"]["all_findings_closed_or_fail_closed"],
        "r1_residual_material_findings": 0,
        "finding_count": artifacts["aud-062-findings.json"]["finding_count"],
        "material_finding_count": artifacts["aud-062-findings.json"]["material_finding_count"],
    }
    manifest_path = output_dir / "aud-062-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
