"""
SENECIO ORACLE — FastAPI Main App
==================================
Wires all 7 layers into a runnable service.

Run:
    uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

Endpoints:
    GET  /             — dashboard (static)
    GET  /api/health   — liveness probe
    GET  /api/stats    — bus + scheduler + audit stats
    GET  /api/audit?day=YYYY-MM-DD&limit=100  — recent audit events
    GET  /api/state    — current portfolio + scanner state
    GET  /api/catalog  — symbol catalog
    WS   /ws?type=...  — live event stream
    GET  /sse?type=... — SSE fallback
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
import json

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .audit_store import AuditStore
from .event_bus import EventBus
from .data_retriever import DataRetriever
from .scanner_a import ScannerA
from .scanner_b import ScannerB
from .wallet_tracker import WalletTracker
from .oracle_engine import OracleEngine
from .execution_simulator import ExecutionSimulator
from .scheduler import Scheduler
from .ws_server import make_router as make_ws_router
from . import oracle_runner
from .authoritative_score import build_authoritative_score
from .settlement_proof import filter_proof_qualified, is_proof_qualified
# ACT-XXVII: research layer (lazy-initialized to avoid import-time failures
# if optional deps like shap are missing)
try:
    from .research import ResearchCoordinator, get_registry
    _research_coord = ResearchCoordinator()
    _metrics_registry = get_registry()
except Exception as _research_init_err:  # pragma: no cover — research layer must never break the app
    _research_coord = None
    _metrics_registry = None
    _research_init_err_msg = str(_research_init_err)
else:
    _research_init_err_msg = None

# ACT-XXIX: anti-fragility layer (lazy-initialized; never breaks the app)
try:
    from .antifragility import AntiFragilityCoordinator as _AFCoord
    _antifragility_coord = _AFCoord(start_biv=False)
except Exception as _af_init_err:  # pragma: no cover
    _antifragility_coord = None
    _af_init_err_msg = str(_af_init_err)
else:
    _af_init_err_msg = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("senecio.main")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# module-level singletons (created once, used by both lifespan and route handlers)
_audit = AuditStore(root="data/audit")
_bus = EventBus(audit=_audit)
_retriever = DataRetriever(mode="LIVE", seed=42)
_scanner_a = ScannerA()
_scanner_b = ScannerB()
_wallet_tracker = WalletTracker()
_engine = OracleEngine()
_executor = ExecutionSimulator()
_scheduler = Scheduler(
    bus=_bus, retriever=_retriever, scanner_a=_scanner_a, scanner_b=_scanner_b,
    wallet_tracker=_wallet_tracker, engine=_engine, executor=_executor,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.audit = _audit
    app.state.bus = _bus
    app.state.retriever = _retriever
    app.state.scanner_a = _scanner_a
    app.state.scanner_b = _scanner_b
    app.state.wallet_tracker = _wallet_tracker
    app.state.engine = _engine
    app.state.executor = _executor
    app.state.scheduler = _scheduler

    _scheduler.start()
    oracle_runner.start()
    log.info("SENECIO ORACLE backend up — layers: data, scanner_a, scanner_b, wallet, brain, exec, ws + REAL ORACLE")

    yield

    await oracle_runner.stop()
    await _scheduler.stop()
    await _bus.close()
    log.info("SENECIO ORACLE backend down")


app = FastAPI(title="SENECIO ORACLE", version="ACT-XXIX-systemic-antifragility", lifespan=lifespan)

# WebSocket / SSE router
app.include_router(make_ws_router(_bus))


# ---- REST endpoints ----
@app.get("/api/health")
async def health():
    """Real health check — includes oracle runner state."""
    oracle_state = oracle_runner.get_state()
    # Best-effort Supabase count (don't fail health if DB unreachable)
    sb_total = 0
    try:
        from . import supabase_client
        sb_total = await supabase_client.count_predictions()
    except Exception:
        pass
    return {
        "status": "ok",
        "version": "ACT-XXIX-systemic-antifragility",
        "oracle": {
            "started_at": oracle_state.get("started_at"),
            "last_prediction_ts": oracle_state.get("last_prediction_ts"),
            "last_prediction_symbol": oracle_state.get("last_prediction_symbol"),
            "predictions_count": oracle_state.get("predictions_count", 0),
            "cycles_run": oracle_state.get("cycles_run", 0),
            "cycles_failed": oracle_state.get("cycles_failed", 0),
            "last_error": oracle_state.get("last_error"),
            "last_cycle_at": oracle_state.get("last_cycle_at"),
            "next_cycle_at": oracle_state.get("next_cycle_at"),
            "exchange_used_last": oracle_state.get("exchange_used_last"),
            "supabase_total": sb_total,
        },
    }


@app.get("/api/oracle/state")
async def oracle_state():
    """Detailed oracle runner state + last prediction."""
    state = oracle_runner.get_state()
    return {
        **state,
        "last_prediction": state.get("last_prediction_result"),
    }


@app.get("/api/oracle/predictions")
async def oracle_predictions(limit: int = Query(default=20, le=200)):
    """Return last N predictions from predictions.jsonl (most recent first)."""
    from pathlib import Path
    pred_path = Path(__file__).resolve().parent.parent / "oracle" / "senecio_output" / "predictions.jsonl"
    if not pred_path.exists():
        return {"count": 0, "predictions": []}
    rows = []
    with open(pred_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    # most recent first
    rows.reverse()
    # strip _audit for slim view (client can request /api/oracle/predictions/full for it)
    slim = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows[:limit]]
    return {"count": len(slim), "total_in_file": len(rows), "predictions": slim}


@app.get("/api/oracle/predictions/db")
async def oracle_predictions_db(limit: int = Query(default=50, le=500), symbol: str | None = None):
    """Read predictions directly from Supabase — survives container redeploys."""
    from . import supabase_client
    rows = await supabase_client.fetch_predictions(limit=limit, symbol=symbol)
    total = await supabase_client.count_predictions()
    return {
        "source": "supabase",
        "count": len(rows),
        "total_in_db": total,
        "predictions": rows,
    }


@app.get("/api/oracle/score")
async def oracle_score():
    """Oracle accuracy score computed from Supabase (verified predictions only).

    ACT XXIII: now returns per-direction × per-window breakdown + gate states.
    The LONG vs SHORT asymmetry is a critical GO/NO-GO signal for live capital.

    Returns:
      - total_predictions: count of all rows in Supabase
      - verified: count of rows with WIN/LOSS outcome (= outcome_1h, the gating column)
      - wins/losses/win_rate_pct: aggregate across all verified
      - by_direction: per-direction breakdown using outcome_1h (primary column)
      - by_window: {15m: {LONG, SHORT, FLAT, global}, 1h: {...}} — 15m reads from
                   audit.outcomes_dual.outcome_15m, 1h reads primary `outcome` column
      - gates: {long_1h, short_1h, global_1h} with pass/fail + thresholds + n
      - short_only_paper_mode: True when SHORT passes 1h gate but LONG fails
      - trade_mode: "PAPER" (always, per ACT XXIII directive 5 — no live capital)
      - live_capital_locked: True (hard guard)
    """
    from . import supabase_client
    from . import oracle_runner
    # Get all predictions with outcome filled
    rows = await supabase_client.fetch_predictions(limit=500)
    verified = filter_proof_qualified(rows)
    wins = sum(1 for r in verified if r.get("outcome") == "WIN")
    losses = sum(1 for r in verified if r.get("outcome") == "LOSS")
    win_rate = (wins / len(verified) * 100) if verified else 0.0

    # Per-direction breakdown — surfaces edge asymmetry that the aggregate
    # win_rate hides (e.g. LONG 0% + SHORT 82% averages to 46% which looks
    # mediocre but actually means LONG is broken and SHORT is the alpha).
    by_direction: dict[str, dict] = {}
    for direction in ("LONG", "SHORT", "FLAT"):
        sub = [r for r in verified if (r.get("prediction") or "").upper() == direction]
        sub_w = sum(1 for r in sub if r.get("outcome") == "WIN")
        sub_l = sum(1 for r in sub if r.get("outcome") == "LOSS")
        sub_decided = sub_w + sub_l
        by_direction[direction] = {
            "verified": sub_decided,
            "wins": sub_w,
            "losses": sub_l,
            "win_rate_pct": round((sub_w / sub_decided * 100) if sub_decided > 0 else 0.0, 2),
        }

    # ACT XXIII: pull full state from oracle_runner (directional stats + gates)
    runner_state = oracle_runner.get_state()
    by_window = runner_state.get("directional_stats", {}).get("by_window", {})
    gates = runner_state.get("gates", {})
    short_only_paper_mode = runner_state.get("short_only_paper_mode", False)
    trade_mode = runner_state.get("trade_mode", "PAPER")
    live_capital_locked = runner_state.get("live_capital_locked", True)

    return {
        "version": "ACT-XXIX-systemic-antifragility",
        "total_predictions": len(rows),
        "verified": len(verified),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 2),
        "by_direction": by_direction,           # 1h-window primary breakdown (backward compat)
        "by_window": by_window,                 # ACT XXIII: {15m: {...}, 1h: {...}}
        "gates": gates,                         # ACT XXIII: directional GO/NO-GO
        "short_only_paper_mode": short_only_paper_mode,
        "trade_mode": trade_mode,               # always "PAPER" per directive 5
        "live_capital_locked": live_capital_locked,
    }


@app.get("/api/stats")
async def stats():
    return {
        "scheduler": _scheduler.stats(),
        "bus": _bus.stats(),
        "audit": _audit.stats(),
        "executor": _executor.risk_state(),
        "wallet_tracker": _wallet_tracker.stats(),
        "cursors": _retriever.cursor_state(),
    }


# ---- ACT-XXV: Portfolio endpoints ----

def _get_coordinator():
    """Lazy access to the portfolio coordinator from oracle_runner."""
    try:
        return oracle_runner._get_portfolio_coordinator()
    except Exception:
        return None


def _paper_locked_live_gate_state(coord, rows: list[dict], *, symbol: str) -> dict:
    """Build symbol authority, then evaluate diagnostically under PAPER lock."""
    score = build_authoritative_score(rows, symbol=symbol)
    status = coord.evaluate_live_gate(oracle_score=score)
    status.unlocked = False
    status.trade_mode = "PAPER"
    status.live_capital_locked = True
    policy_reason = "LIVE_CAPITAL_LOCKED_BY_PAPER_POLICY"
    if policy_reason not in status.failed_reasons:
        status.failed_reasons.append(policy_reason)

    state = status.to_dict()
    conditions = state.get("conditions") or {}
    state.update({
        "diagnostic_only": True,
        "effective_gate": "LOCKED_BY_PAPER_POLICY",
        "paper_only": True,
        "score_scope": score.get("score_scope"),
        "requested_symbol": score.get("requested_symbol"),
        "authority_cohort": score.get("authority_cohort"),
        "authority_n_source": (score.get("authority_1h") or {}).get("n_source"),
        "verified": int(score.get("independent_1h_rows") or 0),
        "proof_qualified_rows_raw": int(score.get("proof_qualified_rows_raw") or 0),
        "conditions_passed": sum(
            1 for item in conditions.values()
            if isinstance(item, dict) and item.get("pass")
        ),
        "conditions_total": len(conditions),
    })
    return state


def _normalize_authority_symbol(symbol: str) -> str:
    """Normalize the instrument key used at the evidence-query boundary."""
    return str(symbol or "").upper().replace("/", "").replace("-", "").strip()


async def _symbol_scoped_paper_live_gate_state(coord, *, symbol: str) -> dict:
    """Fetch one symbol before the bounded limit, then apply the PAPER lock."""
    from . import supabase_client

    normalized_symbol = _normalize_authority_symbol(symbol)
    rows = []
    if normalized_symbol:
        try:
            rows = await supabase_client.fetch_predictions(
                limit=500,
                symbol=normalized_symbol,
            )
        except Exception as e:
            log.warning("symbol-scoped live-gate fetch failed for %s: %s", normalized_symbol, e)
    return _paper_locked_live_gate_state(
        coord,
        rows,
        symbol=normalized_symbol,
    )


@app.get("/api/portfolio/state")
async def portfolio_state():
    """ACT-XXV/XXVI: full portfolio subsystem snapshot (includes microstructure + regime_hmm)."""
    coord = _get_coordinator()
    if coord is None:
        return {"error": "portfolio coordinator not initialized", "version": "ACT-XXIX-systemic-antifragility"}
    return coord.get_state()


@app.get("/api/portfolio/analytics")
async def portfolio_analytics():
    """ACT-XXV: Sharpe / Sortino / PF / Expectancy / Recovery / Calmar / Kelly / MaxDD."""
    coord = _get_coordinator()
    if coord is None:
        return {"error": "portfolio coordinator not initialized"}
    return coord.get_analytics()


@app.get("/api/portfolio/trades")
async def portfolio_trades(limit: int = Query(default=50, le=500)):
    """ACT-XXV: recent closed trades from the journal."""
    coord = _get_coordinator()
    if coord is None:
        return {"error": "portfolio coordinator not initialized", "trades": []}
    return {"count": limit, "trades": coord.get_recent_trades(limit=limit)}


@app.get("/api/portfolio/audit")
async def portfolio_audit(limit: int = Query(default=50, le=500)):
    """ACT-XXV: recent execution audit events (order lifecycle)."""
    coord = _get_coordinator()
    if coord is None:
        return {"error": "portfolio coordinator not initialized", "events": []}
    return {"count": limit, "events": coord.get_audit_log(limit=limit)}


@app.get("/api/portfolio/shadow")
async def portfolio_shadow():
    """ACT-XXV: ShadowLive status + recent shadow trades."""
    coord = _get_coordinator()
    if coord is None:
        return {"error": "portfolio coordinator not initialized"}
    return {
        "status": coord.shadow_live.stats(),
        "report": coord.shadow_live.generate_report(),
        "recent_trades": coord.shadow_live.fetch_trades(limit=20),
    }


@app.get("/api/portfolio/microstructure")
async def portfolio_microstructure():
    """ACT-XXVI: microstructure intelligence snapshot.

    Returns the most recent MicrostructureReport with:
      - toxic_score (0..1 composite)
      - VPIN, OFI normalized, liquidation cluster proximity
      - funding/OI extremity flags
      - action recommendation (ALLOW / REDUCE / REJECT)
    """
    coord = _get_coordinator()
    if coord is None:
        return {"error": "portfolio coordinator not initialized"}
    return {
        "version": "ACT-XXIX-systemic-antifragility",
        "report": coord.get_microstructure_report(),
        "stats": coord.microstructure.stats(),
    }


@app.get("/api/portfolio/regime_hmm")
async def portfolio_regime_hmm():
    """ACT-XXVI: HMM regime overlay — probabilistic belief over BULL/BEAR/HIGH_VOL.

    Returns the most recent RegimeBelief with:
      - probabilities: {BULL, BEAR, HIGH_VOL} posterior
      - dominant state
      - entropy (uncertainty)
      - transition_risk_to_bear (prob of flipping bearish next step)
      - long_bias / short_bias / size_mult
    """
    coord = _get_coordinator()
    if coord is None:
        return {"error": "portfolio coordinator not initialized"}
    return {
        "version": "ACT-XXIX-systemic-antifragility",
        "belief": coord.get_regime_belief(),
        "stats": coord.regime_hmm.stats(),
    }


@app.get("/api/portfolio/meta_labeler")
async def portfolio_meta_labeler():
    """ACT-XXVI: meta-labeler (triple-barrier LONG-side filter) stats.

    Returns per-direction outcome counts + current loss streaks +
    ML-readiness flag (becomes True once 300+ LONG outcomes recorded).
    """
    coord = _get_coordinator()
    if coord is None:
        return {"error": "portfolio coordinator not initialized"}
    return {
        "version": "ACT-XXIX-systemic-antifragility",
        "stats": coord.meta_labeler.stats(),
    }


@app.get("/api/portfolio/live_gate")
async def portfolio_live_gate(symbol: str = Query(default="BTCUSDT")):
    """ACT-XXV: evaluate the 6 LIVE_GATE unlock conditions.

    Returns the current gate status. The gate stays LOCKED (PAPER mode)
    until ALL 6 conditions pass simultaneously:
      1. global_win_rate_pct >= 52
      2. verified >= 300
      3. profit_factor > 1.20
      4. max_drawdown_pct < 10
      5. shadow_live_passed = True
      6. execution_engine_verified = True
    """
    coord = _get_coordinator()
    if coord is None:
        return {"error": "portfolio coordinator not initialized"}
    return await _symbol_scoped_paper_live_gate_state(coord, symbol=symbol)


@app.post("/api/portfolio/kill_switch")
async def portfolio_kill_switch(reason: str = "manual API trigger"):
    """ACT-XXV: manually trip the kill switch (halts all new trades)."""
    coord = _get_coordinator()
    if coord is None:
        return {"error": "portfolio coordinator not initialized"}
    coord.trip_kill_switch(reason)
    return {"status": "kill_switch_tripped", "reason": reason}


@app.post("/api/portfolio/reset_kill_switch")
async def portfolio_reset_kill_switch(reason: str = "manual API reset"):
    """ACT-XXV: manually reset the kill switch (requires explicit action)."""
    coord = _get_coordinator()
    if coord is None:
        return {"error": "portfolio coordinator not initialized"}
    coord.reset_kill_switch(reason)
    return {"status": "kill_switch_reset", "reason": reason}


@app.get("/api/audit")
async def audit_events(
    day: str | None = Query(default=None),
    limit: int = Query(default=100, le=1000),
    tail: bool = Query(default=False),
    type: str | None = Query(default=None),
):
    """If tail=True, return the most recent `limit` events (chronologically reversed).
    If type=... is provided, filter by event_type."""
    if tail:
        # collect all, then take last N (with optional type filter)
        all_events = []
        for ev in _audit.iter_events(day=day):
            if type and ev.event_type != type:
                continue
            all_events.append(ev.model_dump())
        events = all_events[-limit:]
        return {"count": len(events), "total_in_log": len(all_events), "events": events}
    else:
        events = []
        for ev in _audit.iter_events(day=day):
            if type and ev.event_type != type:
                continue
            events.append(ev.model_dump())
            if len(events) >= limit:
                break
        return {"count": len(events), "events": events}


@app.get("/api/state")
async def state():
    return {
        "latest_ticks": {sym: t.payload for sym, t in _scheduler.latest_tick.items()},
        "open_positions": [
            {
                "symbol": p.symbol, "qty": p.qty, "entry_price": p.entry_price,
                "entry_ts": p.entry_ts, "stop": p.stop_price, "target": p.target_price,
            } for p in _executor.positions.values() if p.status == "OPEN"
        ],
        "scanner_b_sma": {k: round(v, 4) for k, v in _scanner_b.sma_cache.items()},
        "cursors": _retriever.cursor_state(),
    }


@app.get("/api/catalog")
async def catalog():
    return {"instruments": _retriever.catalog()}


@app.get("/api/replay/{day}")
async def replay(day: str):
    events = [ev.model_dump() for ev in _audit.iter_events(day=day)]
    return {"day": day, "count": len(events), "events": events}


# ---- ACT-XXVII: Research endpoints (STRICT_ADDITIVE) ----
#
# All endpoints below are NEW — they do not touch any existing endpoint or
# module. They expose the 6 research-layer priorities (PurgedKFold/CPCV,
# calibration, drift detection, research metrics, explainability, and
# observability) via JSON + Prometheus exposition.

@app.get("/api/research/state")
async def research_state():
    """ACT-XXVII: research coordinator state + last full-pass summary."""
    if _research_coord is None:
        return {
            "error": "research coordinator not initialized",
            "init_error": _research_init_err_msg,
            "version": "ACT-XXIX-systemic-antifragility",
        }
    last = _research_coord.get_last_report()
    return {
        "version": "ACT-XXIX-systemic-antifragility",
        "initialized": True,
        "n_predictions_loaded": len(_research_coord.predictions),
        "feature_names": _research_coord.feature_names,
        "last_pass": (last.to_dict() if last is not None else None),
        "drift_stats": _research_coord.get_drift_stats(),
        "explainer_stats": (
            _research_coord.get_explainer().stats()
            if _research_coord.get_explainer() is not None else None
        ),
    }


@app.post("/api/research/run_full_pass")
async def research_run_full_pass(limit: int = Query(default=0, ge=0, le=10000)):
    """ACT-XXVII Priority 1-5: run a full research pass on loaded predictions.

    Loads predictions from `predictions.jsonl` (or whatever was last loaded),
    then runs PurgedKFold + CPCV + calibration × 3 methods + drift replay +
    research metrics + explainer fit. Returns the aggregate report.
    Pass limit=0 to use all available records.
    """
    if _research_coord is None:
        return {"error": "research coordinator not initialized",
                "init_error": _research_init_err_msg}
    if limit > 0:
        _research_coord.load_predictions(limit=limit)
    elif len(_research_coord.predictions) == 0:
        _research_coord.load_predictions()
    report = _research_coord.run_full_pass()
    return report.to_dict()


@app.get("/api/research/calibration")
async def research_calibration(method: str = "isotonic"):
    """ACT-XXVII Priority 2: run calibration for one method on loaded predictions.

    Returns the CalibrationReport (Brier + ECE + reliability curve before/after).
    """
    if _research_coord is None:
        return {"error": "research coordinator not initialized"}
    if _research_coord.confidences is None or _research_coord.confidences.shape[0] == 0:
        _research_coord.load_predictions()
    if _research_coord.confidences is None or _research_coord.confidences.shape[0] == 0:
        return {"error": "no predictions loaded"}
    from .research import fit_and_evaluate
    rep = fit_and_evaluate(
        y_true=_research_coord.y,
        y_prob=_research_coord.confidences,
        method=method,
        extra={"endpoint": "/api/research/calibration"},
    )
    return rep.to_dict()


@app.get("/api/research/drift")
async def research_drift():
    """ACT-XXVII Priority 3: drift monitor state + last warnings."""
    if _research_coord is None:
        return {"error": "research coordinator not initialized"}
    return {
        "version": "ACT-XXIX-systemic-antifragility",
        "drift_stats": _research_coord.get_drift_stats(),
    }


@app.get("/api/research/metrics")
async def research_metrics(window: int = Query(default=50, ge=10, le=1000)):
    """ACT-XXVII Priority 4: research metrics (IC + rolling Sharpe/PF/MDD)."""
    if _research_coord is None:
        return {"error": "research coordinator not initialized"}
    if _research_coord.confidences is None or _research_coord.confidences.shape[0] == 0:
        _research_coord.load_predictions()
    if _research_coord.confidences is None or _research_coord.confidences.shape[0] == 0:
        return {"error": "no predictions loaded"}
    from .research import compute_research_metrics
    preds_signed = _research_coord.confidences * (2 * _research_coord.y - 1)
    realized = (2 * _research_coord.y - 1).astype(float)
    report = compute_research_metrics(
        predictions=preds_signed,
        realized_returns=realized,
        window=window,
        step=max(1, window // 10),
        extra={"endpoint": "/api/research/metrics"},
    )
    return report.to_dict()


@app.post("/api/research/explainer/fit")
async def research_explainer_fit(
    model_type: str = Query(default="tree"),
    prefer_shap: bool = Query(default=True),
):
    """ACT-XXVII Priority 5: fit the explainer surrogate model."""
    if _research_coord is None:
        return {"error": "research coordinator not initialized"}
    if _research_coord.X is None or _research_coord.X.shape[0] == 0:
        _research_coord.load_predictions()
        # Force rebuild of feature matrix
        _research_coord.X, _research_coord.y, _research_coord.confidences, _research_coord.timestamps = \
            _research_coord._build_feature_matrix()
    if _research_coord.X is None or _research_coord.X.shape[0] == 0:
        return {"error": "no predictions loaded"}
    from .research import fit_explainer
    expl = fit_explainer(
        X=_research_coord.X,
        y=_research_coord.y,
        feature_names=_research_coord.feature_names,
        model_type=model_type,
        prefer_shap=prefer_shap,
    )
    _research_coord.explainer = expl
    return {
        "version": "ACT-XXIX-systemic-antifragility",
        "stats": expl.stats(),
    }


@app.post("/api/research/explainer/explain")
async def research_explainer_explain(prediction: dict):
    """ACT-XXVII Priority 5: explain a single prediction's feature contributions.

    Body: a prediction dict (must contain the feature fields configured in
    ResearchCoordinator.feature_names).
    """
    if _research_coord is None:
        return {"error": "research coordinator not initialized"}
    if _research_coord.get_explainer() is None:
        return {"error": "explainer not fitted — POST /api/research/explainer/fit first"}
    explanation = _research_coord.explain_prediction(prediction)
    if explanation is None:
        return {"error": "explanation failed"}
    return {
        "version": "ACT-XXIX-systemic-antifragility",
        "explanation": explanation,
    }


@app.get("/api/research/explainer/history")
async def research_explainer_history():
    """ACT-XXVII Priority 5: feature importance history (for stability analysis)."""
    if _research_coord is None or _research_coord.get_explainer() is None:
        return {"error": "explainer not fitted",
                "history": []}
    history = _research_coord.get_explainer().feature_importance_history()
    return {
        "version": "ACT-XXIX-systemic-antifragility",
        "n_snapshots": len(history),
        "history": history,
    }


@app.get("/api/observability")
async def observability():
    """ACT-XXVII Priority 6: JSON snapshot of all Prometheus metrics."""
    if _metrics_registry is None:
        return {"error": "metrics registry not initialized",
                "init_error": _research_init_err_msg}
    return {
        "version": "ACT-XXIX-systemic-antifragility",
        "snapshot": _metrics_registry.stats(),
    }


@app.get("/metrics")
async def prometheus_metrics():
    """ACT-XXVII Priority 6: Prometheus exposition endpoint.

    Returns text/plain Prometheus format (scrape target for Prometheus /
    Grafana / VictoriaMetrics / etc.).
    """
    if _metrics_registry is None:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(
            "# metrics registry not initialized\n",
            media_type="text/plain; version=0.0.4",
        )
    body, content_type = _metrics_registry.expose()
    # Refresh runtime gauges on each scrape
    _metrics_registry.update_runtime_metrics()
    from fastapi.responses import Response
    return Response(content=body, media_type=content_type)


# ---- ACT-XXVIII: Institutional Validation endpoints (STRICT_ADDITIVE) ----
#
# All 6 endpoints below are NEW — they do not touch any existing endpoint
# or module. They expose the ACT-XXVIII validation battery (walk-forward,
# Monte Carlo, statistical, stress, capacity, institutional report) via
# JSON. Each accepts an optional JSON body with explicit arrays
# (`returns`, `y`, `y_pred`, `volumes`, `prices`, `depth_usd`,
# `strategy_returns`, `directions`); when not provided, inputs are
# derived from the loaded predictions.jsonl.

def _derive_returns_from_predictions() -> tuple[list[float], list[int], list[float], list[float]]:
    """Synthesise (returns, directions, y, y_pred) from loaded predictions.

    Each prediction with a known outcome (WIN/CORRECT vs LOSS/WRONG;
    SKIP/None is dropped) contributes:
      return = (+/-1) * confidence * ev
      direction = +1 if prediction LONG else -1
      y = 1.0 if WIN/CORRECT else 0.0
      y_pred = confidence
    """
    if _research_coord is None or not _research_coord.predictions:
        return [], [], [], []
    rets: list[float] = []
    dirs: list[int] = []
    ys:   list[float] = []
    yp:   list[float] = []
    for rec in _research_coord.predictions:
        if not is_proof_qualified(rec):
            continue
        outcome = (rec.get("outcome") or "").upper()
        if outcome in ("WIN", "CORRECT"):
            y_v = 1.0
        elif outcome in ("LOSS", "WRONG"):
            y_v = 0.0
        else:
            continue  # SKIP / None / unknown
        try:
            conf = float(rec.get("confidence") or 0.5)
            ev_v = float(rec.get("ev") or 0.0)
        except (TypeError, ValueError):
            continue
        sign = +1.0 if y_v == 1.0 else -1.0
        ret = sign * conf * ev_v
        direction = +1 if (rec.get("prediction") or "LONG").upper() == "LONG" else -1
        rets.append(float(ret))
        dirs.append(int(direction))
        ys.append(float(y_v))
        yp.append(float(conf))
    return rets, dirs, ys, yp


@app.post("/api/research/walkforward")
async def research_walkforward(request: Request):
    """ACT-XXVIII Module 1: walk-forward optimization.

    Body (all optional):
      scheme: "rolling" | "anchored" | "expanding" (default "rolling")
      train_size: int (default 100)
      test_size:  int (default 30)
      step:       int (default 20)
      y:          list[float] (auto-derived from predictions if absent)
      y_pred:     list[float] (auto-derived from predictions if absent)
    """
    if _research_coord is None:
        return {"error": "research coordinator not initialized",
                "init_error": _research_init_err_msg}
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    scheme = str(body.get("scheme", "rolling"))
    train_size = int(body.get("train_size", 100))
    test_size  = int(body.get("test_size", 30))
    step       = int(body.get("step", 20))
    y = body.get("y")
    yp = body.get("y_pred")
    if y is None or yp is None:
        _research_coord.load_predictions()
        _, _, ys, yps = _derive_returns_from_predictions()
        y = y if y is not None else ys
        yp = yp if yp is not None else yps
    if not y or not yp:
        return {"error": "no labelled predictions available"}
    from .research import run_walk_forward
    import numpy as _np
    rep = run_walk_forward(
        y=_np.asarray(y, dtype=float),
        y_pred=_np.asarray(yp, dtype=float),
        scheme=scheme, train_size=train_size, test_size=test_size, step=step,
        extra={"endpoint": "/api/research/walkforward"},
    )
    if _metrics_registry is not None:
        _metrics_registry.observe(
            "senecio_research_runs_total", 1,
            labels={"module": "walk_forward"},
        )
    return rep.to_dict()


@app.post("/api/research/montecarlo")
async def research_montecarlo(request: Request):
    """ACT-XXVIII Module 2: Monte Carlo validation.

    Body (all optional):
      returns:             list[float] (auto-derived from predictions if absent)
      n_bootstrap:         int (default 2000)
      n_reshuffle:         int (default 1000)
      ruin_threshold_pct:  float (default -0.20)
      slippage_bps_std:    float (default 2.0)
      fee_bps_std:         float (default 0.5)
      gap_penalty_bps:     float (default 0.5)
      random_seed:         int (default 1337)
    """
    if _research_coord is None:
        return {"error": "research coordinator not initialized",
                "init_error": _research_init_err_msg}
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    returns = body.get("returns")
    if returns is None:
        _research_coord.load_predictions()
        returns, _, _, _ = _derive_returns_from_predictions()
    if not returns:
        return {"error": "no returns available (provide `returns` in body)"}
    from .research import run_monte_carlo
    import numpy as _np
    rep = run_monte_carlo(
        returns=_np.asarray(returns, dtype=float),
        n_bootstrap=int(body.get("n_bootstrap", 2000)),
        n_reshuffle=int(body.get("n_reshuffle", 1000)),
        ruin_threshold_pct=body.get("ruin_threshold_pct", -0.20),
        slippage_bps_std=body.get("slippage_bps_std", 2.0),
        fee_bps_std=body.get("fee_bps_std", 0.5),
        gap_penalty_bps=body.get("gap_penalty_bps", 0.5),
        random_seed=body.get("random_seed", 1337),
        extra={"endpoint": "/api/research/montecarlo"},
    )
    if _metrics_registry is not None:
        _metrics_registry.observe(
            "senecio_research_runs_total", 1,
            labels={"module": "monte_carlo"},
        )
        _metrics_registry.set_gauge(
            "senecio_last_ic",  # we abuse this slot — TODO add a dedicated gauge
            float(rep.ruin_probability),
        )
    return rep.to_dict()


@app.post("/api/research/statistics")
async def research_statistics(request: Request):
    """ACT-XXVIII Module 3: statistical validation battery.

    Body (all optional):
      returns:           list[float] (auto-derived from predictions if absent)
      strategy_returns:  list[list[float]] — (T, N) matrix for PBO/WRC/SPA
      n_trials:          int (default 1) — for DSR deflation
      sharpe_benchmark:  float (default 0.0)
      n_bootstrap:       int (default 1000)
      periods_per_year:  int (default 252)
    """
    if _research_coord is None:
        return {"error": "research coordinator not initialized",
                "init_error": _research_init_err_msg}
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    returns = body.get("returns")
    if returns is None:
        _research_coord.load_predictions()
        returns, _, _, _ = _derive_returns_from_predictions()
    if not returns:
        return {"error": "no returns available"}
    from .research import run_statistical_battery
    import numpy as _np
    sr = body.get("strategy_returns")
    sr_arr = _np.asarray(sr, dtype=float) if sr is not None else None
    rep = run_statistical_battery(
        returns=_np.asarray(returns, dtype=float),
        strategy_returns=sr_arr,
        n_trials=int(body.get("n_trials", 1)),
        sharpe_benchmark=float(body.get("sharpe_benchmark", 0.0)),
        n_bootstrap=int(body.get("n_bootstrap", 1000)),
        periods_per_year=int(body.get("periods_per_year", 252)),
        extra={"endpoint": "/api/research/statistics"},
    )
    if _metrics_registry is not None:
        _metrics_registry.observe(
            "senecio_research_runs_total", 1,
            labels={"module": "statistical"},
        )
    return rep.to_dict()


@app.post("/api/research/stress")
async def research_stress(request: Request):
    """ACT-XXVIII Module 5: stress test battery.

    Body (all optional):
      returns:        list[float] (auto-derived from predictions if absent)
      directions:     list[int] (auto-derived; +1 LONG, -1 SHORT)
      vol_mult:       float (default 3.0)
      spread_bps:     float (default 5.0)
      latency_bps:    float (default 3.0)
      funding_bps:    float (default 10.0)
      gap_pct:        float (default -10.0)
      gap_position:   float (default 0.5)
      outage_trades:  int (default 5)
    """
    if _research_coord is None:
        return {"error": "research coordinator not initialized",
                "init_error": _research_init_err_msg}
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    returns = body.get("returns")
    directions = body.get("directions")
    if returns is None:
        _research_coord.load_predictions()
        returns, dirs, _, _ = _derive_returns_from_predictions()
        directions = directions if directions is not None else dirs
    if not returns:
        return {"error": "no returns available"}
    from .research import run_stress_battery
    import numpy as _np
    rep = run_stress_battery(
        returns=_np.asarray(returns, dtype=float),
        directions=_np.asarray(directions, dtype=int) if directions else None,
        vol_mult=float(body.get("vol_mult", 3.0)),
        spread_bps=float(body.get("spread_bps", 5.0)),
        latency_bps=float(body.get("latency_bps", 3.0)),
        funding_bps=float(body.get("funding_bps", 10.0)),
        gap_pct=float(body.get("gap_pct", -10.0)),
        gap_position=float(body.get("gap_position", 0.5)),
        outage_trades=int(body.get("outage_trades", 5)),
        extra={"endpoint": "/api/research/stress"},
    )
    if _metrics_registry is not None:
        _metrics_registry.observe(
            "senecio_research_runs_total", 1,
            labels={"module": "stress"},
        )
    return rep.to_dict()


@app.post("/api/research/capacity")
async def research_capacity(request: Request):
    """ACT-XXVIII Module 4: capacity model.

    Body (all optional):
      volumes:            list[float] — historical volume series
      prices:             list[float] — historical price series
      depth_usd:          float — top-of-book depth in USD
      gross_edge_bps:     float (default 50)
      trades_per_day:     float (default 10)
      fee_bps_per_trade:  float (default 2)
      capacity_target_usd: float (default 100000)
    """
    if _research_coord is None:
        return {"error": "research coordinator not initialized",
                "init_error": _research_init_err_msg}
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    volumes = body.get("volumes")
    prices  = body.get("prices")
    depth_usd = body.get("depth_usd")
    if volumes is None:
        # Fallback: synthesise volumes from the loaded predictions' price × ev
        _research_coord.load_predictions()
        vols = []
        for rec in _research_coord.predictions:
            try:
                p = float(rec.get("price_now") or 0.0)
                ev = float(rec.get("ev") or 0.0)
                # volume proxy = price × |return| × 1e6 (arbitrary scale)
                vols.append(max(p * abs(ev) * 1e6, 1.0))
            except (TypeError, ValueError):
                continue
        volumes = vols
        if prices is None:
            prices = [float(rec.get("price_now") or 1.0)
                      for rec in _research_coord.predictions]
    if not volumes:
        return {"error": "no volumes available"}
    from .research import estimate_capacity
    import numpy as _np
    rep = estimate_capacity(
        volumes=_np.asarray(volumes, dtype=float),
        prices=_np.asarray(prices, dtype=float) if prices else None,
        depth_usd=depth_usd,
        gross_edge_bps=float(body.get("gross_edge_bps", 50.0)),
        trades_per_day=float(body.get("trades_per_day", 10.0)),
        fee_bps_per_trade=float(body.get("fee_bps_per_trade", 2.0)),
        extra={"endpoint": "/api/research/capacity"},
    )
    if _metrics_registry is not None:
        _metrics_registry.observe(
            "senecio_research_runs_total", 1,
            labels={"module": "capacity"},
        )
    return rep.to_dict()


@app.post("/api/research/report")
async def research_report(request: Request):
    """ACT-XXVIII Module 6: single institutional research report.

    Orchestrates all 5 prior ACT-XXVIII modules + the existing ACT-XXVII
    calibration/drift/research-metrics/explainability/observability
    layers, then produces a single institutional report with:
      - Robustness scorecard (0..1 composite)
      - Deployment readiness scorecard (0..1 composite)
      - Live-gate explanation (read-only)
      - Every sub-module's full report

    Body (all optional):
      capacity_target_usd: float (default 100000)
      min_verified_n:      int (default 300)
      run_walk_forward:    bool (default true)
      run_monte_carlo:     bool (default true)
      run_statistical:     bool (default true)
      run_stress:          bool (default true)
      run_capacity:        bool (default true)
      persist_html:        bool (default false)
    """
    if _research_coord is None:
        return {"error": "research coordinator not initialized",
                "init_error": _research_init_err_msg}
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    import numpy as _np
    from .research import (
        run_walk_forward, run_monte_carlo, run_statistical_battery,
        run_stress_battery, estimate_capacity, fit_and_evaluate,
        build_institutional_report,
    )
    # Ensure predictions are loaded
    if not _research_coord.predictions:
        _research_coord.load_predictions()
    # Build matrices
    _research_coord.X, _research_coord.y, _research_coord.confidences, _research_coord.timestamps = \
        _research_coord._build_feature_matrix()
    # Derive returns/directions from predictions
    returns, directions, ys, yps = _derive_returns_from_predictions()

    wf_rep = mc_rep = stat_rep = stress_rep = cap_rep = None
    cal_rep = None

    # 1) Walk-forward
    if body.get("run_walk_forward", True) and ys:
        try:
            wf_rep = run_walk_forward(
                y=_np.asarray(ys, dtype=float),
                y_pred=_np.asarray(yps, dtype=float),
                scheme="rolling", train_size=100, test_size=30, step=20,
                extra={"endpoint": "/api/research/report"},
                persist=False,
            ).to_dict()
        except Exception as e:
            log.warning("WF in report failed: %s", e)

    # 2) Monte Carlo
    if body.get("run_monte_carlo", True) and returns:
        try:
            mc_rep = run_monte_carlo(
                returns=_np.asarray(returns, dtype=float),
                n_bootstrap=500, n_reshuffle=200,
                extra={"endpoint": "/api/research/report"},
                persist=False,
            ).to_dict()
        except Exception as e:
            log.warning("MC in report failed: %s", e)

    # 3) Statistical
    if body.get("run_statistical", True) and returns:
        try:
            stat_rep = run_statistical_battery(
                returns=_np.asarray(returns, dtype=float),
                n_bootstrap=500,
                extra={"endpoint": "/api/research/report"},
                persist=False,
            ).to_dict()
        except Exception as e:
            log.warning("Stat in report failed: %s", e)

    # 4) Stress
    if body.get("run_stress", True) and returns:
        try:
            stress_rep = run_stress_battery(
                returns=_np.asarray(returns, dtype=float),
                directions=_np.asarray(directions, dtype=int) if directions else None,
                extra={"endpoint": "/api/research/report"},
                persist=False,
            ).to_dict()
        except Exception as e:
            log.warning("Stress in report failed: %s", e)

    # 5) Capacity
    if body.get("run_capacity", True):
        try:
            # synthesise volumes from predictions
            vols = []
            prices = []
            for rec in _research_coord.predictions:
                try:
                    p = float(rec.get("price_now") or 0.0)
                    ev = float(rec.get("ev") or 0.0)
                    vols.append(max(p * abs(ev) * 1e6, 1.0))
                    prices.append(p)
                except (TypeError, ValueError):
                    continue
            if vols:
                cap_rep = estimate_capacity(
                    volumes=_np.asarray(vols, dtype=float),
                    prices=_np.asarray(prices, dtype=float),
                    gross_edge_bps=50.0,
                    extra={"endpoint": "/api/research/report"},
                    persist=False,
                ).to_dict()
        except Exception as e:
            log.warning("Capacity in report failed: %s", e)

    # 6) Calibration (one method)
    try:
        if _research_coord.y is not None and _research_coord.y.size > 0:
            cal_rep = fit_and_evaluate(
                y_true=_research_coord.y,
                y_prob=_research_coord.confidences,
                method="isotonic",
                extra={"endpoint": "/api/research/report"},
            ).to_dict()
    except Exception as e:
        log.warning("Calibration in report failed: %s", e)

    # Drift stats from coordinator
    drift_stats = _research_coord.get_drift_stats()

    # Live gate (read-only consumption)
    live_gate_state = None
    try:
        coord = _get_coordinator()
        if coord is not None:
            report_symbol = str(body.get("symbol") or "BTCUSDT")
            live_gate_state = await _symbol_scoped_paper_live_gate_state(
                coord,
                symbol=report_symbol,
            )
    except Exception as e:
        log.warning("Live-gate fetch in report failed: %s", e)

    # Verified predictions count
    verified_n = 0
    if live_gate_state is not None:
        verified_n = int(live_gate_state.get("verified", 0))

    # Observability snapshot
    obs_snapshot = _metrics_registry.stats() if _metrics_registry is not None else {}

    # Build the institutional report
    inst_rep = build_institutional_report(
        n_trades=len(returns),
        n_predictions=len(_research_coord.predictions),
        walk_forward_report=wf_rep,
        monte_carlo_report=mc_rep,
        statistical_report=stat_rep,
        capacity_report=cap_rep,
        stress_report=stress_rep,
        calibration_report=cal_rep,
        drift_stats=drift_stats,
        research_metrics_report=None,  # populated by /api/research/metrics
        explainer_stats=(
            _research_coord.get_explainer().stats()
            if _research_coord.get_explainer() is not None else None
        ),
        observability_snapshot=obs_snapshot,
        live_gate_state=live_gate_state,
        verified_predictions_n=verified_n,
        capacity_target_usd=float(body.get("capacity_target_usd", 100_000.0)),
        min_verified_n=int(body.get("min_verified_n", 300)),
        extra={"endpoint": "/api/research/report"},
        persist=True,
        persist_html=bool(body.get("persist_html", False)),
    )
    if _metrics_registry is not None:
        _metrics_registry.observe(
            "senecio_research_runs_total", 1,
            labels={"module": "institutional_report"},
        )
    return inst_rep.to_dict()


# ---- static frontend ----
@app.get("/")
async def root():
    idx = FRONTEND_DIR / "index.html"
    if idx.exists():
        return FileResponse(idx)
    return JSONResponse({"error": "frontend not built"}, status_code=404)


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ---- ACT-XXIX: Systemic Anti-Fragility Layer endpoints (STRICT_ADDITIVE) ----
# These 6 endpoints expose the anti-fragility stack built in
# backend/antifragility/. They do NOT modify any existing module — only
# add new endpoints that wrap the AntiFragilityCoordinator.

@app.get("/api/antifragility/state")
async def antifragility_state():
    """Full snapshot of the anti-fragility subsystems."""
    if _antifragility_coord is None:
        return {"error": "antifragility coordinator not initialized",
                "init_error": _af_init_err_msg,
                "version": "ACT-XXIX-systemic-antifragility"}
    return _antifragility_coord.snapshot()


@app.post("/api/antifragility/invariants/run")
async def antifragility_run_invariants():
    """Run all registered invariants and return per-invariant results."""
    if _antifragility_coord is None:
        return {"error": "antifragility coordinator not initialized"}
    return {"results": _antifragility_coord.run_invariants(),
            "summary": _antifragility_coord.invariants.summary()}


@app.post("/api/antifragility/lineage/explain")
async def antifragility_lineage_explain(body: dict):
    """Explain a prediction's ancestry (inputs + descendants in lineage DAG)."""
    if _antifragility_coord is None:
        return {"error": "antifragility coordinator not initialized"}
    prediction_id = body.get("prediction_id")
    if not prediction_id:
        return {"error": "prediction_id required"}
    return _antifragility_coord.get_prediction_ancestry(prediction_id)


@app.post("/api/antifragility/diagnostics/run")
async def antifragility_diagnostics_run(body: dict | None = None):
    """Run self-diagnostics with optional feature/prediction inputs."""
    if _antifragility_coord is None:
        return {"error": "antifragility coordinator not initialized"}
    body = body or {}
    return _antifragility_coord.run_diagnostics(
        features=body.get("features"),
        member_predictions=body.get("member_predictions"),
        sample_vector=body.get("sample_vector"),
    )


@app.get("/api/antifragility/architecture/validate")
async def antifragility_architecture_validate():
    """Validate the actual code structure against the declared architecture spec."""
    if _antifragility_coord is None:
        return {"error": "antifragility coordinator not initialized"}
    return _antifragility_coord.run_architecture_validation()


@app.post("/api/antifragility/market/simulate")
async def antifragility_market_simulate(body: dict):
    """Generate synthetic market data, scenarios, or simulate regime transitions.

    Body params:
      - mode: 'synthetic' (default) | 'scenario' | 'adversarial' | 'regime'
      - n_bars: int (for synthetic mode, default 100)
      - scenario_name: str (for scenario mode; defaults to 'all')
      - adversarial_kind: 'max_drawdown' | 'whipsaw_extreme' | 'tail_event'
      - regime_kind: 'bull_to_bear' | 'crash_recovery' | 'volatility_cycle'
    """
    if _antifragility_coord is None:
        return {"error": "antifragility coordinator not initialized"}
    mode = body.get("mode", "synthetic")
    if mode == "synthetic":
        n = int(body.get("n_bars", 100))
        bars = _antifragility_coord.generate_synthetic_bars(n)
        return {"mode": mode, "bars": bars, "n": len(bars)}
    elif mode == "scenario":
        name = body.get("scenario_name", "all")
        if name == "all":
            return {"mode": mode, "scenarios": _antifragility_coord.generate_scenarios()}
        else:
            scenarios = _antifragility_coord.generate_scenarios()
            return {"mode": mode, "scenario": scenarios.get(name)}
    elif mode == "adversarial":
        kind = body.get("adversarial_kind", "max_drawdown")
        if kind == "max_drawdown":
            bars = _antifragility_coord.adversarial.max_drawdown_path(
                n_bars=int(body.get("n_bars", 100)))
        elif kind == "whipsaw_extreme":
            bars = _antifragility_coord.adversarial.whipsaw_extreme(
                n_bars=int(body.get("n_bars", 50)))
        elif kind == "tail_event":
            bars = _antifragility_coord.adversarial.tail_event(
                direction=body.get("direction", "down"))
        else:
            return {"error": f"unknown adversarial_kind: {kind}"}
        return {"mode": mode, "kind": kind, "bars": [b.to_dict() for b in bars]}
    elif mode == "regime":
        kind = body.get("regime_kind", "bull_to_bear")
        if kind == "bull_to_bear":
            bars = _antifragility_coord.regime_sim.bull_to_bear(
                n_bars=int(body.get("n_bars", 60)))
        elif kind == "crash_recovery":
            bars = _antifragility_coord.regime_sim.crash_recovery()
        elif kind == "volatility_cycle":
            bars = _antifragility_coord.regime_sim.volatility_cycle()
        else:
            return {"error": f"unknown regime_kind: {kind}"}
        return {"mode": mode, "kind": kind, "bars": [b.to_dict() for b in bars]}
    else:
        return {"error": f"unknown mode: {mode}"}


@app.post("/api/antifragility/faults/inject")
async def antifragility_faults_inject(body: dict):
    """Inject a fault into the system (for chaos testing).

    Body params:
      - kind: one of exchange_outage, partial_fill, rejected_orders,
              latency_spike, packet_loss, desync, stale_quotes,
              wrong_symbol, schema_drift, time_jump, drift
      - kwargs: kind-specific parameters (duration_s, rate, etc.)
    """
    if _antifragility_coord is None:
        return {"error": "antifragility coordinator not initialized"}
    kind = body.get("kind")
    if not kind:
        return {"error": "kind required"}
    kwargs = {k: v for k, v in body.items() if k != "kind"}
    try:
        fault = _antifragility_coord.inject_fault(kind, **kwargs)
        return {"ok": True, "fault": fault}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/antifragility/faults/active")
async def antifragility_faults_active():
    """List currently-active faults."""
    if _antifragility_coord is None:
        return {"error": "antifragility coordinator not initialized"}
    return {"active": _antifragility_coord.active_faults()}


@app.post("/api/antifragility/experiments/register")
async def antifragility_experiments_register(body: dict):
    """Register a new experiment for reproducibility tracking."""
    if _antifragility_coord is None:
        return {"error": "antifragility coordinator not initialized"}
    required = ("name", "kind", "params", "metrics")
    for k in required:
        if k not in body:
            return {"error": f"missing required field: {k}"}
    return _antifragility_coord.register_experiment(
        name=body["name"], kind=body["kind"],
        params=body["params"], metrics=body["metrics"],
        artifacts=body.get("artifacts"),
        notes=body.get("notes", ""),
    )


@app.get("/api/antifragility/experiments/{experiment_id}/report")
async def antifragility_experiments_report(experiment_id: str):
    """Generate a reproducibility report for an experiment."""
    if _antifragility_coord is None:
        return {"error": "antifragility coordinator not initialized"}
    return _antifragility_coord.get_reproducibility_report(experiment_id)


@app.get("/api/antifragility/benchmarks")
async def antifragility_benchmarks_list():
    """List registered benchmarks and recent history."""
    if _antifragility_coord is None:
        return {"error": "antifragility coordinator not initialized"}
    return {
        "registered": _antifragility_coord.benchmarks.list_benchmarks(),
        "history": [r.to_dict() for r in _antifragility_coord.benchmarks.history(limit=20)],
    }


@app.post("/api/antifragility/benchmarks/run")
async def antifragility_benchmarks_run():
    """Run all registered benchmarks."""
    if _antifragility_coord is None:
        return {"error": "antifragility coordinator not initialized"}
    return {"results": _antifragility_coord.run_benchmarks()}


@app.get("/api/antifragility/checkpoint/{subsystem}")
async def antifragility_checkpoint_restore(subsystem: str):
    """Restore the latest clean checkpoint state for a subsystem."""
    if _antifragility_coord is None:
        return {"error": "antifragility coordinator not initialized"}
    state = _antifragility_coord.resilience.checkpoints.restore(subsystem)
    if state is None:
        return {"error": f"no clean checkpoint for {subsystem}",
                "subsystem": subsystem}
    return {"subsystem": subsystem, "state": state}


# ═══════════════════════════════════════════════════════════════════════════
# ACT FINAL_AUDIT (MYTHOS) — STRICT_ADDITIVE observability endpoints
#
# All endpoints below are NEW (additive). They expose the forensic pipeline
# (A3), watchdogs (A4), evidence tracker (A6), statistical study (A7),
# decision engine (A8), and executive report (A9).
#
# DO NOT modify trading logic. DO NOT unlock LIVE gate.
# ═══════════════════════════════════════════════════════════════════════════

FINAL_AUDIT_VERSION = "ACT-FINAL_AUDIT-MYTHOS-STRICT_ADDITIVE"


@app.get("/api/final_audit/version")
async def final_audit_version():
    """Return the FINAL_AUDIT infrastructure version stamp."""
    return {
        "final_audit_version": FINAL_AUDIT_VERSION,
        "base_app_version": app.version,
        "mode": "STRICT_ADDITIVE",
        "trade_mode": "PAPER_ONLY",
        "live_gate": "LOCKED",
        "freeze_tag": "PRE_LONG_FIX_FREEZE",
        "do_not_touch": [
            "prediction_model", "feature_engineering", "signal_generation",
            "verifier", "long_logic", "thresholds",
        ],
    }


@app.get("/api/final_audit/forensics/latest")
async def final_audit_forensics_latest():
    """Return the most recent forensic pipeline summary."""
    try:
        from .forensics import pipeline as fp
        return {
            "last_summary": fp.get_last_run_summary(),
            "recent_runs": fp.list_runs(limit=5),
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/final_audit/forensics/run")
async def final_audit_forensics_run():
    """Manually trigger the forensic pipeline (does NOT block verifier)."""
    try:
        from .forensics import pipeline as fp
        report = await fp.run_pipeline_async()
        return {"summary": report.get("summary"), "ran_at_utc": report.get("run_completed_at_utc")}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/final_audit/watchdogs/latest")
async def final_audit_watchdogs_latest():
    """Return the latest watchdog alerts (from append-only JSONL log)."""
    try:
        from .watchdogs import coordinator as wd
        return {
            "alerts": wd.latest_alerts(limit=50),
            "watchdog_names": [name for name, _ in wd.WATCHDOGS],
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/final_audit/evidence/progress")
async def final_audit_evidence_progress():
    """Return progress toward evidence targets (>=200 verified, >=100 long, >=100 short)."""
    try:
        from . import evidence_tracker
        return evidence_tracker.get_progress()
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/final_audit/study/run")
async def final_audit_study_run():
    """Manually trigger the full A7 statistical study + A8 decision + A9 report.

    This is a heavy operation (can take several seconds). Use sparingly.
    """
    try:
        from .research import final_audit_orchestrator as fa
        report = await fa.run_final_audit_async()
        # Return a compact summary — full report is saved to disk
        return {
            "FINAL_VERDICT": report.get("FINAL_VERDICT"),
            "GO_NO_GO": report.get("GO_NO_GO"),
            "CONFIDENCE_SCORE": report.get("CONFIDENCE_SCORE"),
            "ROOT_CAUSES_COUNT": len(report.get("ROOT_CAUSES_ORDERED", [])),
            "PATCH_RECOMMENDATIONS_COUNT": len(report.get("PATCH_RECOMMENDATIONS", [])),
            "EVIDENCE_PROGRESS": report.get("EVIDENCE_PROGRESS"),
            "report_generated_at_utc": report.get("report_generated_at_utc"),
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/final_audit/study/latest")
async def final_audit_study_latest():
    """Return the latest executive report (or empty if none yet)."""
    try:
        from .research import executive_report as er
        report = er.latest_report()
        if report is None:
            return {"available": False, "reason": "no executive report generated yet"}
        return {
            "available": True,
            "FINAL_VERDICT": report.get("FINAL_VERDICT"),
            "GO_NO_GO": report.get("GO_NO_GO"),
            "CONFIDENCE_SCORE": report.get("CONFIDENCE_SCORE"),
            "ROOT_CAUSES_ORDERED": report.get("ROOT_CAUSES_ORDERED"),
            "PATCH_RECOMMENDATIONS": report.get("PATCH_RECOMMENDATIONS"),
            "EVIDENCE_PROGRESS": report.get("EVIDENCE_PROGRESS"),
            "DECISION_DETAIL": report.get("DECISION_DETAIL"),
            "report_generated_at_utc": report.get("report_generated_at_utc"),
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/final_audit/audit_enrichment/schema")
async def final_audit_enrichment_schema():
    """Return the schema of the enriched audit fields (A2)."""
    try:
        from . import audit_enrichment
        return {
            "enrichment_version": audit_enrichment.ENRICHMENT_VERSION,
            "required_fields": audit_enrichment.REQUIRED_FIELDS,
            "field_count": len(audit_enrichment.REQUIRED_FIELDS),
            "model_version": audit_enrichment.MODEL_VERSION,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/final_audit/state")
async def final_audit_state():
    """Return the full FINAL_AUDIT infrastructure state in one call."""
    try:
        from .forensics import pipeline as fp
        from .watchdogs import coordinator as wd
        from . import evidence_tracker
        from .research import executive_report as er

        forensics_summary = fp.get_last_run_summary()
        evidence = evidence_tracker.get_progress(forensics_summary)
        latest_alerts = wd.latest_alerts(limit=10)
        latest_report = er.latest_report()

        return {
            "version": FINAL_AUDIT_VERSION,
            "freeze_tag": "PRE_LONG_FIX_FREEZE",
            "mode": "STRICT_ADDITIVE",
            "trade_mode": "PAPER_ONLY",
            "live_gate": "LOCKED",

            "evidence_progress": evidence,
            "forensics_last_run": forensics_summary,
            "forensics_recent_runs": fp.list_runs(limit=3),
            "watchdog_alerts_recent": latest_alerts,
            "watchdog_names": [name for name, _ in wd.WATCHDOGS],
            "executive_report_available": latest_report is not None,
            "executive_report_summary": ({
                "FINAL_VERDICT": latest_report.get("FINAL_VERDICT"),
                "GO_NO_GO": latest_report.get("GO_NO_GO"),
                "CONFIDENCE_SCORE": latest_report.get("CONFIDENCE_SCORE"),
                "report_generated_at_utc": latest_report.get("report_generated_at_utc"),
            } if latest_report else None),
        }
    except Exception as e:
        return {"error": str(e)}
