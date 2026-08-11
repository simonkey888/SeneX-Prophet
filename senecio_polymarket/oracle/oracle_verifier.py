#!/usr/bin/env python3
"""
SENECIO Oracle — Standalone Verifier (ACT-XXXI PASO_2 Opción B)
================================================================

Cron-style independent verifier. Runs every 15 min (configurable via
VERIFIER_INTERVAL_SEC env var). For each Supabase row with outcome=NULL
and ts older than 15 min, fetches the OKX 15m candle that closed at
ts+15min and computes WIN/LOSS/SKIP/STALE.

DOES NOT TOUCH:
  - oracle_runner.py
  - institutional_core.py / predict_only.py / market_ev.py
  - any prediction logic

Only depends on: httpx, ccxt (already in requirements.txt), asyncio.
Speaks to Supabase via the same anon key as backend/supabase_client.py.

Usage:
  python3 oracle_verifier.py              # daemon loop (default 900s)
  python3 oracle_verifier.py --once       # single cycle (manual test)
  python3 oracle_verifier.py --once --dry # query only, no PATCH
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

import httpx

try:
    import ccxt.async_support as ccxt_async  # type: ignore
except ImportError:  # pragma: no cover
    ccxt_async = None  # type: ignore

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE", "oracle_predictions")

VERIFIER_INTERVAL_SEC = int(os.environ.get("VERIFIER_INTERVAL_SEC", "900"))
VERIFICATION_WINDOW_MIN = int(os.environ.get("VERIFIER_WINDOW_MIN", "15"))
MAX_AGE_MIN = int(os.environ.get("VERIFIER_MAX_AGE_MIN", "1440"))  # 24h
OKX_REQUEST_DELAY = float(os.environ.get("OKX_REQUEST_DELAY", "0.3"))
BATCH_LIMIT = int(os.environ.get("VERIFIER_BATCH_LIMIT", "50"))

# ACT-XXXII Fix3 — checkpoint in Supabase for crash recovery.
# Table is OPTIONAL: if it doesn't exist (HTTP 404 / PGRST205), checkpoint
# load/save is silently skipped and the verifier runs as before. Create with:
#   CREATE TABLE public.oracle_state (
#     key text PRIMARY KEY,
#     value jsonb NOT NULL DEFAULT '{}'::jsonb,
#     updated_at timestamptz NOT NULL DEFAULT now()
#   );
#   ALTER TABLE public.oracle_state ENABLE ROW LEVEL SECURITY;
#   CREATE POLICY "anon_read"  ON public.oracle_state FOR SELECT TO anon USING (true);
#   CREATE POLICY "anon_write" ON public.oracle_state FOR INSERT TO anon WITH CHECK (true);
#   CREATE POLICY "anon_upd"   ON public.oracle_state FOR UPDATE TO anon USING (true) WITH CHECK (true);
CHECKPOINT_TABLE = os.environ.get("CHECKPOINT_TABLE", "oracle_state")
CHECKPOINT_KEY = "verifier_state"

DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("oracle_verifier")


# ---------------------------------------------------------------------------
# Supabase REST helpers (no extra dep — direct PATCH on PostgREST)
# ---------------------------------------------------------------------------
def _sb_headers(prefer: str = "return=representation") -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


async def sb_fetch_pending(client: httpx.AsyncClient, limit: int = BATCH_LIMIT) -> list[dict]:
    """Fetch predictions with outcome=NULL ordered by ts asc (oldest first)."""
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(minutes=VERIFICATION_WINDOW_MIN)).isoformat()
    # PostgREST filter: outcome=is.null AND ts=lt.<cutoff>
    params = {
        "select": "id,ts,symbol,prediction,price_now,exchange_used,audit",
        "outcome": "is.null",
        "ts": f"lt.{cutoff_iso}",
        "order": "ts.asc",
        "limit": str(limit),
    }
    r = await client.get(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
        params=params,
        headers=_sb_headers(),
        timeout=20.0,
    )
    if r.status_code != 200:
        log.error("sb_fetch_pending %s: %s", r.status_code, r.text[:300])
        return []
    data = r.json()
    return data if isinstance(data, list) else []


async def sb_patch_outcome(
    client: httpx.AsyncClient,
    row_id: int,
    outcome: str,
    price_15m_later: Optional[float],
) -> bool:
    """PATCH a single row with outcome + price_15m_later. Returns True on success."""
    payload = {"outcome": outcome}
    if price_15m_later is not None:
        payload["price_15m_later"] = float(price_15m_later)
    r = await client.patch(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id=eq.{row_id}",
        json=payload,
        headers=_sb_headers(),
        timeout=20.0,
    )
    if r.status_code not in (200, 204):
        log.error("sb_patch_outcome id=%s %s: %s", row_id, r.status_code, r.text[:300])
        return False
    return True


# ---------------------------------------------------------------------------
# OKX historical price fetch
# ---------------------------------------------------------------------------
def _normalize_symbol(s: str) -> str:
    """Convert 'BTCUSDT' or 'BTC/USDT' → 'BTC/USDT' (ccxt unified)."""
    s = (s or "").upper().strip()
    if "/" in s:
        return s
    # Try common quote currencies
    for quote in ("USDT", "USDC", "USD", "BUSD", "PERP"):
        if s.endswith(quote):
            base = s[: -len(quote)]
            if base:
                return f"{base}/{quote}"
    return s  # fall through; ccxt will reject


async def fetch_price_at(
    symbol: str, target_ts: datetime, exchange_used: Optional[str] = None
) -> Optional[float]:
    """Fetch the close price of the 15m OKX candle that contains target_ts.

    Strategy: pull 5 candles of 15m timeframe ending around target_ts, find the
    candle whose [open_time, open_time+15min) window contains target_ts, return
    its close. If exact candle not found, fall back to the closest candle's close.
    """
    if ccxt_async is None:
        log.error("ccxt not available — cannot fetch historical price")
        return None

    sym = _normalize_symbol(symbol)
    target_ms = int(target_ts.timestamp() * 1000)

    # Prefer the exchange recorded at prediction time if it's in our supported set
    exchange_name = "okx"
    if exchange_used and exchange_used.lower() in ("okx", "kraken", "gate", "mexc", "bitget"):
        # Use the original exchange when possible (consistency with price_now)
        exchange_name = exchange_used.lower()

    ex = getattr(ccxt_async, exchange_name)({"enableRateLimit": True})
    try:
        # Fetch 15m candles: 5 candles ending just after target_ts+15min buffer
        since_ms = target_ms - 60 * 60 * 1000  # 1h before target
        ohlcv = await ex.fetch_ohlcv(sym, timeframe="15m", since=since_ms, limit=10)
        if not ohlcv:
            log.warning("no ohlcv returned for %s @ %s", sym, target_ts.isoformat())
            return None

        # Find candle containing target_ts: open_time <= target < open_time+15min
        candle_ms = 15 * 60 * 1000
        for candle in ohlcv:
            open_t, _o, _h, _l, close, _v = candle
            if open_t <= target_ms < open_t + candle_ms:
                return float(close)
        # Fallback: closest candle by open time
        closest = min(ohlcv, key=lambda c: abs(c[0] - target_ms))
        log.info(
            "exact candle not found for %s @ %s — using closest open_t=%s delta=%dms",
            sym, target_ts.isoformat(), closest[0], abs(closest[0] - target_ms),
        )
        return float(closest[4])
    except Exception as e:
        log.error("fetch_price_at %s @ %s failed: %s", sym, target_ts.isoformat(), e)
        return None
    finally:
        await ex.close()


# ---------------------------------------------------------------------------
# ACT-XXXII Fix3 — Checkpoint helpers (graceful: table is optional)
# ---------------------------------------------------------------------------
_checkpoint_warned = False  # one-shot warning if table missing


async def sb_load_checkpoint(client: httpx.AsyncClient) -> Optional[dict]:
    """Load last verifier checkpoint. Returns None if table missing or empty."""
    global _checkpoint_warned
    try:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/{CHECKPOINT_TABLE}",
            params={"key": f"eq.{CHECKPOINT_KEY}", "limit": "1"},
            headers=_sb_headers(),
            timeout=10.0,
        )
        if r.status_code == 404 or "PGRST205" in r.text:
            if not _checkpoint_warned:
                log.warning(
                    "checkpoint table %s missing — run without crash-recovery "
                    "(see SQL in oracle_verifier.py header to enable)",
                    CHECKPOINT_TABLE,
                )
                _checkpoint_warned = True
            return None
        if r.status_code != 200:
            log.warning("checkpoint load status=%s body=%s", r.status_code, r.text[:200])
            return None
        data = r.json() or []
        if not data:
            return None
        v = data[0].get("value")
        return v if isinstance(v, dict) else None
    except Exception as e:
        log.warning("checkpoint load error: %s", e)
        return None


async def sb_save_checkpoint(client: httpx.AsyncClient, state: dict) -> bool:
    """Upsert verifier checkpoint. Returns False if table missing (non-fatal)."""
    global _checkpoint_warned
    try:
        payload = {
            "key": CHECKPOINT_KEY,
            "value": state,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        r = await client.post(
            f"{SUPABASE_URL}/rest/v1/{CHECKPOINT_TABLE}",
            json=payload,
            headers=_sb_headers(prefer="return=representation,resolution=merge-duplicates"),
            timeout=10.0,
        )
        if r.status_code in (200, 201):
            return True
        if r.status_code == 404 or "PGRST205" in r.text:
            if not _checkpoint_warned:
                log.warning(
                    "checkpoint table %s missing — skip save (non-fatal). "
                    "Run SQL in oracle_verifier.py header to enable.",
                    CHECKPOINT_TABLE,
                )
                _checkpoint_warned = True
            return False
        log.warning("checkpoint save status=%s body=%s", r.status_code, r.text[:200])
        return False
    except Exception as e:
        log.warning("checkpoint save error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Core verifier logic
# ---------------------------------------------------------------------------
def compute_outcome(direction: str, price_now: float, price_then: float) -> str:
    """Map (direction, price_change) → WIN/LOSS/SKIP."""
    direction = (direction or "").upper()
    if direction in ("FLAT", "NEUTRAL", ""):
        return "SKIP"
    if price_now <= 0:
        return "SKIP"
    if direction == "LONG":
        return "WIN" if price_then > price_now else "LOSS"
    if direction == "SHORT":
        return "WIN" if price_then < price_now else "LOSS"
    return "SKIP"


async def verify_one(
    client: httpx.AsyncClient, row: dict, dry_run: bool
) -> Tuple[str, Optional[float]]:
    """Process a single pending row. Returns (outcome, price_15m_later)."""
    row_id = row.get("id")
    ts_str = row.get("ts")
    symbol = row.get("symbol", "BTC/USDT")
    direction = (row.get("prediction") or "").upper()
    price_now = float(row.get("price_now") or 0.0)
    exchange_used = row.get("exchange_used")

    try:
        pred_ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
    except Exception as e:
        log.error("id=%s unparseable ts=%s: %s", row_id, ts_str, e)
        outcome = "STALE"
        await sb_patch_outcome(client, row_id, outcome, None) if not dry_run else None
        return outcome, None

    now = datetime.now(timezone.utc)
    age_min = (now - pred_ts).total_seconds() / 60.0
    target_ts = pred_ts + timedelta(minutes=15)

    # Too old → STALE (don't keep re-trying forever)
    if age_min > MAX_AGE_MIN:
        log.info("id=%s STALE age=%.1fmin > %dmin", row_id, age_min, MAX_AGE_MIN)
        if not dry_run:
            await sb_patch_outcome(client, row_id, "STALE", None)
        return "STALE", None

    price_then = await fetch_price_at(symbol, target_ts, exchange_used=exchange_used)
    if price_then is None:
        log.warning("id=%s no price — leaving NULL for next cycle", row_id)
        return "PENDING", None

    outcome = compute_outcome(direction, price_now, price_then)
    pct = ((price_then - price_now) / price_now * 100) if price_now > 0 else 0.0
    log.info(
        "id=%s %s price_now=%.2f price_15m=%.2f Δ=%+.3f%% → %s",
        row_id, direction, price_now, price_then, pct, outcome,
    )
    if not dry_run:
        ok = await sb_patch_outcome(client, row_id, outcome, price_then)
        if not ok:
            return "PATCH_FAIL", price_then
    return outcome, price_then


async def run_cycle(
    dry_run: bool = False,
    cycle_num: int = 0,
    prior_checkpoint: Optional[dict] = None,
) -> dict:
    """Run one verification cycle. Returns summary dict.

    ACT-XXXII Fix3: tracks last_resolved_id and saves a checkpoint to
    oracle_state at the end of each cycle. Checkpoint save is best-effort
    (non-fatal if table missing).
    """
    started = time.time()
    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "cycle_num": cycle_num,
        "fetched": 0,
        "patched": 0,
        "win": 0,
        "loss": 0,
        "skip": 0,
        "stale": 0,
        "pending": 0,
        "errors": 0,
        "last_resolved_id": prior_checkpoint.get("last_resolved_id") if prior_checkpoint else None,
    }

    async with httpx.AsyncClient() as client:
        pending = await sb_fetch_pending(client, limit=BATCH_LIMIT)
        summary["fetched"] = len(pending)
        log.info(
            "cycle %d start: %d pending rows (dry_run=%s, prior_last_id=%s)",
            cycle_num, len(pending), dry_run, summary["last_resolved_id"],
        )

        for row in pending:
            try:
                outcome, _ = await verify_one(client, row, dry_run)
                if outcome == "WIN":
                    summary["win"] += 1
                elif outcome == "LOSS":
                    summary["loss"] += 1
                elif outcome == "SKIP":
                    summary["skip"] += 1
                elif outcome == "STALE":
                    summary["stale"] += 1
                elif outcome == "PENDING":
                    summary["pending"] += 1
                else:
                    summary["errors"] += 1
                if outcome in ("WIN", "LOSS", "SKIP", "STALE"):
                    summary["patched"] += 1
                    rid = row.get("id")
                    if isinstance(rid, int) and (
                        summary["last_resolved_id"] is None
                        or rid > summary["last_resolved_id"]
                    ):
                        summary["last_resolved_id"] = rid
                await asyncio.sleep(OKX_REQUEST_DELAY)
            except Exception as e:
                # ACT-XXXII Fix2 — per-row try/except: one bad row never kills the batch.
                log.error("id=%s unexpected error: %s", row.get("id"), e)
                summary["errors"] += 1

        # ACT-XXXII Fix3 — save checkpoint (best-effort, non-fatal)
        if not dry_run:
            ck = {
                "last_resolved_id": summary["last_resolved_id"],
                "last_cycle_at": datetime.now(timezone.utc).isoformat(),
                "cycles_run": cycle_num,
                "last_summary": {
                    k: v for k, v in summary.items()
                    if k not in ("last_summary",)
                },
            }
            try:
                await sb_save_checkpoint(client, ck)
            except Exception as cke:
                log.warning("checkpoint save crashed (non-fatal): %s", cke)

    summary["duration_sec"] = round(time.time() - started, 2)
    log.info(
        "cycle %d done: fetched=%d patched=%d W=%d L=%d SKIP=%d STALE=%d PENDING=%d ERR=%d last_id=%s (%.2fs)",
        cycle_num, summary["fetched"], summary["patched"], summary["win"], summary["loss"],
        summary["skip"], summary["stale"], summary["pending"], summary["errors"],
        summary["last_resolved_id"], summary["duration_sec"],
    )
    return summary


async def daemon_loop():
    """ACT-XXXII: load checkpoint on start, increment cycle_num, save after each cycle."""
    log.info("oracle_verifier daemon starting — interval=%ds", VERIFIER_INTERVAL_SEC)

    # Load prior checkpoint to know where we left off (best-effort)
    prior_ckpt = None
    async with httpx.AsyncClient() as client:
        prior_ckpt = await sb_load_checkpoint(client)
    if prior_ckpt:
        log.info(
            "checkpoint recovered: last_resolved_id=%s cycles_run=%s last_cycle_at=%s",
            prior_ckpt.get("last_resolved_id"),
            prior_ckpt.get("cycles_run"),
            prior_ckpt.get("last_cycle_at"),
        )
    else:
        log.info("no prior checkpoint — starting fresh (cycle_num=1)")

    start_cycle = (prior_ckpt.get("cycles_run") or 0) + 1 if prior_ckpt else 1

    cycle_num = start_cycle
    while True:
        try:
            await run_cycle(
                dry_run=False,
                cycle_num=cycle_num,
                prior_checkpoint=prior_ckpt,
            )
            cycle_num += 1
        except Exception as e:
            log.error("daemon cycle crashed (will retry): %s", e)
        await asyncio.sleep(VERIFIER_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="run single cycle then exit")
    p.add_argument("--dry", action="store_true", help="do not PATCH — query only")
    args = p.parse_args()

    if args.once:
        summary = asyncio.run(run_cycle(dry_run=args.dry))
        print("\n=== CYCLE SUMMARY ===")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        sys.exit(0 if summary["errors"] == 0 else 1)
    else:
        asyncio.run(daemon_loop())


if __name__ == "__main__":
    main()
