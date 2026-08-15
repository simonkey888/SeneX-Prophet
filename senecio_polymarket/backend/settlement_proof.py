"""Authoritative settlement-proof gate for SENEX scoring.

A raw WIN/LOSS is never proof-qualified. AUD-063 additionally requires each
15m/1h outcome to carry bounded historical-candle evidence from the same public
exchange/source as the persisted origin witness. Legacy rows without that
causal provenance remain diagnostic only.
"""
from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

from .settlement_contract import (
    WINDOW_15M_S,
    WINDOW_1H_S,
    directional_outcome,
    normalize_exchange,
    normalize_symbol,
    parse_utc,
    target_epoch_ms,
    validate_price_evidence,
)

VALID_OUTCOMES = {"WIN", "LOSS"}
ORIGIN_PRICE_REL_TOL = 1e-9
ORIGIN_PRICE_ABS_TOL = 1e-9


def _as_audit(row: dict[str, Any]) -> dict[str, Any]:
    audit = row.get("audit") or {}
    return audit if isinstance(audit, dict) else {}


def _price_matches(left: Any, right: Any) -> bool:
    try:
        return math.isclose(
            float(left),
            float(right),
            rel_tol=ORIGIN_PRICE_REL_TOL,
            abs_tol=ORIGIN_PRICE_ABS_TOL,
        )
    except (TypeError, ValueError):
        return False


def is_proof_qualified(row: dict[str, Any]) -> bool:
    """Return True only when the row satisfies the complete causal proof contract."""
    if row.get("outcome") not in VALID_OUTCOMES:
        return False
    direction = str(row.get("prediction") or "").upper()
    if direction not in {"LONG", "SHORT"}:
        return False

    audit = _as_audit(row)
    origin_proof = audit.get("origin_price_v1")
    dual = audit.get("outcomes_dual")
    if not isinstance(origin_proof, dict) or not isinstance(dual, dict):
        return False
    if origin_proof.get("version") != "origin-price-v1":
        return False

    row_ts = row.get("ts")
    origin_ts = origin_proof.get("timestamp")
    origin_dt = parse_utc(origin_ts)
    row_dt = parse_utc(row_ts)
    if origin_dt is None or row_dt is None or origin_dt != row_dt:
        return False

    row_source = normalize_exchange(row.get("exchange_used"))
    origin_source = normalize_exchange(origin_proof.get("source"))
    if row_source is None or origin_source != row_source:
        return False
    symbol = normalize_symbol(row.get("symbol"))
    if not symbol:
        return False

    try:
        origin_price = float(origin_proof.get("price"))
        row_price = float(row.get("price_now"))
        price_15m = float(dual.get("price_15m_later"))
        price_1h = float(dual.get("price_1h_later"))
    except (TypeError, ValueError):
        return False
    if min(origin_price, row_price, price_15m, price_1h) <= 0:
        return False
    if not _price_matches(origin_price, row_price):
        return False

    if dual.get("primary_window") != "1h":
        return False
    if dual.get("settlement_contract_version") != "aud063-v1":
        return False
    if dual.get("outcome_15m") not in VALID_OUTCOMES or dual.get("outcome_1h") not in VALID_OUTCOMES:
        return False
    if dual.get("outcome_1h") != row.get("outcome"):
        return False

    historical = dual.get("price_evidence_v1")
    if not isinstance(historical, dict):
        return False
    evidence_15m = historical.get("15m")
    evidence_1h = historical.get("1h")
    if not validate_price_evidence(
        evidence_15m,
        expected_exchange=row_source,
        expected_symbol=symbol,
        expected_ts=row_ts,
        expected_window_seconds=WINDOW_15M_S,
    ):
        return False
    if not validate_price_evidence(
        evidence_1h,
        expected_exchange=row_source,
        expected_symbol=symbol,
        expected_ts=row_ts,
        expected_window_seconds=WINDOW_1H_S,
    ):
        return False
    if not _price_matches(price_15m, evidence_15m.get("price")):
        return False
    if not _price_matches(price_1h, evidence_1h.get("price")):
        return False

    observation = dual.get("settlement_observation_v1")
    if not isinstance(observation, dict) or observation.get("version") != "settlement-observation-v1":
        return False
    observed_at = parse_utc(observation.get("observed_at"))
    if observed_at is None:
        return False
    # A settlement cannot become causally available before the 1h target exists.
    if observed_at < row_dt + timedelta(seconds=WINDOW_1H_S):
        return False
    if target_epoch_ms(row_ts, WINDOW_1H_S) != int(evidence_1h.get("target_epoch_ms")):
        return False

    expected_15m = directional_outcome(direction, origin_price, price_15m)
    expected_1h = directional_outcome(direction, origin_price, price_1h)
    if expected_15m is None or expected_1h is None:
        return False
    return dual.get("outcome_15m") == expected_15m and dual.get("outcome_1h") == expected_1h


def filter_proof_qualified(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only rows satisfying the complete authoritative proof contract."""
    return [row for row in rows if is_proof_qualified(row)]


def score_qualified_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the authoritative score from proof-qualified rows only."""
    verified = filter_proof_qualified(rows)
    wins = sum(1 for row in verified if row.get("outcome") == "WIN")
    losses = sum(1 for row in verified if row.get("outcome") == "LOSS")
    return {
        "rows": verified,
        "verified": len(verified),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": (wins / len(verified) * 100) if verified else 0.0,
    }


def proof_status(row: dict[str, Any]) -> str:
    """Return the explicit scoring status required by the SENEX contract."""
    return "PROOF_QUALIFIED" if is_proof_qualified(row) else "RAW_UNVERIFIED"
