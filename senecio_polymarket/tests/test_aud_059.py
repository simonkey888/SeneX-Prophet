from __future__ import annotations

import copy
import random
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np

from backend import oracle_runner
from backend.authoritative_score import build_authoritative_score, independent_1h_cohort
from backend.portfolio.live_gate import GateStatus, LiveGate
from backend import supabase_client
from backend.research import calibration, decision_engine
from backend.research.coordinator import ResearchCoordinator
from oracle_runtime import institutional_core as learning

from tests.aud063_fixture_support import upgrade_proof_row

def _aud063_upgrade(fn):
    def wrapped(*args, **kwargs):
        return upgrade_proof_row(fn(*args, **kwargs))
    return wrapped

BASE = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)


@_aud063_upgrade
def proof_row(idx: int, minute: int, *, symbol="BTCUSDT", direction="LONG", outcome="WIN", confidence=0.7, valid=True):
    ts = (BASE + timedelta(minutes=minute)).isoformat()
    settled_at = (BASE + timedelta(minutes=minute + 60)).isoformat()
    origin = 100.0
    later = (101.0 if outcome == "WIN" else 99.0) if direction == "LONG" else (99.0 if outcome == "WIN" else 101.0)
    audit = {
        "origin_price_v1": {"version": "origin-price-v1", "price": origin, "timestamp": ts, "source": "okx"},
        "outcomes_dual": {"outcome_15m": outcome, "outcome_1h": outcome, "price_15m_later": later, "price_1h_later": later, "primary_window": "1h", "settled_at": settled_at},
        "pipeline": {"step2_features": {"conviction": confidence, "regime_4h": "NEUTRAL", "pressures": {"orderflow": 0.4}}},
    }
    if not valid:
        audit.pop("outcomes_dual")
    return {"id": idx, "ts": ts, "symbol": symbol, "prediction": direction, "confidence": confidence, "price_now": origin, "outcome": outcome, "audit": audit}


def core():
    return learning.SingleDecisionCore(max_drawdown=0.12, ruin_probability_threshold=0.05, hard_stop=True, max_position_pct=0.25, max_leverage=1, min_confidence=0.40, min_ev_to_trade=0.001, no_trade_noise=0.60, initial_capital=1000.0)


class _BoundedPredictionFetch:
    """PostgREST-like boundary: filter by symbol, then apply newest-N limit."""

    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda row: row["ts"], reverse=True)
        self.calls = []

    async def __call__(self, limit=50, symbol=None):
        self.calls.append({"limit": limit, "symbol": symbol})
        selected = self.rows
        if symbol:
            normalized = str(symbol).upper().replace("/", "").replace("-", "").strip()
            selected = [
                row for row in selected
                if str(row.get("symbol") or "").upper().replace("/", "").replace("-", "").strip() == normalized
            ]
        return selected[:limit]


class _BoundaryGateCoordinator:
    def evaluate_live_gate(self, oracle_score=None):
        authority = (oracle_score or {}).get("authority_1h") or {}
        verified = int((authority.get("global") or {}).get("verified") or 0)
        return GateStatus(
            unlocked=True,
            trade_mode="LIVE",
            live_capital_locked=False,
            conditions={
                "verified": {
                    "value": verified,
                    "threshold": 300,
                    "op": ">=",
                    "pass": verified >= 300,
                },
            },
        )


class _RegistrySpy:
    def __init__(self):
        self.gauges = []

    def observe(self, *args, **kwargs):
        return None

    def set_gauge(self, name, value, labels=None):
        self.gauges.append((name, float(value), labels))


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


class RuntimeControlAuthorityTests(unittest.TestCase):
    def setUp(self):
        self._saved_state = copy.deepcopy(oracle_runner._state)

    def tearDown(self):
        oracle_runner._state.clear()
        oracle_runner._state.update(self._saved_state)

    def _refresh(self, rows):
        with patch.object(supabase_client, "fetch_predictions", new=AsyncMock(return_value=rows)):
            import asyncio
            asyncio.run(oracle_runner._refresh_directional_stats())
        return oracle_runner._state["directional_stats"]

    def test_runtime_keeps_raw4_diagnostic_but_control_authority_n1(self):
        rows = [proof_row(i + 1, i * 15) for i in range(4)]
        stats = self._refresh(rows)["per_symbol"]["BTCUSDT"]
        self.assertEqual(stats["by_window"]["1h"]["global"]["verified"], 4)
        self.assertTrue(stats["by_window"]["1h"]["global"]["diagnostic_only"])
        self.assertEqual(stats["authority_1h"]["global"]["verified"], 1)
        self.assertEqual(stats["gates"]["global_1h"]["n"], 1)

    def test_raw_short_gate_cannot_enable_control_short_only(self):
        rows = [proof_row(i + 1, i, direction="SHORT") for i in range(30)]
        stats = self._refresh(rows)["per_symbol"]["BTCUSDT"]
        self.assertEqual(stats["by_window"]["1h"]["SHORT"]["verified"], 30)
        self.assertEqual(stats["authority_1h"]["SHORT"]["verified"], 1)
        self.assertFalse(stats["gates"]["short_1h"]["pass"])
        self.assertFalse(stats["short_only_paper_mode"])

    def test_eth_rows_cannot_change_btc_runtime_control(self):
        btc = [proof_row(i + 1, i * 15) for i in range(4)]
        eth = [proof_row(1000 + i, i * 60, symbol="ETHUSDT", outcome="LOSS") for i in range(40)]
        solo = self._refresh(btc)["per_symbol"]["BTCUSDT"]
        mixed = self._refresh(btc + eth)["per_symbol"]["BTCUSDT"]
        for key in ("authority_1h", "gates", "short_only_paper_mode"):
            self.assertEqual(solo[key], mixed[key], key)

    def test_newest_500_eth_rows_cannot_evict_btc_runtime_authority(self):
        btc = [
            proof_row(i + 1, i * 60, outcome="WIN" if i % 3 else "LOSS")
            for i in range(40)
        ]
        eth = [
            proof_row(10_000 + i, 10_000 + i, symbol="ETHUSDT", outcome="LOSS")
            for i in range(510)
        ]

        def refresh(rows):
            boundary = _BoundedPredictionFetch(rows)
            with patch.object(supabase_client, "fetch_predictions", new=boundary):
                import asyncio
                asyncio.run(oracle_runner._refresh_directional_stats())
            return copy.deepcopy(oracle_runner._state["directional_stats"]), boundary.calls

        solo, solo_calls = refresh(btc)
        mixed, mixed_calls = refresh(btc + eth)
        btc_solo = solo["per_symbol"]["BTCUSDT"]
        btc_mixed = mixed["per_symbol"]["BTCUSDT"]
        for key in ("authority_1h", "gates", "short_only_paper_mode", "authority_cohort"):
            self.assertEqual(btc_solo[key], btc_mixed[key], key)
        self.assertEqual(btc_mixed["independent_1h_rows"], 40)
        self.assertIn({"limit": 500, "symbol": "BTCUSDT"}, mixed_calls)
        self.assertIn({"limit": 500, "symbol": "ETHUSDT"}, mixed_calls)
        self.assertIn({"limit": 500, "symbol": None}, mixed_calls)
        self.assertIn({"limit": 500, "symbol": "BTCUSDT"}, solo_calls)


class LiveReadinessAuthorityTests(unittest.TestCase):
    @staticmethod
    def _otherwise_passing_inputs():
        return {
            "analytics_report": {"profit_factor": 2.0, "max_drawdown_pct": 1.0},
            "shadow_report": {"passed": True},
            "exec_self_test": {"verified": True},
        }

    def test_live_gate_uses_independent_authority_not_raw_by_window(self):
        rows = [proof_row(i + 1, i, outcome="WIN") for i in range(100)]
        score = build_authoritative_score(rows, symbol="BTCUSDT")
        status = LiveGate().evaluate(oracle_score=score, **self._otherwise_passing_inputs())
        self.assertEqual(score["proof_qualified_rows_raw"], 100)
        self.assertEqual(score["independent_1h_rows"], 2)
        self.assertEqual(status.conditions["verified"]["value"], 2)
        self.assertFalse(status.conditions["verified"]["pass"])
        self.assertFalse(status.unlocked)

    def test_raw_unverified_cannot_change_authority_or_readiness(self):
        proof = [proof_row(i + 1, i * 60) for i in range(4)]
        poisoned = proof + [proof_row(999, 500, outcome="WIN", valid=False)]
        clean_score = build_authoritative_score(proof, symbol="BTCUSDT")
        poisoned_score = build_authoritative_score(poisoned, symbol="BTCUSDT")
        self.assertEqual(clean_score["authority_1h"], poisoned_score["authority_1h"])
        clean = LiveGate().evaluate(oracle_score=clean_score, **self._otherwise_passing_inputs())
        poisoned_status = LiveGate().evaluate(oracle_score=poisoned_score, **self._otherwise_passing_inputs())
        self.assertEqual(clean.conditions, poisoned_status.conditions)

    def test_main_policy_adapter_keeps_effective_gate_locked_and_diagnostic(self):
        from backend.main import _paper_locked_live_gate_state
        from backend.portfolio.live_gate import GateStatus

        class CaptureCoordinator:
            def __init__(self):
                self.oracle_score = None

            def evaluate_live_gate(self, oracle_score=None):
                self.oracle_score = oracle_score
                return GateStatus(
                    unlocked=True,
                    trade_mode="LIVE",
                    live_capital_locked=False,
                    conditions={
                        "verified": {"value": 300, "threshold": 300, "op": ">=", "pass": True},
                    },
                )

        rows = [proof_row(1, 0), proof_row(2, 60, valid=False)]
        coord = CaptureCoordinator()
        state = _paper_locked_live_gate_state(coord, rows, symbol="BTCUSDT")
        self.assertEqual(coord.oracle_score["input_rows"], 2)
        self.assertEqual(coord.oracle_score["proof_qualified_rows_raw"], 1)
        self.assertEqual(coord.oracle_score["independent_1h_rows"], 1)
        self.assertFalse(state["unlocked"])
        self.assertEqual(state["trade_mode"], "PAPER")
        self.assertTrue(state["live_capital_locked"])
        self.assertTrue(state["diagnostic_only"])
        self.assertEqual(state["requested_symbol"], "BTCUSDT")
        self.assertEqual(state["verified"], 1)

    def test_research_report_accepts_authority_gate_state_and_stays_locked(self):
        from backend.main import _paper_locked_live_gate_state
        from backend.portfolio.live_gate import GateStatus
        from backend.research.institutional_report import build_institutional_report

        class ResearchCoordinatorStub:
            def evaluate_live_gate(self, oracle_score=None):
                verified = oracle_score["authority_1h"]["global"]["verified"]
                return GateStatus(
                    unlocked=False,
                    conditions={
                        "verified": {
                            "value": verified,
                            "threshold": 300,
                            "op": ">=",
                            "pass": verified >= 300,
                        },
                    },
                    failed_reasons=["verified"],
                )

        rows = [proof_row(1, 0), proof_row(2, 60, valid=False)]
        state = _paper_locked_live_gate_state(
            ResearchCoordinatorStub(),
            rows,
            symbol="BTCUSDT",
        )
        report = build_institutional_report(
            live_gate_state=state,
            verified_predictions_n=state["verified"],
            persist=False,
        )
        self.assertFalse(report.live_gate_explanation["unlocked"])
        self.assertEqual(report.live_gate_explanation["conditions"][0]["actual"], 1)
        self.assertEqual(report.readiness["verified_predictions_n"], 1)
        self.assertIn("live_gate locked", report.readiness["blockers"])

    def test_live_gate_query_boundary_filters_btc_before_limit(self):
        import asyncio
        from backend import main

        btc = [proof_row(i + 1, i * 60) for i in range(40)]
        eth = [proof_row(10_000 + i, 10_000 + i, symbol="ETHUSDT", outcome="LOSS") for i in range(510)]

        def evaluate(rows):
            boundary = _BoundedPredictionFetch(rows)
            with (
                patch.object(main, "_get_coordinator", return_value=_BoundaryGateCoordinator()),
                patch.object(supabase_client, "fetch_predictions", new=boundary),
            ):
                state = asyncio.run(main.portfolio_live_gate(symbol="BTC/USDT"))
            return state, boundary.calls

        solo, _ = evaluate(btc)
        mixed, calls = evaluate(btc + eth)
        for key in ("verified", "conditions", "unlocked", "trade_mode", "live_capital_locked"):
            self.assertEqual(solo[key], mixed[key], key)
        self.assertEqual(mixed["verified"], 40)
        self.assertEqual(calls, [{"limit": 500, "symbol": "BTCUSDT"}])

    def test_research_report_query_boundary_filters_btc_before_limit(self):
        import asyncio
        from backend import main, research

        btc = [proof_row(i + 1, i * 60) for i in range(40)]
        eth = [proof_row(10_000 + i, 10_000 + i, symbol="ETHUSDT", outcome="LOSS") for i in range(510)]

        class RequestStub:
            async def json(self):
                return {
                    "symbol": "BTC-USDT",
                    "run_walk_forward": False,
                    "run_monte_carlo": False,
                    "run_statistical": False,
                    "run_stress": False,
                    "run_capacity": False,
                }

        class ResearchCoordinatorStub:
            predictions = [btc[0]]
            y = np.asarray([], dtype=float)
            confidences = np.asarray([], dtype=float)

            def _build_feature_matrix(self):
                return np.empty((0, 0)), self.y, self.confidences, []

            def get_drift_stats(self):
                return {}

            def get_explainer(self):
                return None

        captured = {}

        class ReportStub:
            def to_dict(self):
                return {"live_gate_state": captured["live_gate_state"]}

        def build_report_stub(**kwargs):
            captured.update(kwargs)
            return ReportStub()

        def evaluate(rows):
            boundary = _BoundedPredictionFetch(rows)
            with (
                patch.object(main, "_research_coord", ResearchCoordinatorStub()),
                patch.object(main, "_get_coordinator", return_value=_BoundaryGateCoordinator()),
                patch.object(supabase_client, "fetch_predictions", new=boundary),
                patch.object(research, "build_institutional_report", side_effect=build_report_stub),
            ):
                result = asyncio.run(main.research_report(RequestStub()))
            return result["live_gate_state"], boundary.calls

        solo, _ = evaluate(btc)
        mixed, calls = evaluate(btc + eth)
        for key in ("verified", "conditions", "unlocked", "trade_mode", "live_capital_locked"):
            self.assertEqual(solo[key], mixed[key], key)
        self.assertEqual(mixed["verified"], 40)
        self.assertEqual(calls, [{"limit": 500, "symbol": "BTCUSDT"}])


class TestDiscoveryIntegrityTests(unittest.TestCase):
    def test_repository_discovery_has_unique_test_ids(self):
        tests_dir = Path(__file__).resolve().parent
        suite = unittest.defaultTestLoader.discover(str(tests_dir))

        def iter_cases(node):
            for item in node:
                if isinstance(item, unittest.TestSuite):
                    yield from iter_cases(item)
                else:
                    yield item

        ids = [case.id() for case in iter_cases(suite)]
        duplicates = {name: count for name, count in Counter(ids).items() if count > 1}
        self.assertEqual(duplicates, {})


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

    def test_sufficient_timestamped_sample_uses_temporal_purged_oos(self):
        y = np.asarray([0, 1] * 60, dtype=float)
        p = np.asarray([0.35, 0.65] * 60, dtype=float)
        timestamps = [(BASE + timedelta(hours=i)).isoformat() for i in range(len(y))]
        with tempfile.TemporaryDirectory() as tmp:
            report = calibration.fit_and_evaluate(y, p, method="platt", calibrators_dir=tmp, reliability_dir=tmp, timestamps=timestamps)
        self.assertEqual(report.evaluation_status, "OOS_TEMPORAL_HOLDOUT")
        self.assertEqual(report.evaluation_scope, "CHRONOLOGICAL_TRAIN_PURGE_TEST")
        self.assertTrue(report.authority_eligible)
        self.assertTrue(report.extra["chronology_verified"])
        self.assertFalse(report.extra["input_reordered_by_timestamp"])
        self.assertGreater(report.extra["n_purged"], 0)
        self.assertGreater(report.extra["n_oos"], 0)

    def test_sufficient_sample_without_timestamps_fails_closed(self):
        y = np.asarray([0, 1] * 60, dtype=float)
        p = np.asarray([0.35, 0.65] * 60, dtype=float)
        with tempfile.TemporaryDirectory() as tmp:
            report = calibration.fit_and_evaluate(y, p, method="platt", calibrators_dir=tmp, reliability_dir=tmp)
        self.assertEqual(report.evaluation_status, "INSUFFICIENT_OOS_EVIDENCE")
        self.assertEqual(report.evaluation_scope, "UNVERIFIED_TEMPORAL_ORDER")
        self.assertFalse(report.authority_eligible)
        self.assertFalse(report.extra["chronology_verified"])
        self.assertEqual(report.extra["chronology_reason"], "TIMESTAMPS_MISSING")

    def test_descending_input_is_reordered_by_verified_timestamps(self):
        y = np.asarray([0, 1] * 60, dtype=float)
        p = np.asarray([0.35, 0.65] * 60, dtype=float)
        timestamps = [(BASE + timedelta(hours=i)).isoformat() for i in range(len(y))]
        with tempfile.TemporaryDirectory() as tmp:
            ordered = calibration.fit_and_evaluate(y, p, method="platt", calibrators_dir=tmp, reliability_dir=tmp, timestamps=timestamps)
            descending = calibration.fit_and_evaluate(y[::-1], p[::-1], method="platt", calibrators_dir=tmp, reliability_dir=tmp, timestamps=list(reversed(timestamps)))
        self.assertTrue(descending.authority_eligible)
        self.assertTrue(descending.extra["chronology_verified"])
        self.assertTrue(descending.extra["input_reordered_by_timestamp"])
        self.assertAlmostEqual(descending.brier_after, ordered.brier_after)
        self.assertAlmostEqual(descending.ece_after, ordered.ece_after)


class CoordinatorCalibrationTruthTests(unittest.TestCase):
    @staticmethod
    def _records(n: int):
        return [proof_row(i + 1, i * 60, outcome="WIN" if i % 2 else "LOSS", confidence=0.65) for i in range(n)]

    def test_n60_insufficient_oos_propagates_without_calibration_error_or_gauge(self):
        coord = ResearchCoordinator(config={"calibration_methods": ["platt"], "explainer_prefer_shap": False})
        spy = _RegistrySpy()
        coord._registry = spy
        coord.load_predictions_from_records(self._records(60))
        report = coord.run_full_pass(persist=False)
        self.assertEqual(len(report.calibration_reports), 1)
        cal = report.calibration_reports[0]
        self.assertEqual(cal["evaluation_status"], "INSUFFICIENT_OOS_EVIDENCE")
        self.assertFalse(cal["authority_eligible"])
        self.assertIsNone(cal["ece_after"])
        self.assertFalse(any(err.startswith("calibration_") for err in report.errors))
        self.assertFalse(any(name == "senecio_last_calibration_ece" for name, _, _ in spy.gauges))

    def test_descending_db_records_get_causal_timestamp_order_before_oos_authority(self):
        coord = ResearchCoordinator(config={"calibration_methods": ["platt"], "explainer_prefer_shap": False})
        spy = _RegistrySpy()
        coord._registry = spy
        coord.load_predictions_from_records(list(reversed(self._records(120))))
        report = coord.run_full_pass(persist=False)
        self.assertEqual(len(report.calibration_reports), 1)
        cal = report.calibration_reports[0]
        self.assertEqual(cal["evaluation_status"], "OOS_TEMPORAL_HOLDOUT")
        self.assertTrue(cal["authority_eligible"])
        self.assertTrue(cal["extra"]["chronology_verified"])
        self.assertTrue(cal["extra"]["input_reordered_by_timestamp"])
        self.assertFalse(any(err.startswith("calibration_") for err in report.errors))
        self.assertTrue(any(name == "senecio_last_calibration_ece" for name, _, _ in spy.gauges))


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
