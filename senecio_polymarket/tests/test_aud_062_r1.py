from __future__ import annotations

import gzip
import json
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

from backend.research import aud062_forensics as audit
from backend.research.aud062_r1_contracts import (
    attach_truthful_score_semantics,
    canonical_ev_contract,
    enrich_action_reason,
    feature_provenance_contract,
    instrument_cost_contract,
    verify_persisted_roundtrip,
)


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
INPUT = ROOT / "docs" / "evidence" / "aud062-public-inputs.json.gz"


def load_bundle() -> dict:
    with gzip.open(INPUT, "rt", encoding="utf-8") as handle:
        return json.load(handle)


class Aud062R1EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle()
        cls.artifacts, cls.attribution = audit.build_artifacts(cls.bundle)

    def test_f001_single_ev_authority_is_fail_closed_without_minmax(self):
        contract = canonical_ev_contract(
            "BTCUSDT",
            {"direction": "LONG", "heuristic_up_score": 0.91},
            {"ruin_prob": 0.0, "survivability_ruin_prob": 0.5},
            {"base_ev": 0.01, "adjusted_ev": -0.01, "market_anchor_ev": -0.01},
        )
        self.assertEqual(contract["authority_status"], "NOT_ESTIMABLE")
        self.assertFalse(contract["tradeable"])
        self.assertEqual(contract["fail_closed_reason"], "COST_MODEL_NOT_AUTHORITATIVE")
        self.assertEqual(
            contract["historical_parallel_market_anchor"]["selection_operator"],
            "NO_MIN_MAX_OR_FALLBACK_AUTHORITY",
        )
        self.assertFalse(contract["probability_input"]["calibrated_probability"])
        from oracle_runtime.institutional_core import SingleDecisionCore

        runtime = SingleDecisionCore().compute_ev(
            {
                "direction": "LONG", "conviction": 0.8, "noise": 0.1,
                "up_prob": 0.9, "down_prob": 0.1,
                "heuristic_up_score": 0.9, "heuristic_down_score": 0.1,
            },
            {"risk_score": 0.0, "size_multiplier": 1.0, "ruin_prob": 0.0, "survivability_ruin_prob": 0.5},
            {"symbol": "BTC/USDT", "volatility": 0.01},
            slippage_bps=1.0,
            ohlcv=None,
        )
        self.assertFalse(runtime["tradeable"])
        self.assertFalse(runtime["parallel_market_anchor_decision_authority"])
        self.assertEqual(runtime["canonical_ev_v1"]["authority_status"], "NOT_ESTIMABLE")

    def test_f002_reason_and_survivability_machine_probability_share_calculation(self):
        from oracle_runtime.institutional_core import SingleDecisionCore

        result = SingleDecisionCore().filter_risk({}, {})
        self.assertEqual(result["survivability_ruin_prob"], 0.5)
        self.assertIn("50.00%", result["surv_reason"])
        parsed = float(re.search(r"([0-9]+(?:\.[0-9]+)?)%", result["surv_reason"]).group(1)) / 100.0
        self.assertEqual(parsed, result["survivability_ruin_prob"])
        semantics = result["ruin_probability_semantics_v1"]
        self.assertTrue(semantics["survivability_reason_and_machine_field_same_calculation"])
        self.assertFalse(semantics["same_semantics"])

    def test_f003_no_workflow_direct_push_and_reviewable_settings_manifest(self):
        oracle = (REPO / ".github/workflows/oracle.yml").read_text(encoding="utf-8")
        self.assertNotRegex(oracle, r"contents:\s*write")
        self.assertNotRegex(oracle, r"\bgit\s+push\b")
        manifest = self.artifacts["aud-062-r1-governance-settings-manifest.json"]
        self.assertEqual(manifest["status"], "PROPOSED_NOT_APPLIED")
        self.assertFalse(manifest["github_settings_applied"])
        self.assertEqual(manifest["direct_push_to_main"], "DENY")
        self.assertEqual(manifest["bypass_actors"], [])
        self.assertIn("AUD_EXACT_HEAD_GATE", manifest["required_checks"])

    def test_f004_persisted_row_only_provenance_round_trip(self):
        market = {
            "symbol": "BTC/USDT", "timeframe": "15m", "exchange_used": "okx",
            "candle_ts": 1_786_665_600_000,
            "ticker": {"last": 100000.0, "timestamp": 1_786_665_600_000},
            "orderbook": {"bid": 99999.0, "ask": 100001.0, "timestamp": 1_786_665_600_000},
        }
        result = {
            "timestamp": "2026-08-14T00:00:00+00:00",
            "_audit": {
                "pipeline": {
                    "step1_market": {"feature_availability_v1": {
                        "price_momentum": {
                            "status": "REAL_NONZERO", "source": "okx:BTC-USDT:ohlcv",
                            "observed_at": "2026-08-14T00:00:00+00:00",
                            "exchange_timestamp": 1_786_665_600_000,
                            "query_observation_epoch": 1_786_665_600.0,
                        },
                        "funding_signal": {"status": "MISSING", "source": "not_applicable_spot", "observed_at": None},
                    }},
                    "step2_features": {"learning_state_v1": {
                        "source_prediction_ids": [1],
                        "source_settlement_observation_epochs": [{"prediction_id": 1, "observed_at_epoch": 1_786_662_000.0}],
                        "source_evidence_hash": "a" * 64,
                        "decision_weights_hash": "b" * 64,
                        "shadow_weights_hash": "c" * 64,
                        "code_hash": "d" * 64,
                        "config_hash": "e" * 64,
                    }},
                },
                "external_markets_v1": {
                    name: {"source": name.upper(), "status": "OK", "observed_at": "2026-08-14T00:00:00+00:00"}
                    for name in ("polymarket", "kalshi", "boros")
                },
            },
        }
        persisted = json.loads(json.dumps(feature_provenance_contract(market, result), sort_keys=True))
        check = verify_persisted_roundtrip(persisted)
        self.assertTrue(check["hash_matches"])
        self.assertEqual(check["feature_observation_cutoff_classification"], "COMPLETE")
        self.assertTrue(check["learning_hash_present"])
        self.assertTrue(check["market_snapshot_identity_present"])

    def test_f005_instrument_units_and_cost_authority(self):
        btc = instrument_cost_contract("BTCUSDT")
        eth = instrument_cost_contract("ETHUSDT")
        self.assertEqual(btc["instrument_id"], "BTC-USDT")
        self.assertEqual(eth["instrument_id"], "ETH-USDT")
        self.assertEqual(btc["instrument_type"], "SPOT")
        self.assertEqual(btc["unit_conversions"]["one_basis_point_decimal_return"], 0.0001)
        self.assertEqual(1 / 10_000, 0.0001)
        self.assertEqual(btc["cost_model_authority"], "COST_MODEL_NOT_AUTHORITATIVE")
        self.assertFalse(btc["tradeable"])

    def test_f006_truthful_score_names_and_deprecated_aliases(self):
        features = attach_truthful_score_semantics({"up_prob": 0.9, "down_prob": 0.1})
        self.assertEqual(features["heuristic_up_score"], 0.9)
        self.assertFalse(features["score_semantics_v1"]["heuristic_up_score"]["calibrated_probability"])
        aliases = features["score_semantics_v1"]["deprecated_aliases"]
        self.assertTrue(aliases["up_prob"]["deprecated"])
        self.assertFalse(aliases["up_prob"]["calibrated_probability"])
        dashboard = (ROOT / "frontend/app.js").read_text(encoding="utf-8")
        self.assertIn("raw conviction", dashboard)
        self.assertNotIn("calibrated probability", dashboard.lower())

    def test_f007_learning_is_shadow_only_and_size_uses_frozen_base(self):
        from oracle_runtime import institutional_core as learning

        base = datetime(2026, 7, 1, tzinfo=timezone.utc)
        rows = []
        for index in range(10):
            ts = base + timedelta(hours=index * 2)
            observed = ts + timedelta(hours=1)
            rows.append({
                "id": index + 1, "ts": ts.isoformat(), "symbol": "BTCUSDT",
                "prediction": "LONG", "confidence": 0.8, "price_now": 100.0,
                "outcome": "WIN" if index % 2 == 0 else "LOSS",
                "_senex_snapshot_observed_at_epoch": observed.timestamp(),
                "audit": {
                    "origin_price_v1": {"version": "origin-price-v1", "price": 100.0, "timestamp": ts.isoformat(), "source": "okx"},
                    "outcomes_dual": {"outcome_15m": "WIN", "outcome_1h": "WIN" if index % 2 == 0 else "LOSS", "price_15m_later": 100.5, "price_1h_later": 101.0 if index % 2 == 0 else 99.0, "primary_window": "1h"},
                    "pipeline": {"step2_features": {"conviction": 0.8, "regime_4h": "NEUTRAL", "pressures": {"orderflow": 0.5}}},
                },
            })
        core = learning.SingleDecisionCore()
        frozen = dict(core.weights)
        state = learning.replay_authoritative_learning(
            core, rows, "BTCUSDT", decision_cutoff=base + timedelta(days=3),
        )
        self.assertEqual(state["status"], "SHADOW_ONLY_FAIL_CLOSED")
        self.assertEqual(state["learning_mutation_authority"], "SHADOW_ONLY")
        self.assertEqual(state["size_calibration_authority"], "FROZEN_BASE_ONLY")
        self.assertEqual(core.weights, frozen)
        self.assertEqual(state["decision_weights"], learning._weights_payload(frozen))
        self.assertEqual(state["mutations"], 0)
        self.assertGreater(state["shadow_mutations"], 0)

    def test_f008_positive_ev_never_uses_negative_ev_reason(self):
        result = enrich_action_reason(
            {"action": "HOLD", "reason": "negative_ev: 0.00004000"},
            {"direction": "LONG", "conviction": 0.6, "noise": 0.1},
            {"verdict": "ALLOW"},
            {"tradeable": False, "adjusted_ev": 0.00004, "dynamic_min_ev": 0.00005},
            {"feasible": False, "reason": "ev_not_tradeable"},
        )
        self.assertEqual(result["reason_code"], "EV_BELOW_DYNAMIC_MIN")
        self.assertTrue(result["reason"].startswith("ev_below_dynamic_min:"))
        self.assertNotIn("negative_ev", result["reason"])
        self.assertTrue(result["first_binding_gate_v1"]["reproducible"])

    def test_candidate_fixture_reason_paths_never_fall_to_unknown(self):
        fixtures = [
            ({"action": "KILL", "reason": "risk"}, {"direction": "LONG"}, {"verdict": "KILL", "reason": "risk"}, {"tradeable": True}, {"feasible": True}),
            ({"action": "HOLD", "reason": "no_direction"}, {"direction": "NEUTRAL"}, {"verdict": "ALLOW"}, {"tradeable": True}, {"feasible": True}),
            ({"action": "HOLD", "reason": "regime"}, {"direction": "NEUTRAL", "long_suppressed_by_regime": True}, {"verdict": "ALLOW"}, {"tradeable": True}, {"feasible": True}),
            ({"action": "HOLD", "reason": "low_conviction: 0.1"}, {"direction": "LONG"}, {"verdict": "ALLOW"}, {"tradeable": True}, {"feasible": True}),
            ({"action": "HOLD", "reason": "negative_ev"}, {"direction": "SHORT"}, {"verdict": "ALLOW"}, {"tradeable": False, "adjusted_ev": -0.1, "dynamic_min_ev": 0.01}, {"feasible": False}),
            ({"action": "HOLD", "reason": "negative_ev"}, {"direction": "SHORT"}, {"verdict": "ALLOW"}, {"tradeable": False, "adjusted_ev": 0.001, "dynamic_min_ev": 0.01}, {"feasible": False}),
            ({"action": "HOLD", "reason": "negative_ev"}, {"direction": "LONG"}, {"verdict": "ALLOW"}, {"tradeable": False, "fail_closed_reason": "COST_MODEL_NOT_AUTHORITATIVE"}, {"feasible": False}),
            ({"action": "HOLD", "reason": "not_feasible"}, {"direction": "LONG"}, {"verdict": "ALLOW"}, {"tradeable": True}, {"feasible": False, "reason": "liquidity"}),
        ]
        classes = [enrich_action_reason(*fixture)["reason_code"] for fixture in fixtures]
        self.assertNotIn("UNKNOWN_CAUSAL_PATH", classes)

    def test_f009_external_agreement_is_not_accuracy_or_value_add(self):
        evidence = self.artifacts["aud-062-r1-external-truth.json"]
        self.assertEqual(evidence["metric_name"], "same_market_5m_resolved_label_agreement")
        self.assertFalse(evidence["senex_1h_predictive_accuracy"])
        self.assertEqual(evidence["incremental_value"], "NOT_ESTIMABLE")
        self.assertEqual(evidence["blended_shadow"], "NOT_ESTIMABLE")
        self.assertEqual(evidence["external_applied"], 0)
        source = (ROOT / "oracle_runtime/predict_only.py").read_text(encoding="utf-8")
        self.assertNotIn("polymarket_model_edge_v1", source)

    def test_external_directional_activation_is_unconditionally_locked(self):
        from oracle_runtime.institutional_core_real import polymarket_experiment_enabled

        with patch.dict(os.environ, {"SENEX_PAPER_POLYMARKET_DIRECTIONAL_EXPERIMENT": "true"}):
            self.assertFalse(polymarket_experiment_enabled())

    def test_complete_348_row_r1_evidence_and_finding_dispositions(self):
        counterfactual = self.artifacts["aud-062-r1-canonical-ev-counterfactual.json"]
        summary = self.artifacts["aud-062-r1-behavior-summary.json"]
        dispositions = self.artifacts["aud-062-r1-finding-disposition.json"]
        self.assertEqual(counterfactual["row_count"], 348)
        self.assertEqual(len(counterfactual["rows"]), 348)
        self.assertEqual(summary["row_count"], 348)
        self.assertEqual(summary["canonical_ev_shadow_distribution"]["tradeable"], 0)
        self.assertEqual(summary["canonical_ev_shadow_distribution"]["fail_closed"], 348)
        self.assertEqual(set(dispositions["findings"]), {f"F{index:03d}" for index in range(1, 10)})
        self.assertTrue(dispositions["all_findings_closed_or_fail_closed"])
        self.assertEqual(dispositions["threshold_changes"], 0)
        self.assertEqual(dispositions["post_hoc_weight_tuning"], 0)
        self.assertEqual(dispositions["external_directional_activation"], 0)

    def test_actions_are_pinned_to_full_immutable_shas(self):
        def action_refs(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "uses" and isinstance(item, str):
                        yield item
                    yield from action_refs(item)
            elif isinstance(value, list):
                for item in value:
                    yield from action_refs(item)

        for name in ("aud-062-forensics.yml", "oracle.yml"):
            source = (REPO / ".github/workflows" / name).read_text(encoding="utf-8")
            refs = list(action_refs(yaml.safe_load(source)))
            self.assertTrue(refs)
            self.assertTrue(all("@" in ref and re.fullmatch(r"[0-9a-f]{40}", ref.rsplit("@", 1)[1]) for ref in refs))


if __name__ == "__main__":
    unittest.main()
