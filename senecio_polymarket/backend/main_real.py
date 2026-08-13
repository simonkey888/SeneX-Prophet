"""Production app wiring for SENEX REAL-only market data.

The legacy FastAPI app and its endpoints are retained, but its synthetic demo
scheduler is NOT started unless SENEX_ENABLE_SYNTHETIC_DEMO=1 is explicitly set.
Production starts only:
- proof/settlement oracle_runner
- public read-only Polymarket market adapter
- public read-only Boros context adapter
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from . import main as legacy
from . import oracle_runner
from .boros_market_adapter import get_boros_adapter
from .polymarket_market_adapter import get_polymarket_adapter

log = logging.getLogger("senecio.main_real")

app = legacy.app
_poly = get_polymarket_adapter()
_boros = get_boros_adapter()


def synthetic_demo_enabled() -> bool:
    return (os.environ.get("SENEX_ENABLE_SYNTHETIC_DEMO") or "").strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def real_lifespan(app):
    # Preserve app.state compatibility for existing read-only endpoints.
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

    await _poly.start()
    await _boros.start()
    oracle_runner.start()
    log.info("SENEX REAL market runtime up — Polymarket+CLOB + Boros + authoritative oracle")

    yield

    await oracle_runner.stop()
    await _boros.stop()
    await _poly.stop()
    if demo:
        await legacy._scheduler.stop()
    await legacy._bus.close()
    log.info("SENEX REAL market runtime down")


# Replace the legacy app lifespan before uvicorn starts it.
app.router.lifespan_context = real_lifespan


@app.get("/api/market-context")
async def market_context():
    """One real-only dashboard payload; no synthetic market values."""
    return {
        "mode": "REAL_PLUS_EXPLICIT_DEMO" if synthetic_demo_enabled() else "REAL_ONLY",
        "synthetic_demo_enabled": synthetic_demo_enabled(),
        "polymarket": _poly.snapshot(),
        "boros": _boros.snapshot(),
        "oracle": oracle_runner.get_state(),
        "safety": {
            "trade_mode": "PAPER",
            "allow_live": False,
            "live_capital_locked": True,
            "read_only_market_adapters": True,
        },
    }
