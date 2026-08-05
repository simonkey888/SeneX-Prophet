from __future__ import annotations

import json
from pathlib import Path

from polymarket.monitoring.site import build_monitoring_model, render_monitoring_site


def sources():
    manifest = {"code_sha":"a"*40,"tree_sha":"b"*40,"config_sha":"c"*64,"experiment_id":"exp","execution_model_version":"seq-v1","fee_model_version":"fee-v1","acceptance_result":"PASS","safety_invariants":{"paper_only":True,"orders_enabled":False,"live_capital_locked":True},"gates":[{"gate_id":"GATE_1","status":"PASS"}],"known_unknowns":["PROFITABILITY"]}
    summary = {"trial_id":"trial","gamma_discovered_windows":3,"valid_order_book_windows":2,"expected_closed_no_book_windows":1,"unexpected_source_failures":0,"decisions_total":2,"abstention_counts":{"INSUFFICIENT_EDGE":1},"order_intents":2,"fills":1,"fees_applied_usd":0.123,"raw_chain_verified":True,"replay_verified":True}
    source = {"last_verified_observation_utc":"2026-08-04T12:00:00Z","source_timestamp_utc":"2026-08-04T11:59:59Z","received_timestamp_utc":"2026-08-04T12:00:00Z","source_age_ms":1000,"pair_skew_ms":50,"result":"PASS"}
    fee = {"fee_enabled":True,"raw_schedule_hash":"d"*64}
    sequential = {"scenarios":[{"scenario":"BOTH_LEGS_FILL","completion_status":"BOTH_LEGS_FILL"},{"scenario":"EMPTY","completion_status":"SECOND_LEG_FAILED_AFTER_FIRST_FILL"}]}
    settlement = {"positions":[{"state":"RESOLUTION_PENDING"}],"realized_settled_pnl":None,"marked_unsettled_pnl":None,"equity_known":False,"unknown_valuation_positions":1,"replay_result":"PASS"}
    risk = {"result":"PASS","authoritative_paper_components":["polymarket/paper/risk.py"],"duplicate_authoritative_components":[]}
    return manifest,summary,source,fee,sequential,settlement,risk


def test_projection_copies_artifact_values_and_preserves_unknowns(tmp_path: Path):
    model = build_monitoring_model(execution_manifest=sources()[0],trial_summary=sources()[1],source_time_audit=sources()[2],fee_report=sources()[3],sequential_report=sources()[4],settlement_report=sources()[5],risk_authority_map=sources()[6],artifact_digest="e"*64,generated_at_utc="2026-08-04T12:00:00Z")
    assert model["data_quality"]["source_age_ms"] == 1000
    assert model["execution"]["fees_applied_usd"] == 0.123
    assert model["execution"]["fill_label"] == "SIMULATED"
    assert model["portfolio_and_settlement"]["realized_settled_pnl"] == "PENDING"
    assert model["portfolio_and_settlement"]["marked_unsettled_pnl"] == "UNKNOWN"
    result = render_monitoring_site(model=model, output_dir=tmp_path)
    assert result["result"] == "PASS"
    data = json.loads((tmp_path/"data.json").read_text())
    assert data == model


def test_site_has_safety_and_no_mutating_controls(tmp_path: Path):
    manifest,summary,source,fee,sequential,settlement,risk=sources()
    model=build_monitoring_model(execution_manifest=manifest,trial_summary=summary,source_time_audit=source,fee_report=fee,sequential_report=sequential,settlement_report=settlement,risk_authority_map=risk,artifact_digest="e"*64,generated_at_utc="2026-08-04T12:00:00Z")
    render_monitoring_site(model=model, output_dir=tmp_path)
    page=(tmp_path/"index.html").read_text()
    assert "PAPER ONLY" in page
    assert "PROFITABILITY_NOT_ESTABLISHED" in page
    assert "SIMULATED" in page
    assert "<button" not in page and "<form" not in page and "wallet" not in page.lower()
    assert (tmp_path/"rendered_monitoring_preview.svg").exists()
