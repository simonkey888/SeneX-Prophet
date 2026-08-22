from __future__ import annotations

import asyncio
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.routing import Mount, WebSocketRoute

from backend import main_real, supabase_client
from backend.authority_snapshot import AuthoritySnapshotStore

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_PUBLIC_API_PATHS = {
    "/",
    "/api/health",
    "/healthz",
    "/readyz",
    "/api/oracle/score",
    "/api/oracle/state",
    "/api/oracle/predictions/db",
    "/api/portfolio/live_gate",
    "/api/authority/snapshot",
    "/api/runtime/provenance",
    "/api/market-context",
}
ROW = {"id": 1, "symbol": "BTCUSDT", "horizon": "1h", "outcome": "WIN", "ts": "2026-01-01T00:00:00Z", "audit": {}}
PROVENANCE = {
    "contract": "senex-runtime-provenance-v1",
    "source_commit": "a" * 40, "source_tree": "b" * 40,
    "image_digest": "sha256:" + "c" * 64, "build_digest": "sha256:" + "d" * 64,
    "computed_build_digest": "sha256:" + "d" * 64,
    "checks": {"commit_exact": True, "tree_exact": True, "image_digest_exact": True, "build_digest_exact": True, "build_digest_matches_runtime_files": True},
    "exact": True,
}


def gate(score):
    return {"trade_mode": "PAPER", "live_capital_locked": True, "orders_enabled": False, "effective_gate": "LOCKED_BY_PAPER_POLICY", "unlocked": False}


class ConsolidatedR6Tests(unittest.TestCase):
    def test_f1_public_route_set_equals_independent_positive_allowlist(self):
        actual = {r.path for r in main_real.app.router.routes if isinstance(r, APIRoute)}
        self.assertEqual(actual, EXPECTED_PUBLIC_API_PATHS)
        self.assertFalse(any(isinstance(r, WebSocketRoute) for r in main_real.app.router.routes))
        mounts = {r.path for r in main_real.app.router.routes if isinstance(r, Mount)}
        self.assertEqual(mounts, {"/static"})

    def test_f1_unknown_and_optional_legacy_routes_default_absent(self):
        actual = {r.path for r in main_real.app.router.routes if isinstance(r, APIRoute)}
        forbidden = {"/api/research/status", "/api/antifragility/status", "/api/observability", "/metrics", "/api/stats", "/api/audit", "/api/catalog"}
        self.assertTrue(actual.isdisjoint(forbidden))

    def test_f2_public_get_storm_causes_zero_supabase_refresh_and_zero_generation_mutation(self):
        store = AuthoritySnapshotStore(ttl_s=60)
        calls = {"history": 0, "count": 0}
        async def history(symbol=None): calls["history"] += 1; return [dict(ROW)]
        async def count(): calls["count"] += 1; return 9
        async def run():
            snap = await store.get("BTCUSDT", live_gate_builder=gate, force=True)
            before = (snap.snapshot_id, snap.generation, store.refresh_status("BTCUSDT"))
            with mock.patch.object(main_real, "authority_store", store), mock.patch.object(main_real.oracle_runner, "get_state", return_value={"started_at": "x"}):
                for _ in range(20):
                    await main_real.public_authority_snapshot("BTCUSDT")
                    await main_real.public_authoritative_oracle_score("BTCUSDT")
                    await main_real.public_live_gate("BTCUSDT")
                    await main_real.public_oracle_state("BTCUSDT")
                    await main_real.public_predictions_db(50, "BTCUSDT")
            after_snap, after_status = store.observe("BTCUSDT")
            return before, after_snap, after_status
        with mock.patch.object(supabase_client, "fetch_authority_history", side_effect=history), mock.patch.object(supabase_client, "count_predictions_exact", side_effect=count), mock.patch("backend.authority_snapshot.runtime_provenance", return_value=dict(PROVENANCE)):
            before, after_snap, after_status = asyncio.run(run())
        self.assertEqual(calls, {"history": 1, "count": 1})
        self.assertEqual((after_snap.snapshot_id, after_snap.generation), before[:2])
        self.assertEqual(after_status["last_refresh_attempt_at"], before[2]["last_refresh_attempt_at"])

    def test_f2_stale_public_read_fails_closed_without_supabase_io(self):
        store = AuthoritySnapshotStore(ttl_s=0.001)
        with mock.patch.object(supabase_client, "fetch_authority_history", return_value=[dict(ROW)]), mock.patch.object(supabase_client, "count_predictions_exact", return_value=9), mock.patch("backend.authority_snapshot.runtime_provenance", return_value=dict(PROVENANCE)):
            asyncio.run(store.get("BTCUSDT", live_gate_builder=gate, force=True))
        time.sleep(0.01)
        with mock.patch.object(main_real, "authority_store", store), mock.patch.object(supabase_client, "fetch_authority_history", side_effect=AssertionError("public GET performed DB read")), mock.patch.object(supabase_client, "count_predictions_exact", side_effect=AssertionError("public GET performed count")):
            with self.assertRaises(HTTPException) as cm:
                asyncio.run(main_real.public_authority_snapshot("BTCUSDT"))
        self.assertEqual(cm.exception.status_code, 503)

    def test_f2_runtime_refresh_is_bounded_and_has_ttl_headroom(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            store = AuthoritySnapshotStore()
        self.assertGreaterEqual(store.refresh_interval_s(), 300.0)
        self.assertGreater(store.ttl_s, store.refresh_interval_s() + 60.0)

    def test_f2_arbitrary_symbol_is_rejected_before_store_cardinality(self):
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(main_real.public_authority_snapshot("ETHUSDT"))
        self.assertEqual(cm.exception.status_code, 404)

    def test_f3_single_root_canonical_build_nonroot_and_no_overlay(self):
        docker = (ROOT / "Dockerfile").read_text()
        runner = (ROOT / "senecio_polymarket/backend/oracle_runner.py").read_text()
        provenance = (ROOT / "senecio_polymarket/backend/runtime_provenance.py").read_text()
        self.assertFalse((ROOT / "senecio_polymarket/Dockerfile").exists())
        self.assertIn("USER senex:senex", docker)
        self.assertNotIn("chmod -R 777", docker)
        self.assertNotIn("mv /app/oracle/predict_only.py", docker)
        self.assertNotIn("cp /app/oracle_runtime/predict_only.py", docker)
        self.assertIn("from oracle_runtime.predict_only import", runner)
        self.assertNotIn("from predict_only import", runner)
        self.assertIn('root / "Dockerfile"', provenance)
        self.assertNotIn('root / "senecio_polymarket" / "Dockerfile"', provenance)

    def test_f4_edge_dashboard_route_parity_and_bounded_polling(self):
        edge = (ROOT / "edge/order070/worker.js").read_text()
        js = (ROOT / "senecio_polymarket/frontend/app.js").read_text()
        self.assertIn('"/api/oracle/predictions/db"', edge)
        self.assertIn("/api/oracle/predictions/db?limit=50&symbol=BTCUSDT", js)
        self.assertIn("setInterval(refreshPredictions, 60000)", js)
        self.assertNotIn("setInterval(refreshOracle, 10000)", js)

    def test_f5_artifact_name_uses_checked_out_exact_head(self):
        wf = (ROOT / ".github/workflows/senex-order-070.yml").read_text()
        self.assertIn("name: order070-sealed-${{ steps.exact_identity.outputs.head }}", wf)
        self.assertNotIn("name: order070-sealed-${{ github.sha }}", wf)


if __name__ == "__main__":
    unittest.main()
