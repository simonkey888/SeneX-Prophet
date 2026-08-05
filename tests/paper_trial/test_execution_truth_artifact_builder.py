from __future__ import annotations
import json, subprocess, sys
from pathlib import Path

REQUIRED={"execution_truth_manifest.json","source_time_audit.json","fee_model_conformance.json","sequential_leg_scenarios.json","settlement_replay_report.json","paper_risk_authority_map.json","monitoring_truth_report.json","regression_summary.json","SHA256SUMS"}

def test_builder_produces_hashed_truth_bundle(tmp_path: Path):
    subprocess.run([sys.executable,"tools/build_execution_truth_artifacts.py","--output-dir",str(tmp_path),"--code-sha","a"*40,"--tree-sha","b"*40,"--generated-at","2026-08-04T12:00:00Z","--regression-count","1"],check=True)
    assert REQUIRED <= {p.name for p in tmp_path.iterdir()}
    subprocess.run(["sha256sum","-c","SHA256SUMS"],cwd=tmp_path,check=True,capture_output=True,text=True)
    manifest=json.loads((tmp_path/"execution_truth_manifest.json").read_text())
    assert manifest["acceptance_result"]=="PASS"
    assert all(g["status"]=="PASS" for g in manifest["gates"])
    fee=json.loads((tmp_path/"fee_model_conformance.json").read_text())
    assert fee["official_source_conflict_detected"] is True
    assert fee["fail_closed_verified"] is True
    seq=json.loads((tmp_path/"sequential_leg_scenarios.json").read_text())
    assert len(seq["scenarios"])==8 and seq["result"]=="PASS"
    page=(tmp_path/"monitoring_site"/"index.html").read_text()
    assert "PROFITABILITY_NOT_ESTABLISHED" in page and "<button" not in page
