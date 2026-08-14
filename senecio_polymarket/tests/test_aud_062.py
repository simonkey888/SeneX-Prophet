from __future__ import annotations

import ast
import gzip
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.research import aud062_forensics as audit


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "docs" / "evidence" / "aud062-public-inputs.json.gz"
EVIDENCE = ROOT / "docs" / "evidence"


def _load_bundle() -> dict:
    with gzip.open(INPUT, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Aud062FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = _load_bundle()
        cls.rows = cls.bundle["predictions_payload"]["predictions"]
        cls.artifacts, cls.attribution = audit.build_artifacts(cls.bundle)

    def test_exact_base_and_complete_public_sample(self):
        self.assertEqual(audit.BASE_SHA, "49c5f0a69609c005da80e48b585e91d8582a5ac6")
        self.assertEqual(audit.BASE_TREE, "3e323bcc2795f97b29242883d3bf2a015c092ccd")
        self.assertEqual(len(self.rows), 348)
        self.assertEqual({row["symbol"] for row in self.rows}, {"BTCUSDT", "ETHUSDT"})
        self.assertGreaterEqual(len(self.rows), 100)

    def test_every_decision_has_explicit_first_binding_gate(self):
        self.assertEqual(len(self.attribution), 348)
        self.assertFalse(any(item["first_binding_gate"] == "UNKNOWN_CAUSAL_PATH" for item in self.attribution))
        self.assertTrue(all(item["first_binding_gate"] in audit.FLAT_CAUSES | {"NONE_DIRECTIONAL_EXECUTE"} for item in self.attribution))

    def test_flat_distribution_covers_all_rows_and_postdeploy_rows(self):
        evidence = self.artifacts["aud-062-flat-cause-distribution.json"]
        self.assertEqual(evidence["total"]["n"], 348)
        self.assertEqual(evidence["post_aud061"]["n"], 8)
        self.assertEqual(evidence["total"]["flat_n"], 253)
        self.assertEqual(evidence["unknown_causal_path_n"], 0)


class EVFormulaTests(Aud062FixtureTests):
    def test_full_numeric_chain_reconciles_without_residual(self):
        evidence = self.artifacts["aud-062-ev-bridge.json"]
        self.assertTrue(evidence["ev_formula_reconciled"])
        self.assertFalse(evidence["unexplained_residual"])
        self.assertLessEqual(evidence["max_absolute_residual"], evidence["tolerance"])
        self.assertEqual(evidence["rows_reconciled"], 348)

    def test_hidden_market_anchor_materially_changes_tradeability(self):
        evidence = self.artifacts["aud-062-ev-bridge.json"]
        self.assertEqual(evidence["market_anchor_binding_n"], 317)
        self.assertEqual(evidence["positive_base_to_negative_adjusted_n"], 159)
        self.assertEqual(evidence["core_tradeable_but_anchor_rejected_n"], 175)
        self.assertEqual(evidence["invariant_results"]["NEGATIVE_EV_FROM_HIDDEN_TERM"], "CONFIRMED")

    def test_representative_long_short_flat_bridges_exist(self):
        evidence = self.artifacts["aud-062-ev-bridge.json"]
        self.assertEqual(set(evidence["representative_numeric_bridges"]), {"LONG", "SHORT", "FLAT"})
        self.assertLess(evidence["representative_numeric_bridges"]["FLAT"]["adjusted_ev_stored"], 0)


class RiskSemanticsTests(Aud062FixtureTests):
    def test_current_rows_expose_two_unlabeled_ruin_semantics(self):
        evidence = self.artifacts["aud-062-risk-survivability-audit.json"]
        self.assertEqual(evidence["contradictory_display_state_n"], 348)
        self.assertFalse(evidence["risk_fields_coherent"])
        self.assertEqual(evidence["ruin_prob_contradiction"], "CONFIRMED")
        self.assertEqual(evidence["missing_machine_field"], "pipeline.step3_risk.survivability_ruin_prob")

    def test_core_ruin_kill_payload_omits_numeric_authority_characterization(self):
        from oracle_runtime.institutional_core import OriginalSingleDecisionCore

        core = OriginalSingleDecisionCore()
        result = core.filter_risk({}, {"drawdown": 0.10, "var": 0.05, "loss_streak": 4, "capital": 900.0})
        self.assertEqual(result["verdict"], "KILL")
        self.assertIn("RUIN_PROB", result["reason"])
        self.assertNotIn("ruin_prob", result)

    def test_candidate_serializes_distinct_survivability_machine_probability(self):
        from oracle_runtime.institutional_core import SingleDecisionCore

        result = SingleDecisionCore().filter_risk({}, {})
        self.assertEqual(result["ruin_prob"], 0.0)
        self.assertEqual(result["survivability_ruin_prob"], 0.5)
        self.assertIn("HIGH_RUIN_PROB: 50.00%", result["surv_reason"])


class FeatureTruthTests(Aud062FixtureTests):
    def test_missing_is_masked_and_never_reclassified_as_observed_zero(self):
        evidence = self.artifacts["aud-062-feature-availability.json"]
        self.assertTrue(evidence["missing_is_not_real_zero"])
        self.assertTrue(evidence["source_error_is_not_real_zero"])
        self.assertTrue(evidence["missing_excluded_from_agreement_denominator"])
        self.assertGreaterEqual(len(evidence["masked_rows"]), 2)

    def test_feature_cutoff_gaps_are_explicit_not_silently_passed(self):
        evidence = self.artifacts["aud-062-feature-availability.json"]
        self.assertFalse(evidence["provenance_complete"])
        self.assertTrue(evidence["missing_feature_observation_timestamp_counts"])
        self.assertEqual(evidence["missing_material_counterfactual"], "NOT_ESTIMABLE_UNKNOWN_TRUE_VALUES")

    def test_candidate_feature_observations_have_exact_source_timestamps(self):
        from oracle.exchange_connector import build_feature_observations

        observations = build_feature_observations(
            exchange="okx",
            ohlcv=[[1_000, 1, 2, 1, 1.5, 10], [2_000, 1, 2, 1, 1.6, 12]],
            orderbook={"bid_depth": 20, "ask_depth": 10, "timestamp": 1_500},
            funding={"rate": 0.0001, "timestamp": 1_700},
            open_interest={"oi_change_observed": True, "oi_change_24h_pct": 1.0, "timestamp": 1_800},
        )
        self.assertEqual(observations["price_momentum"]["observed_at"], 2_000)
        self.assertEqual(observations["volume_delta"]["observed_at"], 2_000)
        self.assertEqual(observations["bidask_imbalance"]["observed_at"], 1_500)
        self.assertEqual(observations["orderflow"]["observed_at"], 2_000)


class LearningProvenanceTests(Aud062FixtureTests):
    def test_exact_component_replay_does_not_claim_full_model_ab(self):
        evidence = self.artifacts["aud-062-learning-frozen-vs-learned.json"]
        self.assertEqual(evidence["status"], "INSUFFICIENT_CAUSAL_PROVENANCE")
        self.assertEqual(evidence["analysis_type"], "COMPONENT_LEVEL_WEIGHT_SENSITIVITY_NOT_MODEL_AB")
        self.assertEqual(evidence["paired_n"], 8)
        self.assertEqual(evidence["learned_exact_final_replay_n"], 8)
        self.assertEqual(evidence["effective_weight_hash_match_n"], 8)
        self.assertEqual(evidence["source_evidence_hash_match_n"], 0)

    def test_required_frozen_learned_fields_exist_for_every_pair(self):
        required = {
            "FROZEN_DECISION", "LEARNED_DECISION", "FROZEN_PRESSURE",
            "LEARNED_PRESSURE", "FROZEN_EV", "LEARNED_EV",
            "DECISION_CHANGED", "WEIGHT_DELTA_BY_FEATURE",
        }
        evidence = self.artifacts["aud-062-learning-frozen-vs-learned.json"]
        self.assertTrue(evidence["decisions"])
        self.assertTrue(all(required <= set(item) for item in evidence["decisions"]))
        self.assertEqual(evidence["decision_changed_n"], 0)

    def test_candidate_persists_exact_source_observation_epochs(self):
        from oracle_runtime import institutional_core as learning

        base = datetime(2026, 8, 1, tzinfo=timezone.utc)
        ts = base.isoformat()
        observed = (base + timedelta(hours=1)).timestamp()
        row = {
            "id": 1, "ts": ts, "symbol": "BTCUSDT", "prediction": "LONG",
            "confidence": 0.7, "price_now": 100.0, "outcome": "WIN",
            "_senex_snapshot_observed_at_epoch": observed,
            "audit": {
                "origin_price_v1": {"version": "origin-price-v1", "price": 100.0, "timestamp": ts, "source": "okx"},
                "outcomes_dual": {"outcome_15m": "WIN", "outcome_1h": "WIN", "price_15m_later": 101.0, "price_1h_later": 101.0, "primary_window": "1h"},
                "pipeline": {"step2_features": {"conviction": 0.7, "regime_4h": "NEUTRAL", "pressures": {"orderflow": 0.2}}},
            },
        }
        core = learning.SingleDecisionCore()
        state = learning.replay_authoritative_learning(
            core, [row], "BTCUSDT", decision_cutoff=base + timedelta(hours=2),
        )
        self.assertEqual(state["source_prediction_ids"], [1])
        self.assertEqual(state["source_settlement_observation_epochs"], [
            {"prediction_id": 1, "observed_at_epoch": observed},
        ])


class ConfidenceSemanticsTests(Aud062FixtureTests):
    def test_probability_like_scores_are_not_calibrated_probability(self):
        evidence = self.artifacts["aud-062-confidence-semantics.json"]
        self.assertEqual(evidence["fields"]["up_prob"]["class"], "HEURISTIC_TRANSFORM")
        self.assertFalse(evidence["fields"]["up_prob"]["calibrated_probability"])
        self.assertEqual(evidence["reported_96pct_up_interpretation"], "HEURISTIC_TRANSFORM_NOT_P_CORRECT")

    def test_score_authority_is_not_a_production_flat_gate(self):
        self.assertTrue(all(item["authority_gate"] == "NOT_IN_PRODUCTION_DECISION_EQUATION" for item in self.attribution))
        self.assertEqual(self.artifacts["aud-062-flat-cause-distribution.json"]["flat_rate_due_to_authority"], 0.0)


class ExternalShadowTests(Aud062FixtureTests):
    def test_external_ledger_is_read_only_resolved_and_never_applied(self):
        evidence = self.artifacts["aud-062-external-shadow-ledger.json"]
        self.assertEqual(evidence["polymarket_rows"], 167)
        self.assertEqual(evidence["resolved_rows"], 166)
        self.assertGreater(evidence["external_strong_event_count"], 0)
        self.assertTrue(all(item["external_applied"] == 0 for item in evidence["ledger"]))
        self.assertFalse(evidence["production_writeback"])

    def test_horizon_and_snapshot_limits_are_not_overclaimed(self):
        evidence = self.artifacts["aud-062-external-shadow-ledger.json"]
        self.assertEqual(evidence["horizon_match_rate"], 0.0)
        self.assertEqual(evidence["exact_snapshot_timestamp_coverage"], 0.0)
        self.assertTrue(all(item["BLENDED_SHADOW"] == "NOT_ESTIMABLE_NO_PREDECLARED_BLEND" for item in evidence["ledger"]))

    def test_same_market_agreement_is_not_claimed_as_accuracy_or_value_add(self):
        evidence = self.artifacts["aud-062-external-shadow-ledger.json"]
        self.assertEqual(evidence["same_market_resolved_label_agreement"], 0.813253)
        self.assertFalse(evidence["same_market_metric_is_predictive_accuracy"])
        self.assertEqual(evidence["external_value_add_status"], "NOT_ESTIMABLE_NO_CAUSALLY_ALIGNED_OOS_LABEL")
        self.assertTrue(evidence["external_signal_value_assessment"]["unsupported"])
        self.assertTrue(all(row["external_applied"] == 0 for row in evidence["kalshi_ledger"]))
        self.assertTrue(all(row["external_applied"] == 0 for row in evidence["boros_ledger"]))


class ActionReasonSemanticsTests(Aud062FixtureTests):
    def test_historical_positive_ev_negative_label_is_fully_quantified(self):
        evidence = self.artifacts["aud-062-action-reason-semantics.json"]
        self.assertEqual(evidence["positive_adjusted_ev_mislabeled_negative_n"], 22)
        self.assertEqual(len(evidence["affected_prediction_ids"]), 22)

    def test_candidate_reason_is_truthful_without_changing_hold_gate(self):
        from oracle_runtime.institutional_core import SingleDecisionCore

        core = SingleDecisionCore()
        result = core.produce_action(
            {"direction": "LONG", "conviction": 0.6, "noise": 0.1},
            {"verdict": "ALLOW", "reason": "ok", "risk_score": 0.0, "size_multiplier": 1.0},
            {"tradeable": False, "adjusted_ev": 0.00004, "dynamic_min_ev": 0.00005},
            {"feasible": False, "reason": "ev_not_tradeable", "size_adjustment": 0.0},
            {"price": 100.0, "symbol": "BTCUSDT", "timeframe": "15m"},
        )
        self.assertEqual(result["action"], "HOLD")
        self.assertTrue(result["reason"].startswith("ev_below_dynamic_min:"))
        self.assertIn("adjusted_ev=0.00004000", result["reason"])
        self.assertIn("dynamic_min_ev=0.00005000", result["reason"])


class GovernanceTests(Aud062FixtureTests):
    def test_auto_cd_is_unsafe_under_current_unprotected_main(self):
        evidence = self.artifacts["aud-062-governance-auto-cd.json"]
        self.assertFalse(evidence["branch_protected"])
        self.assertEqual(evidence["ruleset_count"], 0)
        self.assertEqual(evidence["AUTO_CD_SAFE_UNDER_CURRENT_GOVERNANCE"], "NO")
        self.assertTrue(any("oracle.yml" in item for item in evidence["UNREVIEWED_MAIN_TO_PROD_PATHS"]))

    def test_oracle_workflow_has_contents_write_and_direct_push(self):
        source = (ROOT.parent / ".github" / "workflows" / "oracle.yml").read_text(encoding="utf-8")
        self.assertIn("contents: write", source)
        self.assertIn("workflow_dispatch:", source)
        self.assertIn("git push", source)


class ArtifactAndSafetyTests(Aud062FixtureTests):
    def test_required_artifacts_exist_and_manifest_hashes_match(self):
        required = {
            "aud-062-decision-graph.json", "aud-062-decision-attribution.csv",
            "aud-062-decision-attribution.json", "aud-062-flat-cause-distribution.json",
            "aud-062-ev-bridge.json", "aud-062-cost-model-audit.json",
            "aud-062-risk-survivability-audit.json", "aud-062-feature-availability.json",
            "aud-062-learning-frozen-vs-learned.json", "aud-062-confidence-semantics.json",
            "aud-062-external-shadow-ledger.json", "aud-062-horizon-alignment.json",
            "aud-062-pre-post-behavior.json", "aud-062-governance-auto-cd.json",
            "aud-062-findings.json", "aud-062-manifest.json",
            "aud-062-action-reason-semantics.json", "aud-062-authority-feedback-loop.json",
            "aud-062-score-truth.json", "aud-062-dataset-provenance.json",
            "aud-062-publication-sanitization.json",
        }
        self.assertTrue(all((EVIDENCE / name).is_file() for name in required))
        manifest = json.loads((EVIDENCE / "aud-062-manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(all(_sha(EVIDENCE / name) == value for name, value in manifest["artifact_hashes"].items()))

    def test_artifact_generation_is_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            audit.write_artifacts(self.bundle, Path(left))
            audit.write_artifacts(self.bundle, Path(right))
            left_files = sorted(path.name for path in Path(left).iterdir())
            right_files = sorted(path.name for path in Path(right).iterdir())
            self.assertEqual(left_files, right_files)
            self.assertTrue(all(_sha(Path(left) / name) == _sha(Path(right) / name) for name in left_files))

    def test_forensic_module_imports_no_network_or_mutation_client(self):
        tree = ast.parse(Path(audit.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue({"httpx", "requests", "supabase", "subprocess"}.isdisjoint(imported))

    def test_input_and_outputs_contain_no_secret_material(self):
        needles = ("NORTHFLANK_API_TOKEN=", "SUPABASE_KEY=", "PRIVATE KEY-----", "sb_secret_")
        paths = [INPUT, *EVIDENCE.glob("aud-062-*")]
        for path in paths:
            if path.suffix == ".gz":
                with gzip.open(path, "rb") as handle:
                    data = handle.read()
            else:
                data = path.read_bytes()
            self.assertFalse(any(needle.encode() in data for needle in needles), path.name)

    def test_hard_safety_contract_is_unchanged(self):
        manifest = json.loads((EVIDENCE / "aud-062-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["production_mutations"], 0)
        self.assertEqual(manifest["northflank_mutations"], 0)
        self.assertEqual(manifest["database_mutations"], 0)
        self.assertEqual(manifest["runtime017_mutations"], 0)
        self.assertFalse(manifest["merge"])
        self.assertFalse(manifest["deploy"])
        self.assertFalse(manifest["threshold_changes"])
        self.assertFalse(manifest["weight_changes"])
        self.assertFalse(manifest["external_directional_activation"])
        self.assertFalse(manifest["production_decision_semantics_changed"])

    def test_publication_sanitization_and_dataset_provenance_gates(self):
        report = json.loads((EVIDENCE / "aud-062-publication-sanitization.json").read_text(encoding="utf-8"))
        for key in ("PUBLICATION_SECRET_SCAN", "PUBLICATION_PII_REVIEW", "PUBLICATION_SCOPE_REVIEW"):
            self.assertEqual(report[key], "PASS")
        self.assertEqual(report["PUBLICATION_NONPUBLIC_DATA"], "NONE")
        self.assertEqual(report["PUBLICATION_AUTH_HEADERS"], "NONE")
        self.assertEqual(report["PUBLICATION_CREDENTIALS"], "NONE")
        self.assertTrue(all(item["confirmed_secret_count"] == 0 for item in report["scanners"]))
        self.assertGreaterEqual(report["decompressed_archive_count"], 1)
        bundle = self.bundle
        self.assertNotIn("enriched", bundle["predictions_payload"]["predictions"][0]["audit"])
        inventory = json.loads((EVIDENCE / "aud-062-dataset-provenance.json").read_text(encoding="utf-8"))
        required_fields = {"SOURCE_CLASS", "CAPTURE_TIME_UTC", "SOURCE_ENDPOINT_OR_CLASS", "RAW_OR_DERIVED", "TRANSFORMATION", "ROW_COUNT", "SHA256"}
        self.assertTrue(all(required_fields <= set(item) for item in inventory["datasets"].values()))
        csv_head = (EVIDENCE / "aud-062-decision-attribution.csv").read_text(encoding="utf-8").splitlines()[:7]
        self.assertEqual(csv_head[0], "# SOURCE_CLASS=PRODUCTION_DERIVED_FROM_PUBLIC_INPUTS")
        self.assertTrue(csv_head[-1].startswith("# SHA256="))

    def test_findings_standard_and_claim_assessments_are_complete(self):
        evidence = self.artifacts["aud-062-findings.json"]
        required = {
            "FINDING_ID", "SEVERITY", "STATUS", "TITLE", "AFFECTED_PATHS",
            "AFFECTED_RUNTIME_FIELDS", "FIRST_BAD_OR_RELEVANT_COMMIT", "EVIDENCE",
            "REPRODUCTION", "CAUSAL_IMPACT", "SAFETY_IMPACT", "STATISTICAL_IMPACT",
            "WHY_EXISTING_TESTS_MISSED_IT", "MINIMUM_CORRECTION", "REGRESSION_GATE",
        }
        self.assertEqual(evidence["finding_count"], 9)
        self.assertEqual(evidence["material_finding_count"], 9)
        self.assertTrue(all(required <= set(item) for item in evidence["findings"]))
        claims = self.artifacts["aud-062-claim-assessments.json"]
        self.assertEqual(set(claims), set("ABCDEFG"))


if __name__ == "__main__":
    unittest.main()
