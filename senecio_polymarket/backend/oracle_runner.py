"""
SENECIO ORACLE — Real Oracle Runner (ACT XXIII)
================================================

Bridges the FastAPI dashboard with the REAL oracle pipeline (predict_only.py).

ACT XXIII changes:
  - Verifier upgraded from 15min → 1h primary window (gating source of truth)
  - Dual-window settlement: both outcome_15m AND outcome_1h stored in audit jsonb
  - Directional gate logic: LONG ≥50% n≥30, SHORT ≥55% n≥30, global ≥52% n≥100
  - SHORT_ONLY_PAPER_MODE flag emitted when LONG fails gate but SHORT passes
  - Backfill routine now computes both 15m and 1h outcomes for already-settled rows
  - No live capital — paper trading only (directive 5)

Responsibilities:
  1. On startup: count existing predictions in the seed file
  2. Every 15 min: call predict_only.fetch_market_snapshot + run_prediction
     for ETH/USDT and BTC/USDT, append to predictions.jsonl
  3. Expose state: last_prediction_ts, predictions_count, last_prediction
  4. Every cycle: run dual-window verifier (1h gate + 15m research)

This module does NOT touch the demo scheduler (which still powers the live
dashboard panels with synthetic ticks). Both run in parallel.

Memory budget: ccxt + SDC + predict_only ~ 50-80MB. Fits in 256MB.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from .authoritative_score import build_authoritative_score
from .settlement_proof import is_proof_qualified

# Make oracle modules importable
ORACLE_DIR = Path(__file__).resolve().parent.parent / "oracle"
sys.path.insert(0, str(ORACLE_DIR))

log = logging.getLogger("senecio.oracle_runner")

# Path to predictions JSONL — must match predict_only.DEFAULT_PREDICTIONS_PATH
PREDICTIONS_PATH = ORACLE_DIR / "senecio_output" / "predictions.jsonl"

# Runtime state (read by /api/health and /api/oracle/*)
_state: dict[str, Any] = {
    "started_at": None,
    "last_prediction_ts": None,
    "last_prediction_symbol": None,
    "last_prediction_result": None,   # cleaned (no _audit) dict
    "predictions_count": 0,
    "cycles_run": 0,
    "cycles_failed": 0,
    "last_error": None,
    "last_cycle_at": None,
    "next_cycle_at": None,
    "exchange_used_last": None,
    # Outcome verifier state (ACT XXI)
    "last_verify_at": None,
    "last_verify_count": None,        # how many outcomes were settled in last run
    "last_verify_ids": [],            # ids settled in last run (for debug, capped at 10)
    "verified_total": 0,              # raw cross-symbol diagnostic count only
    "verified_total_diagnostic_only": True,
    "verified_total_scope": "CROSS_SYMBOL_RAW_PROOF_QUALIFIED",
    # ACT-XXII-prereq: bogus-outcome backfill state
    "bogus_backfill_done": False,     # set True after _backfill_bogus_outcomes() runs once
    "bogus_backfill_count": None,     # how many rows re-settled with historical price
    "bogus_backfill_errors": None,    # how many rows we couldn't re-settle (no historical price)
    # AUD-057: proof-qualified directional stats and gates are symbol-scoped.
    # Any cross-symbol view is explicitly diagnostic and must never configure
    # a symbol-specific score, Kelly input, or PAPER portfolio gate.
    "directional_stats": {
        "per_symbol": {},
        "aggregate_diagnostic": {
            "diagnostic_only": True,
            "scope": "CROSS_SYMBOL_RAW_PROOF_QUALIFIED",
            "by_window": {
                "15m": {"LONG": {}, "SHORT": {}, "FLAT": {}, "global": {}},
                "1h": {"LONG": {}, "SHORT": {}, "FLAT": {}, "global": {}},
            },
        },
    },
    "trade_mode": "PAPER",            # ACT XXIII directive 5: never "LIVE" until long side improves
    "live_capital_locked": True,      # Hard guard — even if gates pass, do NOT unlock real money
}

# Cycle config
CYCLE_INTERVAL_S = 900  # 15 minutes
SYMBOLS = ["ETH/USDT", "BTC/USDT"]
TIMEFRAME = "15m"
INITIAL_DELAY_S = 30    # wait for uvicorn + scheduler to stabilize
MAX_CONCURRENT_PREDICTIONS = 1  # serialize to keep memory bounded

# ACT XXIII: settlement windows (seconds after prediction ts)
WINDOW_15M_S = 900
WINDOW_1H_S = 3600
PRIMARY_WINDOW = "1h"   # gating source of truth per ACT XXIII directive 1


def _normalize_symbol(value: Any) -> str:
    """Normalize runtime symbols for proof/gate/portfolio isolation."""
    return str(value or "").upper().replace("/", "").replace("-", "").strip()


def _count_predictions() -> int:
    """Count existing lines in predictions.jsonl (seed from repo + runtime)."""
    try:
        if not PREDICTIONS_PATH.exists():
            return 0
        with open(PREDICTIONS_PATH, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except Exception as e:
        log.warning("failed to count predictions: %s", e)
        return 0


def _get_last_prediction() -> Optional[dict]:
    """Return the last line of predictions.jsonl as dict, or None."""
    try:
        if not PREDICTIONS_PATH.exists():
            return None
        last = None
        with open(PREDICTIONS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line)
                except Exception:
                    continue
        return last
    except Exception:
        return None


def _seed_state_from_existing() -> None:
    """On startup, populate state from existing predictions.jsonl (seed from repo)."""
    _state["predictions_count"] = _count_predictions()
    last = _get_last_prediction()
    if last:
        _state["last_prediction_ts"] = last.get("timestamp")
        _state["last_prediction_symbol"] = last.get("symbol")
        _state["last_prediction_result"] = {k: v for k, v in last.items() if not k.startswith("_")}
        _state["exchange_used_last"] = last.get("_audit", {}).get("exchange_used") or last.get("exchange_used")
    log.info(
        "oracle_runner seeded: count=%d last_ts=%s",
        _state["predictions_count"], _state["last_prediction_ts"],
    )


def get_state() -> dict[str, Any]:
    """Public accessor for /api/health and /api/oracle/state."""
    return dict(_state)


async def _run_one_prediction(symbol: str) -> Optional[dict]:
    """Run a single prediction for a symbol. Returns the prediction dict or None."""
    # Import inside the function so module load is cheap and errors are isolated
    try:
        from predict_only import fetch_market_snapshot, run_prediction, log_prediction, check_candle_duplicate
    except Exception as e:
        log.exception("failed to import predict_only: %s", e)
        _state["last_error"] = f"import_error: {e}"
        _state["cycles_failed"] += 1
        return None

    try:
        log.info("fetching market snapshot for %s @ %s", symbol, TIMEFRAME)
        market_data = await asyncio.to_thread(fetch_market_snapshot, symbol, TIMEFRAME)
        if not market_data:
            log.warning("no market data for %s", symbol)
            _state["last_error"] = f"no_market_data: {symbol}"
            _state["cycles_failed"] += 1
            return None

        # Check for candle duplicate (avoid logging same 15m candle twice)
        candle_ts = market_data.get("candle_ts", 0)
        if candle_ts and check_candle_duplicate(candle_ts, str(PREDICTIONS_PATH), symbol):
            log.info("skip duplicate candle_ts=%s for %s", candle_ts, symbol)
            return None

        # Run the pipeline (CPU-bound, run in thread)
        prediction = await asyncio.to_thread(run_prediction, market_data)
        if not prediction:
            log.warning("no prediction produced for %s", symbol)
            _state["last_error"] = f"no_prediction: {symbol}"
            _state["cycles_failed"] += 1
            return None

        # Tag with exchange used (extract from market_data)
        exchange_used = market_data.get("exchange_used") or "unknown"
        prediction["exchange_used"] = exchange_used
        if "_audit" in prediction:
            prediction["_audit"]["exchange_used"] = exchange_used

        # SCORE-002: immutable origin witness captured at prediction creation.
        audit = prediction.setdefault("_audit", {})
        if not isinstance(audit, dict):
            audit = {}
            prediction["_audit"] = audit
        audit["origin_price_v1"] = {
            "version": "origin-price-v1",
            "price": float(prediction.get("price_now") or 0),
            "timestamp": prediction.get("timestamp"),
            "source": exchange_used,
        }

        # ── ACT FINAL_AUDIT (A2) — STRICT_ADDITIVE audit enrichment ──
        # Adds 30+ derived fields to _audit.enriched (hour, weekday, session,
        # regime, microstructure, hashes, etc.). Never modifies the prediction
        # itself or any existing _audit sub-dict. Never raises.
        try:
            from . import audit_enrichment
            audit_enrichment.enrich_prediction(
                prediction,
                runtime_meta={
                    "execution_model": "PAPER",
                    "latency_ms": None,
                    "slippage_bps": None,
                },
            )
        except Exception as enrich_err:
            log.warning("audit_enrichment failed (continuing): %s", enrich_err)

        # Persist
        await asyncio.to_thread(log_prediction, prediction, str(PREDICTIONS_PATH))

        # Dual-write to Supabase (best-effort — failure doesn't block the cycle)
        try:
            from . import supabase_client
            sb_row = await supabase_client.insert_prediction(prediction)
            if sb_row:
                log.info("supabase insert OK id=%s", sb_row.get("id"))
                # Attach the Supabase row id back onto the prediction dict so
                # the portfolio coordinator can use it as prediction_id FK.
                prediction["id"] = sb_row.get("id")
            else:
                log.warning("supabase insert returned None — predictions.jsonl is source of truth")
        except Exception as sb_err:
            log.warning("supabase insert failed (continuing): %s", sb_err)

        # Update runtime state
        _state["last_prediction_ts"] = prediction.get("timestamp")
        _state["last_prediction_symbol"] = prediction.get("symbol")
        _state["last_prediction_result"] = {k: v for k, v in prediction.items() if not k.startswith("_")}
        _state["predictions_count"] += 1
        _state["exchange_used_last"] = exchange_used
        _state["last_error"] = None

        # ACT-XXV: Route prediction through the institutional portfolio
        # pipeline (PortfolioEngine → RiskKernel → ExecutionEngine → Journal
        # → ShadowLive). This is ADDITIVE — the prediction model, feature
        # engineering, signal generation, and verifier are NOT touched.
        try:
            await _route_to_portfolio(prediction, market_data)
        except Exception as pe_err:
            log.warning("portfolio routing failed (non-fatal): %s", pe_err)

        log.info(
            "prediction logged: %s %s conf=%.4f ev=%.8f price=%s exchange=%s",
            prediction.get("symbol"),
            prediction.get("prediction"),
            prediction.get("confidence", 0),
            prediction.get("ev", 0),
            prediction.get("price_now"),
            exchange_used,
        )
        return prediction

    except Exception as e:
        log.exception("prediction cycle failed for %s: %s", symbol, e)
        _state["last_error"] = f"cycle_error: {symbol}: {e}"
        _state["cycles_failed"] += 1
        return None


async def _fetch_current_price(symbol: str) -> Optional[float]:
    """Fetch the latest price for a symbol via ccxt (OKX public ticker).

    Lightweight: only fetches ticker (no OHLCV/orderbook), so ~10x faster than
    full fetch_market_snapshot. Used for live-cycle settlement (predictions
    whose 15min window just elapsed — close enough to "now").

    Args:
        symbol: e.g. "ETH/USDT" (ccxt format with slash)
    Returns:
        Last price as float, or None on failure.
    """
    def _fetch() -> Optional[float]:
        try:
            import ccxt
            ex = ccxt.okx({"enableRateLimit": True})
            t = ex.fetch_ticker(symbol)
            return float(t.get("last") or 0) or None
        except Exception as e:
            log.warning("ccxt fetch_ticker failed for %s: %s", symbol, e)
            return None
    return await asyncio.to_thread(_fetch)


async def _fetch_price_evidence_at_time(
    symbol: str,
    ts_iso: str,
    window_seconds: int,
    exchange_name: str,
) -> Optional[dict[str, Any]]:
    """Fetch bounded same-source historical evidence for an exact target."""
    from .settlement_contract import fetch_historical_price_evidence
    return await asyncio.to_thread(
        fetch_historical_price_evidence,
        exchange_name,
        symbol,
        ts_iso,
        window_seconds,
    )


async def _fetch_price_at_time(symbol: str, ts_iso: str, window_seconds: int = WINDOW_15M_S) -> Optional[float]:
    """Backward-compatible OKX helper; authoritative verifier uses evidence API."""
    evidence = await _fetch_price_evidence_at_time(symbol, ts_iso, window_seconds, "okx")
    return float(evidence["price"]) if evidence else None

def _outcome_for_direction(direction: str, price_now: float, price_later: float) -> Optional[str]:
    from .settlement_contract import directional_outcome
    return directional_outcome(direction, price_now, price_later)

async def _verify_pending_outcomes() -> int:
    """Settle one bounded, starvation-safe page of directional predictions."""
    try:
        from . import supabase_client
        from .settlement_contract import normalize_exchange, normalize_symbol, target_epoch_ms
    except Exception as e:
        log.warning("settlement dependencies unavailable, skipping verifier: %s", e)
        return 0

    try:
        pending = await supabase_client.fetch_pending_outcomes(
            older_than_seconds=WINDOW_1H_S, limit=100
        )
        scan = supabase_client.get_pending_scan_diagnostics()
    except Exception as e:
        log.exception("fetch_pending_outcomes failed: %s", e)
        return 0

    _state["eligible_directional_pending_count"] = scan.get("eligible_directional_pending_count")
    _state["oldest_eligible_directional_pending_id"] = scan.get("oldest_eligible_directional_pending_id")
    _state["oldest_eligible_directional_pending_ts"] = scan.get("oldest_eligible_directional_pending_ts")
    _state["last_verify_rows_scanned"] = scan.get("rows_scanned_last_pass", len(pending))
    _state["last_verify_scan_cap_hit"] = bool(scan.get("scan_cap_hit"))
    _state["last_verify_cursor"] = scan.get("cursor_after")
    _state["last_verify_scan_pass_complete"] = bool(scan.get("pass_complete"))
    oldest_ts = scan.get("oldest_eligible_directional_pending_ts")
    try:
        oldest_dt = datetime.fromisoformat(str(oldest_ts).replace("Z", "+00:00"))
        _state["oldest_eligible_directional_pending_age_seconds"] = max(
            0.0, (datetime.now(timezone.utc) - oldest_dt).total_seconds()
        )
    except Exception:
        _state["oldest_eligible_directional_pending_age_seconds"] = None

    settled = 0
    settled_ids: list[int] = []
    unresolved_proof = 0
    unresolved_price = 0
    cas_conflicts = 0
    cache: dict[tuple[str, str, int, int], Optional[dict[str, Any]]] = {}

    for row in pending:
        pred_id = row.get("id")
        direction = str(row.get("prediction") or "").upper()
        if direction not in {"LONG", "SHORT"}:
            unresolved_proof += 1
            continue
        ts_iso = row.get("ts")
        try:
            price_now = float(row.get("price_now") or 0)
        except (TypeError, ValueError):
            price_now = 0.0
        if not ts_iso or price_now <= 0:
            unresolved_proof += 1
            continue

        audit = row.get("audit") if isinstance(row.get("audit"), dict) else {}
        origin = audit.get("origin_price_v1") if isinstance(audit, dict) else None
        row_source = normalize_exchange(row.get("exchange_used"))
        if (
            not isinstance(origin, dict)
            or origin.get("version") != "origin-price-v1"
            or row_source is None
            or normalize_exchange(origin.get("source")) != row_source
        ):
            unresolved_proof += 1
            continue

        symbol = normalize_symbol(row.get("symbol"))
        evidence: dict[str, Optional[dict[str, Any]]] = {}
        for name, window_s in (("15m", WINDOW_15M_S), ("1h", WINDOW_1H_S)):
            target = target_epoch_ms(ts_iso, window_s)
            if target is None:
                evidence[name] = None
                continue
            key = (row_source, symbol, target, window_s)
            if key not in cache:
                cache[key] = await _fetch_price_evidence_at_time(
                    symbol, str(ts_iso), window_s, row_source
                )
                await asyncio.sleep(0.05)
            evidence[name] = cache[key]

        ev15 = evidence.get("15m")
        ev1h = evidence.get("1h")
        if not ev15 or not ev1h:
            unresolved_price += 1
            continue
        price_15m = float(ev15["price"])
        price_1h = float(ev1h["price"])
        outcome_15m = _outcome_for_direction(direction, price_now, price_15m)
        outcome_1h = _outcome_for_direction(direction, price_now, price_1h)
        if outcome_15m is None or outcome_1h is None:
            unresolved_proof += 1
            continue

        ok = await supabase_client.update_outcome_dual(
            prediction_id=pred_id,
            outcome_15m=outcome_15m,
            outcome_1h=outcome_1h,
            price_15m_later=price_15m,
            price_1h_later=price_1h,
            primary_window=PRIMARY_WINDOW,
            price_evidence_15m=ev15,
            price_evidence_1h=ev1h,
        )
        if ok:
            settled += 1
            if len(settled_ids) < 10:
                settled_ids.append(pred_id)
        else:
            cas_conflicts += 1
        await asyncio.sleep(0.02)

    _state["last_verify_at"] = datetime.now(timezone.utc).isoformat()
    _state["last_verify_count"] = settled
    _state["last_verify_ids"] = settled_ids
    _state["verified_total"] = (_state.get("verified_total") or 0) + settled
    _state["last_verify_unresolved_proof"] = unresolved_proof
    _state["last_verify_unresolved_price"] = unresolved_price
    _state["last_verify_cas_conflicts"] = cas_conflicts
    _state["last_verify_unresolved_due_price_or_proof"] = unresolved_proof + unresolved_price
    if pending and settled == 0:
        if unresolved_proof:
            reason = "ELIGIBLE_PENDING_MISSING_CAUSAL_PROOF"
        elif unresolved_price:
            reason = "ELIGIBLE_PENDING_HISTORICAL_PRICE_UNAVAILABLE"
        elif cas_conflicts:
            reason = "ELIGIBLE_PENDING_CAS_CONFLICT_OR_ALREADY_SETTLED"
        else:
            reason = "ELIGIBLE_PENDING_NO_PROGRESS"
    elif not pending and scan.get("eligible_directional_pending_count"):
        reason = "SCAN_PASS_BOUNDARY_CURSOR_RESET"
    else:
        reason = None
    _state["last_verify_no_progress_reason"] = reason

    log.info(
        "verifier aud063: scanned=%d settled=%d proof_unresolved=%d price_unresolved=%d cas=%d cap=%s",
        len(pending), settled, unresolved_proof, unresolved_price, cas_conflicts,
        bool(scan.get("scan_cap_hit")),
    )
    await _refresh_directional_stats()
    if settled > 0:
        try:
            from .forensics import pipeline as forensics_pipeline
            asyncio.create_task(forensics_pipeline.run_pipeline_async())
        except Exception as f_err:
            log.warning("forensics pipeline scheduling failed (continuing): %s", f_err)
    return settled

async def _backfill_bogus_outcomes() -> int:
    """Re-settle predictions whose outcome was computed with the buggy
    current-price verifier (before ACT-XXII-prereq), AND upgrade them to
    dual-window outcomes (15m + 1h) per ACT XXIII directive 1.

    The bug: _fetch_current_price() returned the spot price AT VERIFIER RUNTIME
    instead of the historical close at ts+15min. This meant all predictions
    got the same price_15m_later, conflating a multi-hour trend with 15min
    directional accuracy.

    ACT XXIII upgrade: now also fetches ts+1h close and stores both outcomes
    in audit.outcomes_dual. The primary `outcome` column is set to outcome_1h
    (the gating source of truth per directive 1).

    Triggered once on startup (when bogus_backfill_done != True).
    Marks _state['bogus_backfill_done']=True when complete.

    Returns the number of outcomes re-settled.
    """
    if _state.get("bogus_backfill_done"):
        return 0

    try:
        from . import supabase_client
    except Exception as e:
        log.warning("supabase_client unavailable for backfill: %s", e)
        return 0

    try:
        # Fetch ALL predictions that already have WIN/LOSS — those are the
        # ones that may have been settled with the buggy current-price logic.
        # We re-fetch their historical price (15m AND 1h) and recompute outcomes.
        rows = await supabase_client.fetch_predictions(limit=500)
        to_resettle = [
            r for r in rows
            if r.get("outcome") in ("WIN", "LOSS")
            and r.get("ts")
            and r.get("price_now")
        ]
    except Exception as e:
        log.exception("backfill fetch failed: %s", e)
        return 0

    if not to_resettle:
        log.info("backfill: no WIN/LOSS rows to re-settle")
        _state["bogus_backfill_done"] = True
        _state["bogus_backfill_count"] = 0
        await _refresh_directional_stats()
        return 0

    log.info(
        "backfill: re-settling %d outcomes with dual-window historical prices",
        len(to_resettle),
    )

    resettled = 0
    errors = 0
    cache: dict[tuple[str, int, int], Optional[float]] = {}

    for row in to_resettle:
        pred_id = row.get("id")
        sym_raw = row.get("symbol", "")
        sym_ccxt = sym_raw[:3] + "/" + sym_raw[3:] if len(sym_raw) >= 6 else sym_raw
        direction = (row.get("prediction") or "").upper()
        price_now = float(row.get("price_now") or 0)
        ts_iso = str(row.get("ts"))

        try:
            ts_dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        except Exception as e:
            log.warning("backfill: cannot parse ts=%s id=%s: %s", ts_iso, pred_id, e)
            errors += 1
            continue

        # Fetch prices at both windows (cached per symbol+minute+window)
        prices: dict[str, Optional[float]] = {}
        for window_name, window_s in (("15m", WINDOW_15M_S), ("1h", WINDOW_1H_S)):
            settle_dt = ts_dt + timedelta(seconds=window_s)
            settle_minute_ms = int(settle_dt.timestamp() // 60 * 60 * 1000)
            cache_key = (sym_ccxt, settle_minute_ms, window_s)
            if cache_key in cache:
                prices[window_name] = cache[cache_key]
            else:
                p = await _fetch_price_at_time(sym_ccxt, ts_iso, window_seconds=window_s)
                cache[cache_key] = p
                prices[window_name] = p
                await asyncio.sleep(0.3)

        price_15m = prices.get("15m")
        price_1h = prices.get("1h")

        if not price_15m or price_15m <= 0:
            log.warning(
                "backfill: no 15m price for %s id=%s, leaving as-is",
                sym_ccxt, pred_id,
            )
            errors += 1
            continue
        if not price_1h or price_1h <= 0:
            log.warning(
                "backfill: no 1h price for %s id=%s, leaving as-is",
                sym_ccxt, pred_id,
            )
            errors += 1
            continue

        outcome_15m = _outcome_for_direction(direction, price_now, price_15m)
        outcome_1h = _outcome_for_direction(direction, price_now, price_1h)
        if outcome_15m is None or outcome_1h is None:
            continue

        old_outcome = row.get("outcome")
        ok = await supabase_client.update_outcome_dual(
            prediction_id=pred_id,
            outcome_15m=outcome_15m,
            outcome_1h=outcome_1h,
            price_15m_later=price_15m,
            price_1h_later=price_1h,
            primary_window=PRIMARY_WINDOW,
        )
        if ok:
            resettled += 1
            if old_outcome != outcome_1h:
                log.info(
                    "backfill FLIP id=%s %s %s now=$%.2f 15m=$%.2f(%s) 1h=$%.2f(%s) → primary=%s (was %s)",
                    pred_id, sym_raw, direction, price_now, price_15m, outcome_15m,
                    price_1h, outcome_1h, outcome_1h, old_outcome,
                )
        else:
            errors += 1
        await asyncio.sleep(0.1)

    _state["bogus_backfill_done"] = True
    _state["bogus_backfill_count"] = resettled
    _state["bogus_backfill_errors"] = errors
    log.info(
        "backfill complete (dual-window): resettled=%d errors=%d (flips logged above)",
        resettled, errors,
    )
    # Refresh directional stats + gates with newly settled outcomes
    await _refresh_directional_stats()
    return resettled


async def _refresh_directional_stats() -> None:
    """Recompute proof-qualified directional stats independently per symbol.

    AUD-057 invariant: BTC evidence must never modify ETH gates and ETH
    evidence must never modify BTC gates. A cross-symbol aggregate is retained
    only under ``aggregate_diagnostic`` and is not consumed by portfolio or
    authoritative score routing.
    """
    try:
        from . import supabase_client
    except Exception as e:
        log.warning("supabase_client unavailable for directional stats: %s", e)
        return

    # The global bounded query is diagnostic only. It must never provide the
    # authority cohort because newer activity in one symbol can evict another
    # symbol from the global newest-N window.
    try:
        diagnostic_rows = await supabase_client.fetch_predictions(limit=500)
    except Exception as e:
        log.warning("aggregate directional diagnostic fetch failed: %s", e)
        diagnostic_rows = []

    def build_diagnostic_by_window(source_rows: list[dict[str, Any]]) -> dict[str, dict]:
        buckets: dict[str, dict[str, dict[str, int]]] = {
            "15m": {
                "LONG": {"WIN": 0, "LOSS": 0},
                "SHORT": {"WIN": 0, "LOSS": 0},
                "FLAT": {"WIN": 0, "LOSS": 0},
            },
            "1h": {
                "LONG": {"WIN": 0, "LOSS": 0},
                "SHORT": {"WIN": 0, "LOSS": 0},
                "FLAT": {"WIN": 0, "LOSS": 0},
            },
        }

        for row in source_rows:
            direction = (row.get("prediction") or "").upper()
            if direction not in ("LONG", "SHORT", "FLAT"):
                continue
            outcome_1h = row.get("outcome")
            if outcome_1h in ("WIN", "LOSS"):
                buckets["1h"][direction][outcome_1h] += 1

            audit = row.get("audit") or {}
            dual = audit.get("outcomes_dual") if isinstance(audit, dict) else {}
            if isinstance(dual, dict):
                outcome_15m = dual.get("outcome_15m")
                if outcome_15m in ("WIN", "LOSS"):
                    buckets["15m"][direction][outcome_15m] += 1

        by_window: dict[str, dict] = {}
        for window in ("15m", "1h"):
            by_window[window] = {"diagnostic_only": True}
            total_w = total_l = 0
            for direction in ("LONG", "SHORT", "FLAT"):
                wins = buckets[window][direction]["WIN"]
                losses = buckets[window][direction]["LOSS"]
                verified = wins + losses
                total_w += wins
                total_l += losses
                by_window[window][direction] = {
                    "verified": verified,
                    "wins": wins,
                    "losses": losses,
                    "win_rate_pct": round((wins / verified * 100) if verified else 0.0, 2),
                    "diagnostic_only": True,
                }
            total = total_w + total_l
            by_window[window]["global"] = {
                "verified": total,
                "wins": total_w,
                "losses": total_l,
                "win_rate_pct": round((total_w / total * 100) if total else 0.0, 2),
                "diagnostic_only": True,
            }
        return by_window

    all_qualified: list[dict[str, Any]] = []
    for row in diagnostic_rows:
        if not is_proof_qualified(row):
            continue
        symbol = _normalize_symbol(row.get("symbol"))
        if not symbol:
            continue
        all_qualified.append(row)

    configured_symbols = {_normalize_symbol(symbol) for symbol in SYMBOLS}
    symbols = sorted(configured_symbols)
    qualified_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        try:
            # PostgREST applies this equality filter before the bounded limit,
            # preserving an independent evidence window for each instrument.
            symbol_rows = await supabase_client.fetch_predictions(
                limit=500,
                symbol=symbol,
            )
        except Exception as e:
            log.warning("directional stats fetch failed for %s: %s", symbol, e)
            symbol_rows = []
        qualified_by_symbol[symbol] = [
            row for row in symbol_rows
            if is_proof_qualified(row)
            and _normalize_symbol(row.get("symbol")) == symbol
        ]

    per_symbol: dict[str, dict[str, Any]] = {}

    for symbol in symbols:
        score = build_authoritative_score(
            qualified_by_symbol.get(symbol, []),
            symbol=symbol,
        )
        gates = score["gates"]
        short_only = bool(score["short_only_paper_mode"])
        per_symbol[symbol] = {
            # Overlapping proof-qualified rows remain visible for diagnosis only.
            "by_window": score["by_window"],
            # Portfolio/risk control consumes only this independent 1h authority.
            "authority_1h": score["authority_1h"],
            "gates": gates,
            "short_only_paper_mode": short_only,
            "proof_qualified_rows_raw": score["proof_qualified_rows_raw"],
            "independent_1h_rows": score["independent_1h_rows"],
            "authority_cohort": score["authority_cohort"],
        }
        log.info(
            "directional gates symbol=%s LONG_1h=%s(wr=%.1f%% n=%d) "
            "SHORT_1h=%s(wr=%.1f%% n=%d) GLOBAL_1h=%s(wr=%.1f%% n=%d) "
            "short_only_paper_mode=%s",
            symbol,
            "PASS" if gates["long_1h"]["pass"] else "FAIL",
            gates["long_1h"]["win_rate_pct"], gates["long_1h"]["n"],
            "PASS" if gates["short_1h"]["pass"] else "FAIL",
            gates["short_1h"]["win_rate_pct"], gates["short_1h"]["n"],
            "PASS" if gates["global_1h"]["pass"] else "FAIL",
            gates["global_1h"]["win_rate_pct"], gates["global_1h"]["n"],
            short_only,
        )

    aggregate_by_window = build_diagnostic_by_window(all_qualified)
    _state["directional_stats"] = {
        "per_symbol": per_symbol,
        "aggregate_diagnostic": {
            "diagnostic_only": True,
            "scope": "CROSS_SYMBOL_RAW_PROOF_QUALIFIED",
            "by_window": aggregate_by_window,
        },
    }
    _state["verified_total"] = aggregate_by_window["1h"]["global"]["verified"]

async def _oracle_loop() -> None:
    """Main loop: every CYCLE_INTERVAL_S, run predictions for all symbols."""
    log.info("oracle_loop waiting %ds before first cycle...", INITIAL_DELAY_S)
    await asyncio.sleep(INITIAL_DELAY_S)

    # ACT-XXII-prereq: ONE-TIME backfill of bogus outcomes that were settled
    # with current-price instead of historical price. Runs once at startup
    # before the first prediction cycle, so the dashboard reflects correct
    # win rates as soon as possible.
    try:
        await _backfill_bogus_outcomes()
    except Exception as e:
        log.exception("backfill error (non-fatal, continuing): %s", e)

    while True:
        cycle_start = datetime.now(timezone.utc)
        _state["last_cycle_at"] = cycle_start.isoformat()
        _state["cycles_run"] += 1
        log.info("=== oracle cycle #%d start @ %s ===", _state["cycles_run"], cycle_start.isoformat())

        # ACT XXI: Verify pending outcomes BEFORE producing new predictions.
        # This settles predictions whose 15min window elapsed in the previous cycle.
        # First cycle after boot will backfill all 200+ accumulated predictions.
        try:
            settled = await _verify_pending_outcomes()
            if settled > 0:
                log.info("verifier settled %d outcomes in cycle #%d", settled, _state["cycles_run"])
        except Exception as e:
            log.exception("verifier error (non-fatal, continuing): %s", e)

        for symbol in SYMBOLS:
            try:
                await _run_one_prediction(symbol)
            except Exception as e:
                log.exception("unexpected error for %s: %s", symbol, e)
                _state["last_error"] = f"unexpected: {symbol}: {e}"
                _state["cycles_failed"] += 1
            # Small breather between symbols to keep memory bounded
            await asyncio.sleep(2)

        # Schedule next cycle
        next_at = datetime.now(timezone.utc).timestamp() + CYCLE_INTERVAL_S
        _state["next_cycle_at"] = datetime.fromtimestamp(next_at, tz=timezone.utc).isoformat()
        log.info(
            "=== cycle #%d done — next at %s ===",
            _state["cycles_run"], _state["next_cycle_at"],
        )
        await asyncio.sleep(CYCLE_INTERVAL_S)


_tasks: list[asyncio.Task] = []


# ACT-XXV: Portfolio coordinator singleton
# Lazily initialized on first use to avoid import-time side effects.
_portfolio_coordinator = None


def _get_portfolio_coordinator():
    """Lazily instantiate the PortfolioCoordinator (ACT-XXV)."""
    global _portfolio_coordinator
    if _portfolio_coordinator is None:
        try:
            from .portfolio import PortfolioCoordinator
            _portfolio_coordinator = PortfolioCoordinator()
            _portfolio_coordinator.start()
            log.info("PortfolioCoordinator (ACT-XXV) initialized and started")
        except Exception as e:
            log.exception("failed to init PortfolioCoordinator: %s", e)
            return None
    return _portfolio_coordinator


async def _route_to_portfolio(prediction: dict, market_data: dict) -> None:
    """Route a new oracle prediction through the ACT-XXV portfolio pipeline.

    Called after each prediction is persisted. The portfolio subsystem runs
    in PAPER mode with live_capital_locked=True per the LIVE_GATE directive.

    Best-effort: failures here do NOT block the prediction cycle.
    """
    coord = _get_portfolio_coordinator()
    if coord is None:
        return

    # Extract last price + volatility from the market data (without modifying
    # the prediction itself — we only read from market_data).
    last_price = float(prediction.get("price_now") or 0)
    # Realized vol: stdev of last 16 closes (4h on 15m) / mean
    vol_pct = 0.0
    try:
        ohlcv = market_data.get("ohlcv") or []
        if len(ohlcv) >= 16:
            closes = [float(c[4]) for c in ohlcv[-16:] if c and len(c) > 4]
            if len(closes) >= 8:
                mean_c = sum(closes) / len(closes)
                if mean_c > 0:
                    var = sum((c - mean_c) ** 2 for c in closes) / len(closes)
                    vol_pct = (var ** 0.5) / mean_c
    except Exception:
        pass

    # AUD-057: Kelly and short-only configuration are scoped to the
    # current prediction symbol. Cross-symbol aggregate diagnostics are never
    # consumed here.
    symbol = _normalize_symbol(prediction.get("symbol"))
    symbol_stats = (
        (_state.get("directional_stats") or {}).get("per_symbol") or {}
    ).get(symbol) or {}
    authority_1h = symbol_stats.get("authority_1h") or {}
    win_rate_by_dir = {}
    try:
        for direction in ("LONG", "SHORT"):
            direction_stat = authority_1h.get(direction) or {}
            win_rate_by_dir[direction] = float(direction_stat.get("win_rate_pct") or 0.0) / 100.0
    except Exception:
        win_rate_by_dir = {}

    short_only = bool(symbol_stats.get("short_only_paper_mode", False))
    coord.portfolio_engine.update_config(short_only_paper_mode=short_only)
    coord.risk_kernel.update_config(
        short_only_paper_mode=short_only,
        trade_mode=_state.get("trade_mode", "PAPER"),
        live_capital_locked=_state.get("live_capital_locked", True),
    )
    coord.execution_engine.update_config(
        trade_mode=_state.get("trade_mode", "PAPER"),
        allow_live=not _state.get("live_capital_locked", True),
    )

    # ACT-XXVI: extract ohlcv + funding + OI from market_data and pass to
    # coordinator. The coordinator feeds these to MicrostructureIntelligence
    # (VPIN + OFI + liquidation + funding/OI) and HMMRegimeOverlay BEFORE
    # the proposal is built, so the RiskKernel can REJECT/REDUCE based on
    # toxic flow and the MetaLabeler can soft-scale LONG confidence.
    ohlcv_rows = market_data.get("ohlcv") or []
    observations = market_data.get("feature_observations") or {}
    funding_observation = observations.get("funding_signal") or {}
    oi_observation = observations.get("oi_momentum") or {}
    observed_statuses = {"REAL_OBSERVED_ZERO", "REAL_NONZERO"}
    funding_rate = (
        (market_data.get("funding") or {}).get("rate")
        if funding_observation.get("status") in observed_statuses else None
    )
    oi_change_24h_pct = (
        (market_data.get("open_interest") or {}).get("oi_change_24h_pct")
        if oi_observation.get("status") in observed_statuses else None
    )

    # ACT-XXVI: synthesize a thin L2 orderbook snapshot from the depth
    # numbers we have. The full L2 isn't available without re-fetching, but
    # we can build a 3-level approximation that the FillSimulator's
    # walk_book() can consume. This makes the high-fidelity fill path active.
    orderbook_snap = None
    try:
        ob = market_data.get("orderbook") or {}
        bid_depth = float(ob.get("bid_depth", 0) or 0)
        ask_depth = float(ob.get("ask_depth", 0) or 0)
        ticker = market_data.get("ticker") or {}
        bid_px = float(ticker.get("bid", 0) or 0)
        ask_px = float(ticker.get("ask", 0) or 0)
        if bid_px > 0 and ask_px > 0 and (bid_depth > 0 or ask_depth > 0):
            # Build 3 synthetic levels around best bid/ask.
            # Distribute the depth across 3 levels (60%/25%/15%) at increasing
            # distance from the BBO (0 bps, 2 bps, 5 bps).
            def _split_depth(total_depth_usd: float, mid_px: float) -> list[list]:
                # total_depth is in BASE currency already (USDT * price not represented),
                # but for crypto pairs on OKX the depth values are quote volumes.
                # We treat them as quote, convert to base via price.
                if mid_px <= 0:
                    return []
                base_total = total_depth_usd / mid_px
                return [
                    [mid_px, base_total * 0.60],
                    [mid_px, base_total * 0.25],
                    [mid_px, base_total * 0.15],
                ]
            mid = (bid_px + ask_px) / 2
            bid_levels = []
            for i, frac in enumerate([0.60, 0.25, 0.15]):
                offset_bps = [0, 2, 5][i]
                px = mid * (1 - offset_bps / 10_000)
                bid_levels.append([px, (bid_depth / mid) * frac])
            ask_levels = []
            for i, frac in enumerate([0.60, 0.25, 0.15]):
                offset_bps = [0, 2, 5][i]
                px = mid * (1 + offset_bps / 10_000)
                ask_levels.append([px, (ask_depth / mid) * frac])
            orderbook_snap = {
                "bids": bid_levels,
                "asks": ask_levels,
                "last_price": mid,
            }
    except Exception as e:
        log.debug("orderbook snapshot synth failed: %s", e)

    # Ingest the prediction
    result = await coord.ingest_prediction(
        prediction=prediction,
        last_price=last_price,
        vol_pct=vol_pct,
        win_rate_by_direction=win_rate_by_dir,
        ohlcv=ohlcv_rows,
        orderbook=orderbook_snap,
        funding_rate=funding_rate,
        oi_change_24h_pct=oi_change_24h_pct,
    )
    if result:
        if "skipped" in result:
            log.info("portfolio skip: %s reason=%s", result.get("skipped"), result.get("reason"))
        else:
            order = (result.get("order") or {})
            log.info(
                "portfolio fill: %s %s status=%s filled_qty=%.6f avg=$%.4f fidelity=%s",
                (result.get("proposal") or {}).get("symbol"),
                (result.get("proposal") or {}).get("direction"),
                order.get("status"),
                order.get("filled_qty", 0),
                order.get("avg_fill_price", 0),
                result.get("fidelity_model", "unknown"),
            )


def start() -> None:
    """Start the oracle runner. Called from main.py lifespan()."""
    if _state["started_at"] is not None:
        return  # already started
    _state["started_at"] = datetime.now(timezone.utc).isoformat()
    _seed_state_from_existing()
    t = asyncio.create_task(_oracle_loop(), name="oracle_loop")
    _tasks.append(t)
    log.info("oracle_runner started — interval=%ds symbols=%s", CYCLE_INTERVAL_S, SYMBOLS)


async def stop() -> None:
    """Graceful shutdown."""
    for t in _tasks:
        t.cancel()
    for t in _tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
    _tasks.clear()
    # ACT-XXV: stop portfolio coordinator (generates shadow report)
    global _portfolio_coordinator
    if _portfolio_coordinator is not None:
        try:
            await _portfolio_coordinator.stop()
        except Exception as e:
            log.warning("portfolio coordinator stop error: %s", e)
        _portfolio_coordinator = None
    # Close Supabase HTTP client
    try:
        from . import supabase_client
        await supabase_client.close()
    except Exception:
        pass
    log.info("oracle_runner stopped")
