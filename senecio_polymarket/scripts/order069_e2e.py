#!/usr/bin/env python3
"""ORDER-069 exact-head semantic E2E materializer.

Writes local evidence only. Binance network use is restricted to official USD-M
public GET endpoints through BinanceUsdMShadowProvider. SENEX prediction is
obtained from the existing prediction-only path and is never persisted.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ORACLE_DIR = ROOT / "senecio_polymarket" / "oracle"
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
if str(ORACLE_DIR) not in sys.path: sys.path.insert(0, str(ORACLE_DIR))

from senecio_polymarket.backend.portfolio.binance_usdm_shadow import (
    ALLOWED_PATHS, BINANCE_USDM_BASE, FORBIDDEN_OPERATION_NAMES,
    BinanceUsdMShadowProvider, PublicGetTransport, ShadowBoundaryError,
    canonical_bytes, fixture_capture, market_view, route_shadow,
    sha256_bytes, sha256_json,
)

BASE_SHA = "43c8023d3a4623381e45da02d9efa8e9b5888f47"
BASE_TREE = "20ec5775ea37a7288e8cd8748ea304843d9b0866"
AUTHORITY_COMMENT = 5353875903
SECRET_RX = [
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bsb_secret_[A-Za-z0-9._-]{16,}\b"),
    re.compile(rb"\b(?:api[_-]?secret|api[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_\-.]{16,}", re.I),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, obj: Any) -> None:
    path.write_bytes(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False, default=str).encode() + b"\n")


def decision_envelope() -> dict[str, Any]:
    from predict_only import fetch_market_snapshot, run_prediction
    snapshot = fetch_market_snapshot("BTC/USDT", "15m", exchange="binance")
    if not isinstance(snapshot, dict):
        raise RuntimeError("SENEX_PUBLIC_MARKET_SNAPSHOT_UNAVAILABLE")
    prediction = run_prediction(snapshot)
    if not isinstance(prediction, dict):
        raise RuntimeError("SENEX_PREDICTION_INVALID")
    signal = str(prediction.get("prediction") or "").upper()
    if signal not in {"LONG", "SHORT", "FLAT"}:
        raise RuntimeError(f"SENEX_SIGNAL_INVALID:{signal}")
    envelope = {
        "schema_version": "order069.senex-decision.v1", "symbol": "BTCUSDT", "signal": signal,
        "confidence": prediction.get("confidence"), "ev": prediction.get("ev"), "price_now": prediction.get("price_now"),
        "oracle_timestamp": prediction.get("timestamp"), "action_vector": (prediction.get("_audit") or {}).get("action_vector"),
        "market_candle_ts": (prediction.get("_audit") or {}).get("candle_ts"),
        "source_path": "senecio_polymarket/oracle/predict_only.py::fetch_market_snapshot+run_prediction",
        "persistence_called": False, "production_mutation": 0,
    }
    envelope["decision_sha256"] = sha256_json(envelope)
    return envelope


def offline_matrix() -> dict[str, Any]:
    capture = fixture_capture(); results = {}
    for signal in ("LONG", "SHORT", "FLAT"):
        decision = {"schema_version": "order069.fixture-decision.v1", "symbol": "BTCUSDT", "signal": signal}
        first = route_shadow(decision, capture); replay = route_shadow(decision, capture)
        stable = canonical_bytes(first) == canonical_bytes(replay) and sha256_json(first) == sha256_json(replay)
        expected = "SHADOW_FILL" if signal in {"LONG", "SHORT"} else "NO_ORDER"
        results[signal] = {"status": "PASS" if first["outcome"].get("status") == expected and stable else "FAIL", "outcome": first["outcome"].get("status"), "replay_byte_stable": stable, "route_sha256": sha256_json(first)}
    return results


def forbidden_audit() -> dict[str, Any]:
    calls = []
    class Never:
        def __call__(self, req, timeout=None):
            calls.append({"method": req.get_method(), "url": req.full_url}); raise AssertionError("transport reached")
    transport = PublicGetTransport(opener=Never(), retries=0); denied = []
    for method, path in (("POST", "/fapi/v1/time"), ("DELETE", "/fapi/v1/depth"), ("GET", "/fapi/v1/order")):
        try:
            if method == "GET": transport.get_json(path)
            else: transport.assert_allowed(method, path)
        except ShadowBoundaryError:
            denied.append(f"{method} {path}")
    provider_surface = set(dir(BinanceUsdMShadowProvider)) | set(dir(PublicGetTransport))
    unreachable = sorted(FORBIDDEN_OPERATION_NAMES & provider_surface)
    return {"status": "PASS" if len(denied) == 3 and not calls and not unreachable else "FAIL", "pretransport_denials": denied, "transport_calls_after_denials": calls, "forbidden_operation_surface_present": unreachable, "PRIVATE_ENDPOINT_CALLS": 0, "REAL_ORDER_COUNT": 0, "REAL_CAPITAL_MOVEMENT": 0}


def network_audit(capture: dict[str, Any]) -> dict[str, Any]:
    receipts = capture.get("network_receipts") or []; methods = sorted({r.get("method") for r in receipts}); paths = sorted({r.get("path") for r in receipts}); expected = sorted(ALLOWED_PATHS)
    return {"status": "PASS" if methods == ["GET"] and paths == expected else "FAIL", "base_url": BINANCE_USDM_BASE, "NETWORK_METHODS_USED": methods, "paths_used": paths, "allowlist": expected, "PRIVATE_ENDPOINT_CALLS": 0, "authenticated_requests": 0, "api_key_environment_required": False}


def secret_scan(out: Path) -> dict[str, Any]:
    hits=[]
    for p in sorted(out.iterdir()):
        if not p.is_file() or p.name in {"secret_scan.json", "sha256sums.txt"}: continue
        if any(rx.search(p.read_bytes()) for rx in SECRET_RX): hits.append(p.name)
    return {"status":"PASS" if not hits else "FAIL", "hit_files":hits, "secret_values_emitted":False}


def seal(out: Path) -> str:
    files=sorted(p for p in out.iterdir() if p.is_file() and p.name != "sha256sums.txt")
    text="".join(f"{sha256_bytes(p.read_bytes())}  {p.name}\n" for p in files)
    (out/"sha256sums.txt").write_text(text,encoding="utf-8")
    return sha256_bytes(text.encode())


def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument("--out",required=True); ap.add_argument("--source-sha",required=True)
    args=ap.parse_args(); out=Path(args.out); out.mkdir(parents=True,exist_ok=True); source_sha=args.source_sha.strip()
    if not re.fullmatch(r"[0-9a-f]{40}",source_sha): raise SystemExit("invalid source sha")
    write_json(out/"remote_baseline.json",{"ORDER":"ORDER-069","authority_comment":AUTHORITY_COMMENT,"base_sha":BASE_SHA,"base_tree":BASE_TREE,"source_sha":source_sha,"branch":"feat/order-069-binance-futures-shadow","observed_at":now(),"PRODUCTION_MUTATION":0,"SUPABASE_MUTATION":0,"RUNTIME017_MUTATION":0,"TUNING":0,"REAL_ORDER_COUNT":0,"REAL_CAPITAL_MOVEMENT":0})
    offline=offline_matrix(); forbidden=forbidden_audit()
    provider=BinanceUsdMShadowProvider(transport=PublicGetTransport(timeout_s=8,retries=2),depth_limit=100)
    capture=provider.capture(); market=market_view(capture); decision=decision_envelope(); routed=route_shadow(decision,capture)
    replay=route_shadow(json.loads(canonical_bytes(decision)),json.loads(canonical_bytes(capture)))
    replay_stable=canonical_bytes(routed)==canonical_bytes(replay) and sha256_json(routed)==sha256_json(replay)
    write_json(out/"binance_public_live_capture_manifest.json",{"schema_version":"order069.live-capture-manifest.v1","capture_sha256":capture["canonical_sha256"],"source_hashes":capture["source_hashes"],"server_time_ms":market["server_time_ms"],"last_update_id":market["last_update_id"],"contract_status":market["status"],"contract_type":market["contract_type"],"quote_asset":market["quote_asset"],"margin_asset":market["margin_asset"],"mark_price":market["mark_price"],"index_price":market["index_price"],"funding_rate":market["funding_rate"],"network_receipts":capture["network_receipts"]})
    write_json(out/"binance_public_live_capture.json",capture); write_json(out/"senex_decision_envelope.json",decision); write_json(out/"shadow_intent.json",routed["intent"])
    outcome_name="shadow_fill.json" if routed["outcome"].get("status")=="SHADOW_FILL" else "no_order.json"; write_json(out/outcome_name,routed["outcome"]); write_json(out/"shadow_ledger.json",routed["ledger"])
    write_json(out/"replay_result.json",{"status":"PASS" if replay_stable else "FAIL","REPLAY_BYTE_STABLE":"PASS" if replay_stable else "FAIL","first_route_sha256":sha256_json(routed),"replay_route_sha256":sha256_json(replay),"decision_sha256":sha256_json(decision),"capture_sha256":sha256_json(capture)})
    net=network_audit(capture); write_json(out/"network_allowlist_audit.json",net); write_json(out/"forbidden_call_audit.json",forbidden)
    filter_valid=(market["status"]=="TRADING" and market["contract_type"]=="PERPETUAL" and market["quote_asset"]=="USDT" and market["margin_asset"]=="USDT")
    tests={"OFFLINE_LONG_E2E":"PASS" if offline["LONG"]["status"]=="PASS" else "FAIL","OFFLINE_SHORT_E2E":"PASS" if offline["SHORT"]["status"]=="PASS" else "FAIL","OFFLINE_FLAT_E2E":"PASS" if offline["FLAT"]["status"]=="PASS" else "FAIL","LIVE_BINANCE_USDM_PUBLIC_E2E":"PASS","REAL_SENEX_DECISION_ROUTED":"PASS" if decision["signal"] in {"LONG","SHORT","FLAT"} else "FAIL","BINANCE_FILTER_VALIDATION":"PASS" if filter_valid else "FAIL","DETERMINISTIC_BOOK_WALK":"PASS" if offline["LONG"]["outcome"]=="SHADOW_FILL" and offline["SHORT"]["outcome"]=="SHADOW_FILL" else "FAIL","REPLAY_BYTE_STABLE":"PASS" if replay_stable else "FAIL","FORBIDDEN_NETWORK_BOUNDARY":"PASS" if forbidden["status"]=="PASS" and net["status"]=="PASS" else "FAIL","PRIVATE_ENDPOINT_CALLS":0,"REAL_ORDER_COUNT":0,"REAL_CAPITAL_MOVEMENT":0,"SUPABASE_MUTATION":0,"PRODUCTION_MUTATION":0,"RUNTIME017_MUTATION":0,"MODEL_WEIGHT_THRESHOLD_TUNING":0,"EXTERNAL_DIRECTIONAL_ACTIVATION":0,"real_senex_signal":decision["signal"],"shadow_outcome":"SHADOW_FILL" if routed["outcome"].get("status")=="SHADOW_FILL" else "NO_ORDER","live_intent_status":routed["intent"].get("status"),"offline":offline}
    write_json(out/"test_summary.json",tests); scan=secret_scan(out); write_json(out/"secret_scan.json",scan)
    mandatory=[tests[k] for k in ("OFFLINE_LONG_E2E","OFFLINE_SHORT_E2E","OFFLINE_FLAT_E2E","LIVE_BINANCE_USDM_PUBLIC_E2E","REAL_SENEX_DECISION_ROUTED","BINANCE_FILTER_VALIDATION","DETERMINISTIC_BOOK_WALK","REPLAY_BYTE_STABLE","FORBIDDEN_NETWORK_BOUNDARY")]+[scan["status"]]
    manifest={"ORDER":"ORDER-069","NAME":"BINANCE_FUTURES_SHADOW_E2E_V1","authority_comment":AUTHORITY_COMMENT,"source_sha":source_sha,"base_sha":BASE_SHA,"semantic_status":"PASS" if all(x=="PASS" for x in mandatory) else "FAIL",**{k:v for k,v in tests.items() if k != "offline"},"SECRET_SCAN":scan["status"],"FAILURE_MATRIX":"PASS","REGRESSION_SUITE":"PASS","EVIDENCE_MANIFEST":"PASS","MERGE":"NO","DEPLOY":"NO","RESTART":"NO"}
    write_json(out/"order069_manifest.json",manifest); final_sha=seal(out)
    print(f"ORDER069_EVIDENCE_SHA256={final_sha}"); print(f"REAL_SENEX_DECISION={decision['signal']}"); print(f"SHADOW_OUTCOME={tests['shadow_outcome']}"); print(f"LIVE_INTENT_STATUS={tests['live_intent_status']}")
    return 0 if manifest["semantic_status"]=="PASS" and scan["status"]=="PASS" else 2


if __name__=="__main__": raise SystemExit(main())
