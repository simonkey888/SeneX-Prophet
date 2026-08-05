"""SENEX / SENECIO H-011 V3 committed control-plane API.

The committed manifest chain is authoritative. ``latest.json`` is a derived
cache and is served only when it is bound to the latest verified manifest.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

try:
    from h011_v3_committed_snapshot import (
        CommittedChainError,
        NoCommittedScan,
        load_committed_snapshot,
        replay_latest_committed,
        snapshot_age_sec,
    )
except ModuleNotFoundError:
    from polymarket.h011_v3_committed_snapshot import (  # type: ignore
        CommittedChainError,
        NoCommittedScan,
        load_committed_snapshot,
        replay_latest_committed,
        snapshot_age_sec,
    )

app = FastAPI(title="SENEX / SENECIO H-011 V3 Control Plane", docs_url=None, redoc_url=None)
RESULTS_DIR = Path(os.environ.get("H011_RESULTS_DIR", "/app/polymarket/results"))
RAW_CHAIN_DIR = RESULTS_DIR / "h011_v3" / "raw_chain_v1"
RUNTIME_STATE_FILE = RESULTS_DIR / "h011_v3" / "runtime_state.json"


def _runtime_state() -> dict[str, Any]:
    try:
        payload = json.loads(RUNTIME_STATE_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {
            "runtime_state": "STARTING",
            "readiness": False,
            "liveness": True,
            "scanner_enabled": False,
            "publication_enabled": False,
            "recovery_status": "UNKNOWN",
            "storage_status": "UNKNOWN",
            "chain_verified": False,
            "paper_only": True,
            "orders_enabled": False,
            "live_capital_locked": True,
            "legacy_mode": False,
        }


def _committed() -> tuple[dict[str, Any], Any]:
    return load_committed_snapshot(results_root=RESULTS_DIR, raw_directory=RAW_CHAIN_DIR)


def _diagnostic_error(exc: Exception) -> dict[str, Any]:
    runtime = _runtime_state()
    return {
        "error": type(exc).__name__,
        "detail": str(exc),
        "runtime": runtime,
        "paper_only": True,
        "orders_enabled": False,
        "live_capital_locked": True,
        "legacy_mode": False,
    }


@app.get("/api/v3/state")
def api_state():
    try:
        snapshot, chain = _committed()
    except (NoCommittedScan, CommittedChainError) as exc:
        return JSONResponse(_diagnostic_error(exc), status_code=503)
    snapshot = dict(snapshot)
    snapshot["runtime"] = _runtime_state()
    snapshot["raw_chain"] = chain.to_dict()
    snapshot["snapshot_age_sec"] = snapshot_age_sec(snapshot)
    return JSONResponse(snapshot)


@app.get("/api/v3/integrity")
def api_integrity():
    runtime = _runtime_state()
    try:
        snapshot, chain = _committed()
        replay = replay_latest_committed(results_root=RESULTS_DIR, raw_directory=RAW_CHAIN_DIR)
    except (NoCommittedScan, CommittedChainError) as exc:
        return JSONResponse({
            **_diagnostic_error(exc),
            "pipeline_version": "h011-integrity-v3",
            "window_s": 300,
            "raw_store_available": False,
            "replay_verified": False,
            "readiness": bool(runtime.get("readiness")),
        })
    discovery = (snapshot.get("aggregate_metrics") or {}).get("discovery") or {}
    return JSONResponse({
        "pipeline_version": snapshot.get("pipeline_version", "h011-integrity-v3"),
        "cohort_id": snapshot.get("cohort_id"),
        "window_s": snapshot.get("window_s", 300),
        "paper_only": True,
        "live_capital_locked": True,
        "orders_enabled": False,
        "code_sha": snapshot.get("code_sha", "unknown"),
        "config_sha": snapshot.get("config_sha", "unknown"),
        "snapshot_hash": snapshot.get("snapshot_hash"),
        "canonical_content_hash": snapshot.get("canonical_content_hash"),
        "snapshot_age_sec": snapshot_age_sec(snapshot),
        "raw_store_available": True,
        "file_sha256_matches": replay["file_sha256_matches"],
        "replay_verified": replay["replay_verified"],
        "chain_verified": chain.chain_verified,
        "raw_chain": chain.to_dict(),
        "runtime": runtime,
        "readiness": bool(runtime.get("readiness")) and chain.chain_verified,
        "legacy_mode": False,
        "discovery_status": discovery.get("status", "UNKNOWN"),
        "discovery_complete": bool(discovery.get("discovery_complete", False)),
        "discovery_replay_verified": bool(discovery.get("discovery_replay_verified", False)),
        "markets_selected": int(discovery.get("markets_selected", 0) or 0),
        "invariants": (snapshot.get("invariants") or {}).get("summary", {}),
    })


def _snapshot_section(name: str, default: Any):
    try:
        snapshot, _ = _committed()
    except (NoCommittedScan, CommittedChainError) as exc:
        return JSONResponse(_diagnostic_error(exc), status_code=503)
    return JSONResponse(snapshot.get(name, default))


@app.get("/api/v3/sources")
def api_sources():
    return _snapshot_section("source_health", {})


@app.get("/api/v3/funnel")
def api_funnel():
    return _snapshot_section("funnel", {})


@app.get("/api/v3/lifecycle")
def api_lifecycle():
    return _snapshot_section("lifecycle", {})


@app.get("/api/v3/invariants")
def api_invariants():
    return _snapshot_section("invariants", {})


@app.get("/api/v3/drift")
def api_drift():
    return _snapshot_section("drift", {})


@app.get("/api/v3/operations")
def api_operations():
    try:
        snapshot, _ = _committed()
    except (NoCommittedScan, CommittedChainError):
        return JSONResponse({"operations": [], "total": 0})
    operations = [
        record for record in snapshot.get("market_records", [])
        if record.get("record_status") == "SHADOW_EXECUTABLE"
    ]
    return JSONResponse({"operations": operations, "total": len(operations)})


@app.get("/api/v3/replay")
def api_replay():
    try:
        return JSONResponse(replay_latest_committed(results_root=RESULTS_DIR, raw_directory=RAW_CHAIN_DIR))
    except (NoCommittedScan, CommittedChainError) as exc:
        return JSONResponse(_diagnostic_error(exc), status_code=503)


@app.get("/api/v3/rejections")
def api_rejections():
    try:
        snapshot, _ = _committed()
    except (NoCommittedScan, CommittedChainError):
        return JSONResponse({"rejections": [], "total": 0})
    rejected = [
        record for record in snapshot.get("market_records", [])
        if str(record.get("record_status", "")).startswith("REJECTED_")
        or record.get("record_status") == "HISTORICAL_SIGNAL_ONLY"
    ]
    return JSONResponse({"rejections": rejected, "total": len(rejected)})


@app.get("/api/v3/alerts")
def api_alerts():
    try:
        snapshot, _ = _committed()
    except (NoCommittedScan, CommittedChainError) as exc:
        return JSONResponse({"alerts": [], **_diagnostic_error(exc)}, status_code=503)
    return JSONResponse({"alerts": snapshot.get("alerts", [])})


@app.get("/livez")
def livez():
    return JSONResponse({"ok": True, "liveness": True})


@app.get("/readyz")
def readyz():
    runtime = _runtime_state()
    ready = bool(runtime.get("readiness"))
    return JSONResponse({"ok": ready, "readiness": ready, "runtime_state": runtime.get("runtime_state")}, status_code=200 if ready else 503)


@app.get("/healthz")
def healthz():
    runtime = _runtime_state()
    blocking_runtime = str(runtime.get("runtime_state")) in {
        "BLOCKED_RAW_INTEGRITY", "BLOCKED_STORAGE_UNVERIFIED", "SCANNER_FAILED"
    }
    try:
        snapshot, chain = _committed()
        summary = (snapshot.get("invariants") or {}).get("summary", {})
        unknown = int(summary.get("unknown", 0) or 0)
        failed = [
            result for result in (snapshot.get("invariants") or {}).get("results", [])
            if result.get("status") == "FAIL" and result.get("severity") in ("BLOCKING", "CRITICAL")
        ]
        blocking_alerts = [alert for alert in snapshot.get("alerts", []) if alert.get("blocking")]
        operational_ok = not blocking_runtime and not failed and not blocking_alerts and chain.chain_verified
        status = (
            "BLOCKED" if not operational_ok
            else "DEGRADED" if runtime.get("runtime_state") == "DEGRADED" or unknown > 0
            else "HEALTHY"
        )
        return JSONResponse({
            "ok": operational_ok,
            "status": status,
            "liveness": True,
            "readiness": bool(runtime.get("readiness")) and chain.chain_verified,
            "validation_complete": unknown == 0,
            "unknown_invariants": unknown,
            "snapshot_age_sec": snapshot_age_sec(snapshot),
            "runtime": runtime,
            "raw_chain": chain.to_dict(),
            "blocking_alerts": [alert.get("code") for alert in blocking_alerts],
            "failed_invariants": [result.get("invariant_id") for result in failed],
            "source_summary": snapshot.get("source_health", {}),
            "paper_only": True,
            "orders_enabled": False,
            "live_capital_locked": True,
        })
    except NoCommittedScan as exc:
        status = "BLOCKED" if blocking_runtime else "NO_COMMITTED_SCAN"
        return JSONResponse({
            "ok": not blocking_runtime,
            "status": status,
            "liveness": True,
            "readiness": bool(runtime.get("readiness")),
            "validation_complete": False,
            "snapshot_age_sec": None,
            "runtime": runtime,
            "detail": str(exc),
            "paper_only": True,
            "orders_enabled": False,
            "live_capital_locked": True,
        })
    except CommittedChainError as exc:
        return JSONResponse({
            "ok": False,
            "status": "BLOCKED",
            "liveness": True,
            "readiness": False,
            "runtime": runtime,
            "detail": str(exc),
            "paper_only": True,
            "orders_enabled": False,
            "live_capital_locked": True,
        })


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(content=_DASHBOARD_HTML)


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#000000">
<title>SENECIO H-011 V3 — Control Plane</title>
<style>
:root{--bg:#000;--surface:#0a0a0a;--border:#1c1c1e;--text:#ededed;--text2:#767677;--green:#32d74b;--red:#ff453a;--blue:#0a84ff;--orange:#ff9f0a}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,sans-serif;margin:0;padding:40px 20px;-webkit-font-smoothing:antialiased;font-feature-settings:"tnum" 1}
.container{max-width:1000px;margin:0 auto}
.header{display:flex;justify-content:space-between;align-items:baseline;border-bottom:1px solid var(--border);padding-bottom:20px;margin-bottom:30px}
h1{font-size:22px;font-weight:500;margin:0;letter-spacing:-0.02em}
h1 .sub{font-weight:300;color:var(--text2);margin-left:6px}
.banner{background:rgba(255,69,58,0.08);border:1px solid rgba(255,69,58,0.25);border-radius:8px;padding:12px 16px;margin-bottom:30px;font-size:13px}
.banner strong{color:var(--red)}
h2{font-size:13px;font-weight:500;color:var(--text2);text-transform:uppercase;letter-spacing:0.06em;margin:30px 0 12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px}
.card-label{font-size:10px;color:var(--text2);text-transform:uppercase;margin-bottom:6px;letter-spacing:0.06em}
.card-value{font-size:22px;font-weight:400}
.green{color:var(--green)}.red{color:var(--red)}.blue{color:var(--blue)}.orange{color:var(--orange)}
.mono{font-family:"SF Mono",monospace;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--border)}
th{color:var(--text2);font-weight:400;font-size:10px;text-transform:uppercase}
.tag{font-size:10px;padding:2px 6px;border-radius:3px;display:inline-block}
.tag.pass{background:rgba(50,215,75,0.12);color:var(--green);border:1px solid rgba(50,215,75,0.25)}
.tag.fail{background:rgba(255,69,58,0.12);color:var(--red);border:1px solid rgba(255,69,58,0.25)}
.tag.unknown{background:rgba(118,118,119,0.12);color:var(--text2);border:1px solid rgba(118,118,119,0.25)}
.footer{margin-top:40px;padding-top:20px;border-top:1px solid var(--border);color:var(--text2);font-size:11px}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>SENECIO H-011 V3 <span class="sub">Control Plane</span></h1>
    <div style="font-size:12px;color:var(--text2)" id="last-updated">—</div>
  </div>
  <div class="banner">
    <strong>PAPER ONLY · NO ORDERS · LIVE CAPITAL LOCKED · NO REAL FILLS · NO REALIZED PNL</strong><br>
    VWAP history is not executable price. Two-leg CLOB execution is not atomic.
  </div>

  <div id="visible-error" class="banner" style="display:none"></div>
  <h2>Estado Operativo</h2>
  <div class="grid" id="runtime-grid"></div>

  <h2>Estado Global</h2>
  <div class="grid" id="global-grid"></div>

  <h2>Salud de Fuentes</h2>
  <table id="sources-table"><thead><tr><th>Fuente</th><th>Estado</th><th>Edad (ms)</th><th>Latencia</th><th>Fallos</th><th>Fallback</th></tr></thead><tbody></tbody></table>

  <h2>Funnel de Validación</h2>
  <table id="funnel-table"><thead><tr><th>Etapa</th><th>Cantidad</th></tr></thead><tbody></tbody></table>

  <h2>Invariantes</h2>
  <div class="grid" id="invariants-grid"></div>
  <table id="invariants-table"><thead><tr><th>ID</th><th>Estado</th><th>Severidad</th><th>Razón</th></tr></thead><tbody></tbody></table>

  <h2>Alertas</h2>
  <table id="alerts-table"><thead><tr><th>Severidad</th><th>Código</th><th>Título</th><th>Detalle</th></tr></thead><tbody></tbody></table>

  <div class="footer">SENECIO H-011 V3 · PAPER_ONLY · LIVE_CAPITAL_LOCKED · <a href="/api/v3/integrity" style="color:var(--text2)">/api/v3/integrity</a></div>
</div>
<script>
function displayMetric(value){return value===null||value===undefined?'—':value;}
function card(label,value){return `<div class="card"><div class="card-label">${label}</div><div class="card-value" style="font-size:16px">${displayMetric(value)}</div></div>`;}
async function load(){
  const errorBox=document.getElementById('visible-error');
  try{
    const r=await fetch('/api/v3/state',{cache:'no-store'});
    const d=await r.json();
    if(!r.ok)throw new Error(d.detail||d.error||`API ${r.status}`);
    errorBox.style.display='none';
    document.getElementById('last-updated').textContent=d.generated_at||'?';
    const runtime=d.runtime||{}; const chain=d.raw_chain||{};
    document.getElementById('runtime-grid').innerHTML=[
      ['Runtime',runtime.runtime_state],['Readiness',runtime.readiness],['Recovery',runtime.recovery_status],
      ['Storage',runtime.storage_status],['Scanner',runtime.scanner_enabled],['Sequence',chain.current_sequence],
      ['Manifest',chain.manifest_hash?chain.manifest_hash.slice(0,12)+'…':'—'],['Snapshot age',displayMetric(d.snapshot_age_sec)]
    ].map(x=>card(x[0],x[1])).join('');
    const gg=document.getElementById('global-grid');
    gg.innerHTML=[['Pipeline',d.pipeline_version],['Cohort',d.cohort_id],['Window',d.window_s+'s'],['Status',d.scan_status],['Paper Only',d.paper_only],['Capital Locked',d.live_capital_locked],['Orders',d.orders_enabled],['Snapshot SHA',d.snapshot_hash?d.snapshot_hash.slice(0,12)+'…':'—']].map(x=>card(x[0],x[1])).join('');
    const st=document.querySelector('#sources-table tbody');st.innerHTML='';
    Object.entries(d.source_health||{}).forEach(([k,v])=>{st.innerHTML+=`<tr><td>${k}</td><td><span class="tag ${(v.level||v.status)==='HEALTHY'?'pass':(v.level||v.status)==='UNKNOWN'?'unknown':'fail'}">${v.level||v.status}</span></td><td class="mono">${displayMetric(v.age_ms)}</td><td class="mono">${displayMetric(v.latency_ms)}</td><td>${displayMetric(v.consecutive_failures)}</td><td>${v.fallback_used?'SÍ':'no'}</td></tr>`});
    const ft=document.querySelector('#funnel-table tbody');ft.innerHTML='';Object.entries(d.funnel||{}).forEach(([k,v])=>{ft.innerHTML+=`<tr><td>${k}</td><td class="mono">${v}</td></tr>`});
    const inv=d.invariants||{}, summary=inv.summary||{};document.getElementById('invariants-grid').innerHTML=[['PASS',summary.pass||0],['FAIL',summary.fail||0],['UNKNOWN',summary.unknown||0]].map(x=>card(x[0],x[1])).join('');
    const it=document.querySelector('#invariants-table tbody');it.innerHTML='';(inv.results||[]).forEach(i=>{it.innerHTML+=`<tr><td class="mono">${i.invariant_id}</td><td><span class="tag ${i.status==='PASS'?'pass':i.status==='UNKNOWN'?'unknown':'fail'}">${i.status}</span></td><td>${i.severity}</td><td>${i.reason||''}</td></tr>`});
    const at=document.querySelector('#alerts-table tbody');at.innerHTML='';(d.alerts||[]).forEach(a=>{at.innerHTML+=`<tr><td>${a.severity}</td><td class="mono">${a.code}</td><td>${a.title}</td><td>${a.detail||''}</td></tr>`});if(!(d.alerts||[]).length)at.innerHTML='<tr><td colspan="4">Sin alertas</td></tr>';
  }catch(e){
    errorBox.style.display='block';errorBox.innerHTML=`<strong>API_ERROR</strong><br>${e.message}. Reintento automático en 10 segundos.`;
    try{const h=await fetch('/healthz',{cache:'no-store'});const d=await h.json();const runtime=d.runtime||{};document.getElementById('runtime-grid').innerHTML=[['Runtime',runtime.runtime_state||d.status],['Readiness',d.readiness],['Recovery',runtime.recovery_status],['Bloqueo',runtime.blocking_reason||d.detail]].map(x=>card(x[0],x[1])).join('');}catch(_){}
  }
}
load();setInterval(load,10000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
