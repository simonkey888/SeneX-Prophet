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
    ROW = {"id": 1, "symbol": "BTCUSDT", "horizon": "1h", "outcome": "WIN", "ts": "2026-01-01T00:00:00Z", "audit": {}}
    PROVENANCE = {
        "contract": "senex-runtime-provenance-v1",
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "image_digest": "sha256:" + "c" * 64,
        "build_digest": "sha256:" + "d" * 64,
        "computed_build_digest": "sha256:" + "d" * 64,
        "checks": {
            "commit_exact": True,
            "tree_exact": True,
            "image_digest_exact": True,
            "build_digest_exact": True,
            "build_digest_matches_runtime_files": True,
        },
        "exact": True,
    }

    @staticmethod
    def gate(score):
        return {
            "trade_mode": "PAPER",
            "live_capital_locked": True,
            "orders_enabled": False,
            "effective_gate": "LOCKED_BY_PAPER_POLICY",
            "unlocked": False,
            "verified": int(score.get("independent_1h_rows") or 0),
        }

    @staticmethod
    def decode(payload):
        if isinstance(payload, dict):
            return payload
        return json.loads(payload.body.decode())

    def test_r4_t1_concurrent_readiness_and_authority_surfaces_share_one_generation(self):
        store = AuthoritySnapshotStore(ttl_s=60)
        calls = {"history": 0, "count": 0}

        async def history(symbol=None):
            calls["history"] += 1
            return [dict(self.ROW)]

        async def count():
            calls["count"] += 1
            return 1414

        async def run():
            await store.get("BTCUSDT", live_gate_builder=self.gate)
            with mock.patch.object(main_real, "authority_store", store), \
                 mock.patch.object(main_real, "_live_gate_from_score", side_effect=self.gate), \
                 mock.patch.object(main_real.oracle_runner, "get_state", return_value={"started_at": "2026-08-21T00:00:00Z"}):
                return await asyncio.gather(
                    main_real.readyz("BTCUSDT"),
                    main_real.public_authority_snapshot("BTCUSDT"),
                    main_real.public_authoritative_oracle_score("BTCUSDT"),
                    main_real.public_oracle_state("BTCUSDT"),
                    main_real.public_live_gate("BTCUSDT"),
                )

        with mock.patch.object(supabase_client, "fetch_authority_history", side_effect=history), \
             mock.patch.object(supabase_client, "count_predictions_exact", side_effect=count), \
             mock.patch("backend.authority_snapshot.runtime_provenance", return_value=dict(self.PROVENANCE)):
            ready, snapshot, score, state, gate = asyncio.run(run())

        ready = self.decode(ready)
        ids = {
            ready["authority_snapshot_id"],
            snapshot["snapshot_id"],
            score["authority_snapshot_id"],
            state["authority_snapshot_id"],
            gate["authority_snapshot_id"],
        }
        generations = {
            ready["generation"],
            snapshot["generation"],
            score["authority_generation"],
            state["authority_generation"],
            gate["authority_generation"],
        }
        self.assertEqual(len(ids), 1)
        self.assertEqual(generations, {1})
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(calls, {"history": 1, "count": 1})

    def test_r4_t2_readiness_is_observational_and_does_not_rotate_generation(self):
        store = AuthoritySnapshotStore(ttl_s=60)
        calls = {"history": 0, "count": 0}

        async def history(symbol=None):
            calls["history"] += 1
            return [dict(self.ROW)]

        async def count():
            calls["count"] += 1
            return 1414

        async def run():
            snap = await store.get("BTCUSDT", live_gate_builder=self.gate)
            with mock.patch.object(main_real, "authority_store", store), \
                 mock.patch.object(main_real.oracle_runner, "get_state", return_value={"started_at": "2026-08-21T00:00:00Z"}):
                first = self.decode(await main_real.readyz("BTCUSDT"))
                second = self.decode(await main_real.readyz("BTCUSDT"))
            return snap, first, second

        with mock.patch.object(supabase_client, "fetch_authority_history", side_effect=history), \
             mock.patch.object(supabase_client, "count_predictions_exact", side_effect=count), \
             mock.patch("backend.authority_snapshot.runtime_provenance", return_value=dict(self.PROVENANCE)):
            snap, first, second = asyncio.run(run())

        self.assertEqual(first["authority_snapshot_id"], snap.snapshot_id)
        self.assertEqual(second["authority_snapshot_id"], snap.snapshot_id)
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual(calls, {"history": 1, "count": 1})

    def test_r4_t3_identical_complete_refresh_keeps_content_identity_and_generation(self):
        store = AuthoritySnapshotStore(ttl_s=60)

        async def run():
            first = await store.get("BTCUSDT", live_gate_builder=self.gate, force=True)
            second = await store.get("BTCUSDT", live_gate_builder=self.gate, force=True)
            return first, second

        with mock.patch.object(supabase_client, "fetch_authority_history", return_value=[dict(self.ROW)]), \
             mock.patch.object(supabase_client, "count_predictions_exact", return_value=1414), \
             mock.patch("backend.authority_snapshot.runtime_provenance", return_value=dict(self.PROVENANCE)):
            first, second = asyncio.run(run())

        self.assertIs(first, second)
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(first.canonical_sha256, second.canonical_sha256)
        self.assertEqual(first.generation, second.generation)

    def test_r4_t4_canonical_authority_change_publishes_one_new_atomic_generation(self):
        store = AuthoritySnapshotStore(ttl_s=60)
        changed = dict(self.ROW, id=2, ts="2026-01-01T01:00:00Z", outcome="LOSS")

        async def run():
            first = await store.get("BTCUSDT", live_gate_builder=self.gate, force=True)
            second = await store.get("BTCUSDT", live_gate_builder=self.gate, force=True)
            return first, second

        with mock.patch.object(supabase_client, "fetch_authority_history", side_effect=[[dict(self.ROW)], [dict(self.ROW), changed]]), \
             mock.patch.object(supabase_client, "count_predictions_exact", side_effect=[1414, 1415]), \
             mock.patch("backend.authority_snapshot.runtime_provenance", return_value=dict(self.PROVENANCE)):
            first, second = asyncio.run(run())

        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        self.assertNotEqual(first.canonical_sha256, second.canonical_sha256)
        self.assertEqual((first.generation, second.generation), (1, 2))
        self.assertEqual(second.score["authority_snapshot_id"], second.live_gate["authority_snapshot_id"])
        self.assertEqual(second.score["authority_generation"], 2)
        self.assertEqual(second.live_gate["authority_generation"], 2)

    def test_r4_t5_history_failure_retains_last_good_and_readiness_fails(self):
        store = AuthoritySnapshotStore(ttl_s=60)

        async def run():
            good = await store.get("BTCUSDT", live_gate_builder=self.gate, force=True)
            retained = await store.get("BTCUSDT", live_gate_builder=self.gate, force=True)
            with mock.patch.object(main_real, "authority_store", store), \
                 mock.patch.object(main_real.oracle_runner, "get_state", return_value={"started_at": "2026-08-21T00:00:00Z"}):
                ready = self.decode(await main_real.readyz("BTCUSDT"))
            return good, retained, store.refresh_status("BTCUSDT"), ready

        with mock.patch.object(supabase_client, "fetch_authority_history", side_effect=[[dict(self.ROW)], RuntimeError("history down")]), \
             mock.patch.object(supabase_client, "count_predictions_exact", side_effect=[1414, 1414]), \
             mock.patch("backend.authority_snapshot.runtime_provenance", return_value=dict(self.PROVENANCE)):
            good, retained, status, ready = asyncio.run(run())

        self.assertIs(good, retained)
        self.assertIn("AUTHORITY_HISTORY:RuntimeError", status["last_refresh_error"])
        self.assertEqual(ready["status"], "not_ready")
        self.assertFalse(ready["checks"]["last_refresh_ok"])
        self.assertEqual(ready["authority_snapshot_id"], good.snapshot_id)

    def test_r4_t6_exact_count_failure_retains_last_good_and_readiness_fails(self):
        store = AuthoritySnapshotStore(ttl_s=60)

        async def run():
            good = await store.get("BTCUSDT", live_gate_builder=self.gate, force=True)
            retained = await store.get("BTCUSDT", live_gate_builder=self.gate, force=True)
            with mock.patch.object(main_real, "authority_store", store), \
                 mock.patch.object(main_real.oracle_runner, "get_state", return_value={"started_at": "2026-08-21T00:00:00Z"}):
                ready = self.decode(await main_real.readyz("BTCUSDT"))
            return good, retained, store.refresh_status("BTCUSDT"), ready

        with mock.patch.object(supabase_client, "fetch_authority_history", side_effect=[[dict(self.ROW)], [dict(self.ROW)]]), \
             mock.patch.object(supabase_client, "count_predictions_exact", side_effect=[1414, RuntimeError("count down")]), \
             mock.patch("backend.authority_snapshot.runtime_provenance", return_value=dict(self.PROVENANCE)):
            good, retained, status, ready = asyncio.run(run())

        self.assertIs(good, retained)
        self.assertIn("EXACT_COUNT:RuntimeError", status["last_refresh_error"])
        self.assertEqual(ready["status"], "not_ready")
        self.assertFalse(ready["checks"]["last_refresh_ok"])
        self.assertEqual(ready["authority_snapshot_id"], good.snapshot_id)

    def test_r4_t7_first_refresh_failure_creates_no_valid_generation(self):
        from backend.authority_snapshot import AuthoritySnapshotRefreshError

        store = AuthoritySnapshotStore(ttl_s=60)
        with mock.patch.object(supabase_client, "fetch_authority_history", side_effect=RuntimeError("history down")), \
             mock.patch.object(supabase_client, "count_predictions_exact", return_value=1414):
            with self.assertRaises(AuthoritySnapshotRefreshError):
                asyncio.run(store.get("BTCUSDT", live_gate_builder=self.gate, force=True))
        snap, status = store.observe("BTCUSDT")
        self.assertIsNone(snap)
        self.assertTrue(status["snapshot_stale"])
        self.assertIsNotNone(status["last_refresh_error"])

    def test_r4_t8_capture_time_change_alone_does_not_change_canonical_identity(self):
        store = AuthoritySnapshotStore(ttl_s=60)

        async def run():
            first = await store._capture_complete("BTCUSDT", self.gate)
            second = await store._capture_complete("BTCUSDT", self.gate)
            return first, second

        with mock.patch.object(supabase_client, "fetch_authority_history", return_value=[dict(self.ROW)]), \
             mock.patch.object(supabase_client, "count_predictions_exact", return_value=1414), \
             mock.patch("backend.authority_snapshot.runtime_provenance", return_value=dict(self.PROVENANCE)), \
             mock.patch("backend.authority_snapshot._utcnow", side_effect=["2026-08-21T00:00:00+00:00", "2026-08-21T00:01:00+00:00"]):
            first, second = asyncio.run(run())

        self.assertNotEqual(first.captured_at, second.captured_at)
        self.assertEqual(first.canonical_sha256, second.canonical_sha256)
        self.assertEqual(first.canonical_hex, second.canonical_hex)

    def test_r4_t9_generation_canonical_and_freshness_fields_are_exposed(self):
        store = AuthoritySnapshotStore(ttl_s=60)
        with mock.patch.object(supabase_client, "fetch_authority_history", return_value=[dict(self.ROW)]), \
             mock.patch.object(supabase_client, "count_predictions_exact", return_value=1414), \
             mock.patch("backend.authority_snapshot.runtime_provenance", return_value=dict(self.PROVENANCE)):
            snap = asyncio.run(store.get("BTCUSDT", live_gate_builder=self.gate, force=True))
        payload = snap.to_dict(store.refresh_status("BTCUSDT"))
        required = {
            "snapshot_id", "generation", "symbol", "captured_at", "canonical_sha256",
            "authority_history_complete", "authority_history_rows", "exact_total_predictions",
            "exact_count_complete", "last_cursor_or_equivalent", "failure_reason", "score",
            "live_gate", "provenance", "last_refresh_attempt_at", "last_refresh_success_at",
            "last_refresh_error", "snapshot_age_s", "snapshot_stale",
        }
        self.assertTrue(required <= set(payload))
        self.assertEqual(payload["generation"], 1)
        self.assertTrue(payload["canonical_sha256"].startswith("sha256:"))
        self.assertFalse(payload["snapshot_stale"])
        self.assertIsNone(payload["last_refresh_error"])
        self.assertEqual(payload["score"]["authority_snapshot_id"], payload["snapshot_id"])
        self.assertEqual(payload["live_gate"]["authority_snapshot_id"], payload["snapshot_id"])

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
        docker=(ROOT/"Dockerfile").read_text()
        self.assertFalse((ROOT/"senecio_polymarket/Dockerfile").exists())
        self.assertIn("--require-hashes -r requirements.lock",docker)
        self.assertIn("USER senex:senex",docker)
        self.assertNotIn("chmod -R 777",docker)
        self.assertNotIn("mv /app/oracle/predict_only.py",docker)
        self.assertNotIn("cp /app/oracle_runtime/predict_only.py",docker)

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
