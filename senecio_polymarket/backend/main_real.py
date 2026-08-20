"""Production public runtime wiring — ORDER-070-R1 truth boundary.

Public FastAPI is observational only. Mutating/control routes live exclusively
in ``backend.admin:admin_app`` and are never mounted by the production launcher.
All authority-bearing public surfaces consume one cached atomic
``AuthoritySnapshot`` per symbol instead of independently reading Supabase.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.routing import Mount, WebSocketRoute

from . import main as legacy
from . import oracle_runner
from .authority_snapshot import STORE as authority_store, normalize_symbol
from .authoritative_score import build_authoritative_score
from .boros_market_adapter import get_boros_adapter
from .kalshi_market_adapter import get_kalshi_adapter
from .polymarket_market_adapter import get_polymarket_adapter
from .runtime_provenance import runtime_provenance

log = logging.getLogger("senecio.main_real")
_poly = get_polymarket_adapter()
_boros = get_boros_adapter()
_kalshi = get_kalshi_adapter()

SAFE_PUBLIC_METHODS = {"GET", "HEAD", "OPTIONS"}
OVERRIDDEN_GET_PATHS = {
    "/api/health",
    "/api/oracle/state",
    "/api/oracle/score",
    "/api/portfolio/live_gate",
}


def synthetic_demo_enabled() -> bool:
    return (os.environ.get("SENEX_ENABLE_SYNTHETIC_DEMO") or "").strip().lower() in {"1", "true", "yes", "on"}


def quarantine_legacy_outcome_backfill() -> None:
    """Disable obsolete historical WIN/LOSS rewrite path; no data mutation."""
    oracle_runner._state["bogus_backfill_done"] = True
    oracle_runner._state["bogus_backfill_count"] = 0
    oracle_runner._state["bogus_backfill_errors"] = 0
    oracle_runner._state["legacy_backfill_quarantined"] = True


@asynccontextmanager
async def real_lifespan(public_app: FastAPI):
    public_app.state.audit = legacy._audit
    public_app.state.bus = legacy._bus
    public_app.state.retriever = legacy._retriever
    public_app.state.scanner_a = legacy._scanner_a
    public_app.state.scanner_b = legacy._scanner_b
    public_app.state.wallet_tracker = legacy._wallet_tracker
    public_app.state.engine = legacy._engine
    public_app.state.executor = legacy._executor
    public_app.state.scheduler = legacy._scheduler

    demo = synthetic_demo_enabled()
    if demo:
        log.warning("SENEX synthetic demo scheduler EXPLICITLY ENABLED")
        legacy._scheduler.start()
    else:
        log.info("SENEX production mode: synthetic scheduler disabled")

    await asyncio.gather(_poly.start(), _boros.start(), _kalshi.start())
    quarantine_legacy_outcome_backfill()
    authority_store.clear()
    oracle_runner.start()
    log.info("SENEX public read-only runtime up")
    yield
    await oracle_runner.stop()
    await asyncio.gather(_kalshi.stop(), _boros.stop(), _poly.stop())
    if demo:
        await legacy._scheduler.stop()
    await legacy._bus.close()


def _build_public_app() -> FastAPI:
    public = FastAPI(
        title="SENEX PUBLIC READ-ONLY",
        version="ORDER-070-R1",
        lifespan=real_lifespan,
    )
    # Copy only observational HTTP routes plus static/websocket transports.
    for route in legacy.app.router.routes:
        if isinstance(route, APIRoute):
            methods = set(route.methods or set())
            if route.path not in OVERRIDDEN_GET_PATHS and methods <= SAFE_PUBLIC_METHODS:
                public.router.routes.append(route)
        elif isinstance(route, (Mount, WebSocketRoute)):
            public.router.routes.append(route)
    return public


app = _build_public_app()


@app.middleware("http")
async def public_method_guard(request: Request, call_next):
    if request.method.upper() not in SAFE_PUBLIC_METHODS:
        return JSONResponse(
            {"detail": "PUBLIC_READ_ONLY_METHOD_DENIED"},
            status_code=405,
            headers={"X-Senex-Public-Decision": "DENY_UNSAFE_METHOD"},
        )
    response = await call_next(request)
    response.headers.setdefault("X-Senex-Public-Decision", "ALLOW_READ_ONLY")
    return response


def _locked_gate_without_coordinator(score: dict[str, Any]) -> dict[str, Any]:
    return {
        "unlocked": False,
        "trade_mode": "PAPER",
        "live_capital_locked": True,
        "orders_enabled": False,
        "diagnostic_only": True,
        "effective_gate": "LOCKED_COORDINATOR_UNAVAILABLE",
        "failed_reasons": ["PORTFOLIO_COORDINATOR_UNAVAILABLE", "LIVE_CAPITAL_LOCKED_BY_PAPER_POLICY"],
        "verified": int(score.get("independent_1h_rows") or 0),
        "proof_qualified_rows_raw": int(score.get("proof_qualified_rows_raw") or 0),
        "authority_cohort": score.get("authority_cohort"),
        "authority_n_source": (score.get("authority_1h") or {}).get("n_source"),
    }


def _live_gate_from_score(score: dict[str, Any]) -> dict[str, Any]:
    coord = legacy._get_coordinator()
    if coord is None:
        return _locked_gate_without_coordinator(score)
    state = legacy._paper_locked_live_gate_from_score(coord, score)
    state["orders_enabled"] = False
    return state


async def _snapshot(symbol: str = "BTCUSDT", *, force: bool = False):
    return await authority_store.get(
        normalize_symbol(symbol),
        live_gate_builder=_live_gate_from_score,
        force=force,
    )


# Compatibility callable retained for established unit tests. Public routing uses
# the shared snapshot function below.
async def authoritative_oracle_score(symbol: str | None = Query(default=None)):
    from . import supabase_client
    normalized_symbol = normalize_symbol(symbol) if symbol else None
    rows = await supabase_client.fetch_authority_history(symbol=normalized_symbol)
    score = build_authoritative_score(rows, oracle_runner.get_state(), symbol=normalized_symbol)
    score["authority_history_complete"] = True
    score["authority_history_rows"] = len(rows)
    return score


@app.get("/api/oracle/score")
async def public_authoritative_oracle_score(symbol: str = Query(default="BTCUSDT")):
    snap = await _snapshot(symbol)
    return dict(snap.score)


@app.get("/api/portfolio/live_gate")
async def public_live_gate(symbol: str = Query(default="BTCUSDT")):
    snap = await _snapshot(symbol)
    return dict(snap.live_gate)


@app.get("/api/oracle/state")
async def public_oracle_state(symbol: str = Query(default="BTCUSDT")):
    snap = await _snapshot(symbol)
    state = oracle_runner.get_state()
    return {
        **state,
        "last_prediction": state.get("last_prediction_result"),
        "authority_snapshot_id": snap.snapshot_id,
        "authority": {
            "symbol": snap.symbol,
            "independent_1h_rows": snap.score.get("independent_1h_rows"),
            "proof_qualified_rows_raw": snap.score.get("proof_qualified_rows_raw"),
            "authority_1h": snap.score.get("authority_1h"),
            "history_complete": snap.authority_history_complete,
            "history_rows": snap.authority_history_rows,
        },
        "exact_total_predictions": snap.exact_total_predictions,
        "exact_count_complete": snap.exact_count_complete,
        "provenance": snap.provenance,
    }


@app.get("/api/authority/snapshot")
async def public_authority_snapshot(symbol: str = Query(default="BTCUSDT")):
    return (await _snapshot(symbol)).to_dict()


@app.get("/api/runtime/provenance")
async def public_runtime_provenance():
    return runtime_provenance()


@app.get("/healthz")
async def healthz():
    """Pure process liveness; external dependencies do not redefine liveness."""
    return {
        "status": "alive",
        "probe": "liveness",
        "trade_mode": "PAPER",
        "orders_enabled": False,
        "live_capital_locked": True,
        "provenance": runtime_provenance(),
    }


@app.get("/api/health")
async def compatibility_health():
    return await healthz()


@app.get("/readyz")
async def readyz(symbol: str = Query(default="BTCUSDT")):
    """Fail-closed readiness: authority, exact count and provenance must all be exact."""
    try:
        snap = await _snapshot(symbol, force=True)
    except Exception as exc:
        return JSONResponse(
            {"status": "not_ready", "probe": "readiness", "reason": type(exc).__name__},
            status_code=503,
        )
    runner = oracle_runner.get_state()
    checks = {
        "authority_history_complete": snap.authority_history_complete,
        "exact_count_complete": snap.exact_count_complete,
        "provenance_exact": bool(snap.provenance.get("exact")),
        "oracle_started": bool(runner.get("started_at")),
        "paper_lock": snap.live_gate.get("trade_mode") == "PAPER" and bool(snap.live_gate.get("live_capital_locked")),
        "orders_disabled": snap.live_gate.get("orders_enabled", False) is False,
    }
    ready = all(checks.values())
    payload = {
        "status": "ready" if ready else "not_ready",
        "probe": "readiness",
        "checks": checks,
        "authority_snapshot_id": snap.snapshot_id,
        "provenance": snap.provenance,
    }
    return payload if ready else JSONResponse(payload, status_code=503)


@app.get("/api/market-context")
async def market_context(symbol: str = Query(default="BTCUSDT")):
    snap = await _snapshot(symbol)
    return {
        "mode": "REAL_PLUS_EXPLICIT_DEMO" if synthetic_demo_enabled() else "REAL_ONLY",
        "synthetic_demo_enabled": synthetic_demo_enabled(),
        "polymarket": _poly.snapshot(),
        "kalshi": _kalshi.snapshot(),
        "boros": _boros.snapshot(),
        "oracle": oracle_runner.get_state(),
        "authority_snapshot_id": snap.snapshot_id,
        "authority": {
            "symbol": snap.symbol,
            "authority_1h": snap.score.get("authority_1h"),
            "score_status": snap.score.get("score_status"),
        },
        "safety": {
            "trade_mode": "PAPER",
            "allow_live": False,
            "orders_enabled": False,
            "live_capital_locked": True,
            "read_only_market_adapters": True,
        },
    }
