import asyncio
import json
import os
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend import admin, main_real, supabase_client
from backend.authority_snapshot import AuthoritySnapshotStore
from backend.order070_contracts import bps_to_decimal, canonical_ev_audit
from backend.runtime_provenance import runtime_provenance

ROOT = Path(__file__).resolve().parents[2]
SAFE = {"GET", "HEAD", "OPTIONS"}


class PublicBoundaryTests(unittest.TestCase):
    def test_public_fastapi_post_count_is_zero(self):
        routes=[r for r in main_real.app.router.routes if isinstance(r, APIRoute)]
        self.assertEqual(sum("POST" in (r.methods or set()) for r in routes), 0)
        self.assertFalse([(r.path, set(r.methods or set())-SAFE) for r in routes if set(r.methods or set())-SAFE])

    def test_public_openapi_has_no_unsafe_operations(self):
        schema=main_real.app.openapi()
        unsafe=[]
        for path, item in schema.get("paths", {}).items():
            for method in item:
                if method.lower() in {"post","put","patch","delete"}: unsafe.append((path,method))
        self.assertEqual(unsafe, [])

    def test_public_guard_denies_mutation_before_endpoint(self):
        client=TestClient(main_real.app)
        r=client.post("/api/oracle/score")
        self.assertEqual(r.status_code,405)
        self.assertEqual(r.headers.get("x-senex-public-decision"),"DENY_UNSAFE_METHOD")

    def test_admin_is_separate_and_fail_closed(self):
        self.assertIsNot(admin.admin_app, main_real.app)
        self.assertNotIn("admin_app", repr(main_real.app.router.routes))
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(HTTPException) as cm: admin.require_admin_auth(None)
            self.assertEqual(cm.exception.status_code,503)
        with mock.patch.dict(os.environ,{"SENEX_ADMIN_TOKEN":"expected"},clear=True):
            with self.assertRaises(HTTPException) as cm: admin.require_admin_auth("Bearer wrong")
            self.assertEqual(cm.exception.status_code,403)


class AuthoritySnapshotTests(unittest.TestCase):
    def test_atomic_snapshot_builds_authority_once_and_reuses_id(self):
        rows=[{"id":1,"symbol":"BTCUSDT","horizon":"1h","outcome":"WIN","ts":"2026-01-01T00:00:00Z","audit":{} }]
        calls={"history":0,"count":0,"gate":0}
        async def history(symbol=None): calls["history"]+=1; return rows
        async def count(): calls["count"]+=1; return 1414
        def gate(score): calls["gate"]+=1; return {"trade_mode":"PAPER","live_capital_locked":True,"orders_enabled":False,"effective_gate":"LOCKED","unlocked":False}
        store=AuthoritySnapshotStore(ttl_s=60)
        async def run():
            a=await store.get("BTCUSDT",live_gate_builder=gate)
            b=await store.get("BTCUSDT",live_gate_builder=gate)
            return a,b
        with mock.patch.object(supabase_client,"fetch_authority_history",side_effect=history), mock.patch.object(supabase_client,"count_predictions_exact",side_effect=count):
            a,b=asyncio.run(run())
        self.assertEqual(a.snapshot_id,b.snapshot_id)
        self.assertEqual(a.score["authority_snapshot_id"],a.live_gate["authority_snapshot_id"])
        self.assertEqual(a.exact_total_predictions,1414)
        self.assertEqual(calls,{"history":1,"count":1,"gate":1})

    def test_exact_count_uses_content_range_not_response_length(self):
        class Resp:
            status_code=206
            headers={"content-range":"0-0/1414"}
        class Client:
            async def get(self,*a,**kw):
                self.kw=kw; return Resp()
        c=Client()
        with mock.patch.object(supabase_client,"_get_client",return_value=c):
            value=asyncio.run(supabase_client.count_predictions_exact())
        self.assertEqual(value,1414)
        self.assertEqual(c.kw["headers"]["Prefer"],"count=exact")
        self.assertEqual(c.kw["headers"]["Range"],"0-0")


class HealthProvenanceTests(unittest.TestCase):
    def test_health_is_liveness_without_external_fetch(self):
        payload=asyncio.run(main_real.healthz())
        self.assertEqual(payload["status"],"alive")
        self.assertEqual(payload["probe"],"liveness")
        self.assertEqual(payload["trade_mode"],"PAPER")
        self.assertFalse(payload["orders_enabled"])
        self.assertTrue(payload["live_capital_locked"])

    def test_provenance_contract_accepts_exact_identifiers(self):
        fake={
            "SENEX_SOURCE_COMMIT":"a"*40,
            "SENEX_SOURCE_TREE":"b"*40,
            "SENEX_IMAGE_DIGEST":"sha256:"+"c"*64,
            "SENEX_BUILD_DIGEST":"sha256:"+"d"*64,
        }
        with mock.patch.dict(os.environ,fake,clear=True), mock.patch("backend.runtime_provenance.canonical_build_digest",return_value="sha256:"+"d"*64):
            p=runtime_provenance()
        self.assertTrue(p["exact"])


class SemanticContractTests(unittest.TestCase):
    def test_bps_and_cost_authority_fail_closed(self):
        self.assertEqual(bps_to_decimal(1),0.0001)
        ev=canonical_ev_audit(model_score=0.7,fee_bps=1,spread_bps=2,slippage_bps=3,impact_bps=4)
        self.assertEqual(ev["status"],"COST_MODEL_NOT_AUTHORITATIVE")
        self.assertEqual(ev["authority"],"DIAGNOSTIC_ONLY")
        self.assertFalse(ev["double_counting"])
        self.assertEqual(ev["instrument"]["instrument_identifier"],"BTC/USDT")
        self.assertIn("NOT_CALIBRATED_PROBABILITY",ev["semantic_classes"]["model_score"])

    def test_runtime_ruin_probabilities_are_distinct_semantics_and_same_reason_value(self):
        from oracle_runtime import institutional_core_real as real
        parent=real.SingleDecisionCore.__mro__[1]
        obj=object.__new__(real.SingleDecisionCore)
        obj.survivability=mock.Mock()
        obj.survivability.compute_survival_probability.return_value={"ruin_prob":0.37,"warning":"insufficient"}
        with mock.patch.object(parent,"filter_risk",return_value={"ruin_prob":0.11,"surv_reason":"blocked"}):
            result=real.SingleDecisionCore.filter_risk(obj,{}, {})
        self.assertEqual(result["state_ruin_probability"],0.11)
        self.assertEqual(result["survivability_ruin_probability"],0.37)
        self.assertEqual(result["survivability_reason_probability"],result["survivability_ruin_probability"])
        self.assertNotEqual(result["state_ruin_probability_semantics"],result["survivability_ruin_probability_semantics"])


class BuildEdgeSecretTests(unittest.TestCase):
    def test_dependencies_are_pinned_and_hashed(self):
        req=(ROOT/"senecio_polymarket/requirements.txt").read_text()
        direct=[line for line in req.splitlines() if line and not line.startswith("#")]
        self.assertTrue(direct)
        self.assertTrue(all("==" in line for line in direct))
        lock=(ROOT/"senecio_polymarket/requirements.lock").read_text()
        self.assertIn("--hash=sha256:",lock)
        self.assertIn("setuptools==",lock)
        docker=(ROOT/"senecio_polymarket/Dockerfile").read_text()
        self.assertIn("--require-hashes -r requirements.lock",docker)

    def test_edge_deny_is_before_origin_fetch_and_has_no_secret_binding(self):
        src=(ROOT/"edge/order070/worker.js").read_text()
        deny=src.index('if (!SAFE.has(request.method))')
        fetch=src.index('await fetch(')
        self.assertLess(deny,fetch)
        self.assertIn('"DENY_METHOD"',src)
        self.assertIn('"DENY_PATH"',src)
        self.assertIn('"ALLOW_GET_PROXY"',src)
        config=(ROOT/"edge/order070/wrangler.jsonc").read_text()
        self.assertNotRegex(config,re.compile(r"token|secret|account_id",re.I))

    def test_secret_scan_explicitly_includes_markdown(self):
        src=(ROOT/"senecio_polymarket/scripts/order070_secret_scan.py").read_text()
        self.assertIn('.suffix.lower()==".md"',src)


if __name__ == "__main__":
    unittest.main()
