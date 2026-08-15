"""AUD-062-R1 truth contracts shared by runtime and deterministic evidence.

The R1 candidate does not claim an edge.  It makes the current uncertainty
executable: the historical heuristic score and cost constants remain available
for diagnostics, while PAPER decisions fail closed until one venue-bound cost
model and a causally validated probability model are authoritative.
"""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from typing import Any


CONTRACT_VERSION = "aud-062-r1-canonical-paper-ev-v1"
CANONICAL_HORIZON = "1h"
PAPER_INSTRUMENTS = {
    "BTCUSDT": {
        "instrument_contract": "OKX:BTC-USDT:SPOT:PAPER:1H",
        "instrument_id": "BTC-USDT",
        "instrument_type": "SPOT",
        "base_currency": "BTC",
        "quote_currency": "USDT",
    },
    "ETHUSDT": {
        "instrument_contract": "OKX:ETH-USDT:SPOT:PAPER:1H",
        "instrument_id": "ETH-USDT",
        "instrument_type": "SPOT",
        "base_currency": "ETH",
        "quote_currency": "USDT",
    },
}

OKX_API_DOC = "https://www.okx.com/docs-v5/en/"
OKX_FEE_RULES = "https://www.okx.com/help/trading-fee-rules-faq"

MACHINE_REASON_CLASSES = (
    "EV_NEGATIVE",
    "EV_BELOW_DYNAMIC_MIN",
    "RISK_REJECT",
    "REGIME_REJECT",
    "LOW_CONVICTION_OR_SIZE",
    "NO_DIRECTIONAL_SIGNAL",
    "OTHER_EXPLICIT",
    "UNKNOWN_CAUSAL_PATH",
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_symbol(value: Any) -> str:
    return str(value or "").upper().replace("/", "").replace("-", "").strip()


def instrument_cost_contract(symbol: Any) -> dict[str, Any]:
    """Return the single reviewable PAPER instrument/cost authority contract.

    Public market data is enough to identify the intended spot instrument, but
    not enough to establish the account fee tier, executable order type, or a
    replayable fill/impact curve.  Consequently the only truthful current cost
    authority is fail-closed.
    """
    normalized = normalize_symbol(symbol)
    instrument = deepcopy(PAPER_INSTRUMENTS.get(normalized) or {})
    return {
        "version": CONTRACT_VERSION,
        "symbol": normalized,
        **instrument,
        "decision_horizon": CANONICAL_HORIZON,
        "execution_mode": "PAPER_ONLY",
        "maker_taker_interpretation": "UNRESOLVED_ORDER_TYPE",
        "fee": {
            "status": "NOT_AUTHORITATIVE_ACCOUNT_TIER_AND_ORDER_TYPE_UNKNOWN",
            "unit": "decimal_return_per_side",
            "round_trip_convention": "entry_plus_exit",
            "primary_source": OKX_FEE_RULES,
            "source_version_observed_at": "2026-08-14",
            "historical_literal_0_0002_per_side_authoritative": False,
        },
        "spread": {
            "status": "REQUIRES_DECISION_TIME_INSTRUMENT_ORDER_BOOK",
            "unit": "decimal_return_one_way",
            "source": "OKX_PUBLIC_ORDER_BOOK_FOR_EXACT_INSTRUMENT",
            "primary_source": OKX_API_DOC,
        },
        "slippage": {
            "status": "NOT_AUTHORITATIVE_NO_PREREGISTERED_FILL_MODEL",
            "unit": "decimal_return_per_side",
            "historical_runtime_source": "max(1bp, observed_spread_bps/2)",
        },
        "order_book_depth": {
            "status": "REQUIRES_TIMESTAMPED_LEVELS_AND_PAPER_SIZE",
            "unit": "base_and_quote_quantity_by_price_level",
            "primary_source": OKX_API_DOC,
        },
        "market_impact": {
            "status": "NOT_AUTHORITATIVE_DEFAULT_DEPTH_500000_IS_DIAGNOSTIC_ONLY",
            "unit": "decimal_return_per_side",
        },
        "unit_conversions": {
            "one_basis_point_decimal_return": 0.0001,
            "one_percent_decimal_return": 0.01,
            "bps_to_decimal_formula": "bps / 10000",
        },
        "cost_model_authority": "COST_MODEL_NOT_AUTHORITATIVE",
        "tradeable": False,
        "orders_enabled": False,
        "live_capital_locked": True,
    }


def canonical_ev_contract(
    symbol: Any,
    features: dict[str, Any],
    risk_filter: dict[str, Any],
    historical_ev: dict[str, Any],
) -> dict[str, Any]:
    """Serialize one EV authority and demote both historical EVs to diagnostics."""
    cost = instrument_cost_contract(symbol)
    direction = str(features.get("direction") or "").upper()
    if direction == "LONG":
        heuristic_score = features.get("heuristic_up_score", features.get("up_prob"))
        probability_semantic_class = "HEURISTIC_DIRECTIONAL_SCORE"
    elif direction == "SHORT":
        heuristic_score = features.get("heuristic_down_score", features.get("down_prob"))
        probability_semantic_class = "HEURISTIC_DIRECTIONAL_SCORE"
    else:
        heuristic_score = None
        probability_semantic_class = "NOT_APPLICABLE"
    core_survival_ev = historical_ev.get("core_survival_ev")
    core_reconstructible = isinstance(core_survival_ev, (int, float))
    return {
        "version": CONTRACT_VERSION,
        "symbol": normalize_symbol(symbol),
        "instrument_contract": cost.get("instrument_contract"),
        "horizon": CANONICAL_HORIZON,
        "probability_input": {
            "value": heuristic_score,
            "semantic_class": probability_semantic_class,
            "calibrated_probability": False,
            "eligible_as_p_win": False,
        },
        "historical_core_ev": {
            "status": "DIAGNOSTIC_ONLY" if core_reconstructible else "NOT_RECONSTRUCTIBLE",
            "provenance_status": "EXPLICIT_CORE_RECONSTRUCTION" if core_reconstructible else "INSUFFICIENT_PROVENANCE",
            "base_ev": historical_ev.get("base_ev"),
            "survival_adjusted_ev": core_survival_ev if core_reconstructible else None,
        },
        "historical_parallel_market_anchor": {
            "status": "DISABLED_AS_DECISION_AUTHORITY",
            "selection_operator": "NO_MIN_MAX_OR_FALLBACK_AUTHORITY",
            "serialized_value": historical_ev.get("market_anchor_ev"),
            "historical_final_adjusted_ev": historical_ev.get(
                "historical_adjusted_ev", historical_ev.get("adjusted_ev")
            ),
            "may_include_hidden_parallel_anchor": True,
        },
        "canonical_ev_before_cost": None,
        "canonical_cost_terms": cost,
        "canonical_ev_after_cost": None,
        "survivability": {
            "core_ruin_probability": risk_filter.get("ruin_prob"),
            "survivability_ruin_probability": risk_filter.get("survivability_ruin_prob"),
            "survival_discount": historical_ev.get("survival_discount"),
            "affects": "SIZE_AND_HISTORICAL_DIAGNOSTIC_EV",
        },
        "authority_status": "NOT_ESTIMABLE",
        "fail_closed_reason": "COST_MODEL_NOT_AUTHORITATIVE",
        "tradeable": False,
        "threshold_changed": False,
        "weight_tuned": False,
        "edge_claimed": False,
    }


def attach_truthful_score_semantics(features: dict[str, Any]) -> dict[str, Any]:
    """Expose truthful score names while retaining explicit deprecated aliases."""
    result = dict(features)
    up = result.get("heuristic_up_score", result.get("up_prob"))
    down = result.get("heuristic_down_score", result.get("down_prob"))
    result["heuristic_up_score"] = up
    result["heuristic_down_score"] = down
    result["score_semantics_v1"] = {
        "version": "senex-score-semantics-v1",
        "heuristic_up_score": {
            "class": "HEURISTIC_DIRECTIONAL_SCORE",
            "formula": "sigmoid(total_pressure*5)",
            "calibrated_probability": False,
        },
        "heuristic_down_score": {
            "class": "HEURISTIC_DIRECTIONAL_SCORE",
            "formula": "sigmoid(-total_pressure*5)",
            "calibrated_probability": False,
        },
        "deprecated_aliases": {
            "up_prob": {"alias_of": "heuristic_up_score", "deprecated": True, "calibrated_probability": False},
            "down_prob": {"alias_of": "heuristic_down_score", "deprecated": True, "calibrated_probability": False},
        },
    }
    return result


def classify_machine_reason(
    action: dict[str, Any],
    features: dict[str, Any],
    risk_filter: dict[str, Any],
    ev_result: dict[str, Any],
    feasibility: dict[str, Any],
) -> tuple[str, str]:
    """Return the first binding gate using the frozen pipeline's strict order."""
    reason = str(action.get("reason") or "").lower()
    if str(risk_filter.get("verdict") or "") == "KILL":
        return "RISK_REJECT", str(risk_filter.get("reason") or "risk_kill")
    if features.get("long_suppressed_by_regime"):
        return "REGIME_REJECT", "long_suppressed_by_regime"
    if str(features.get("direction") or "").upper() == "NEUTRAL":
        return "NO_DIRECTIONAL_SIGNAL", "no_direction"
    if "low_conviction" in reason or "size_too_small" in reason:
        return "LOW_CONVICTION_OR_SIZE", str(action.get("reason") or "")
    if "volatile_shield" in reason or "regime_guard" in reason:
        return "REGIME_REJECT", str(action.get("reason") or "")
    if "high_noise" in reason:
        return "OTHER_EXPLICIT", str(action.get("reason") or "high_noise")
    if not bool(ev_result.get("tradeable")):
        fail_closed = str(ev_result.get("fail_closed_reason") or "")
        if fail_closed:
            return "OTHER_EXPLICIT", fail_closed
        adjusted = float(ev_result.get("adjusted_ev") or 0.0)
        threshold = float(ev_result.get("dynamic_min_ev") or 0.0)
        if adjusted < 0.0:
            return "EV_NEGATIVE", f"adjusted_ev={adjusted:.8f}"
        return "EV_BELOW_DYNAMIC_MIN", f"adjusted_ev={adjusted:.8f}<=dynamic_min_ev={threshold:.8f}"
    if not bool(feasibility.get("feasible")):
        return "OTHER_EXPLICIT", str(feasibility.get("reason") or "execution_infeasible")
    if str(action.get("action") or "") == "EXECUTE":
        return "OTHER_EXPLICIT", "EXECUTE_PAPER_DIAGNOSTIC"
    if "cooldown" in reason or "signal_density" in reason:
        return "OTHER_EXPLICIT", str(action.get("reason") or "")
    return "UNKNOWN_CAUSAL_PATH", str(action.get("reason") or "")


def enrich_action_reason(
    action: dict[str, Any],
    features: dict[str, Any],
    risk_filter: dict[str, Any],
    ev_result: dict[str, Any],
    feasibility: dict[str, Any],
) -> dict[str, Any]:
    result = dict(action)
    reason_class, detail = classify_machine_reason(result, features, risk_filter, ev_result, feasibility)
    result["reason_code"] = reason_class
    result["reason_detail"] = detail
    result["first_binding_gate_v1"] = {
        "version": "first-binding-gate-v1",
        "reason_class": reason_class,
        "detail": detail,
        "reproducible": reason_class != "UNKNOWN_CAUSAL_PATH",
    }
    if reason_class == "OTHER_EXPLICIT" and detail == "COST_MODEL_NOT_AUTHORITATIVE":
        result["reason"] = "COST_MODEL_NOT_AUTHORITATIVE"
    elif reason_class == "EV_NEGATIVE":
        result["reason"] = f"ev_negative: {detail}"
    elif reason_class == "EV_BELOW_DYNAMIC_MIN":
        result["reason"] = f"ev_below_dynamic_min: {detail}"
    return result


def feature_provenance_contract(market: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Build the persisted-only round-trip contract for one future decision."""
    audit = result.get("_audit") if isinstance(result.get("_audit"), dict) else {}
    pipeline = audit.get("pipeline") if isinstance(audit.get("pipeline"), dict) else {}
    step1 = pipeline.get("step1_market") if isinstance(pipeline.get("step1_market"), dict) else {}
    step2 = pipeline.get("step2_features") if isinstance(pipeline.get("step2_features"), dict) else {}
    learning = step2.get("learning_state_v1") if isinstance(step2.get("learning_state_v1"), dict) else {}
    decision_time = result.get("timestamp")
    feature_rows = []
    availability = step1.get("feature_availability_v1") if isinstance(step1.get("feature_availability_v1"), dict) else {}
    for name in sorted(availability):
        item = availability.get(name) if isinstance(availability.get(name), dict) else {}
        feature_rows.append({
            "feature": name,
            "status": item.get("status"),
            "source_identity": item.get("source"),
            "observed_at": item.get("observed_at"),
            "exchange_timestamp": item.get("exchange_timestamp"),
            "query_observation_epoch": item.get("query_observation_epoch"),
        })
    external = audit.get("external_markets_v1") if isinstance(audit.get("external_markets_v1"), dict) else {}
    external_rows = []
    for source in ("polymarket", "kalshi", "boros"):
        item = external.get(source) if isinstance(external.get(source), dict) else {}
        external_rows.append({
            "source": source,
            "source_identity": item.get("source"),
            "observed_at": item.get("observed_at"),
            "status": item.get("status"),
            "directional_use": False,
        })
    market_identity_payload = {
        "symbol": market.get("symbol"),
        "timeframe": market.get("timeframe"),
        "exchange_used": market.get("exchange_used"),
        "candle_ts": market.get("candle_ts"),
        "ticker": market.get("ticker"),
        "orderbook": market.get("orderbook"),
    }
    contract = {
        "version": "decision-provenance-roundtrip-v2",
        "decision_time": decision_time,
        "feature_observations": feature_rows,
        "external_observations": external_rows,
        "learning_source_prediction_ids": list(learning.get("source_prediction_ids") or []),
        "learning_source_settlement_observation_epochs": deepcopy(learning.get("source_settlement_observation_epochs") or []),
        "learning_evidence_hash": learning.get("source_evidence_hash"),
        "decision_weights_hash": learning.get("decision_weights_hash", learning.get("effective_weights_hash")),
        "shadow_weights_hash": learning.get("shadow_weights_hash"),
        "code_hash": learning.get("code_hash"),
        "config_hash": learning.get("config_hash"),
        "market_snapshot_identity": canonical_hash(market_identity_payload),
        "legacy_timestamp_invention": False,
    }
    contract["source_evidence_hash"] = canonical_hash(contract)
    return contract


def verify_persisted_roundtrip(contract: dict[str, Any]) -> dict[str, Any]:
    payload = {key: deepcopy(value) for key, value in contract.items() if key != "source_evidence_hash"}
    recomputed = canonical_hash(payload)
    observed_statuses = {"REAL_OBSERVED_ZERO", "REAL_NONZERO"}
    feature_cutoff = all(
        item.get("status") not in observed_statuses
        or (
            bool(item.get("source_identity"))
            and item.get("observed_at") is not None
        )
        for item in payload.get("feature_observations") or []
    )
    external_cutoff = all(
        item.get("status") == "NOT_APPLICABLE"
        or (
            bool(item.get("source_identity"))
            and item.get("observed_at") is not None
        )
        for item in payload.get("external_observations") or []
    )
    learning_ids = list(payload.get("learning_source_prediction_ids") or [])
    learning_epochs = list(payload.get("learning_source_settlement_observation_epochs") or [])
    epoch_ids = [item.get("prediction_id") for item in learning_epochs if isinstance(item, dict)]
    learning_complete = (
        learning_ids == epoch_ids
        and all(item.get("observed_at_epoch") is not None for item in learning_epochs if isinstance(item, dict))
        and bool(payload.get("learning_evidence_hash"))
        and bool(payload.get("decision_weights_hash"))
    )
    return {
        "source_evidence_hash": recomputed,
        "hash_matches": recomputed == contract.get("source_evidence_hash"),
        "feature_observation_cutoff_classification": "COMPLETE" if feature_cutoff else "INSUFFICIENT_CAUSAL_PROVENANCE",
        "external_observation_cutoff_classification": "COMPLETE" if external_cutoff else "INSUFFICIENT_CAUSAL_PROVENANCE",
        "learning_provenance_classification": "COMPLETE" if learning_complete else "INSUFFICIENT_CAUSAL_PROVENANCE",
        "learning_hash_present": bool(payload.get("learning_evidence_hash")),
        "market_snapshot_identity_present": bool(payload.get("market_snapshot_identity")),
        "legacy_timestamp_invention": bool(payload.get("legacy_timestamp_invention")),
    }


def historical_canonical_counterfactual(row: dict[str, Any], historical_ev: dict[str, Any]) -> dict[str, Any]:
    """Create the required frozen diagnostic row without claiming an edge."""
    audit = row.get("audit") if isinstance(row.get("audit"), dict) else row.get("_audit") or {}
    pipeline = audit.get("pipeline") if isinstance(audit, dict) else {}
    step2 = pipeline.get("step2_features") if isinstance(pipeline, dict) else {}
    step3 = pipeline.get("step3_risk") if isinstance(pipeline, dict) else {}
    symbol = normalize_symbol(row.get("symbol"))
    contract = canonical_ev_contract(symbol, step2 or {}, step3 or {}, historical_ev)
    historical_decision = str(row.get("prediction") or "UNKNOWN").upper()
    shadow_decision = "FLAT"
    return {
        "row_id": row.get("id"),
        "symbol": symbol,
        "decision_time": row.get("ts", row.get("timestamp")),
        "historical_final_decision": historical_decision,
        "canonical_ev_before_cost": contract["canonical_ev_before_cost"],
        "canonical_cost_terms": contract["canonical_cost_terms"],
        "canonical_ev_after_cost": contract["canonical_ev_after_cost"],
        "historical_adjusted_ev": historical_ev.get("adjusted_ev_stored", historical_ev.get("adjusted_ev")),
        "historical_dynamic_min_ev": (pipeline.get("step4_ev") or {}).get("dynamic_min_ev") if isinstance(pipeline, dict) else None,
        "r1_tradeable_shadow": False,
        "r1_decision_shadow": shadow_decision,
        "decision_changed": historical_decision != shadow_decision,
        "change_reason": "COST_MODEL_NOT_AUTHORITATIVE" if historical_decision != "FLAT" else "HISTORICAL_FLAT_PRESERVED_FAIL_CLOSED",
        "edge_claimed": False,
        "same_sample_performance_claim": False,
    }


def finite_distribution(values: list[float]) -> dict[str, Any]:
    clean = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not clean:
        return {"n": 0, "min": None, "median": None, "mean": None, "max": None}
    middle = len(clean) // 2
    median = clean[middle] if len(clean) % 2 else (clean[middle - 1] + clean[middle]) / 2.0
    return {
        "n": len(clean),
        "min": round(clean[0], 10),
        "median": round(median, 10),
        "mean": round(sum(clean) / len(clean), 10),
        "max": round(clean[-1], 10),
    }
