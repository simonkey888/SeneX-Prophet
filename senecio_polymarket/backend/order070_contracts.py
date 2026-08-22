"""ORDER-070-R1 semantic contracts; audit-only, no model/threshold tuning."""
from __future__ import annotations

from typing import Any

BPS_TO_DECIMAL = 0.0001
PAPER_INSTRUMENT = {
    "contract": "SENEX_PAPER_SPOT_1H_V1",
    "instrument_identifier": "BTC/USDT",
    "market_class": "spot",
    "quote_currency": "USDT",
    "decision_horizon": "1h",
    "execution_authority": "PAPER_ONLY",
    "cost_model_status": "COST_MODEL_NOT_AUTHORITATIVE",
    "reason": "NO_VERSIONED_PRIMARY_SOURCE_FEE_SCHEDULE_IS_BOUND_TO_DECISION_TIME",
}


def bps_to_decimal(bps: float) -> float:
    return float(bps) * BPS_TO_DECIMAL


def canonical_ev_audit(
    *,
    model_score: float | None,
    fee_bps: float | None = None,
    spread_bps: float | None = None,
    slippage_bps: float | None = None,
    impact_bps: float | None = None,
    entropy_discount: float | None = None,
    risk_multiplier: float | None = None,
) -> dict[str, Any]:
    """Describe, but never activate, one non-overlapping paper EV decomposition."""
    semantics = {
        "model_score": "HEURISTIC_MODEL_SCORE_NOT_CALIBRATED_PROBABILITY",
        "fee": "EXPLICIT_EXECUTION_COST",
        "spread": "EXPLICIT_EXECUTION_COST",
        "slippage": "EXPLICIT_EXECUTION_COST_EXCLUDES_SPREAD",
        "impact": "EXPLICIT_EXECUTION_COST_EXCLUDES_SPREAD_AND_SLIPPAGE",
        "entropy": "UNCERTAINTY_DISCOUNT_NOT_EXECUTION_COST",
        "risk": "SIZE_OR_UTILITY_MULTIPLIER_NOT_EXECUTION_COST",
    }
    terms = {"fee_bps": fee_bps, "spread_bps": spread_bps, "slippage_bps": slippage_bps, "impact_bps": impact_bps}
    authoritative = PAPER_INSTRUMENT["cost_model_status"] == "AUTHORITATIVE"
    return {
        "version": "order070-canonical-ev-audit-v1",
        "instrument": dict(PAPER_INSTRUMENT),
        "semantic_classes": semantics,
        "canonical_cost_terms": terms,
        "canonical_ev_after_cost": None,
        "authority": "DIAGNOSTIC_ONLY" if not authoritative else "PAPER_DECISION",
        "status": PAPER_INSTRUMENT["cost_model_status"],
        "model_score": model_score,
        "entropy_discount": entropy_discount,
        "risk_multiplier": risk_multiplier,
        "double_counting": False,
        "unit_contract": "1_bp=0.0001_decimal_return",
    }
