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
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

log = logging.getLogger("senecio.supabase")

# Configuration — can be overridden by env vars at runtime
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://okgxqapbldtldmvjvzfh.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_publishable_ND41HJx4ef7JtjoDetI7RQ_P9JU-Y7Z")
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE", "oracle_predictions")

# Single reusable client (connection pooling)
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=f"{SUPABASE_URL}/rest/v1",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
    return _client


async def insert_prediction(prediction: dict) -> Optional[dict]:
    """Insert a single prediction into Supabase.

    Maps the prediction dict to the table schema:
      timestamp        -> ts
      symbol           -> symbol
      prediction       -> prediction
      confidence       -> confidence
      ev               -> ev
      price_now        -> price_now
      price_15m_later  -> price_15m_later
      outcome          -> outcome
      exchange_used    -> exchange_used
      _audit           -> audit (jsonb)

    Returns the inserted row (with id) on success, None on failure.
    """
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
        # Fetch just the id column, limit to 10k (we won't exceed this for a long time)
        r = await c.get(
            f"/{SUPABASE_TABLE}",
            params={"select": "id", "limit": "10000"},
        )
        if r.status_code == 200:
            data = r.json()
            return len(data) if isinstance(data, list) else 0
        # Fallback: try content-range header
        range_header = r.headers.get("content-range", "")
        if "/" in range_header:
            total = range_header.split("/")[-1]
            return int(total) if total.isdigit() else 0
        return 0
    except Exception as e:
        log.debug("supabase count error: %s", e)
        return 0


async def fetch_pending_outcomes(older_than_seconds: int = 900, limit: int = 100) -> list[dict]:
    """Fetch predictions that have outcome=NULL and are older than `older_than_seconds`.

    Used by the verifier to find predictions whose settlement window has elapsed
    and need to be settled (WIN/LOSS).

    ACT XXIII: default `older_than_seconds` was raised from 900 (15min) to 3600
    (1h) at the call-site, since the primary gating window is now 1h. The 15min
    outcome is still computed for research but is no longer the live gate.

    Returns rows with at least: id, ts, symbol, prediction, price_now.
    """
    try:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        c = _get_client()
        # PostgREST filter: outcome=is.null AND ts=lt.{cutoff}
        query_limit = min(max(limit * 5, limit), 1000)
        params = {
            "select": "id,ts,symbol,prediction,confidence,price_now,exchange_used,audit",
            "outcome": "is.null",
            "ts": f"lt.{cutoff}",
            "order": "ts.desc",
            "limit": str(query_limit),
        }
        r = await c.get(f"/{SUPABASE_TABLE}", params=params)
        if r.status_code == 200:
            rows = r.json() or []
            # Legacy NULL rows cannot be settled reproducibly and must not
            # starve newer proof-bearing predictions at the front of the queue.
            qualified = [
                row for row in rows
                if isinstance(row.get("audit"), dict)
                and isinstance(row["audit"].get("origin_price_proof"), dict)
                and row["audit"]["origin_price_proof"].get("proof_schema")
                == "oracle-origin-price-v1"
            ]
            return qualified[:limit]
        log.error("supabase fetch_pending_outcomes failed: %s %s", r.status_code, r.text[:200])
        return []
    except Exception as e:
        log.error("supabase fetch_pending_outcomes error: %s", e)
        return []


async def update_outcome(prediction_id: int, outcome: str, price_15m_later: float) -> bool:
    """Disabled legacy single-window writer.

    A single current price cannot prove either a 15-minute or 1-hour label.
    Keeping the function as a fail-closed compatibility shim prevents an old
    caller from silently creating scoreable-looking rows.
    """
    log.error(
        "legacy update_outcome disabled id=%s outcome=%s; use update_outcome_dual",
        prediction_id,
        outcome,
    )
    return False


async def update_outcome_dual(
    prediction_id: int,
    outcome_15m: str,
    outcome_1h: str,
    price_15m_later: float,
    price_1h_later: float,
    primary_window: str = "1h",
    settlement_observations: Optional[dict[str, dict[str, Any]]] = None,
) -> bool:
    """Settle a prediction with BOTH 15m and 1h outcomes (ACT XXIII dual-window path).

    Storage strategy (avoids schema migration on RLS-restricted anon key):
      - Primary `outcome` column  ← outcome_1h (the gating source of truth)
      - Primary `price_15m_later` ← price at ts+15min (preserves original column meaning)
      - `audit` JSONB             ← merge `outcomes_dual` sub-dict containing:
            {outcome_15m, outcome_1h, price_15m_later, price_1h_later, primary_window}

    Implementation: fetch existing audit dict (so we don't clobber signal metadata
    like pressures/regime_hint), merge the new outcomes_dual sub-dict, then PATCH.
    Two round-trips per row, but the verifier runs at most 100 rows per cycle.

    RLS safety: same as update_outcome() — require len(response body) > 0.
    """
    try:
        c = _get_client()

        # 1) Fetch the existing audit dict (and verify row exists)
        r_get = await c.get(
            f"/{SUPABASE_TABLE}",
            params={
                "select": "id,ts,symbol,exchange_used,audit,prediction,price_now",
                "id": f"eq.{prediction_id}",
                "limit": "1",
            },
        )
        if r_get.status_code != 200:
            log.error(
                "update_outcome_dual: GET audit failed id=%s status=%s body=%s",
                prediction_id, r_get.status_code, r_get.text[:200],
            )
            return False
        existing_rows = r_get.json() or []
        if not existing_rows:
            log.error(
                "update_outcome_dual: row not found id=%s (RLS or bad id)",
                prediction_id,
            )
            return False
        existing = existing_rows[0]
        direction = str(existing.get("prediction") or "").upper()
        try:
            origin = float(existing.get("price_now"))
            price_15m = float(price_15m_later)
            price_1h = float(price_1h_later)
        except (TypeError, ValueError):
            log.error("update_outcome_dual invalid settlement prices id=%s", prediction_id)
            return False
        if (
            primary_window != "1h"
            or direction not in {"LONG", "SHORT"}
            or outcome_15m not in {"WIN", "LOSS"}
            or outcome_1h not in {"WIN", "LOSS"}
            or not all(math.isfinite(value) and value > 0 for value in (origin, price_15m, price_1h))
        ):
            log.error("update_outcome_dual invalid proof contract id=%s", prediction_id)
            return False

        def expected(price_later: float) -> str:
            if direction == "LONG":
                return "WIN" if price_later > origin else "LOSS"
            return "WIN" if price_later < origin else "LOSS"

        if expected(price_15m) != outcome_15m or expected(price_1h) != outcome_1h:
            log.error("update_outcome_dual recomputation mismatch id=%s", prediction_id)
            return False
        existing_audit = existing.get("audit") or {}
        if not isinstance(existing_audit, dict):
            # Audit might be a JSON string in some edge cases — try parsing
            try:
                if isinstance(existing_audit, str):
                    existing_audit = json.loads(existing_audit)
                else:
                    existing_audit = {}
            except Exception:
                existing_audit = {}

        def normalized_symbol(value: Any) -> str:
            return str(value or "").upper().replace("/", "").replace("-", "")

        try:
            prediction_at = datetime.fromisoformat(
                str(existing.get("ts") or "").replace("Z", "+00:00")
            )
            if prediction_at.tzinfo is None:
                prediction_at = prediction_at.replace(tzinfo=timezone.utc)
            prediction_at = prediction_at.astimezone(timezone.utc)
        except (TypeError, ValueError):
            log.error("update_outcome_dual invalid prediction timestamp id=%s", prediction_id)
            return False

        row_symbol = normalized_symbol(existing.get("symbol"))
        origin_proof = existing_audit.get("origin_price_proof")
        if not isinstance(origin_proof, dict):
            log.error("update_outcome_dual missing origin proof id=%s", prediction_id)
            return False
        origin_observed_at = None
        try:
            origin_observed_at = datetime.fromisoformat(
                str(origin_proof.get("observed_at") or "").replace("Z", "+00:00")
            )
            if origin_observed_at.tzinfo is None:
                origin_observed_at = origin_observed_at.replace(tzinfo=timezone.utc)
            origin_observed_at = origin_observed_at.astimezone(timezone.utc)
            origin_proof_price = float(origin_proof.get("price"))
        except (TypeError, ValueError):
            log.error("update_outcome_dual malformed origin proof id=%s", prediction_id)
            return False
        if (
            origin_proof.get("proof_schema") != "oracle-origin-price-v1"
            or str(existing.get("exchange_used") or "").lower() != "okx"
            or str(origin_proof.get("exchange") or "").lower() != "okx"
            or normalized_symbol(origin_proof.get("instrument")) != row_symbol
            or origin_proof.get("price_source") != "public_ticker_best_bid"
            or abs((origin_observed_at - prediction_at).total_seconds()) > 1.0
            or not math.isclose(origin_proof_price, origin, rel_tol=0.0, abs_tol=1e-9)
        ):
            log.error("update_outcome_dual origin proof mismatch id=%s", prediction_id)
            return False

        if not isinstance(settlement_observations, dict):
            log.error("update_outcome_dual missing settlement observations id=%s", prediction_id)
            return False

        def valid_observation(window_name: str, window_s: int, expected_price: float) -> bool:
            observation = settlement_observations.get(window_name)
            if not isinstance(observation, dict):
                return False
            try:
                target_ts_ms = int(observation.get("target_ts_ms"))
                candle_open_ms = int(observation.get("candle_open_ts_ms"))
                candle_close_ms = int(observation.get("candle_close_ts_ms"))
                observed_price = float(observation.get("price"))
                target_ts = datetime.fromisoformat(
                    str(observation.get("target_ts") or "").replace("Z", "+00:00")
                )
                if target_ts.tzinfo is None:
                    target_ts = target_ts.replace(tzinfo=timezone.utc)
                target_ts = target_ts.astimezone(timezone.utc)
            except (TypeError, ValueError):
                return False
            expected_target = prediction_at + timedelta(seconds=window_s)
            expected_target_ms = int(expected_target.timestamp() * 1000)
            return (
                observation.get("proof_schema") == "oracle-settlement-observation-v1"
                and str(observation.get("exchange") or "").lower() == "okx"
                and normalized_symbol(observation.get("instrument")) == row_symbol
                and observation.get("timeframe") == "1m"
                and observation.get("price_field") == "close"
                and abs((target_ts - expected_target).total_seconds()) <= 0.001
                and abs(target_ts_ms - expected_target_ms) <= 1
                and candle_close_ms - candle_open_ms == 60_000
                and candle_open_ms <= target_ts_ms < candle_close_ms
                and int(datetime.now(timezone.utc).timestamp() * 1000) >= candle_close_ms
                and math.isfinite(observed_price)
                and math.isclose(observed_price, expected_price, rel_tol=0.0, abs_tol=1e-9)
            )

        if (
            not valid_observation("15m", 900, price_15m)
            or not valid_observation("1h", 3600, price_1h)
        ):
            log.error("update_outcome_dual invalid settlement observation id=%s", prediction_id)
            return False

        # 2) Merge new outcomes_dual sub-dict (preserves any pre-existing fields)
        outcomes_dual = {
            "proof_schema": "oracle-settlement-proof-v1",
            "price_source": "okx_public_ohlcv",
            "settlement_method": "historical_1m_close_containing_target",
            "settled_at": datetime.now(timezone.utc).isoformat(),
            "outcome_15m": outcome_15m,
            "outcome_1h": outcome_1h,
            "price_15m_later": float(price_15m_later) if price_15m_later is not None else None,
            "price_1h_later": float(price_1h_later) if price_1h_later is not None else None,
            "primary_window": primary_window,
            "settlement_observations": settlement_observations,
        }
        existing_audit["outcomes_dual"] = outcomes_dual

        # 3) PATCH with primary outcome (1h = gating) + dual audit
        patch_body = {
            "outcome": outcome_1h,                  # primary = 1h
            "price_15m_later": float(price_15m_later) if price_15m_later is not None else None,
            "audit": existing_audit,
        }
        r = await c.patch(
            f"/{SUPABASE_TABLE}",
            params={"id": f"eq.{prediction_id}"},
            json=patch_body,
        )
        if r.status_code in (200, 204):
            try:
                body = r.json() if r.content else []
            except Exception:
                body = []
            if isinstance(body, list) and len(body) > 0:
                log.info(
                    "supabase update_outcome_dual OK id=%s 15m=%s 1h=%s primary=%s",
                    prediction_id, outcome_15m, outcome_1h, primary_window,
                )
                return True
            log.error(
                "supabase update_outcome_dual NO-OP id=%s status=%s body=%r "
                "— RLS likely blocked UPDATE (check UPDATE policy on table)",
                prediction_id, r.status_code, body,
            )
            return False
        log.error(
            "supabase update_outcome_dual failed: %s %s",
            r.status_code, r.text[:300],
        )
        return False
    except Exception as e:
        log.error("supabase update_outcome_dual error: %s", e)
        return False


async def close() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None
