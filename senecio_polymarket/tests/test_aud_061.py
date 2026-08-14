from __future__ import annotations

import hashlib
import json
import random
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from backend.research.aud061_pipeline import (
    FLAT_REASONS,
    classify_flat_reason,
    feature_availability,
    flat_waterfall,
    learning_ab,
    run_all,
    temporal_purged_split,
    threshold_research,
)
from oracle.exchange_connector import (
    ExchangeConnector,
    _OI_SNAPSHOT_CACHE,
    build_feature_observations,
    resolve_public_derivative_symbol,
)
from oracle_runtime import institutional_core as learning

ROOT = Path(__file__).resolve().parents[2]
BASE = datetime(2026, 8, 1, tzinfo=timezone.utc)


def proof_row(idx: int, *, minute: int | None = None, symbol: str = "BTCUSDT", prediction: str = "LONG", outcome: str = "WIN"):
    minute = idx * 61 if minute is None else minute
    ts = (BASE + timedelta(minutes=minute)).isoformat()
    settled_at = (BASE + timedelta(minutes=minute + 60)).isoformat()
    origin = 100.0
    later = 101.0 if (prediction == "LONG") == (outcome == "WIN") else 99.0
    return {
        "id": idx, "ts": ts, "symbol": symbol, "prediction": prediction,
        "confidence": 0.65, "price_now": origin, "outcome": outcome,
        "exchange_used": "okx",
        "audit": {
            "origin_price_v1": {"version": "origin-price-v1", "price": origin, "timestamp": ts, "source": "okx"},
            "outcomes_dual": {"outcome_15m": outcome, "outcome_1h": outcome, "price_15m_later": later, "price_1h_later": later, "primary_window": "1h", "settled_at": settled_at},
            "action_vector": {"action": "EXECUTE", "reason": "EU_EXECUTE"},
            "pipeline": {"step1_market": {"funding_signal": 0.0, "oi_momentum": 0.0}, "step2_features": {
                "direction": prediction, "conviction": 0.65, "regime_4h": "NEUTRAL",
                "pressures": {"orderflow": 0.30, "volume_delta": 0.06, "bidask": 0.07, "funding": 0.0, "oi": 0.0, "price_momentum": 0.05},
            }},
        },
    }


def core():
    return learning.SingleDecisionCore(
        max_drawdown=0.12, ruin_probability_threshold=0.05, hard_stop=True,
        max_position_pct=0.25, max_leverage=1, min_confidence=0.40,
        min_ev_to_trade=0.001, no_trade_noise=0.60, initial_capital=1000.0,
    )


class LearningCausalityTests(unittest.TestCase):
    def test_overlapping_rows_are_diagnostic_not_learning_authority(self):
        rows = [proof_row(i, minute=i * 15) for i in range(10)]
        state = learning.replay_authoritative_learning(core(), rows, "BTCUSDT")
        self.assertEqual(state["proof_qualified_raw_available_before_decision"], 10)
        self.assertEqual(state["proof_qualified_n"], 3)
        self.assertEqual(state["authority_cohort"], "INDEPENDENT_NONOVERLAP_1H")
        self.assertEqual(state["status"], "WARMUP")

    def test_current_row_and_unsettled_horizon_cannot_influence_itself(self):
        rows = [proof_row(i) for i in range(1, 12)]
        cutoff = datetime.fromisoformat(rows[-1]["ts"])
        state = learning.replay_authoritative_learning(core(), rows, "BTCUSDT", decision_cutoff=cutoff)
        self.assertNotIn(rows[-1]["id"], state["source_prediction_ids"])
        for row in rows:
            origin = datetime.fromisoformat(row["ts"])
            if origin + timedelta(hours=1) > cutoff:
                self.assertNotIn(row["id"], state["source_prediction_ids"])

    def test_late_observation_cannot_enter_even_after_horizon_elapsed(self):
        row = proof_row(1, minute=0)
        row["audit"]["outcomes_dual"]["settled_at"] = (BASE + timedelta(hours=4)).isoformat()
        cutoff = BASE + timedelta(hours=3)
        state = learning.replay_authoritative_learning(core(), [row], "BTCUSDT", decision_cutoff=cutoff)
        self.assertEqual(state["proof_qualified_n"], 0)
        self.assertNotIn(row["id"], state["source_prediction_ids"])

    def test_mixed_timestamp_formats_have_deterministic_order(self):
        rows = [proof_row(i) for i in range(1, 15)]
        rows[2]["ts"] = int(datetime.fromisoformat(rows[2]["ts"]).timestamp() * 1000)
        rows[2]["audit"]["origin_price_v1"]["timestamp"] = datetime.fromtimestamp(rows[2]["ts"] / 1000, timezone.utc).isoformat()
        # That deliberately malformed proof is rejected, but ordering remains invariant.
        a = learning.replay_authoritative_learning(core(), rows, "BTCUSDT")
        shuffled = list(rows)
        random.Random(61).shuffle(shuffled)
        b = learning.replay_authoritative_learning(core(), shuffled, "BTCUSDT")
        self.assertEqual(a["source_prediction_ids"], b["source_prediction_ids"])
        self.assertEqual(a["source_evidence_hash"], b["source_evidence_hash"])
        self.assertEqual(a["effective_weights_hash"], b["effective_weights_hash"])

    def test_runtime_refreshes_same_symbol_after_ttl(self):
        instance = core()
        rows_a = [proof_row(i) for i in range(1, 11)]
        rows_b = rows_a + [proof_row(11, outcome="LOSS")]
        with mock.patch.dict("os.environ", {"SUPABASE_URL": "https://example.invalid", "SUPABASE_KEY": "test"}), mock.patch.object(learning, "fetch_authoritative_rows", side_effect=[rows_a, rows_b]) as fetch, mock.patch.object(learning.time, "monotonic", side_effect=[10.0, 10.0 + learning.FETCH_CACHE_TTL_S + 1]):
            instance._load_learning_for_symbol("BTCUSDT")
            first_hash = instance._authoritative_learning_state["source_evidence_hash"]
            instance._load_learning_for_symbol("BTCUSDT")
        self.assertEqual(fetch.call_count, 2)
        self.assertNotEqual(first_hash, instance._authoritative_learning_state["source_evidence_hash"])

    def test_ab_harness_reorder_reproducible_and_provenanced(self):
        rows = [proof_row(i, outcome="WIN" if i % 3 else "LOSS") for i in range(1, 45)]
        a = learning_ab(rows)
        shuffled = list(rows)
        random.Random(610).shuffle(shuffled)
        b = learning_ab(shuffled)
        self.assertEqual(a, b)
        self.assertEqual(a["status"], "INSUFFICIENT_CAUSAL_PROVENANCE")
        self.assertEqual(a["analysis_type"], "COMPONENT_LEVEL_WEIGHT_SENSITIVITY_NOT_MODEL_AB")
        self.assertFalse(a["full_model_ab"])
        self.assertFalse(a["same_inputs_and_timestamps"])
        self.assertEqual(a["paired_n"], 0)
        self.assertEqual(a["decision_replay_snapshot_n"], 0)
        self.assertEqual(a["settlement_observation_provenance_n"], 44)


class FeatureTruthTests(unittest.TestCase):
    @staticmethod
    def connector_with(exchange):
        connector = object.__new__(ExchangeConnector)
        connector.symbol = "BTC/USDT"
        connector.exchanges = {"okx": exchange}
        connector._skip_funding = {}
        connector._funding_fail_count = {}
        connector._record_success = lambda *args: None
        connector._record_failure = lambda *args: None
        return connector

    def test_public_derivative_identity_and_nonzero_funding_provenance(self):
        class FakeExchange:
            options = {"defaultType": "spot"}
            seen = None

            def fetch_funding_rate(self, symbol):
                self.seen = symbol
                return {"fundingRate": 0.00025, "timestamp": 1000}

        exchange = FakeExchange()
        funding = self.connector_with(exchange).fetch_funding_rate("okx")
        self.assertEqual(resolve_public_derivative_symbol("okx", "BTC/USDT"), "BTC/USDT:USDT")
        self.assertEqual(exchange.seen, "BTC/USDT:USDT")
        self.assertEqual(exchange.options["defaultType"], "spot")
        observations = build_feature_observations(
            exchange="okx", ohlcv=None, orderbook=None, funding=funding, open_interest=None,
        )
        item = observations["funding_signal"]
        self.assertEqual(item["status"], "REAL_NONZERO")
        self.assertEqual(item["observed_value"], -0.025)
        self.assertEqual(item["instrument"], "BTC/USDT:USDT")
        self.assertEqual(item["market_type"], "swap")

    def test_two_comparable_oi_snapshots_produce_momentum(self):
        class FakeExchange:
            options = {"defaultType": "spot"}

            def __init__(self):
                self.rows = iter((
                    {"openInterestAmount": 100.0, "timestamp": 1000},
                    {"openInterestAmount": 110.0, "timestamp": 2000},
                ))

            def fetch_open_interest(self, symbol):
                self.symbol = symbol
                return next(self.rows)

        _OI_SNAPSHOT_CACHE.clear()
        exchange = FakeExchange()
        connector = self.connector_with(exchange)
        first = connector.fetch_open_interest("okx")
        second = connector.fetch_open_interest("okx")
        self.assertFalse(first["oi_change_observed"])
        self.assertIsNone(first["oi_change_24h_pct"])
        self.assertTrue(second["oi_change_observed"])
        self.assertEqual(second["oi_change_24h_pct"], 10.0)
        observations = build_feature_observations(
            exchange="okx", ohlcv=None, orderbook=None, funding=None, open_interest=second,
        )
        self.assertEqual(observations["oi_momentum"]["status"], "REAL_NONZERO")
        self.assertAlmostEqual(observations["oi_momentum"]["observed_value"], 0.1)
    def test_missing_derivatives_are_not_real_zero(self):
        candles = [[0, 100, 101, 99, 100, 10], [1, 100, 101, 99, 100, 10]]
        observations = build_feature_observations(exchange="okx", ohlcv=candles, orderbook={"bid_depth_usdt": 100, "ask_depth_usdt": 100}, funding=None, open_interest=None)
        self.assertEqual(observations["funding_signal"]["status"], "SOURCE_ERROR")
        self.assertEqual(observations["oi_momentum"]["status"], "SOURCE_ERROR")
        self.assertEqual(observations["price_momentum"]["status"], "REAL_OBSERVED_ZERO")

    def test_spot_not_applicable_and_fallback_are_explicit(self):
        observations = build_feature_observations(exchange="kraken", ohlcv=None, orderbook=None, funding=None, open_interest=None, fallback_used=True)
        self.assertEqual(observations["funding_signal"]["status"], "NOT_APPLICABLE")
        self.assertEqual(observations["funding_signal"]["transport_status"], "FALLBACK_USED")

    def test_oi_snapshot_without_comparable_prior_is_missing_not_zero(self):
        observations = build_feature_observations(
            exchange="okx", ohlcv=None, orderbook=None, funding={"rate": 0.0},
            open_interest={"oi_value": 123.0, "oi_change_24h_pct": 0.0},
        )
        self.assertEqual(observations["funding_signal"]["status"], "REAL_OBSERVED_ZERO")
        self.assertEqual(observations["oi_momentum"]["status"], "MISSING")

    def test_runtime_pipeline_preserves_missing_status_with_numeric_fallback(self):
        market = {
            "symbol": "BTC/USDT", "timeframe": "15m",
            "ohlcv": [[0, 100, 101, 99, 100, 10], [1, 100, 101, 99, 100, 10]],
            "ticker": {"bid": 100, "ask": 100.01, "spread_pct": 0.0001},
            "orderbook": {"bid_depth": 100, "ask_depth": 100},
            "funding": {"rate": 0.0}, "open_interest": {"oi_change_24h_pct": 0.0},
            "feature_observations": {
                "funding_signal": {"status": "SOURCE_ERROR", "fallback_value": 0.0},
                "oi_momentum": {"status": "MISSING", "fallback_value": 0.0},
            },
        }
        state = core().ingest_market(market)
        self.assertEqual(state["funding_signal"], 0.0)
        self.assertEqual(state["feature_availability_v1"]["funding_signal"]["status"], "SOURCE_ERROR")
        self.assertEqual(state["feature_availability_v1"]["oi_momentum"]["status"], "MISSING")
        features = core().compress_features(state)
        self.assertIsNone(features["pressures"]["funding"])
        self.assertIsNone(features["pressures"]["oi"])
        self.assertEqual(features["missing_input_mask_v1"]["masked_features"], ["funding_signal", "oi_momentum"])
        self.assertTrue(features["missing_input_mask_v1"]["missing_excluded_from_agreement_denominator"])

    def test_legacy_zero_is_reported_as_conflated_not_observed(self):
        report = feature_availability([proof_row(1)])
        self.assertEqual(report["counts"]["okx|BTCUSDT|funding_signal"]["UNKNOWN_ZERO_CONFLATED"], 1)


class DecisionAndGovernanceTests(unittest.TestCase):
    def test_threshold_split_is_real_and_includes_flat_evaluation_snapshots(self):
        rows = [proof_row(i, minute=i * 60, prediction="FLAT" if i % 2 else "LONG") for i in range(10)]
        split = temporal_purged_split(rows)
        self.assertTrue(split["mechanically_disjoint"])
        self.assertGreaterEqual(split["minimum_gap_seconds"], 7200)
        self.assertTrue(split["purged_embargoed_ids"])
        report = threshold_research(rows)
        self.assertEqual(report["status"], "INSUFFICIENT_OOS_EVIDENCE")
        self.assertGreater(report["evaluation_flat_n"], 0)
        self.assertEqual(report["oos_curve"], [])
        self.assertEqual(
            report["diagnostic_in_sample_directional_only"]["label"],
            "DESCRIPTIVE_IN_SAMPLE_DIRECTIONAL_ONLY_NON_OOS",
        )

    def test_all_flat_reason_categories_are_declared(self):
        self.assertEqual(len(FLAT_REASONS), 12)
        row = proof_row(1)
        row["prediction"] = "FLAT"
        row["audit"]["action_vector"] = {"action": "HOLD", "reason": "negative_ev: -0.1"}
        self.assertEqual(classify_flat_reason(row), "NEGATIVE_OR_INSUFFICIENT_EV")
        report = flat_waterfall([row])
        self.assertEqual(report["per_symbol"]["BTCUSDT"]["transition_loss"]["NEGATIVE_OR_INSUFFICIENT_EV"], 1)

    def test_small_sample_gates_are_explicitly_insufficient(self):
        report = run_all([proof_row(i) for i in range(1, 12)])
        self.assertEqual(report["learning_ab"]["status"], "INSUFFICIENT_CAUSAL_PROVENANCE")
        self.assertEqual(report["learning_ab"]["learning_effect"], "NOT_ESTIMABLE")
        self.assertEqual(report["threshold_research"]["BTCUSDT"]["status"], "INSUFFICIENT_OOS_EVIDENCE")
        self.assertEqual(report["horizon_research"]["BTCUSDT"]["status"], "INSUFFICIENT_OOS_EVIDENCE")
        self.assertEqual(report["signal_ablation"]["BTCUSDT"]["status"], "INSUFFICIENT_OOS_EVIDENCE")
        self.assertFalse(report["edge_claim_supported"])

    def test_historical_preregistration_is_byte_locked(self):
        content = (ROOT / "GO_NOGO_CRITERIA.md").read_bytes()
        self.assertEqual(hashlib.sha256(content).hexdigest(), "0b56cef11d21fe74c211de8f0f72e06fd09a6669b45aea8a58d02a9d9670d6ea")

    def test_current_authority_contract_names_exact_runtime_fields(self):
        doc = (ROOT / "senecio_polymarket" / "docs" / "SENEX_CURRENT_AUTHORITY.md").read_text()
        for text in (
            "PER_SYMBOL", "INDEPENDENT_NONOVERLAP_1H", "independent_1h_rows",
            "RAW_CONVICTION", "UNVALIDATED", "authoritative_score_pct",
            "orders_enabled=false", "live_capital_locked=true",
        ):
            self.assertIn(text, doc)

    def test_readme_points_to_current_runtime_and_cannot_imply_live_authority(self):
        readme = (ROOT / "senecio_polymarket" / "README.md").read_text()
        self.assertIn("backend.main_real:app", readme)
        self.assertIn("orders_enabled=false", readme)
        self.assertIn("authoritative_score_pct", readme)
        self.assertNotIn("A separate broker adapter must be added to enable live trading", readme)

    def test_research_harness_has_no_database_or_writeback_client(self):
        source = (ROOT / "senecio_polymarket" / "backend" / "research" / "aud061_pipeline.py").read_text().lower()
        self.assertNotIn("supabase", source)
        self.assertNotIn("httpx", source)
        self.assertNotIn("requests.", source)
        self.assertNotIn("create_order", source)

    def test_semgrep_workflow_remains_absent(self):
        workflows = ROOT / ".github" / "workflows"
        self.assertFalse(any("semgrep" in path.name.lower() for path in workflows.iterdir()))

    def test_runtime017_paths_are_absent_from_candidate_manifest(self):
        manifest = json.loads((ROOT / "senecio_polymarket" / "docs" / "AUD-061-CANDIDATE-MANIFEST.json").read_text())
        touched = "\n".join(manifest["changed_files"]).lower()
        self.assertNotIn("runtime017", touched)
        self.assertNotIn("runtime-017", touched)
        self.assertEqual(manifest["runtime017_mutation"], "NO")
        self.assertEqual(manifest["supabase_data_mutations"], 0)


if __name__ == "__main__":
    unittest.main()
