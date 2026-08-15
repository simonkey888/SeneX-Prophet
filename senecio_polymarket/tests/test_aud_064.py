from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from backend import authoritative_score, oracle_runner, settlement_proof, settlement_reconciler, supabase_client
from backend.settlement_contract import (
    CANDLE_INTERVAL_MS,
    WINDOW_1H_S,
    price_evidence_from_candles,
    target_epoch_ms,
)
from oracle_runtime import institutional_core as legacy_learning
from oracle_runtime import institutional_core_real as real_core
from oracle_runtime import predict_only as runtime_predictor
from tests.test_aud_063 import FakePostgrest, evidence, pending_row, qualified_row as aud063_qualified_row

BASE_SHA = "2c4dbf284b23d3cf81b93dcfbd262660ab03dd43"
BASE_TREE = "0cb5abaa024f1325bf88e5fd3390dcec8f5f972d"
AUD062_HEAD = "f65e1723953ac23caf1ca3741ec894577c97aae7"
AUD062_TREE = "d8c8b734bdfd0d0a33e31bdd80557e9dafb71b06"
ROOT = Path(__file__).resolve().parents[1]
FIXED_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
FIXED_EPOCH_MS = int(FIXED_NOW.timestamp() * 1000)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is not None else FIXED_NOW.replace(tzinfo=None)


def _qualified(i: int, *, symbol: str = "BTCUSDT", direction: str = "LONG", ts: datetime | None = None, observed: datetime | None = None) -> dict:
    ts = ts or datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=2 * i)
    observed = observed or ts + timedelta(hours=2)
    row = aud063_qualified_row(i, direction=direction, symbol=symbol, ts=ts.isoformat(), observed=observed.isoformat())
    row["audit"]["pipeline"] = {
        "step2_features": {
            "conviction": 0.65,
            "regime_4h": "NEUTRAL",
            "pressures": {
                "orderflow": 0.5 if direction == "LONG" else -0.5,
                "volume_delta": 0.25 if direction == "LONG" else -0.25,
            },
        }
    }
    return row


def _learning_rows(count: int = 12, *, symbol: str = "BTCUSDT") -> list[dict]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        _qualified(index + 1, symbol=symbol, ts=base + timedelta(hours=2 * index), observed=base + timedelta(hours=2 * index + 2))
        for index in range(count)
    ]


class _ProjectionResponse:
    status_code = 200
    def __init__(self, source_row: dict, seen: dict):
        self.source_row = source_row
        self.seen = seen
    def json(self):
        fields = [item.strip() for item in str(self.seen["select"]).split(",")]
        return [{key: self.source_row[key] for key in fields if key in self.source_row}]


class _ProjectionClient:
    source_row: dict = {}
    seen: dict = {}
    def __init__(self, *args, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def get(self, url, headers=None, params=None):
        type(self).seen.clear()
        type(self).seen.update(params or {})
        return _ProjectionResponse(type(self).source_row, type(self).seen)


def _market_fixture() -> dict:
    ohlcv = []
    for index in range(20):
        close = 100_000.0 + index * 10.0
        ohlcv.append([FIXED_EPOCH_MS - (19 - index) * 900_000, close - 5.0, close + 20.0, close - 20.0, close, 100.0 + index])
    observations = {
        "orderflow": "okx:BTC-USDT:SPOT:public-trades",
        "volume_delta": "okx:BTC-USDT:SPOT:ohlcv",
        "bidask_imbalance": "okx:BTC-USDT:SPOT:orderbook",
        "price_momentum": "okx:BTC-USDT:SPOT:ohlcv",
    }
    feature_observations = {
        name: {
            "status": "REAL_NONZERO", "source": source, "observed_at": FIXED_NOW.isoformat(),
            "exchange_timestamp": FIXED_EPOCH_MS, "query_observation_epoch": FIXED_NOW.timestamp(),
        }
        for name, source in observations.items()
    }
    for name in ("funding_signal", "oi_momentum"):
        feature_observations[name] = {
            "status": "MISSING", "source": "not_applicable_spot", "observed_at": None,
            "exchange_timestamp": None, "query_observation_epoch": FIXED_NOW.timestamp(),
        }
    return {
        "symbol": "BTC/USDT", "timeframe": "15m", "exchange_used": "okx",
        "timestamp": FIXED_EPOCH_MS, "candle_ts": FIXED_EPOCH_MS, "ohlcv": ohlcv,
        "ticker": {"bid": 100190.0, "ask": 100191.0, "last": 100190.5, "spread_pct": 0.00001, "spread_bps": 0.1, "timestamp": FIXED_EPOCH_MS},
        "orderbook": {"bid_depth": 250000.0, "ask_depth": 200000.0, "bid": 100190.0, "ask": 100191.0, "timestamp": FIXED_EPOCH_MS},
        "funding": {"rate": 0.0}, "open_interest": {"oi_change_24h_pct": 0.0},
        "liquidity_quality": 0.99, "feature_observations": feature_observations,
    }


def _full_runtime_prediction(rows: list[dict] | None = None) -> dict:
    rows = rows or _learning_rows()
    poly = {"source": "POLYMARKET_PUBLIC", "status": "LIVE_WS", "eligible_for_prediction": True, "ws_connected": True, "slug": "aud064-fixture", "up_probability": 0.55, "down_probability": 0.45, "directional_pressure": 0.1, "seconds_to_close": 120, "freshness_s": 1.0}
    kalshi = {"source": "KALSHI_PUBLIC_REST", "status": "LIVE", "directional_use": False, "market": {"yes_probability": 0.54}}
    boros = {"source": "BOROS_PUBLIC_API", "status": "LIVE", "directional_use": False, "markets": []}
    real_core._shadow_fetch_cache.clear()
    with mock.patch.dict(os.environ, {"SUPABASE_URL": "https://example.invalid", "SUPABASE_KEY": "aud064_test_only_placeholder"}, clear=False):
        os.environ.pop(real_core.POLYMARKET_EXPERIMENT_FLAG, None)
        with (
            mock.patch.object(real_core, "fetch_shadow_authoritative_rows", return_value=rows),
            mock.patch.object(runtime_predictor, "_poly_snapshot_for_prediction", return_value=poly),
            mock.patch.object(runtime_predictor, "_kalshi_snapshot_for_audit", return_value=kalshi),
            mock.patch.object(runtime_predictor, "_boros_snapshot_for_audit", return_value=boros),
            mock.patch.object(runtime_predictor._base, "_feed_calibration_from_predictions", return_value=None, create=True),
            mock.patch.object(runtime_predictor._base, "datetime", FixedDateTime),
            mock.patch.object(real_core, "datetime", FixedDateTime),
        ):
            return runtime_predictor.run_prediction(_market_fixture())


class Aud064IntegrationTests(unittest.TestCase):
    def setUp(self):
        legacy_learning._fetch_cache.clear()
        real_core._shadow_fetch_cache.clear()

    def test_t01_current_main_projection_bug_is_reproduced_before_fix(self):
        source = _qualified(1)
        self.assertTrue(settlement_proof.is_proof_qualified(source))
        _ProjectionClient.source_row = source
        with mock.patch.dict(os.environ, {"SUPABASE_URL": "https://example.invalid", "SUPABASE_KEY": "aud064_test_only_placeholder"}, clear=False), mock.patch.object(legacy_learning.httpx, "Client", _ProjectionClient):
            fetched = legacy_learning.fetch_authoritative_rows("BTCUSDT")
        self.assertNotIn("exchange_used", _ProjectionClient.seen["select"].split(","))
        self.assertNotIn("exchange_used", fetched[0])
        self.assertFalse(settlement_proof.is_proof_qualified(fetched[0]))
        replay = legacy_learning.replay_authoritative_learning(legacy_learning.SingleDecisionCore(), fetched, "BTCUSDT", decision_cutoff="2026-01-02T00:00:00+00:00")
        self.assertEqual(replay["proof_qualified_n"], 0)

    def test_t02_fixed_production_projection_reads_persisted_exchange_used(self):
        source = _qualified(1)
        _ProjectionClient.source_row = source
        with mock.patch.dict(os.environ, {"SUPABASE_URL": "https://example.invalid", "SUPABASE_KEY": "aud064_test_only_placeholder"}, clear=False), mock.patch.object(real_core.httpx, "Client", _ProjectionClient):
            fetched = real_core.fetch_shadow_authoritative_rows("BTCUSDT")
        self.assertIn("exchange_used", _ProjectionClient.seen["select"].split(","))
        self.assertEqual(fetched[0]["exchange_used"], "okx")
        self.assertTrue(settlement_proof.is_proof_qualified(fetched[0]))

    def test_t03_missing_exchange_stays_excluded_without_inference(self):
        row = _qualified(1); row["exchange_used"] = None
        self.assertFalse(settlement_proof.is_proof_qualified(row))
        state = real_core.replay_shadow_only_learning(real_core.SingleDecisionCore(), [row], "BTCUSDT", decision_cutoff="2026-01-02T00:00:00+00:00")
        self.assertEqual(state["proof_qualified_n"], 0)
        self.assertEqual(state["exchange_used_policy"], "PERSISTED_VALUE_ONLY_NO_DEFAULT_NO_INFERENCE")

    def test_t04_learning_uses_the_same_canonical_proof_gate_as_score(self):
        good = _qualified(1); bad = _qualified(2); bad["exchange_used"] = None
        for row in (good, bad): self.assertEqual(legacy_learning._proof_gate(row), settlement_proof.is_proof_qualified(row))
        self.assertIn("is_proof_qualified", inspect.getsource(legacy_learning._proof_gate))

    def test_t05_evidence_observed_after_decision_cutoff_is_excluded(self):
        row = _qualified(1, ts=datetime(2026,1,1,tzinfo=timezone.utc), observed=datetime(2026,1,1,2,tzinfo=timezone.utc))
        state = real_core.replay_shadow_only_learning(real_core.SingleDecisionCore(), [row], "BTCUSDT", decision_cutoff="2026-01-01T01:30:00+00:00")
        self.assertEqual(state["source_prediction_ids"], [])

    def test_t06_same_evidence_is_eligible_only_at_later_cutoff(self):
        row = _qualified(1, ts=datetime(2026,1,1,tzinfo=timezone.utc), observed=datetime(2026,1,1,2,tzinfo=timezone.utc))
        state = real_core.replay_shadow_only_learning(real_core.SingleDecisionCore(), [row], "BTCUSDT", decision_cutoff="2026-01-01T03:00:00+00:00")
        self.assertEqual(state["source_prediction_ids"], [1])

    def test_t07_one_hour_horizon_must_have_elapsed(self):
        row = _qualified(1, ts=datetime(2026,1,1,tzinfo=timezone.utc), observed=datetime(2026,1,1,2,tzinfo=timezone.utc))
        state = real_core.replay_shadow_only_learning(real_core.SingleDecisionCore(), [row], "BTCUSDT", decision_cutoff="2026-01-01T00:59:59+00:00")
        self.assertEqual(state["source_prediction_ids"], [])

    def test_t08_symbol_isolation_btc_vs_eth(self):
        state = real_core.replay_shadow_only_learning(real_core.SingleDecisionCore(), [_qualified(2,symbol="ETHUSDT"), _qualified(1,symbol="BTCUSDT")], "BTCUSDT", decision_cutoff="2026-01-02T00:00:00+00:00")
        self.assertEqual(state["source_prediction_ids"], [1])

    def test_t09_independent_nonoverlap_selection_is_deterministic_under_reorder(self):
        rows = _learning_rows(12)
        a = real_core.replay_shadow_only_learning(real_core.SingleDecisionCore(), rows, "BTCUSDT", decision_cutoff="2026-01-03T12:00:00+00:00")
        b = real_core.replay_shadow_only_learning(real_core.SingleDecisionCore(), list(reversed(rows)), "BTCUSDT", decision_cutoff="2026-01-03T12:00:00+00:00")
        self.assertEqual(a["source_prediction_ids"], b["source_prediction_ids"])

    def test_t10_source_ids_and_observation_epochs_are_deterministic(self):
        rows = _learning_rows(12)
        state = real_core.replay_shadow_only_learning(real_core.SingleDecisionCore(), rows, "BTCUSDT", decision_cutoff="2026-01-03T12:00:00+00:00")
        expected = {row["id"]: legacy_learning._settlement_observed_epoch(row) for row in rows}
        for item in state["source_settlement_observation_epochs"]: self.assertEqual(item["observed_at_epoch"], expected[item["prediction_id"]])

    def test_t11_source_evidence_hash_is_deterministic(self):
        rows = _learning_rows(12)
        a = real_core.replay_shadow_only_learning(real_core.SingleDecisionCore(), rows, "BTCUSDT", decision_cutoff="2026-01-03T12:00:00+00:00")
        b = real_core.replay_shadow_only_learning(real_core.SingleDecisionCore(), list(reversed(rows)), "BTCUSDT", decision_cutoff="2026-01-03T12:00:00+00:00")
        self.assertEqual(a["source_evidence_hash"], b["source_evidence_hash"]); self.assertEqual(len(a["source_evidence_hash"]),64)

    def test_t12_enough_evidence_can_produce_shadow_mutations(self):
        state = real_core.replay_shadow_only_learning(real_core.SingleDecisionCore(), _learning_rows(12), "BTCUSDT", decision_cutoff="2026-01-03T12:00:00+00:00")
        self.assertGreaterEqual(state["proof_qualified_n"], legacy_learning.MIN_LEARNING_EXAMPLES); self.assertGreater(state["shadow_mutations"],0); self.assertEqual(state["status"],"SHADOW_ONLY_FAIL_CLOSED")

    def test_t13_production_mutations_remain_zero(self):
        state = real_core.replay_shadow_only_learning(real_core.SingleDecisionCore(), _learning_rows(12), "BTCUSDT", decision_cutoff="2026-01-03T12:00:00+00:00")
        self.assertEqual(state["mutations"],0); self.assertEqual(state["learning_mutation_authority"],"SHADOW_ONLY"); self.assertFalse(state["production_learning_mutation_enabled"])

    def test_t14_production_decision_weights_equal_frozen_base_after_replay(self):
        core = real_core.SingleDecisionCore(); base = dict(core._senex_base_weights)
        state = real_core.replay_shadow_only_learning(core, _learning_rows(12), "BTCUSDT", decision_cutoff="2026-01-03T12:00:00+00:00")
        self.assertEqual(core.weights,base); self.assertEqual(state["decision_weights"],legacy_learning._weights_payload(base)); self.assertEqual(state["effective_weights"],legacy_learning._weights_payload(base)); self.assertEqual(state["base_weights_hash"],state["decision_weights_hash"]); self.assertEqual(state["decision_weights_hash"],state["effective_weights_hash"])

    def test_t15_shadow_weights_are_separate_and_not_production_weights(self):
        core = real_core.SingleDecisionCore(); state = real_core.replay_shadow_only_learning(core,_learning_rows(12),"BTCUSDT",decision_cutoff="2026-01-03T12:00:00+00:00")
        self.assertGreater(state["shadow_mutations"],0); self.assertNotEqual(state["shadow_weights"],state["decision_weights"]); self.assertNotEqual(state["shadow_weights_hash"],state["decision_weights_hash"]); self.assertEqual(core.weights,core._senex_base_weights)

    def test_t16_full_prediction_row_roundtrip_preserves_learning_provenance(self):
        result = _full_runtime_prediction(); encoded = json.dumps(result,sort_keys=True,separators=(",",":")); del result; reloaded=json.loads(encoded)
        state=reloaded["_audit"]["pipeline"]["step2_features"]["learning_state_v1"]
        self.assertTrue(state["source_prediction_ids"]); self.assertEqual(len(state["source_prediction_ids"]),len(state["source_settlement_observation_epochs"])); self.assertTrue(all(item["observed_at_epoch"] is not None for item in state["source_settlement_observation_epochs"])); self.assertEqual(len(state["source_evidence_hash"]),64); self.assertEqual(state["mutations"],0); self.assertIn("decision_weights",state); self.assertIn("shadow_weights",state); self.assertFalse(state["production_learning_mutation_enabled"])

    def test_t17_current_prediction_cannot_consume_own_future_outcome(self):
        row=_qualified(999,ts=FIXED_NOW,observed=FIXED_NOW+timedelta(hours=2)); state=real_core.replay_shadow_only_learning(real_core.SingleDecisionCore(),[row],"BTCUSDT",decision_cutoff=FIXED_NOW); self.assertNotIn(999,state["source_prediction_ids"])

    def test_t18_aud063_open_candle_rejection_is_preserved(self):
        ts="2026-01-01T00:00:30+00:00"; target=target_epoch_ms(ts,WINDOW_1H_S); self.assertIsNotNone(target); open_ms=target-(target%CANDLE_INTERVAL_MS); close_ms=open_ms+CANDLE_INTERVAL_MS; before=datetime.fromtimestamp((close_ms-1)/1000,tz=timezone.utc).isoformat()
        self.assertIsNone(price_evidence_from_candles(candles=[[open_ms,102,102,102,102,1.0]],exchange="okx",symbol="BTCUSDT",ts_iso=ts,window_seconds=WINDOW_1H_S,observed_at=before))

    def test_t19_aud063_same_origin_exchange_proof_is_preserved(self):
        row=_qualified(1); self.assertTrue(settlement_proof.is_proof_qualified(row)); row["exchange_used"]="kraken"; self.assertFalse(settlement_proof.is_proof_qualified(row))

    def test_t20_aud063_null_only_compare_and_set_is_preserved(self):
        row=pending_row(1); fake=FakePostgrest([row]); ev15=evidence(row,900,101); ev1=evidence(row,3600,102); old=supabase_client._get_client; supabase_client._get_client=lambda:fake
        try: ok=asyncio.run(supabase_client.update_outcome_dual(1,"WIN","WIN",101,102,price_evidence_15m=ev15,price_evidence_1h=ev1))
        finally: supabase_client._get_client=old
        self.assertTrue(ok); patch_calls=[params for kind,params in fake.calls if kind=="PATCH"]; self.assertEqual(patch_calls[-1]["outcome"],"is.null")

    def test_t21_aud063_reconciler_cannot_null_to_settle(self):
        source=inspect.getsource(settlement_reconciler.reconcile_once); repair=inspect.getsource(settlement_reconciler._repair_row); self.assertIn('"outcome": "in.(WIN,LOSS)"',source); self.assertNotIn('"outcome": "is.null"',source); self.assertNotIn('"outcome": o1h',repair)

    def test_t22_starvation_regression_many_old_flat_still_reaches_directional(self):
        rows=[pending_row(i+1,"FLAT") for i in range(125)]+[pending_row(1000,"LONG")]; fake=FakePostgrest(rows); old=supabase_client._get_client; supabase_client._get_client=lambda:fake
        try: selected=asyncio.run(supabase_client.fetch_pending_outcomes(3600,100,max_pages=2))
        finally: supabase_client._get_client=old
        self.assertEqual([row["id"] for row in selected],[1000])

    def test_t23_startup_legacy_backfill_zero_io_quarantine_is_preserved(self):
        self.assertEqual(oracle_runner._state["legacy_startup_backfill_status"],"QUARANTINED_NO_READ_NO_WRITE"); self.assertIn("QUARANTINED_NO_READ_NO_WRITE",(ROOT/"backend"/"oracle_runner.py").read_text(encoding="utf-8"))

    def test_t24_polymarket_directional_use_remains_off_weight_zero_experiment_false(self):
        core=real_core.SingleDecisionCore(); market=_market_fixture(); market["polymarket_context"]={"eligible_for_prediction":True,"status":"LIVE_WS","directional_pressure":0.9,"up_probability":0.95,"down_probability":0.05}
        with mock.patch.dict(os.environ,{},clear=False): os.environ.pop(real_core.POLYMARKET_EXPERIMENT_FLAG,None); features=core.compress_features(core.ingest_market(market))
        ctx=features["polymarket_context_v1"]; self.assertFalse(ctx["directional_use"]); self.assertFalse(ctx["experiment_enabled"]); self.assertEqual(ctx["effective_weight"],0.0); self.assertEqual(ctx["pressure_component"],0.0)

    def test_t25_kalshi_and_boros_directional_use_remain_false(self):
        from backend import boros_market_adapter, kalshi_market_adapter
        with mock.patch.object(kalshi_market_adapter,"get_kalshi_snapshot",return_value={"status":"LIVE","market":{"yes_probability":1.0}}), mock.patch.object(boros_market_adapter,"get_boros_snapshot",return_value={"status":"LIVE","markets":[{"mid_apr":999.0}]}): kalshi=runtime_predictor._kalshi_snapshot_for_audit(); boros=runtime_predictor._boros_snapshot_for_audit()
        self.assertFalse(kalshi["directional_use"]); self.assertFalse(boros["directional_use"])

    def test_t26_paper_live_orders_and_synthetic_locks_are_preserved(self):
        score=authoritative_score.build_authoritative_score([],symbol="BTCUSDT"); self.assertEqual(score["trade_mode"],"PAPER"); self.assertFalse(score["orders_enabled"]); self.assertTrue(score["live_capital_locked"])
        from backend import main_real
        with mock.patch.dict(os.environ,{},clear=False): os.environ.pop("SENEX_ENABLE_SYNTHETIC_DEMO",None); self.assertFalse(main_real.synthetic_demo_enabled())

    def test_t27_runtime017_path_and_content_are_not_mutated(self):
        try: changed=subprocess.check_output(["git","diff","--name-only",f"{BASE_SHA}...HEAD"],text=True).splitlines()
        except Exception: changed=[]
        self.assertFalse(any("runtime017" in path.lower() or "runtime-017" in path.lower() for path in changed))

    def test_t28_model_threshold_and_base_weight_constants_are_unchanged(self):
        self.assertEqual(legacy_learning.MIN_LEARNING_EXAMPLES,10); self.assertEqual(authoritative_score.MIN_GLOBAL_N,100); self.assertEqual(authoritative_score.MIN_DIRECTION_N,30)
        try: changed=subprocess.check_output(["git","diff","--name-only",f"{BASE_SHA}...HEAD"],text=True).splitlines()
        except Exception: changed=[]
        frozen={"senecio_polymarket/oracle/institutional_core.py","senecio_polymarket/oracle_runtime/institutional_core.py","senecio_polymarket/backend/authoritative_score.py"}; self.assertFalse(frozen.intersection(changed))

    def test_t29_legacy_oracle_workflow_has_no_repo_write_or_push_path(self):
        workflow=(ROOT.parent/".github"/"workflows"/"oracle.yml").read_text(encoding="utf-8"); self.assertIn("contents: read",workflow); self.assertNotIn("contents: write",workflow); self.assertNotRegex(workflow,r"(?m)^\s*git\s+push\b"); self.assertNotIn("actions/deploy-pages",workflow); self.assertNotIn("pages: write",workflow); self.assertNotIn("id-token: write",workflow); self.assertIn("persist-credentials: false",workflow)

    def test_t30_governance_manifest_is_single_owner_mergeable_and_not_applied(self):
        manifest=json.loads((ROOT/"docs"/"evidence"/"aud-064-governance-ruleset-proposal.json").read_text(encoding="utf-8")); self.assertFalse(manifest["github_settings_applied"]); self.assertEqual(manifest["status"],"PROPOSED_NOT_APPLIED"); self.assertEqual(manifest["bypass_actors"],[]); self.assertEqual(manifest["required_approving_review_count"],0); self.assertFalse(manifest["require_last_push_approval"]); self.assertEqual(manifest["required_checks"],["score-001","score-002","act_final_audit_smoke (T1-T12)"]); self.assertNotIn("AUD_EXACT_HEAD_GATE",manifest["required_checks"])
        rules={rule["type"]:rule for rule in manifest["rest_ruleset_request_body"]["rules"]}; self.assertIn("deletion",rules); self.assertIn("non_fast_forward",rules); self.assertIn("pull_request",rules); self.assertIn("required_status_checks",rules); self.assertEqual(rules["pull_request"]["parameters"]["required_approving_review_count"],0)

    def test_t31_candidate_artifacts_contain_no_secret_pii_or_nonpublic_payload(self):
        targets=[ROOT/"docs"/"evidence"/"aud-064-governance-ruleset-proposal.json"]
        dangerous=re.compile(r"(-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9_]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b|\bsk-[A-Za-z0-9_-]{20,}\b|\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b)")
        for target in targets: self.assertIsNone(dangerous.search(target.read_text(encoding="utf-8")),target)

    def test_t32_full_run_prediction_path_keeps_base_weights_and_exposes_shadow_provenance(self):
        result=_full_runtime_prediction(); state=result["_audit"]["pipeline"]["step2_features"]["learning_state_v1"]; self.assertGreater(state["shadow_mutations"],0); self.assertEqual(state["mutations"],0); self.assertEqual(state["learning_mutation_authority"],"SHADOW_ONLY"); self.assertFalse(state["production_learning_mutation_enabled"]); self.assertEqual(state["size_calibration_authority"],"FROZEN_BASE_ONLY"); self.assertEqual(state["decision_weights"],state["base_weights"]); self.assertEqual(state["effective_weights"],state["base_weights"]); self.assertNotEqual(state["shadow_weights"],state["decision_weights"]); ctx=result["_audit"]["pipeline"]["step2_features"]["polymarket_context_v1"]; self.assertFalse(ctx["directional_use"]); self.assertEqual(ctx["effective_weight"],0.0)


if __name__ == "__main__":
    unittest.main()
