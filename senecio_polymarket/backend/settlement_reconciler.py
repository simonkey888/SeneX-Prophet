"""SENEX settlement reconciliation guard.

This module is NOT a second prediction/settlement authority.
It only repairs rows that already contain WIN/LOSS but lack the
single-authority `audit.outcomes_dual` evidence. The production oracle
runner remains the authority for NULL -> settled transitions.

Safety: public market reads only; paper-only; no wallet, signing or orders.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import ccxt
import httpx

from .supabase_client import build_supabase_headers

log = logging.getLogger("senex.settlement_reconciler")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE", "oracle_predictions")
INTERVAL_S = int(os.environ.get("SETTLEMENT_RECONCILE_INTERVAL_SEC", "900"))
BATCH_LIMIT = int(os.environ.get("SETTLEMENT_RECONCILE_BATCH", "200"))
HEARTBEAT_FILE = Path(os.environ.get("SENEX_RECONCILER_HEARTBEAT_FILE", "/tmp/senex-reconciler-heartbeat"))
WINDOW_15M_S = 900
WINDOW_1H_S = 3600


def _headers() -> dict[str, str]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be provided by the runtime environment")
    return build_supabase_headers(SUPABASE_KEY)


def _normalize_symbol(symbol: str) -> str:
    symbol = (symbol or "").upper().strip()
    if "/" in symbol:
        return symbol
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}/USDT"
    return symbol


def _outcome(direction: str, origin: float, later: float) -> Optional[str]:
    direction = (direction or "").upper()
    if direction == "LONG":
        return "WIN" if later > origin else "LOSS"
    if direction == "SHORT":
        return "WIN" if later < origin else "LOSS"
    return None


def _price_at(exchange, symbol: str, ts_iso: str, window_s: int) -> Optional[float]:
    ts = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
    target_ms = int((ts + timedelta(seconds=window_s)).timestamp() * 1000)
    candles = exchange.fetch_ohlcv(symbol, timeframe="1m", since=target_ms - 60_000, limit=2)
    if not candles:
        return None
    candidates = [c for c in candles if c[0] <= target_ms]
    if not candidates:
        log.warning("no historical candle at/before target for %s target_ms=%s", symbol, target_ms)
        return None
    candle = max(candidates, key=lambda c: c[0])
    price = float(candle[4])
    return price if price > 0 else None


async def reconcile_once() -> dict[str, int]:
    """Repair only already-settled rows that lack dual-window evidence."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be provided by the runtime environment")

    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=WINDOW_1H_S)).isoformat()
    async with httpx.AsyncClient(timeout=20.0, headers=_headers()) as client:
        cursor_ts: Optional[str] = None
        cursor_id: Optional[int] = None
        total_scanned = repaired = skipped = errors = conflicts = 0

        while True:
            params: dict[str, str] = {
                "select": "id,ts,symbol,prediction,price_now,outcome,audit,exchange_used",
                "outcome": "in.(WIN,LOSS)",
                "audit->outcomes_dual": "is.null",
                "ts": f"lt.{cutoff}",
                "order": "ts.asc,id.asc",
                "limit": str(BATCH_LIMIT),
            }
            if cursor_ts is not None and cursor_id is not None:
                params["or"] = f"(ts.gt.{cursor_ts},and(ts.eq.{cursor_ts},id.gt.{cursor_id}))"

            r = await client.get(f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}", params=params)
            if r.status_code != 200:
                log.error("reconcile fetch failed: %s %s", r.status_code, r.text[:300])
                errors += 1
                break

            rows = r.json() or []
            if not rows:
                break
            total_scanned += len(rows)

            exchanges: dict[str, Any] = {}
            try:
                for row in rows:
                    if row.get("outcome") not in ("WIN", "LOSS"):
                        skipped += 1
                        continue

                    audit = row.get("audit") or {}
                    if isinstance(audit, str):
                        try:
                            audit = json.loads(audit)
                        except Exception:
                            audit = {}
                    if not isinstance(audit, dict):
                        audit = {}

                    if "outcomes_dual" in audit and audit.get("outcomes_dual") is not None:
                        skipped += 1
                        continue

                    direction = (row.get("prediction") or "").upper()
                    try:
                        origin = float(row.get("price_now") or 0)
                    except (TypeError, ValueError):
                        origin = 0.0
                    ts_iso = row.get("ts")
                    if direction not in ("LONG", "SHORT") or origin <= 0 or not ts_iso:
                        skipped += 1
                        continue

                    symbol = _normalize_symbol(row.get("symbol", ""))
                    exchange_name = str(row.get("exchange_used") or "okx").lower()
                    if exchange_name not in {"okx", "kraken", "gate", "mexc", "bitget"}:
                        exchange_name = "okx"
                    if exchange_name not in exchanges:
                        exchanges[exchange_name] = getattr(ccxt, exchange_name)({"enableRateLimit": True})
                    ex = exchanges[exchange_name]

                    try:
                        p15 = await asyncio.to_thread(_price_at, ex, symbol, str(ts_iso), WINDOW_15M_S)
                        p1h = await asyncio.to_thread(_price_at, ex, symbol, str(ts_iso), WINDOW_1H_S)
                    except Exception as exc:
                        errors += 1
                        log.warning("reconcile price lookup failed id=%s symbol=%s: %s", row.get("id"), symbol, exc)
                        continue

                    if not p15 or not p1h:
                        errors += 1
                        log.warning("reconcile missing historical evidence id=%s", row.get("id"))
                        continue

                    o15 = _outcome(direction, origin, p15)
                    o1h = _outcome(direction, origin, p1h)
                    if not o15 or not o1h:
                        skipped += 1
                        continue

                    stored_outcome = row.get("outcome")
                    conflict = stored_outcome != o1h
                    if conflict:
                        audit["reconciliation_conflict"] = {
                            "stored_outcome": stored_outcome,
                            "computed_outcome_1h": o1h,
                            "detected_at": datetime.now(timezone.utc).isoformat(),
                            "action": "NO_OUTCOME_OVERWRITE",
                        }
                    else:
                        audit.pop("reconciliation_conflict", None)
                    observed_at = datetime.now(timezone.utc).isoformat()
                    audit["outcomes_dual"] = {
                        "outcome_15m": o15,
                        "outcome_1h": o1h,
                        "price_15m_later": p15,
                        "price_1h_later": p1h,
                        "primary_window": "1h",
                        "reconciled_by": "SENEX-SCORE-002",
                        "reconciled_at": observed_at,
                        "settlement_observation_v1": {
                            "version": "settlement-observation-v1",
                            "observed_at": observed_at,
                            "writer": "SENEX_SCORE_002_RECONCILER",
                            "availability_semantics": "PERSISTED_BY_COMPARE_AND_SET_AT_OR_AFTER_THIS_TIME",
                        },
                    }
                    patch = {"price_15m_later": p15, "audit": audit}
                    # Absolute URL is intentional: this client has no base_url.
                    pr = await client.patch(
                        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
                        params={
                            "id": f"eq.{row['id']}",
                            "outcome": f"eq.{stored_outcome}",
                            "audit->outcomes_dual": "is.null",
                        },
                        json=patch,
                    )
                    try:
                        body = pr.json() if pr.content else []
                    except Exception:
                        body = []
                    if pr.status_code in (200, 204) and isinstance(body, list) and body:
                        if conflict:
                            conflicts += 1
                            log.warning(
                                "reconciliation conflict id=%s stored=%s computed_1h=%s; outcome unchanged",
                                row["id"], stored_outcome, o1h,
                            )
                        else:
                            repaired += 1
                            log.info(
                                "reconciled evidence id=%s stored=%s dual15=%s dual1h=%s",
                                row["id"], stored_outcome, o15, o1h,
                            )
                    else:
                        errors += 1
                        log.error(
                            "reconcile update failed/no-op id=%s status=%s body=%r",
                            row["id"], pr.status_code, body,
                        )

                last = rows[-1]
                cursor_ts = str(last.get("ts"))
                try:
                    cursor_id = int(last.get("id"))
                except (TypeError, ValueError):
                    log.error("reconcile pagination cursor invalid id=%r", last.get("id"))
                    errors += 1
                    break
            finally:
                for ex in exchanges.values():
                    try:
                        ex.close()
                    except Exception:
                        pass

            if len(rows) < BATCH_LIMIT:
                break

    result = {
        "scanned": total_scanned,
        "repaired": repaired,
        "skipped": skipped,
        "errors": errors,
        "conflicts": conflicts,
    }
    log.info("settlement reconciliation complete: %s", result)
    return result


async def daemon() -> None:
    """Run the repair-only reconciler and fail fast on invalid configuration."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError(
            "SENEX-SCORE-002 reconciler requires SUPABASE_URL and SUPABASE_KEY"
        )
    log.info("SENEX-SCORE-002 reconciliation guard started interval=%ss", INTERVAL_S)
    HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    while True:
        result = await reconcile_once()
        HEARTBEAT_FILE.touch()
        log.info("SENEX-SCORE-002 reconciliation heartbeat updated: %s", result)
        await asyncio.sleep(INTERVAL_S)


if __name__ == "__main__":
    asyncio.run(daemon())
