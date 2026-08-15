"""Test-only helpers for migrating historical fixtures to the AUD-063 proof contract."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from backend.settlement_contract import (
    WINDOW_15M_S,
    WINDOW_1H_S,
    price_evidence_from_candles,
    target_epoch_ms,
)


def _evidence(row: dict[str, Any], window_seconds: int, price: float, observed_at: str) -> dict[str, Any]:
    target = target_epoch_ms(row["ts"], window_seconds)
    if target is None:
        raise ValueError("fixture timestamp must be parseable")
    candle_open = target - (target % 60_000)
    evidence = price_evidence_from_candles(
        candles=[[candle_open, price, price, price, price, 1.0]],
        exchange=row["exchange_used"],
        symbol=row["symbol"],
        ts_iso=row["ts"],
        window_seconds=window_seconds,
        observed_at=observed_at,
    )
    if evidence is None:
        raise ValueError("fixture evidence could not be built")
    return evidence


def upgrade_proof_row(row: dict[str, Any], *, observed_at: str | None = None) -> dict[str, Any]:
    """Return a copied synthetic row satisfying AUD-063 causal proof semantics.

    This helper is test-only. It does not relax production validation and it
    never invents provenance for runtime/legacy rows.
    """
    row = dict(row)
    audit = dict(row.get("audit") or {})
    origin = dict(audit.get("origin_price_v1") or {})
    dual = dict(audit.get("outcomes_dual") or {})
    if not origin or not dual:
        row["audit"] = audit
        return row

    source = str(origin.get("source") or row.get("exchange_used") or "okx").lower()
    row["exchange_used"] = source
    if observed_at is None:
        origin_dt = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
        if origin_dt.tzinfo is None:
            origin_dt = origin_dt.replace(tzinfo=timezone.utc)
        observed_at = (origin_dt + timedelta(hours=2)).isoformat()

    p15 = float(dual["price_15m_later"])
    p1h = float(dual["price_1h_later"])
    dual["settlement_contract_version"] = "aud063-v1"
    dual["price_evidence_v1"] = {
        "15m": _evidence(row, WINDOW_15M_S, p15, observed_at),
        "1h": _evidence(row, WINDOW_1H_S, p1h, observed_at),
    }
    dual["settlement_observation_v1"] = {
        "version": "settlement-observation-v1",
        "observed_at": observed_at,
        "writer": "AUD063_SYNTHETIC_TEST_FIXTURE",
        "availability_semantics": "SYNTHETIC_TEST_ONLY",
    }
    audit["origin_price_v1"] = origin
    audit["outcomes_dual"] = dual
    row["audit"] = audit
    return row
