from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from polymarket.mega_research import (
    EvidenceManifest,
    ExperimentConstitution,
    FAILURE_FIXTURES,
    FailureInjectionHarness,
    MegaResearchFusion,
    ResearchLedger,
    ResearchPoint,
    StatisticalValidationEngine,
    SystemTruth,
    ValidationPolicy,
    visual_contract_smoke,
)
from polymarket.mega_research import LeakageGate
from polymarket.signal_lab.contracts import RawEvent
from polymarket.signal_lab.store import PointInTimeStore


def _constitution(experiment_id="EXP-020-001", supersedes=None):
    return ExperimentConstitution(
        experiment_id=experiment_id,
        created_at="2026-08-07T10:00:00Z",
        hypothesis="F01/F03 improve OOS Brier versus decision-time midpoint",
        mechanism="visible book imbalance and microprice divergence",
        feature_ids=("F01", "F03"),
        feature_versions={"F01": "1.0.0", "F03": "1.0.0"},
        baseline="MID_PRICE_AT_DECISION_TIME",
        prediction_target="binary_market_resolution",
        prediction_horizon="5m",
        market_population="BTC_UP_DOWN_5M",
        inclusion_rules=("public_data_complete",),
        exclusion_rules=("stale_book",),
        primary_metric="brier_improvement",
        secondary_metrics=("log_loss", "ece", "rank_ic"),
        minimum_sample_rule=80,
        train_window="walk_forward_train",
        validation_window="walk_forward_validation",
        holdout_rule="single_touch_out_of_time",
        purge_window="300s",
        embargo_window="300s",
        cost_model_version="senex-visible-spread-cost-v1",
        paper_fill_model_version="senex-paper-fill-v1",
        random_seed=20020,
        dataset_manifest_hash="d"*64,
        code_sha="7"*40,
        feature_set_hash="f"*64,
        success_criterion="OOS improvement with CI strictly above zero",
        failure_criterion="leakage or no OOS support",
        status="PREREGISTERED",
        supersedes_experiment_id=supersedes,
    )


def _point(i: int, *, prob=None, baseline=0.5, outcome=None, regime=None, offset_sec=0):
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    when = start + timedelta(minutes=5*i)
    y = (i % 2) if outcome is None else outcome
    p = (0.85 if y else 0.15) if prob is None else prob
    return ResearchPoint(
        market_id=f"m{i:03d}", regime=regime or ("CALM" if i % 2 else "FAST"),
        day=when.date().isoformat(), decision_time=when.isoformat(),
        input_event_max_time=(when + timedelta(seconds=offset_sec)).isoformat(),
        probability=p, baseline_probability=baseline, outcome=y,
        theoretical_edge=abs(p-baseline), executable_edge=abs(p-baseline)-0.01,
        filled=(i % 5 != 0), horizon_error=0.01,
        resolution_time=(when + timedelta(minutes=5)).isoformat(),
        cluster_id=f"{when.date().isoformat()}:{i//5}",
    )


def test_parent_composition_authority_is_preserve_first():
    authority = MegaResearchFusion.authority_map()
    invariants = MegaResearchFusion.authority_invariants()
    assert authority["FEATURE_ENGINE"] == "SIGNAL_LAB_018_FEATURE_ENGINE_F01_F15"
    assert authority["PAPER_FILL_ENGINE"] == "RESEARCH_PACK_019_AGGREGATE_L2_FILL_MODEL"
    assert invariants["ONE_AUTHORITATIVE_RAW_WRITER_MAX"] == 1
    assert invariants["REPLAY_V1_REPLACED"] is False
    assert invariants["DUPLICATE_SIGNAL_AUTHORITY"] is False
    assert invariants["EXTERNAL_LIVE_ADAPTERS_ENABLED"] is False


def test_experiment_constitution_is_complete_and_pinned():
    c = _constitution()
    assert c.to_dict()["status"] == "PREREGISTERED"
    assert len(c.immutable_hash) == 64


def test_experiment_requires_pinned_manifest():
    with pytest.raises(ValueError, match="DATASET_MANIFEST_HASH_MUST_BE_PINNED"):
        replace(_constitution(), dataset_manifest_hash="UNKNOWN")


def test_append_only_prereg_result_and_multiple_testing(tmp_path):
    ledger = ResearchLedger(tmp_path/"ledger.jsonl")
    ledger.preregister(_constitution())
    ledger.start_evaluation("EXP-020-001")
    ledger.record_multiple_testing(
        experiment_id="EXP-020-001", total_hypotheses_tested=4,
        primary_hypothesis_declared_before_run=True,
        p_values_if_used=(0.01, 0.2, 0.4, 0.8), policy="BH_FDR",
        holdout_touched_count=1, experiment_status="INCONCLUSIVE",
    )
    ledger.record_result(
        experiment_id="EXP-020-001", status="INCONCLUSIVE", metrics={"brier": 0.24},
        leakage_gate="PASS", evidence_manifest_hash="e"*64, reason="CI_INCLUDES_ZERO",
    )
    assert ledger.verify()
    assert ResearchLedger(tmp_path/"ledger.jsonl").verify()


def test_outcome_affecting_amendment_after_start_is_blocked():
    ledger = ResearchLedger(); ledger.preregister(_constitution()); ledger.start_evaluation("EXP-020-001")
    with pytest.raises(ValueError, match="IMMUTABLE"):
        ledger.amend_before_evaluation("EXP-020-001", _constitution("EXP-020-002", supersedes="EXP-020-001"))


def test_contradiction_never_disappears_on_resolution():
    ledger = ResearchLedger()
    ledger.open_contradiction(contradiction_id="C-1", claim="source says direct provenance",
                              source_evidence=("a",), contradicting_evidence=("b",), severity="HIGH",
                              affected_experiments=("EXP-020-001",))
    ledger.resolve_contradiction("C-1", "proven indirect")
    assert len(ledger.records) == 2
    assert ledger.records[0]["payload"]["RESOLUTION_STATUS"] == "OPEN"
    assert ledger.records[1]["payload"]["RESOLUTION_STATUS"] == "RESOLVED"


def test_point_in_time_future_input_fails_closed():
    points = [_point(i) for i in range(5)]; points[2] = _point(2, offset_sec=1)
    report = LeakageGate.audit(points)
    assert report["future_event_injection"] == "FAIL"
    assert report["negative_lag_canary"] == "FAIL"


def test_future_raw_event_does_not_change_past_state():
    store = PointInTimeStore(); t0 = "2026-08-01T00:00:00Z"
    store.ingest(RawEvent.build(event_id="e1", event_type="BOOK_SNAPSHOT", market_id="m1", token_id="yes",
        event_time=t0, received_time=t0, sequence_or_source_cursor=1, source="fixture",
        payload={"bids":[[0.49,10]],"asks":[[0.51,10]]}))
    before = store.state_hash(t0, "m1")
    store.ingest(RawEvent.build(event_id="e2", event_type="BOOK_SNAPSHOT", market_id="m1", token_id="yes",
        event_time="2026-08-01T00:05:00Z", received_time="2026-08-01T00:05:00Z",
        sequence_or_source_cursor=2, source="fixture", payload={"bids":[[0.9,10]],"asks":[[0.91,10]]}))
    assert store.state_hash(t0, "m1") == before


def test_raw_negative_lag_canary_rejected():
    with pytest.raises(ValueError, match="NEGATIVE_LAG_CANARY_DETECTED"):
        RawEvent.build(event_id="e1", event_type="TRADE", market_id="m1", token_id="yes",
            event_time="2026-08-01T00:00:02Z", received_time="2026-08-01T00:00:01Z",
            sequence_or_source_cursor=1, source="fixture", payload={"price":0.5})


def test_strong_oos_fixture_can_pass_without_production_claim():
    engine = StatisticalValidationEngine(ValidationPolicy(minimum_sample=80, minimum_regimes=2,
        bootstrap_iterations=100, permutation_iterations=100, random_seed=20020))
    report = engine.evaluate([_point(i) for i in range(100)], holdout_touched_count=1,
                             hypotheses_tested=1, primary_declared_before_run=True)
    assert report["status"] == "PASS"
    assert report["VALIDATED_EDGE"] is False
    assert report["deterministic_replay"] == "PASS"
    assert report["bootstrap"]["ci_low"] > 0
    assert set(("rank_ic","directional_accuracy","horizon_error")) <= set(report["metrics"])


def test_ci_including_zero_never_promotes():
    engine = StatisticalValidationEngine(ValidationPolicy(bootstrap_iterations=80, permutation_iterations=80))
    report = engine.evaluate([_point(i, prob=0.5) for i in range(100)], holdout_touched_count=1,
                             hypotheses_tested=3, primary_declared_before_run=True)
    assert report["status"] == "INCONCLUSIVE"
    assert report["VALIDATED_EDGE"] is False


def test_insufficient_sample_is_explicit_not_pass():
    engine = StatisticalValidationEngine(ValidationPolicy(minimum_sample=80, bootstrap_iterations=20, permutation_iterations=20))
    report = engine.evaluate([_point(i) for i in range(20)], holdout_touched_count=1,
                             hypotheses_tested=1, primary_declared_before_run=True)
    assert report["status"] == "INSUFFICIENT_SAMPLE"


def test_one_regime_is_inconclusive():
    engine = StatisticalValidationEngine(ValidationPolicy(bootstrap_iterations=40, permutation_iterations=40))
    report = engine.evaluate([_point(i, regime="ONLY") for i in range(100)], holdout_touched_count=1,
                             hypotheses_tested=1, primary_declared_before_run=True)
    assert report["status"] == "INCONCLUSIVE"


def test_holdout_retouch_invalidates_result():
    engine = StatisticalValidationEngine(ValidationPolicy(minimum_sample=10, bootstrap_iterations=20, permutation_iterations=20))
    report = engine.evaluate([_point(i) for i in range(20)], holdout_touched_count=2,
                             hypotheses_tested=1, primary_declared_before_run=True)
    assert report["status"] == "INVALID"


def test_walk_forward_has_purge_and_embargo():
    reports = StatisticalValidationEngine.purged_embargo_walk_forward(
        [_point(i) for i in range(40)], folds=3, purge_seconds=300, embargo_seconds=300)
    assert reports
    assert all(row["purge_seconds"] == 300 and row["embargo_seconds"] == 300 for row in reports)


def test_bh_multiple_testing_policy_is_deterministic():
    out = StatisticalValidationEngine.benjamini_hochberg([0.001, 0.02, 0.2, 0.9], alpha=0.05)
    assert out["policy"] == "BH_FDR"
    assert out["rejected"] == [0, 1]


def test_paper_execution_truth_exposes_theoretical_and_executable_edge():
    truth = MegaResearchFusion(PointInTimeStore()).paper_execution_truth(
        side="BUY", quantity=8, limit_price=0.55, book_age_ms=100,
        levels=((0.51, 5), (0.52, 2)), baseline_probability=0.50, research_probability=0.60,
        reference_mid_after_window=0.50)
    assert truth["theoretical_signal_edge"] == pytest.approx(0.10)
    assert truth["fill"]["partial_fill"] is True
    assert truth["fill"]["queue_position_exact"] is False
    assert truth["paper_executable_edge"] is not None


def test_stale_book_rejected_through_019_fill_model():
    truth = MegaResearchFusion(PointInTimeStore()).paper_execution_truth(
        side="BUY", quantity=1, limit_price=0.55, book_age_ms=9999,
        levels=((0.51,5),), baseline_probability=0.50, research_probability=0.60)
    assert truth["status"] == "NO_FILL"
    assert truth["fill"]["status"] == "NO_FILL_STALE_BOOK"


def test_unknown_probabilities_remain_not_available():
    truth = MegaResearchFusion(PointInTimeStore()).paper_execution_truth(
        side="BUY", quantity=1, limit_price=0.55, book_age_ms=1,
        levels=((0.51,5),), baseline_probability=None, research_probability=None)
    assert truth["status"] == "NOT_AVAILABLE"
    assert truth["theoretical_signal_edge"] is None
    assert truth["paper_executable_edge"] is None


def test_all_failure_fixtures_degrade_or_block_health():
    base = SystemTruth(raw_chain_hash="a"*64, raw_chain_tip=10, replay_status="PASS")
    for fixture in FAILURE_FIXTURES:
        health = FailureInjectionHarness.health(FailureInjectionHarness.inject(base, fixture))
        assert health["status"] in {"DEGRADED", "BLOCKED"}


def test_unknown_health_fact_is_not_green():
    assert FailureInjectionHarness.health(SystemTruth(last_event_age_ms=None))["status"] == "UNKNOWN"


def test_terminal_v2_has_six_read_only_responsive_surfaces():
    assert visual_contract_smoke() == {
        "six_surfaces": True, "research_only_label": True, "forbidden_controls_absent": True,
        "responsive_viewport": True, "mobile_breakpoint": True,
    }


def test_evidence_manifest_is_hash_pinned():
    manifest = EvidenceManifest(experiment_id="EXP-020-001", raw_data_hash="a"*64,
        dataset_manifest_hash="b"*64, feature_set_hash="c"*64, code_sha="7"*40,
        paper_fill_model_version="senex-paper-fill-v1", cost_model_version="senex-visible-spread-cost-v1",
        point_count=100, decision_start="2026-08-01T00:00:00Z", decision_end="2026-08-02T00:00:00Z",
        source_ids=("POLYMARKET_GAMMA_PUBLIC", "POLYMARKET_CLOB_PUBLIC"))
    assert len(manifest.manifest_hash) == 64


def test_read_only_service_composes_018_state_without_mutation():
    from polymarket.mega_research.api import MegaResearchService
    service = MegaResearchService(); projection = service.projection()
    assert list(projection["surfaces"]) == [
        "SYSTEM_TRUTH", "MARKET_RADAR", "MICROSTRUCTURE_ANOMALIES",
        "FAIR_VALUE_AND_SIGNAL_DESK", "PAPER_EXECUTION_TRUTH", "EVIDENCE_AND_GOVERNANCE"]
    assert projection["surfaces"]["SYSTEM_TRUTH"]["PAPER_ONLY"] is True
    assert projection["controls"]["capital_actions"] is False
    assert projection["controls"]["authenticated_execution"] is False
