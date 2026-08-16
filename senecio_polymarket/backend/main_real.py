"""Production app wiring for SENEX REAL-only market data.

The legacy FastAPI app and its endpoints are retained, but its synthetic demo
scheduler is NOT started unless SENEX_ENABLE_SYNTHETIC_DEMO=1 is explicitly set.
Production starts only the proof/settlement oracle runner and public read-only
market adapters. AUD-059 explicitly quarantines the pre-SCORE-002 historical
outcome backfill: settled legacy rows are excluded or qualified by the proof
gate, never rewritten as a repair mechanism.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Query
from fastapi.routing import APIRoute

from . import main as legacy
from . import oracle_runner
from .authoritative_score import build_authoritative_score
from .boros_market_adapter import get_boros_adapter
from .kalshi_market_adapter import get_kalshi_adapter
from .polymarket_market_adapter import get_polymarket_adapter

log = logging.getLogger("senecio.main_real")

app = legacy.app
_poly = get_polymarket_adapter()
_boros = get_boros_adapter()
_kalshi = get_kalshi_adapter()


def synthetic_demo_enabled() -> bool:
    return (os.environ.get("SENEX_ENABLE_SYNTHETIC_DEMO") or "").strip().lower() in {"1", "true", "yes", "on"}


def quarantine_legacy_outcome_backfill() -> None:
    """Disable the obsolete historical WIN/LOSS rewrite path before startup.

    This is an in-memory runtime guard only; it performs no Supabase mutation.
    The normal NULL->WIN/LOSS settlement authority and repair-only reconciler
    remain unchanged.
    """
    oracle_runner._state["bogus_backfill_done"] = True
    oracle_runner._state["bogus_backfill_count"] = 0
    oracle_runner._state["bogus_backfill_errors"] = 0
    oracle_runner._state["legacy_backfill_quarantined"] = True


@asynccontextmanager
async def real_lifespan(app):
    app.state.audit = legacy._audit
    app.state.bus = legacy._bus
    app.state.retriever = legacy._retriever
    app.state.scanner_a = legacy._scanner_a
    app.state.scanner_b = legacy._scanner_b
    app.state.wallet_tracker = legacy._wallet_tracker
    app.state.engine = legacy._engine
    app.state.executor = legacy._executor
    app.state.scheduler = legacy._scheduler

    demo = synthetic_demo_enabled()
    if demo:
        log.warning("SENEX synthetic demo scheduler EXPLICITLY ENABLED")
        legacy._scheduler.start()
    else:
        log.info("SENEX production mode: synthetic scheduler disabled")

    await asyncio.gather(_poly.start(), _boros.start(), _kalshi.start())
    quarantine_legacy_outcome_backfill()
    log.info("AUD-059: legacy historical outcome backfill QUARANTINED (no rewrite)")
    oracle_runner.start()
    log.info("SENEX REAL market runtime up — Polymarket+CLOB + Kalshi + Boros + authoritative oracle")

    yield

    await oracle_runner.stop()
    await asyncio.gather(_kalshi.stop(), _boros.stop(), _poly.stop())
    if demo:
        await legacy._scheduler.stop()
    await legacy._bus.close()
    log.info("SENEX REAL market runtime down")


app.router.lifespan_context = real_lifespan


async def authoritative_oracle_score(symbol: str | None = Query(default=None)):
    """Truth-safe 1h score; authority is always scoped to one instrument."""
    from . import supabase_client

    normalized_symbol = (
        str(symbol).upper().replace("/", "").replace("-", "").strip()
        if symbol else None
    )
    rows = await supabase_client.fetch_authority_history(symbol=normalized_symbol)
    score = build_authoritative_score(
        rows,
        oracle_runner.get_state(),
        symbol=normalized_symbol,
    )
    score["authority_history_complete"] = True
    score["authority_history_rows"] = len(rows)
    return score


def _install_authoritative_score_route() -> None:
    """Replace the legacy /api/oracle/score route rather than shadowing it."""
    retained = []
    for route in app.router.routes:
        if isinstance(route, APIRoute) and route.path == "/api/oracle/score" and "GET" in route.methods:
            continue
        retained.append(route)
    app.router.routes[:] = retained
    app.add_api_route(
        "/api/oracle/score",
        authoritative_oracle_score,
        methods=["GET"],
        name="oracle_score_authoritative_aud055",
    )


_install_authoritative_score_route()


@app.get("/api/market-context")
async def market_context():
    """One real-only dashboard payload; no synthetic market values."""
    return {
        "mode": "REAL_PLUS_EXPLICIT_DEMO" if synthetic_demo_enabled() else "REAL_ONLY",
        "synthetic_demo_enabled": synthetic_demo_enabled(),
        "polymarket": _poly.snapshot(),
        "kalshi": _kalshi.snapshot(),
        "boros": _boros.snapshot(),
        "oracle": oracle_runner.get_state(),
        "safety": {
            "trade_mode": "PAPER",
            "allow_live": False,
            "orders_enabled": False,
            "live_capital_locked": True,
            "read_only_market_adapters": True,
        },
    }
