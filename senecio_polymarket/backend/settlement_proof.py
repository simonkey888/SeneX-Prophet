"""Authoritative settlement-proof gate for SENEX scoring.

A raw WIN/LOSS is never proof-qualified. Qualification requires:
- a valid origin_price_v1 proof;
- dual 15m/1h settlement evidence;
- primary_window == 1h;
- primary outcome matching the 1h evidence; and
- both dual prices agreeing with the prediction direction and origin price.

This module is pure and side-effect free so it can be tested independently
from Supabase, ccxt, and the runtime daemon.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

VALID_OUTCOMES = {"WIN", "LOSS"}


def _as_audit(row: dict[str, Any]) -> dict[str, Any]:
    audit = row.get("audit") or {}
    if isinstance(audit, dict):
        return audit
    return {}


def _directional_outcome(direction: str, origin: float, later: float) -> str | None:
    direction = (direction or "").upper()
    if direction == "LONG":
        return "WIN" if later > origin else "LOSS"
    if direction == "SHORT":
        return "WIN" if later < origin else "LOSS"
    return None


def is_proof_qualified(row: dict[str, Any]) -> bool:
    """Return True only when the row satisfies the complete proof contract."""
    if row.get("outcome") not in VALID_OUTCOMES:
        return False

    audit = _as_audit(row)
    origin_proof = audit.get("origin_price_v1")
    dual = audit.get("outcomes_dual")
    if not isinstance(origin_proof, dict) or not isinstance(dual, dict):
        return False

    if origin_proof.get("version") != "origin-price-v1":
        return False
    if not origin_proof.get("source"):
        return False
    origin_ts = origin_proof.get("timestamp")
    row_ts = row.get("ts")
    if not origin_ts or not row_ts:
        return False
    try:
        if datetime.fromisoformat(str(origin_ts).replace("Z", "+00:00")) != datetime.fromisoformat(str(row_ts).replace("Z", "+00:00")):
            return False
    except (TypeError, ValueError):
        return False

    try:
        origin_price = float(origin_proof.get("price"))
        price_15m = float(dual.get("price_15m_later"))
        price_1h = float(dual.get("price_1h_later"))
    except (TypeError, ValueError):
        return False
    if origin_price <= 0 or price_15m <= 0 or price_1h <= 0:
        return False

    if dual.get("primary_window") != "1h":
        return False
    if dual.get("outcome_15m") not in VALID_OUTCOMES or dual.get("outcome_1h") not in VALID_OUTCOMES:
        return False
    if dual.get("outcome_1h") != row.get("outcome"):
        return False

    direction = (row.get("prediction") or "").upper()
    expected_15m = _directional_outcome(direction, origin_price, price_15m)
    expected_1h = _directional_outcome(direction, origin_price, price_1h)
    if expected_15m is None or expected_1h is None:
        return False
    return dual.get("outcome_15m") == expected_15m and dual.get("outcome_1h") == expected_1h


def proof_status(row: dict[str, Any]) -> str:
    """Return the explicit scoring status required by the SENEX contract."""
    return "PROOF_QUALIFIED" if is_proof_qualified(row) else "RAW_UNVERIFIED"
