from __future__ import annotations
from typing import Any
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from polymarket.signal_lab.api import SignalLabService
from .core import (
    FailureInjectionHarness, MegaResearchFusion, ResearchLedger, SystemTruth,
    render_read_only_terminal, terminal_projection,
)


class MegaResearchService:
    """Read-only development composition over accepted 018 state."""

    def __init__(self, *, signal_lab: SignalLabService | None = None, ledger: ResearchLedger | None = None):
        self.signal_lab = signal_lab or SignalLabService()
        self.fusion = MegaResearchFusion(self.signal_lab.store)
        self.ledger = ledger or ResearchLedger()

    def system_truth(self) -> SystemTruth:
        source = self.signal_lab.system_truth()
        replay = source.get("replay_verified")
        stale_count = source.get("stale_data_count")
        stale = None if stale_count is None else bool(stale_count)
        healthy = replay is True and not stale
        return SystemTruth(
            PAPER_ONLY=True, orders_enabled=False, live_capital_locked=True,
            source_connection="CONNECTED" if source.get("ws_connected") else "NOT_CONNECTED",
            last_event_age_ms=None if source.get("last_event_age") is None else int(max(0.0,float(source["last_event_age"]))*1000),
            sequence_gaps=source.get("sequence_gaps"), stale_data=stale,
            raw_chain_hash=source.get("raw_chain_tip_hash"),
            raw_chain_tip=len(self.signal_lab.store.chain.entries),
            replay_status="PASS" if replay is True else "FAIL" if replay is False else "UNKNOWN",
            active_experiment=source.get("active_experiment_id"),
            data_quality_state="HEALTHY" if healthy else "DEGRADED",
            blocking_reason=None if healthy else "source_or_replay_not_fully_healthy",
        )

    def market_projection(self, market_id: str, as_of: str | None = None) -> dict[str, Any]:
        timestamp=as_of or self.signal_lab.now()
        parent=self.signal_lab.market(market_id,timestamp)
        if parent.get("status")=="NOT_AVAILABLE":
            return {"market_id":market_id,"status":"NOT_AVAILABLE","as_of":timestamp,
                    "feature_snapshot":None,"fair_value":None}
        features=self.fusion.features.compute(market_id,timestamp)
        fair=dict(self.fusion.features.fair_value(market_id,timestamp))
        fair.update({"status":"UNVALIDATED_RESEARCH_ONLY","validated_edge":False})
        return {"market_id":market_id,"status":"RESEARCH_ONLY","as_of":timestamp,
                "parent_market":parent,
                "feature_snapshot":{"features":{k:v.to_dict() for k,v in features.items()},
                                    "feature_set_hash":self.fusion.features.featureset_hash(features)},
                "fair_value":fair}

    def projection(self, market_id: str | None = None):
        truth=self.system_truth(); market={}; micro={}; signal={}
        if market_id:
            snapshot=self.market_projection(market_id); market={"market_id":market_id,"status":snapshot["status"],"as_of":snapshot["as_of"]}
            values=(snapshot.get("feature_snapshot") or {}).get("features") or {}
            micro={name:(values.get(fid) or {}).get("value") for fid,name in (
                ("F01","book_imbalance"),("F02","depth_weighted_imbalance"),("F03","microprice_divergence"),
                ("F04","spread"),("F05","visible_depth"),("F07","quote_velocity"),("F09","book_staleness"),
                ("F11","depth_collapse"),("F12","liquidity_shock"),("F14","regime_score"))}
            fair=snapshot.get("fair_value") or {}
            signal={"status":fair.get("status"),"fair_value":fair.get("fair_value"),
                    "mid_price":fair.get("mid_price"),"feature_set_hash":fair.get("featureset_hash"),
                    "validated_edge":False}
        evidence={"authority_map":self.fusion.authority_map(),"authority_invariants":self.fusion.authority_invariants(),
                  "experiment_records":len(self.ledger.records),"experiment_chain_verified":self.ledger.verify(),
                  "validation_status":"NO_PRODUCTION_EDGE_CLAIM"}
        return terminal_projection(system_truth=truth,market=market,microstructure=micro,signal=signal,evidence=evidence)


def build_router(service: MegaResearchService | None = None) -> APIRouter:
    service=service or MegaResearchService(); router=APIRouter()
    @router.get("/mega-research",response_class=HTMLResponse)
    def terminal(market_id: str | None=None):
        return HTMLResponse(render_read_only_terminal(service.projection(market_id)))
    @router.get("/mega-research/api/system-truth")
    def system_truth():
        truth=service.system_truth()
        return JSONResponse({**truth.to_dict(),"health":FailureInjectionHarness.health(truth)})
    @router.get("/mega-research/api/authority")
    def authority():
        return JSONResponse({"authority":service.fusion.authority_map(),"invariants":service.fusion.authority_invariants()})
    @router.get("/mega-research/api/markets")
    def markets():
        return JSONResponse({"markets":service.signal_lab.markets(),"source_policy":"POLYMARKET_OFFICIAL_PUBLIC_ONLY",
                             "external_live_adapters":"DISABLED"})
    @router.get("/mega-research/api/market/{market_id}")
    def market(market_id: str,as_of: str | None=None):
        return JSONResponse(service.market_projection(market_id,as_of))
    @router.get("/mega-research/api/evidence")
    def evidence():
        return JSONResponse({"raw_chain_tip_hash":service.signal_lab.store.chain.tip_hash,
                             "raw_chain_entries":len(service.signal_lab.store.chain.entries),
                             "raw_chain_verified":service.signal_lab.store.chain.verify(),
                             "replay_hash":service.signal_lab.store.chain.replay_hash(),
                             "experiment_chain_verified":service.ledger.verify(),
                             "experiment_records":len(service.ledger.records),
                             "validated_edge":False,"claim":"RESEARCH_ONLY_NOT_PRODUCTION_ALPHA"})
    return router
