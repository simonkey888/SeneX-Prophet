"""SENEX settlement reconciliation guard.

This module is NOT a second prediction/settlement authority.
It only repairs rows that already contain WIN/LOSS but lack the
single-authority `audit.outcomes_dual` evidence. The production oracle
runner remains the authority for NULL -> settled transitions.

Purpose: close the race/legacy gap where another historical writer can leave
an outcome populated without the dual 15m/1h proof required by SCORE-001.

Safety: public market reads only; paper-only; no wallet, signing or orders.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import ccxt
import httpx

log = logging.getLogger("senex.settlement_reconciler")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE", "oracle_predictions")
INTERVAL_S = int(os.environ.get("SETTLEMENT_RECONCILE_INTERVAL_SEC", "900"))
BATCH_LIMIT = int(os.environ.get("SETTLEMENT_RECONCILE_BATCH", "200"))
WINDOW_15M_S = 900
WINDOW_1H_S = 3600


def _headers() -> dict[str, str]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be provided by the runtime environment")
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


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
    candle = max(candidates, key=lambda c: c[0]) if candidates else candles[0]
    price = float(candle[4])
    return price if price > 0 else None


async def reconcile_once() -> dict[str, int]:
    """Repair already-settled rows that lack dual-window evidence."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be provided by the runtime environment")

    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=WINDOW_1H_S)).isoformat()
    async with httpx.AsyncClient(timeout=20.0, headers=_headers()) as client:
        offset = 0
        total_scanned = repaired = skipped = errors = 0
        while True:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
                params={
                    "select": "id,ts,symbol,prediction,price_now,outcome,audit,exchange_used",
                    "outcome": "in.(WIN,LOSS)",
                    "audit->outcomes_dual": "is.null",
                    "ts": f"lt.{cutoff}",
                    "order": "ts.asc,id.asc",
                    "limit": str(BATCH_LIMIT),
                    "offset": str(offset),
                },
            )
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
                    audit = row.get("audit") or {}
                    if isinstance(audit, str):
                        try:
                            audit = json.loads(audit)
                        except Exception:
                            audit = {}
                    dual = audit.get("outcomes_dual") if isinstance(audit, dict) else None
                    if isinstance(dual, dict) and dual.get("outcome_15m") in ("WIN", "LOSS") and dual.get("outcome_1h") in ("WIN", "LOSS"):
                        skipped += 1
                        continue

                    direction = (row.get("prediction") or "").upper()
                    origin = float(row.get("price_now") or 0)
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

                    p15 = await asyncio.to_thread(_price_at, ex, symbol, str(ts_iso), WINDOW_15M_S)
                    p1h = await asyncio.to_thread(_price_at, ex, symbol, str(ts_iso), WINDOW_1H_S)
                    if not p15 or not p1h:
                        errors += 1
                        continue

                    o15 = _outcome(direction, origin, p15)
                    o1h = _outcome(direction, origin, p1h)
                    if not o15 or not o1h:
                        skipped += 1
                        continue

                    audit = dict(audit) if isinstance(audit, dict) else {}
                    audit["outcomes_dual"] = {
                        "outcome_15m": o15,
                        "outcome_1h": o1h,
                        "price_15m_later": p15,
                        "price_1h_later": p1h,
                        "primary_window": "1h",
                        "reconciled_by": "SENEX-SCORE-002",
                        "reconciled_at": datetime.now(timezone.utc).isoformat(),
                    }
                    patch = {"outcome": o1h, "price_15m_later": p15, "audit": audit}
                    pr = await client.patch(
                        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
                        params={"id": f"eq.{row['id']}"},
                        json=patch,
                    )
                    try:
                        body = pr.json() if pr.content else []
                    except Exception:
                        body = []
                    if pr.status_code in (200, 204) and isinstance(body, list) and body:
                        repaired += 1
                        log.info("reconciled id=%s primary=%s dual15=%s dual1h=%s", row["id"], o1h, o15, o1h)
                    else:
                        errors += 1
                        log.error("reconcile update failed id=%s status=%s body=%r", row["id"], pr.status_code, body)
            finally:
                for ex in exchanges.values():
                    try:
                        ex.close()
                    except Exception:
                        pass

            if len(rows) < BATCH_LIMIT:
                break
            offset += len(rows)

    result = {"scanned": total_scanned, "repaired": repaired, "skipped": skipped, "errors": errors}
    log.info("settlement reconciliation complete: %s", result)
    return result


async def daemon() -> None:
    log.info("SENEX-SCORE-002 reconciliation guard started interval=%ss", INTERVAL_S)
    while True:
        try:
            await reconcile_once()
        except Exception:
            log.exception("settlement reconciliation cycle failed")
        await asyncio.sleep(INTERVAL_S)


if __name__ == "__main__":
    asyncio.run(daemon())
