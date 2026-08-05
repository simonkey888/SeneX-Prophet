from __future__ import annotations

import json
from pathlib import Path

from polymarket.monitoring.site import build_monitoring_model, render_monitoring_site


def sources():
    manifest = {"code_sha":"a"*40,"tree_sha":"b"*40,"config_sha":"c"*64,"experiment_id":"exp","execution_model_version":"seq-v1","fee_model_version":"fee-v1","acceptance_result":"PASS","safety_invariants":{"paper_only":True,"orders_enabled":False,"live_capital_locked":True},"gates":[{"gate_id":"GATE_1","status":"PASS"}],"known_unknowns":["PROFITABILITY"]}
    summary = {"trial_id":"trial","run_state":"FIXTURE_CONTRACT_VALIDATION_NOT_CURRENT_RUNTIME_STATE","gamma_discovered_windows":3,"valid_order_book_windows":2,"expected_closed_no_book_windows":1,"unexpected_source_failures":0,"decisions_total":2,"abstention_counts":{"INSUFFICIENT_EDGE":1},"order_intents":2,"fills":1,"fees_applied_usd":0.123,"raw_chain_verified":True,"replay_verified":True}
    source = {"last_verified_observation_utc":"2026-08-04T12:00:00Z","source_timestamp_utc":"2026-08-04T11:59:59Z","received_timestamp_utc":"2026-08-04T12:00:00Z","source_age_ms":1000,"pair_skew_ms":50,"result":"PASS"}
    fee = {"fee_enabled":True,"raw_schedule_hash":"d"*64}
    sequential = {"evidence_class":"FIXTURE_CONTRACT_EVIDENCE","scenarios":[{"scenario":"BOTH_LEGS_FILL","completion_status":"BOTH_LEGS_FILL"},{"scenario":"EMPTY","completion_status":"SECOND_LEG_FAILED_AFTER_FIRST_FILL"}]}
    runner = {"evidence_class":"OBSERVED_RUNNER_INTEGRATION_EVIDENCE","result":"PASS","records":[{"first_and_second_snapshot_hashes_distinct":True}]}
    settlement = {"positions":[{"state":"RESOLUTION_PENDING"}],"realized_settled_pnl":None,"marked_unsettled_pnl":None,"equity_known":False,"unknown_valuation_positions":1,"replay_result":"PASS"}
    risk = {"result":"PASS","authoritative_paper_components":["polymarket/paper/risk.py"],"duplicate_authoritative_components":[]}
    return manifest,summary,source,fee,sequential,runner,settlement,risk


def build_model():
    values=sources()
    return build_monitoring_model(execution_manifest=values[0],trial_summary=values[1],source_time_audit=values[2],fee_report=values[3],sequential_report=values[4],runner_integration_report=values[5],settlement_report=values[6],risk_authority_map=values[7],bundle_content_digest="e"*64,github_artifact_zip_digest="UNAVAILABLE_AT_BUILD_TIME",generated_at_utc="2026-08-04T12:00:00Z")


def test_projection_copies_artifact_values_and_preserves_unknowns(tmp_path: Path):
    model = build_model()
    assert model["data_quality"]["source_age_ms"] == 1000
    assert model["execution"]["fees_applied_usd"] == 0.123
    assert model["execution"]["fill_label"] == "SIMULATED_FIXTURE"
    assert model["runner_integration"]["result"] == "PASS"
    assert model["provenance"]["github_artifact_zip_digest"] == "UNAVAILABLE_AT_BUILD_TIME"
    assert model["portfolio_and_settlement"]["realized_settled_pnl"] == "PENDING"
    assert model["portfolio_and_settlement"]["marked_unsettled_pnl"] == "UNKNOWN"
    result = render_monitoring_site(model=model, output_dir=tmp_path)
    assert result["result"] == "PASS"
    data = json.loads((tmp_path/"data.json").read_text())
    assert data == model


def test_site_has_safety_and_no_mutating_controls(tmp_path: Path):
    model=build_model()
    render_monitoring_site(model=model, output_dir=tmp_path)
    page=(tmp_path/"index.html").read_text()
    assert "PAPER ONLY" in page
    assert "FIXTURE + RUNNER INTEGRATION HARNESS" in page
    assert "NOT CURRENT RUNTIME STATE" in page
    assert "PROFITABILITY_NOT_ESTABLISHED" in page
    assert "SIMULATED_FIXTURE" in page
    assert "UNAVAILABLE_AT_BUILD_TIME" in page
    assert "<button" not in page and "<form" not in page and "wallet" not in page.lower()
    assert (tmp_path/"rendered_monitoring_preview.svg").exists()
