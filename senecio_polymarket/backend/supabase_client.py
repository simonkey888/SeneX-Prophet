"""
SENECIO ORACLE — Supabase Client (ACT XXIII)
=============================================

Lightweight async REST client for Supabase PostgREST.
Supports modern opaque API keys and legacy JWT-based API keys.

ACT XXIII changes:
  - Dual-window outcome support: stores outcome_15m + outcome_1h side-by-side
    in the audit JSONB (avoids schema migration on RLS-restricted anon key).
  - The primary `outcome` column now mirrors `outcome_1h` (the gating window).
  - `price_15m_later` column keeps its original meaning (price at ts+15min).
  - `update_outcome_dual()` fetches existing audit, merges `outcomes_dual` sub-dict,
    then PATCHes (avoids clobbering existing audit signal metadata).
  - `fetch_pending_outcomes()` fetches predictions older than 1h (the gating
    window) so the verifier can settle both 15m and 1h outcomes atomically.

Only depends on httpx (already in requirements.txt).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
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


def build_supabase_headers(
    supabase_key: str,
    *,
    prefer: str = "return=representation",
) -> dict[str, str]:
    """Build PostgREST headers for modern opaque or legacy JWT API keys.

    Modern ``sb_secret_``/``sb_publishable_`` keys are opaque and must be sent
    through ``apikey`` only. Legacy anon/service_role API keys are JWTs; keep
    the historical Bearer header for compatibility with those deployments.
    """
    if not supabase_key:
        raise RuntimeError("SUPABASE_KEY must be provided by the runtime environment")

    headers = {
        "apikey": supabase_key,
        "Content-Type": "application/json",
        "Prefer": prefer,
    }
    if supabase_key.startswith("eyJ") and supabase_key.count(".") == 2:
        headers["Authorization"] = f"Bearer {supabase_key}"
    return headers


# Single reusable client (connection pooling)
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    supabase_url, supabase_key = _require_config()
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=f"{supabase_url}/rest/v1",
            headers=build_supabase_headers(supabase_key),
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


_pending_scan_cursor: tuple[str, str] | None = None
_pending_scan_diagnostics: dict[str, Any] = {}


def reset_pending_scan_cursor() -> None:
    """Reset the bounded keyset scan. Intended for restart semantics/tests."""
    global _pending_scan_cursor, _pending_scan_diagnostics
    _pending_scan_cursor = None
    _pending_scan_diagnostics = {}


def get_pending_scan_diagnostics() -> dict[str, Any]:
    return dict(_pending_scan_diagnostics)


async def fetch_pending_outcomes(older_than_seconds: int = 900, limit: int = 100) -> list[dict]:
    """Fetch one bounded keyset page of eligible directional NULL outcomes.

    FLAT/non-directional rows are excluded server-side. A stable (ts,id) cursor
    advances even when a row later fails historical-price/proof validation, so
    poison rows cannot permanently block later eligible rows. At end-of-pass the
    cursor resets for a later retry pass; failed rows therefore remain retryable.
    """
    global _pending_scan_cursor, _pending_scan_diagnostics
    try:
        from datetime import timedelta

        bounded_limit = max(1, min(int(limit), 500))
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        c = _get_client()
        base_params = {
            "select": "id,ts,symbol,prediction,confidence,price_now,exchange_used,audit",
            "outcome": "is.null",
            "prediction": "in.(LONG,SHORT)",
            "ts": f"lte.{cutoff}",
            "order": "ts.asc,id.asc",
        }

        # Backlog visibility is independent of the current page/cursor.
        metric_params = {
            "select": "id,ts,symbol,prediction",
            "outcome": "is.null",
            "prediction": "in.(LONG,SHORT)",
            "ts": f"lte.{cutoff}",
            "order": "ts.asc,id.asc",
            "limit": "1",
        }
        metric = await c.get(
            f"/{SUPABASE_TABLE}", params=metric_params, headers={"Prefer": "count=exact"}
        )
        eligible_count = None
        oldest = None
        if metric.status_code == 200:
            metric_rows = metric.json() or []
            if isinstance(metric_rows, list) and metric_rows:
                oldest = metric_rows[0]
            content_range = str(getattr(metric, "headers", {}).get("content-range", ""))
            if "/" in content_range:
                total = content_range.rsplit("/", 1)[-1]
                if total.isdigit():
                    eligible_count = int(total)

        cursor_before = _pending_scan_cursor
        params = dict(base_params)
        params["limit"] = str(bounded_limit)
        if cursor_before is not None:
            cursor_ts, cursor_id = cursor_before
            params["or"] = f"(ts.gt.{cursor_ts},and(ts.eq.{cursor_ts},id.gt.{cursor_id}))"

        r = await c.get(f"/{SUPABASE_TABLE}", params=params)
        if r.status_code != 200:
            log.error("supabase fetch_pending_outcomes failed: %s %s", r.status_code, r.text[:200])
            _pending_scan_diagnostics = {
                "eligible_directional_pending_count": eligible_count,
                "oldest_eligible_directional_pending_id": (oldest or {}).get("id"),
                "oldest_eligible_directional_pending_ts": (oldest or {}).get("ts"),
                "rows_scanned_last_pass": 0,
                "scan_cap_hit": False,
                "cursor_before": cursor_before,
                "cursor_after": cursor_before,
                "pass_complete": False,
                "error": f"HTTP_{r.status_code}",
            }
            return []
        rows = r.json() or []
        rows = rows if isinstance(rows, list) else []

        pass_complete = len(rows) < bounded_limit
        cursor_after = cursor_before
        if rows:
            last = rows[-1]
            cursor_after = (str(last.get("ts") or ""), str(last.get("id") or ""))
        if pass_complete:
            _pending_scan_cursor = None
        else:
            _pending_scan_cursor = cursor_after

        _pending_scan_diagnostics = {
            "eligible_directional_pending_count": eligible_count,
            "oldest_eligible_directional_pending_id": (oldest or {}).get("id"),
            "oldest_eligible_directional_pending_ts": (oldest or {}).get("ts"),
            "rows_scanned_last_pass": len(rows),
            "scan_cap_hit": len(rows) >= bounded_limit,
            "cursor_before": cursor_before,
            "cursor_after": cursor_after,
            "pass_complete": pass_complete,
            "error": None,
        }
        return rows
    except Exception as e:
        log.error("supabase fetch_pending_outcomes error: %s", e)
        _pending_scan_diagnostics = {"error": type(e).__name__, "rows_scanned_last_pass": 0}
        return []

async def update_outcome_dual(
    prediction_id: int,
    outcome_15m: str,
    outcome_1h: str,
    price_15m_later: float,
    price_1h_later: float,
    primary_window: str = "1h",
    *,
    price_evidence_15m: dict[str, Any] | None = None,
    price_evidence_1h: dict[str, Any] | None = None,
) -> bool:
    """CAS-settle one directional row only with complete causal evidence."""
    try:
        import math
        from datetime import timedelta
        from .settlement_contract import (
            WINDOW_15M_S,
            WINDOW_1H_S,
            normalize_exchange,
            normalize_symbol,
            parse_utc,
            validate_price_evidence,
        )

        c = _get_client()
        r_get = await c.get(
            f"/{SUPABASE_TABLE}",
            params={
                "select": "id,ts,symbol,prediction,price_now,exchange_used,audit,outcome",
                "id": f"eq.{prediction_id}",
                "limit": "1",
            },
        )
        if r_get.status_code != 200:
            return False
        existing_rows = r_get.json() or []
        if not isinstance(existing_rows, list) or not existing_rows:
            return False
        existing = existing_rows[0]
        if existing.get("outcome") is not None:
            return False
        direction = str(existing.get("prediction") or "").upper()
        if direction not in {"LONG", "SHORT"}:
            return False

        existing_audit = existing.get("audit") or {}
        if not isinstance(existing_audit, dict):
            try:
                existing_audit = json.loads(existing_audit) if isinstance(existing_audit, str) else {}
            except Exception:
                return False
        origin = existing_audit.get("origin_price_v1")
        if not isinstance(origin, dict) or origin.get("version") != "origin-price-v1":
            return False
        expected_source = normalize_exchange(existing.get("exchange_used"))
        if expected_source is None or normalize_exchange(origin.get("source")) != expected_source:
            return False
        row_ts = existing.get("ts")
        row_dt = parse_utc(row_ts)
        if row_dt is None or parse_utc(origin.get("timestamp")) != row_dt:
            return False
        try:
            if not math.isclose(float(origin.get("price")), float(existing.get("price_now")), rel_tol=1e-9, abs_tol=1e-9):
                return False
        except (TypeError, ValueError):
            return False

        if not validate_price_evidence(
            price_evidence_15m,
            expected_exchange=expected_source,
            expected_symbol=normalize_symbol(existing.get("symbol")),
            expected_ts=row_ts,
            expected_window_seconds=WINDOW_15M_S,
        ):
            return False
        if not validate_price_evidence(
            price_evidence_1h,
            expected_exchange=expected_source,
            expected_symbol=normalize_symbol(existing.get("symbol")),
            expected_ts=row_ts,
            expected_window_seconds=WINDOW_1H_S,
        ):
            return False
        try:
            if not math.isclose(float(price_15m_later), float(price_evidence_15m["price"]), rel_tol=1e-9, abs_tol=1e-9):
                return False
            if not math.isclose(float(price_1h_later), float(price_evidence_1h["price"]), rel_tol=1e-9, abs_tol=1e-9):
                return False
        except (TypeError, ValueError, KeyError):
            return False
        observed_at = datetime.now(timezone.utc)
        if observed_at < row_dt + timedelta(seconds=WINDOW_1H_S):
            return False

        observed_iso = observed_at.isoformat()
        outcomes_dual = {
            "outcome_15m": outcome_15m,
            "outcome_1h": outcome_1h,
            "price_15m_later": float(price_15m_later),
            "price_1h_later": float(price_1h_later),
            "primary_window": primary_window,
            "settled_at": observed_iso,
            "settlement_contract_version": "aud063-v1",
            "price_evidence_v1": {
                "15m": dict(price_evidence_15m),
                "1h": dict(price_evidence_1h),
            },
            "settlement_observation_v1": {
                "version": "settlement-observation-v1",
                "observed_at": observed_iso,
                "writer": "SENEX_PRIMARY_DUAL_WINDOW_VERIFIER_V2",
                "availability_semantics": "PERSISTED_BY_COMPARE_AND_SET_AT_OR_AFTER_THIS_TIME",
            },
        }
        merged_audit = dict(existing_audit)
        merged_audit["outcomes_dual"] = outcomes_dual
        patch_body = {
            "outcome": outcome_1h,
            "price_15m_later": float(price_15m_later),
            "audit": merged_audit,
        }
        r = await c.patch(
            f"/{SUPABASE_TABLE}",
            params={
                "id": f"eq.{prediction_id}",
                "outcome": "is.null",
                "audit->outcomes_dual": "is.null",
            },
            json=patch_body,
        )
        if r.status_code not in (200, 204):
            return False
        try:
            body = r.json() if getattr(r, "content", b"") else []
        except Exception:
            body = []
        # HTTP success with no returned changed row is a CAS no-op, not success.
        return isinstance(body, list) and len(body) > 0
    except Exception as e:
        log.error("supabase update_outcome_dual error: %s", e)
        return False

async def close() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None
