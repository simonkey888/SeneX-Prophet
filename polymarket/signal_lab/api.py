from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from .features import FeatureEngine
from .registry import ContradictionLedger, ExperimentRegistry
from .sources import mirrored_depth_provenance_finding
from .store import PointInTimeStore


class SignalLabService:
    def __init__(
        self,
        *,
        store: PointInTimeStore | None = None,
        experiments: ExperimentRegistry | None = None,
        contradictions: ContradictionLedger | None = None,
    ):
        self.store = store or PointInTimeStore()
        self.features = FeatureEngine(self.store)
        self.experiments = experiments or ExperimentRegistry()
        self.contradictions = contradictions or ContradictionLedger()
        self.ws_connected = False
        self.stale_data_count = 0

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def markets(self, as_of: str | None = None) -> list[dict[str, Any]]:
        as_of = as_of or self.now()
        visible = self.store.events_as_of(as_of, event_types={"MARKET_META"})
        latest: dict[str, Any] = {}
        for event in visible:
            payload = dict(event.payload)
            latest[event.market_id] = {
                "market_id": event.market_id,
                "title": payload.get("title") or payload.get("question") or event.market_id,
                "category": payload.get("category") or "OTHER",
                "end_time": payload.get("end_time") or payload.get("close_time"),
                "token_id": event.token_id,
                "as_of_event_time": event.event_time,
            }
        return [latest[key] for key in sorted(latest)]

    def market(self, market_id: str, as_of: str | None = None) -> dict[str, Any]:
        as_of = as_of or self.now()
        events = self.store.events_as_of(as_of, market_id=market_id)
        if not events:
            return {"market_id": market_id, "status": "NOT_AVAILABLE", "as_of": as_of}
        features = self.features.compute(market_id, as_of)
        fair = self.features.fair_value(market_id, as_of)
        books = [event for event in events if event.event_type in {"BOOK_SNAPSHOT", "BOOK_DELTA", "BEST_BID_ASK"}]
        trades = [event for event in events if event.event_type in {"TRADE", "LAST_TRADE_PRICE"}]
        meta = [event for event in events if event.event_type == "MARKET_META"]
        return {
            "market_id": market_id,
            "status": "PAPER_OBSERVED",
            "as_of": as_of,
            "metadata": dict(meta[-1].payload) if meta else {},
            "book": dict(books[-1].payload) if books else None,
            "recent_trades": [dict(event.payload) for event in trades[-25:]],
            "features": {key: value.to_dict() for key, value in features.items()},
            "signal": fair,
        }

    def system_truth(self, as_of: str | None = None) -> dict[str, Any]:
        as_of = as_of or self.now()
        events = self.store.events_as_of(as_of)
        last = events[-1] if events else None
        age = None if last is None else max(0.0, (datetime.now(timezone.utc) - last.event_dt).total_seconds())
        active = self.experiments.latest()
        active_id = active[-1]["payload"]["experiment_id"] if active else None
        feature_hash = None
        markets = self.markets(as_of)
        if markets:
            feature_hash = self.features.featureset_hash(self.features.compute(markets[0]["market_id"], as_of))
        return {
            "paper_only": True,
            "orders_enabled": False,
            "live_capital_locked": True,
            "real_order_network_calls": 0,
            "wallet_or_private_key_access": 0,
            "real_capital_actions": 0,
            "ws_connected": self.ws_connected,
            "last_event_age": age,
            "sequence_gaps": self.store.sequence_gaps(),
            "stale_data_count": self.stale_data_count,
            "raw_chain_tip_hash": self.store.chain.tip_hash,
            "replay_verified": self.store.chain.verify(),
            "active_experiment_id": active_id,
            "featureset_hash": feature_hash,
            "signal_status": "UNVALIDATED",
        }


def build_router(service: SignalLabService | None = None) -> APIRouter:
    service = service or SignalLabService()
    router = APIRouter()

    @router.get("/signal-lab", response_class=HTMLResponse)
    def live_terminal() -> HTMLResponse:
        return HTMLResponse(LIVE_TERMINAL_HTML)

    @router.get("/signal-lab/api/system-truth")
    def system_truth() -> JSONResponse:
        return JSONResponse(service.system_truth())

    @router.get("/signal-lab/api/markets")
    def markets() -> JSONResponse:
        return JSONResponse({"markets": service.markets(), "source": "POLYMARKET_OFFICIAL_ONLY"})

    @router.get("/signal-lab/api/market/{market_id}")
    def market(market_id: str) -> JSONResponse:
        return JSONResponse(service.market(market_id))

    @router.get("/signal-lab/api/features/{market_id}")
    def features(market_id: str, as_of: str | None = None) -> JSONResponse:
        timestamp = as_of or service.now()
        values = service.features.compute(market_id, timestamp)
        return JSONResponse({
            "market_id": market_id,
            "as_of": timestamp,
            "features": {key: value.to_dict() for key, value in values.items()},
            "featureset_hash": service.features.featureset_hash(values),
            "status": "RESEARCH",
        })

    @router.get("/signal-lab/api/experiments")
    def experiments() -> JSONResponse:
        return JSONResponse({
            "experiments": list(service.experiments.records),
            "contradictions": list(service.contradictions.records),
            "append_only": service.experiments.verify() and service.contradictions.verify(),
        })

    @router.get("/signal-lab/api/evidence")
    def evidence() -> JSONResponse:
        return JSONResponse({
            "raw_chain_tip_hash": service.store.chain.tip_hash,
            "raw_chain_entries": len(service.store.chain.entries),
            "raw_chain_verified": service.store.chain.verify(),
            "replay_hash": service.store.chain.replay_hash(),
            "source_policy": "POLYMARKET_OFFICIAL_ONLY",
        })

    @router.get("/signal-lab/api/research/mirror-001")
    def mirror_research() -> JSONResponse:
        return JSONResponse(mirrored_depth_provenance_finding())

    @router.websocket("/signal-lab/ws")
    async def signal_lab_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        service.ws_connected = True
        try:
            await websocket.send_json({"type": "SYSTEM_TRUTH", "payload": service.system_truth()})
            while True:
                message = await websocket.receive_text()
                await websocket.send_json({"type": "READ_ONLY_ACK", "message": message[:128], "payload": service.system_truth()})
        except WebSocketDisconnect:
            pass
        finally:
            service.ws_connected = False

    return router


LIVE_TERMINAL_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>SENEX LIVE TERMINAL</title>
<style>
:root{--bg:#06080b;--panel:#0b1016;--panel2:#0f1620;--line:#202b38;--text:#e8edf4;--muted:#748297;--good:#52d273;--warn:#e6b94d;--bad:#ef6672;--accent:#75a7ff;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);font:13px Inter,system-ui,sans-serif}body{min-height:100vh}.mono{font-family:var(--mono)}
header{height:54px;display:flex;align-items:center;gap:18px;padding:0 18px;border-bottom:1px solid var(--line);background:#080c11;position:sticky;top:0;z-index:4}.brand{font:700 14px var(--mono);letter-spacing:.08em}.paper{color:var(--warn);font:700 11px var(--mono)}.status{color:var(--muted);font:11px var(--mono)}.spacer{flex:1}input{background:var(--panel);border:1px solid var(--line);color:var(--text);padding:8px 10px;border-radius:6px;min-width:220px}
.shell{display:grid;grid-template-columns:240px minmax(420px,1fr) 330px;min-height:calc(100vh - 98px)}aside,main{border-right:1px solid var(--line);min-width:0}.pane-title{padding:12px 14px;color:var(--muted);font:700 10px var(--mono);letter-spacing:.12em;border-bottom:1px solid var(--line)}
#radar{overflow:auto}.market{padding:12px 14px;border-bottom:1px solid #141d27;cursor:pointer}.market:hover{background:var(--panel)}.market b{display:block;font-size:12px}.market small{display:block;color:var(--muted);margin-top:5px}.empty{padding:18px;color:var(--muted)}
.center{display:grid;grid-template-rows:auto 260px auto}.market-head{padding:18px;border-bottom:1px solid var(--line)}.market-head h1{font-size:18px;margin:0 0 7px}.sub{color:var(--muted);font:11px var(--mono)}.chart{display:flex;align-items:center;justify-content:center;background:linear-gradient(180deg,#0b1119,#080b10);color:var(--muted);border-bottom:1px solid var(--line);position:relative}.chart:before{content:"REALTIME PRICE / DEPTH OVERLAY";position:absolute;top:12px;left:14px;font:10px var(--mono);letter-spacing:.08em}.chartline{width:80%;height:1px;background:var(--accent);box-shadow:0 0 30px #75a7ff55}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line)}.metric{padding:14px;background:var(--panel)}.metric label{display:block;color:var(--muted);font:9px var(--mono);margin-bottom:7px}.metric strong{font:15px var(--mono)}
.intel{background:#080c11}.block{border-bottom:1px solid var(--line);padding:13px}.block h3{font:700 10px var(--mono);color:var(--muted);letter-spacing:.1em;margin:0 0 10px}.rows{display:grid;gap:6px}.row{display:flex;justify-content:space-between;font:11px var(--mono)}.unvalidated{color:var(--warn)}
.bottom{height:44px;border-top:1px solid var(--line);display:flex;align-items:center;gap:4px;padding:0 12px;background:#080c11;overflow:auto;white-space:nowrap}.tab{padding:7px 10px;border:1px solid transparent;color:var(--muted);font:10px var(--mono)}.tab.active{border-color:var(--line);color:var(--text);background:var(--panel)}
.truth{position:fixed;right:12px;bottom:52px;width:318px;max-height:260px;overflow:auto;background:#080c11ee;border:1px solid var(--line);border-radius:8px;padding:10px;z-index:5}.truth-title{font:700 10px var(--mono);color:var(--accent);margin-bottom:8px}.truth .row{font-size:9px}.yes{color:var(--good)}.no{color:var(--bad)}
@media(max-width:980px){.shell{grid-template-columns:190px 1fr}.intel{grid-column:1/-1;border-top:1px solid var(--line);display:grid;grid-template-columns:repeat(3,1fr)}.truth{width:280px}.center{grid-template-rows:auto 220px auto}}
@media(max-width:680px){header{height:auto;min-height:58px;flex-wrap:wrap;padding:9px 12px;gap:9px}header input{order:3;width:100%;min-width:0}.shell{display:block}.shell>aside,.shell>main{border-right:0;border-bottom:1px solid var(--line)}#radar{max-height:210px}.center{grid-template-rows:auto 180px auto}.metrics{grid-template-columns:repeat(2,1fr)}.intel{display:block}.truth{position:static;width:auto;margin:10px}.bottom{position:sticky;bottom:0}.market-head{padding:14px}}
</style>
</head>
<body>
<header><span class="brand">SENEX LIVE</span><span class="paper">PAPER ONLY</span><span id="source-health" class="status">SOURCE HEALTH · —</span><span id="ws" class="status">WS · CONNECTING</span><span class="spacer"></span><input id="search" aria-label="Search markets" placeholder="Search markets"></header>
<section class="shell">
<aside><div class="pane-title">MARKET RADAR · MOVERS · CLOSING SOON · ANOMALIES</div><div id="radar"><div class="empty">Waiting for official market evidence…</div></div></aside>
<main class="center"><div class="market-head"><h1 id="title">Select a market</h1><div id="meta" class="sub">READ ONLY · OFFICIAL POLYMARKET SOURCES</div></div><div class="chart"><div class="chartline"></div></div><div id="metrics" class="metrics"><div class="metric"><label>YES / MID</label><strong>—</strong></div><div class="metric"><label>SPREAD</label><strong>—</strong></div><div class="metric"><label>VISIBLE DEPTH</label><strong>—</strong></div><div class="metric"><label>CLOSE ETA</label><strong>—</strong></div></div></main>
<aside class="intel"><div class="block"><h3>ORDER BOOK / RECENT TRADES</h3><div id="book" class="rows"><div class="row"><span>status</span><span>NOT_AVAILABLE</span></div></div></div><div class="block"><h3>INTELLIGENCE</h3><div id="features" class="rows"></div></div><div class="block"><h3>SENEX SIGNAL / FAIR VALUE</h3><div class="row"><span>validation</span><span class="unvalidated">UNVALIDATED</span></div><div class="row"><span>fair value</span><span id="fair">—</span></div><div class="row"><span>claim</span><span>RESEARCH</span></div></div></aside>
</section>
<div class="bottom"><span class="tab active">HISTORY</span><span class="tab">VOLUME</span><span class="tab">OI</span><span class="tab">PUBLIC FLOW</span><span class="tab">FEATURES</span><span class="tab">EXPERIMENTS</span><span class="tab">EVIDENCE</span><span class="tab">SYSTEM TRUTH</span></div>
<div class="truth"><div class="truth-title">SYSTEM TRUTH</div><div id="truth" class="rows"></div></div>
<script>
const $=s=>document.querySelector(s);let markets=[];let selected=null;
function safe(v){return v===null||v===undefined?'—':String(v)}
function rows(obj){return Object.entries(obj).map(([k,v])=>`<div class="row"><span>${k}</span><span>${safe(v)}</span></div>`).join('')}
async function get(url){const r=await fetch(url,{cache:'no-store'});if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()}
async function loadTruth(){try{const d=await get('/signal-lab/api/system-truth');$('#truth').innerHTML=rows(d);$('#source-health').textContent=`SOURCE HEALTH · ${d.replay_verified?'OK':'CHECK'}`}catch(e){$('#source-health').textContent='SOURCE HEALTH · ERROR'}}
async function loadMarkets(){const d=await get('/signal-lab/api/markets');markets=d.markets||[];renderRadar(markets)}
function renderRadar(list){$('#radar').innerHTML=list.length?list.map(m=>`<div class="market" data-id="${m.market_id}"><b>${safe(m.title)}</b><small>${safe(m.category)} · ${safe(m.end_time)}</small></div>`).join(''):'<div class="empty">No point-in-time markets loaded.</div>';document.querySelectorAll('.market').forEach(el=>el.onclick=()=>selectMarket(el.dataset.id))}
async function selectMarket(id){selected=id;const d=await get(`/signal-lab/api/market/${encodeURIComponent(id)}`);$('#title').textContent=d.metadata?.title||d.metadata?.question||id;$('#meta').textContent=`${d.status} · ${d.as_of}`;const f=d.features||{};$('#features').innerHTML=['F01','F03','F08','F09','F11','F12','F14','F15'].map(k=>`<div class="row"><span>${k}</span><span>${safe(f[k]?.value)}</span></div>`).join('');$('#fair').textContent=safe(d.signal?.fair_value);const b=d.book||{};$('#book').innerHTML=`<div class="row"><span>bids</span><span>${(b.bids||[]).length}</span></div><div class="row"><span>asks</span><span>${(b.asks||[]).length}</span></div><div class="row"><span>trades</span><span>${(d.recent_trades||[]).length}</span></div>`;const vals=[d.signal?.mid_price,f.F04?.value,f.F05?.value,f.F10?.value];$('#metrics').querySelectorAll('strong').forEach((el,i)=>el.textContent=safe(vals[i]))}
$('#search').addEventListener('input',e=>{const q=e.target.value.toLowerCase();renderRadar(markets.filter(m=>(m.title||'').toLowerCase().includes(q)||(m.category||'').toLowerCase().includes(q)))})
function connect(){const scheme=location.protocol==='https:'?'wss':'ws';const ws=new WebSocket(`${scheme}://${location.host}/signal-lab/ws`);ws.onopen=()=>{$('#ws').textContent='WS · CONNECTED'};ws.onmessage=()=>loadTruth();ws.onclose=()=>{$('#ws').textContent='WS · RECONNECTING';setTimeout(connect,3000)};ws.onerror=()=>ws.close()}
loadTruth();loadMarkets().catch(()=>{});connect();setInterval(loadTruth,10000);
</script>
</body></html>'''
