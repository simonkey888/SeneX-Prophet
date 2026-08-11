"""
SENECIO ORACLE — Supabase Client (ACT XXIII)
=============================================

Lightweight async REST client for Supabase PostgREST.
Uses the publishable (anon) key — table is RLS-protected for INSERT+SELECT.

ACT XXIII changes:
  - Dual-window outcome support: stores outcome_15m + outcome_1h side-by-side
    in the audit JSONB (avoids schema migration on RLS-restricted anon key).
  - The primary `outcome` column now mirrors `outcome_1h` (the gating window).
  - `price_15m_later` column keeps its original meaning (price at ts+15min).
  - `update_outcome_dual()` fetches existing audit, merges `outcomes_dual` sub-dict,
    then PATCHes (avoids clobbering existing audit signal metadata).
  - `fetch_pending_outcomes_dual()` fetches predictions older than 1h (the gating
    window) so the verifier can settle both 15m and 1h outcomes atomically.
  - Backward-compat: `update_outcome()` kept for callers that only have 1h data.

Only depends on httpx (already in requirements.txt).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx

log = logging.getLogger("senecio.supabase")

# Configuration — runtime-only. There are deliberately no credential fallbacks.
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE", "oracle_predictions")


def _require_config() -> tuple[str, str]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be provided by the runtime environment")
    return SUPABASE_URL, SUPABASE_KEY


# Single reusable client (connection pooling)
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    supabase_url, supabase_key = _require_config()
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=f"{supabase_url}/rest/v1",
            headers={
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
    return _client


async def insert_prediction(prediction: dict) -> Optional[dict]:
    """Insert a single prediction into Supabase."""
    row = {
        "ts": prediction.get("timestamp"),
        "symbol": prediction.get("symbol"),
        "prediction": prediction.get("prediction"),
        "confidence": float(prediction.get("confidence", 0)),
        "ev": float(prediction.get("ev", 0)),
        "price_now": float(prediction.get("price_now", 0)),
        "price_15m_later": prediction.get("price_15m_later"),
        "outcome": prediction.get("outcome"),
        "exchange_used": prediction.get("exchange_used", "unknown"),
        "audit": prediction.get("_audit"),
    }
    try:
        c = _get_client()
        r = await c.post(f"/{SUPABASE_TABLE}", json=row)
        if r.status_code in (200, 201):
            data = r.json()
            if isinstance(data, list) and data:
                log.info("supabase insert OK id=%s", data[0].get("id"))
                return data[0]
            return data
        log.error("supabase insert failed: %s %s", r.status_code, r.text[:300])
        return None
    except Exception as e:
        log.error("supabase insert error: %s", e)
        return None


async def fetch_predictions(limit: int = 50, symbol: Optional[str] = None) -> list[dict]:
    """Fetch recent predictions (most recent first)."""
    try:
        c = _get_client()
        params = {"limit": str(limit), "order": "ts.desc"}
        if symbol:
            params["symbol"] = f"eq.{symbol}"
        r = await c.get(f"/{SUPABASE_TABLE}", params=params)
        if r.status_code == 200:
            return r.json()
        log.error("supabase fetch failed: %s %s", r.status_code, r.text[:200])
        return []
    except Exception as e:
        log.error("supabase fetch error: %s", e)
        return []


async def count_predictions() -> int:
    """Get total prediction count by fetching all IDs (works around content-range header issues)."""
    try:
        c = _get_client()
        r = await c.get(f"/{SUPABASE_TABLE}", params={"select": "id", "limit": "10000"})
        if r.status_code == 200:
            data = r.json()
            return len(data) if isinstance(data, list) else 0
        range_header = r.headers.get("content-range", "")
        if "/" in range_header:
            total = range_header.split("/")[-1]
            return int(total) if total.isdigit() else 0
        return 0
    except Exception as e:
        log.debug("supabase count error: %s", e)
        return 0


async def fetch_pending_outcomes(older_than_seconds: int = 900, limit: int = 100) -> list[dict]:
    """Fetch predictions that have outcome=NULL and are older than the settlement window."""
    try:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        c = _get_client()
        params = {
            "select": "id,ts,symbol,prediction,confidence,price_now,exchange_used,audit",
            "outcome": "is.null",
            "ts": f"lt.{cutoff}",
            "order": "ts.asc",
            "limit": str(limit),
        }
        r = await c.get(f"/{SUPABASE_TABLE}", params=params)
        if r.status_code == 200:
            return r.json() or []
        log.error("supabase fetch_pending_outcomes failed: %s %s", r.status_code, r.text[:200])
        return []
    except Exception as e:
        log.error("supabase fetch_pending_outcomes error: %s", e)
        return []


async def update_outcome(prediction_id: int, outcome: str, price_15m_later: float) -> bool:
    """Legacy single-window update retained for backward compatibility."""
    try:
        c = _get_client()
        r = await c.patch(
            f"/{SUPABASE_TABLE}",
            params={"id": f"eq.{prediction_id}"},
            json={"outcome": outcome, "price_15m_later": float(price_15m_later)},
        )
        if r.status_code in (200, 204):
            try:
                body = r.json() if r.content else []
            except Exception:
                body = []
            if isinstance(body, list) and len(body) > 0:
                log.info("supabase update_outcome OK id=%s outcome=%s", prediction_id, outcome)
                return True
            log.error("supabase update_outcome NO-OP id=%s outcome=%s status=%s body=%r", prediction_id, outcome, r.status_code, body)
            return False
        log.error("supabase update_outcome failed: %s %s", r.status_code, r.text[:300])
        return False
    except Exception as e:
        log.error("supabase update_outcome error: %s", e)
        return False


async def update_outcome_dual(
    prediction_id: int,
    outcome_15m: str,
    outcome_1h: str,
    price_15m_later: float,
    price_1h_later: float,
    primary_window: str = "1h",
) -> bool:
    """Settle a prediction with BOTH 15m and 1h outcomes."""
    try:
        c = _get_client()
        r_get = await c.get(
            f"/{SUPABASE_TABLE}",
            params={"select": "id,audit", "id": f"eq.{prediction_id}", "limit": "1"},
        )
        if r_get.status_code != 200:
            log.error("update_outcome_dual: GET audit failed id=%s status=%s body=%s", prediction_id, r_get.status_code, r_get.text[:200])
            return False
        existing_rows = r_get.json() or []
        if not existing_rows:
            log.error("update_outcome_dual: row not found id=%s (RLS or bad id)", prediction_id)
            return False
        existing_audit = existing_rows[0].get("audit") or {}
        if not isinstance(existing_audit, dict):
            try:
                existing_audit = json.loads(existing_audit) if isinstance(existing_audit, str) else {}
            except Exception:
                existing_audit = {}

        outcomes_dual = {
            "outcome_15m": outcome_15m,
            "outcome_1h": outcome_1h,
            "price_15m_later": float(price_15m_later) if price_15m_later is not None else None,
            "price_1h_later": float(price_1h_later) if price_1h_later is not None else None,
            "primary_window": primary_window,
        }
        existing_audit["outcomes_dual"] = outcomes_dual
        patch_body = {
            "outcome": outcome_1h,
            "price_15m_later": float(price_15m_later) if price_15m_later is not None else None,
            "audit": existing_audit,
        }
        r = await c.patch(f"/{SUPABASE_TABLE}", params={"id": f"eq.{prediction_id}"}, json=patch_body)
        if r.status_code in (200, 204):
            try:
                body = r.json() if r.content else []
            except Exception:
                body = []
            if isinstance(body, list) and len(body) > 0:
                log.info("supabase update_outcome_dual OK id=%s 15m=%s 1h=%s primary=%s", prediction_id, outcome_15m, outcome_1h, primary_window)
                return True
            log.error("supabase update_outcome_dual NO-OP id=%s status=%s body=%r", prediction_id, r.status_code, body)
            return False
        log.error("supabase update_outcome_dual failed: %s %s", r.status_code, r.text[:300])
        return False
    except Exception as e:
        log.error("supabase update_outcome_dual error: %s", e)
        return False


async def close() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None
