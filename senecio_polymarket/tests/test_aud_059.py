from __future__ import annotations

import copy
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from backend import oracle_runner
from backend.authoritative_score import build_authoritative_score, independent_1h_cohort
from backend.research import calibration, decision_engine
from oracle_runtime import institutional_core as learning

BASE = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)


def proof_row(idx: int, minute: int, *, symbol="BTCUSDT", direction="LONG", outcome="WIN", confidence=0.7, valid=True):
    ts = (BASE + timedelta(minutes=minute)).isoformat()
    origin = 100.0
    later = (101.0 if outcome == "WIN" else 99.0) if direction == "LONG" else (99.0 if outcome == "WIN" else 101.0)
    audit = {
        "origin_price_v1": {"version": "origin-price-v1", "price": origin, "timestamp": ts, "source": "okx"},
        "outcomes_dual": {"outcome_15m": outcome, "outcome_1h": outcome, "price_15m_later": later, "price_1h_later": later, "primary_window": "1h"},
        "pipeline": {"step2_features": {"conviction": confidence, "regime_4h": "NEUTRAL", "pressures": {"orderflow": 0.4}}},
    }
    if not valid:
        audit.pop("outcomes_dual")
    return {"id": idx, "ts": ts, "symbol": symbol, "prediction": direction, "confidence": confidence, "price_now": origin, "outcome": outcome, "audit": audit}


def core():
    return learning.SingleDecisionCore(max_drawdown=0.12, ruin_probability_threshold=0.05, hard_stop=True, max_position_pct=0.25, max_leverage=1, min_confidence=0.40, min_ev_to_trade=0.001, no_trade_noise=0.60, initial_capital=1000.0)


class IndependentAuthorityTests(unittest.TestCase):
    def test_four_btc_rows_15m_apart_raw4_independent1(self):
        rows = [proof_row(i + 1, i * 15) for i in range(4)]
        score = build_authoritative_score(rows, symbol="BTCUSDT")
        self.assertEqual(score["proof_qualified_rows_raw"], 4)
        self.assertEqual(score["independent_1h_rows"], 1)
        self.assertEqual(score["verified"], 1)
        self.assertEqual(score["authority_n_field"], "independent_1h_rows")

    def test_five_exactly_60m_apart_independent5(self):
        rows = [proof_row(i + 1, i * 60) for i in range(5)]
        self.assertEqual(len(independent_1h_cohort(rows)), 5)
        self.assertEqual(build_authoritative_score(rows, symbol="BTCUSDT")["independent_1h_rows"], 5)

    def test_eth_cannot_change_btc_n_gates_or_score(self):
        btc = [proof_row(i + 1, i * 60, outcome="WIN" if i % 3 else "LOSS") for i in range(40)]
        eth = [proof_row(1000 + i, i * 60, symbol="ETHUSDT", outcome="LOSS") for i in range(40)]
        solo = build_authoritative_score(btc, symbol="BTCUSDT")
        mixed = build_authoritative_score(btc + eth, symbol="BTCUSDT")
        for key in ("proof_qualified_rows_raw", "independent_1h_rows", "verified", "gates", "score_status", "authoritative_score_pct"):
            self.assertEqual(solo[key], mixed[key], key)

    def test_input_reorder_is_deterministic(self):
        rows = [proof_row(i + 1, i * 15, outcome="WIN" if i % 2 else "LOSS") for i in range(20)]
        shuffled = list(rows)
        random.Random(59).shuffle(shuffled)
        a = build_authoritative_score(rows, symbol="BTCUSDT")
        b = build_authoritative_score(shuffled, symbol="BTCUSDT")
        for key in ("independent_1h_rows", "verified", "wins", "losses", "posterior_accuracy", "gates", "quality"):
            self.assertEqual(a[key], b[key], key)

    def test_nonproof_never_enters_effective_n(self):
        score = build_authoritative_score([proof_row(1, 0), proof_row(2, 60, valid=False), proof_row(3, 120)], symbol="BTCUSDT")
        self.assertEqual(score["proof_qualified_rows_raw"], 2)
        self.assertEqual(score["independent_1h_rows"], 2)

    def test_authority_fails_closed_on_unvalidated_confidence_semantics(self):
        rows = []
        for i in range(100):
            rows.append(proof_row(i + 1, i * 60, direction="LONG" if i < 50 else "SHORT", outcome="WIN" if i % 4 else "LOSS", confidence=0.75))
        score = build_authoritative_score(rows, symbol="BTCUSDT")
        self.assertEqual(score["independent_1h_rows"], 100)
        self.assertIsNone(score["authoritative_score_pct"])
        self.assertEqual(score["confidence_semantics"], "RAW_CONVICTION")
        self.assertEqual(score["confidence_probability_semantics"], "UNVALIDATED")
        self.assertFalse(score["quality"]["gates"]["brier"]["pass"])
        self.assertFalse(score["quality"]["gates"]["ece"]["pass"])
        self.assertTrue(score["by_window_diagnostic_only"])

    def test_paper_live_locks_closed(self):
        score = build_authoritative_score([], symbol="BTCUSDT")
        self.assertEqual(score["trade_mode"], "PAPER")
        self.assertFalse(score["orders_enabled"])
        self.assertTrue(score["live_capital_locked"])
        self.assertEqual(oracle_runner._state["trade_mode"], "PAPER")
        self.assertTrue(oracle_runner._state["live_capital_locked"])


class LearningProvenanceTests(unittest.TestCase):
    def test_predecision_provenance_and_weight_hash_are_deterministic(self):
        rows = [proof_row(i + 1, i * 60, outcome="WIN" if i % 3 == 0 else "LOSS") for i in range(12)]
        sa = learning.replay_authoritative_learning(core(), rows, "BTCUSDT")
        sb = learning.replay_authoritative_learning(core(), list(reversed(rows)), "BTCUSDT")
        self.assertEqual(sa["proof_qualified_available_before_decision"], 12)
        self.assertTrue(sa["uses_only_prior_settled_evidence"])
        self.assertEqual(sa["evidence_cut"], "PRE_DECISION_SNAPSHOT")
        self.assertEqual(sa["source_prediction_ids"], sb["source_prediction_ids"])
        self.assertEqual(sa["effective_weights_hash"], sb["effective_weights_hash"])
        self.assertEqual(len(sa["effective_weights_hash"]), 64)


class CalibrationTruthTests(unittest.TestCase):
    def test_insufficient_sample_reports_insufficient_oos(self):
        y = np.asarray([0, 1] * 20, dtype=float)
        p = np.asarray([0.4, 0.6] * 20, dtype=float)
        with tempfile.TemporaryDirectory() as tmp:
            report = calibration.fit_and_evaluate(y, p, method="platt", calibrators_dir=tmp, reliability_dir=tmp)
        self.assertEqual(report.evaluation_status, "INSUFFICIENT_OOS_EVIDENCE")
        self.assertFalse(report.authority_eligible)
        self.assertIsNone(report.brier_after)
        self.assertTrue(report.extra["in_sample_diagnostic"]["diagnostic_only"])

    def test_sufficient_ordered_sample_uses_temporal_purged_oos(self):
        y = np.asarray([0, 1] * 60, dtype=float)
        p = np.asarray([0.35, 0.65] * 60, dtype=float)
        with tempfile.TemporaryDirectory() as tmp:
            report = calibration.fit_and_evaluate(y, p, method="platt", calibrators_dir=tmp, reliability_dir=tmp)
        self.assertEqual(report.evaluation_status, "OOS_TEMPORAL_HOLDOUT")
        self.assertEqual(report.evaluation_scope, "CHRONOLOGICAL_TRAIN_PURGE_TEST")
        self.assertTrue(report.authority_eligible)
        self.assertGreater(report.extra["n_purged"], 0)
        self.assertGreater(report.extra["n_oos"], 0)


class MultipleTestingTests(unittest.TestCase):
    def test_bonferroni_false_forces_all_checks_fail(self):
        study = {"tests": {"counterfactual_search": {"results": [{"filter": {"feature": "hour_utc", "excluded_bucket": 3}, "wr_delta_pp": 20.0, "n_kept": 100, "n_removed": 20, "wr_with_filter": 0.70, "wr_without_filter": 0.50}]}, "permutation_hour_buckets": {3: {"p_value": 0.001}}, "cpcv": {"pbo": 0.1}, "multiple_testing_corrections": {"tests": [{"test": "permutation_hour_3", "survives_bonferroni": False}]}}}
        item = decision_engine.evaluate_all_candidates(study)["candidates"][0]
        self.assertTrue(item["checks"]["delta_wr_passes"])
        self.assertTrue(item["checks"]["p_value_passes"])
        self.assertTrue(item["checks"]["cpcv_positive"])
        self.assertFalse(item["checks"]["bonferroni_passes"])
        self.assertFalse(item["all_checks_pass"])
        self.assertEqual(item["recommendation"], "REJECT_PATCH")


class LegacyQuarantineTests(unittest.TestCase):
    def test_production_start_guard_quarantines_legacy_backfill(self):
        from backend import main_real
        saved = copy.deepcopy(oracle_runner._state)
        try:
            main_real.quarantine_legacy_outcome_backfill()
            self.assertTrue(oracle_runner._state["bogus_backfill_done"])
            self.assertTrue(oracle_runner._state["legacy_backfill_quarantined"])
            self.assertEqual(oracle_runner._state["bogus_backfill_count"], 0)
        finally:
            oracle_runner._state.clear()
            oracle_runner._state.update(saved)


if __name__ == "__main__":
    unittest.main()
