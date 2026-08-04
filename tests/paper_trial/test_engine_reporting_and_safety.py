from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from paper.broker import PublicOrderBook, SimulatedBroker
from paper.engine import PaperEngine
from paper.portfolio import PaperPortfolio
from paper.report import REQUIRED_ARTIFACTS, TrialArtifactWriter, verify_artifact_bundle
from paper.risk import PaperRiskConfig, PaperRiskEngine
from paper.trial_runner import TrialConfig, run_trial

TS = "2026-08-04T12:00:00Z"
ROOT = Path(__file__).resolve().parents[2]


def fixture_engine():
    return PaperEngine(
        broker=SimulatedBroker(slippage_bps_floor=0),
        risk=PaperRiskEngine(PaperRiskConfig()),
        portfolio=PaperPortfolio(starting_equity_usd=10000),
    )


def books():
    return {
        token: PublicOrderBook.from_payload(
            market_id="m", token_id=token, timestamp_utc=TS,
            source_evidence_hash=hashlib.sha256(token.encode()).hexdigest(),
            payload={"asset_id": token, "bids": [{"price": "0.45", "size": "10"}], "asks": [{"price": "0.46", "size": "10"}]},
        )
        for token in ("yes", "no")
    }


def executable_record():
    return {
        "market_id": "m", "condition_id": "m", "token_ids": ["yes", "no"],
        "shadow_execution": {"status": "SHADOW_EXECUTABLE", "net_edge": 0.08},
        "evidence_verified": True, "raw_chain_verified": True,
        "replay_verified": True, "regime_known": True,
    }


def test_simulated_execution_integration_records_two_equal_legs():
    engine = fixture_engine()
    result = engine.process_h011_record(
        record=executable_record(), books=books(), outcomes={"yes": "UP", "no": "DOWN"},
        timestamp_utc=TS, code_sha="a" * 40, config_sha="b" * 64,
        requested_shares=2, max_notional_per_leg_usd=50,
    )
    assert result.risk_decision and result.risk_decision.allowed
    assert len(result.fills) == 2
    assert result.fills[0].filled_shares == result.fills[1].filled_shares
    assert engine.portfolio.cash_usd < 10000


def test_no_trade_records_explicit_abstention():
    record = executable_record()
    record["shadow_execution"] = {"status": "REJECTED", "net_edge": -0.01}
    result = fixture_engine().process_h011_record(
        record=record, books=books(), outcomes={"yes": "UP", "no": "DOWN"},
        timestamp_utc=TS, code_sha="a" * 40, config_sha="b" * 64,
        requested_shares=2, max_notional_per_leg_usd=50,
    )
    assert result.fills == []
    assert "INSUFFICIENT_EDGE" in result.abstention_reasons


def test_fixture_trial_writes_and_hashes_all_artifacts(tmp_path: Path):
    result = run_trial(
        output_dir=tmp_path,
        config=TrialConfig(duration_minutes=1, minimum_windows=1, poll_seconds=1),
        fixture=True,
    )
    assert result["summary"]["fills"] == 2
    assert result["summary"]["replay_result"] == "PASS"
    assert set(REQUIRED_ARTIFACTS).issubset(path.name for path in tmp_path.iterdir())
    assert verify_artifact_bundle(tmp_path)


def test_zero_mutation_of_authoritative_raw_evidence(tmp_path: Path):
    raw = tmp_path / "raw_chain_v1"
    raw.mkdir()
    evidence = raw / "manifest_000000.json"
    evidence.write_text('{"immutable":true}\n')
    before = hashlib.sha256(evidence.read_bytes()).hexdigest()
    run_trial(
        output_dir=tmp_path / "paper",
        config=TrialConfig(duration_minutes=1, minimum_windows=1, poll_seconds=1),
        fixture=True,
    )
    assert hashlib.sha256(evidence.read_bytes()).hexdigest() == before


def test_paper_package_has_no_wallet_private_key_or_authenticated_client_imports():
    forbidden_import_parts = {"web3", "eth_account", "py_clob_client", "wallet", "private_key"}
    for path in sorted((ROOT / "polymarket" / "paper").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        assert not any(part in name.lower() for name in imports for part in forbidden_import_parts), (path, imports)


def test_paper_runtime_has_no_real_order_calls_reachable():
    forbidden_calls = {"create_order", "place_order", "submit_order", "send_order", "cancel_order", "sign_order", "sign_transaction"}
    for path in sorted((ROOT / "polymarket" / "paper").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        observed = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    observed.add(func.id)
                elif isinstance(func, ast.Attribute):
                    observed.add(func.attr)
        assert observed.isdisjoint(forbidden_calls), (path, observed & forbidden_calls)


def test_artifact_tampering_is_detected(tmp_path: Path):
    run_trial(output_dir=tmp_path, config=TrialConfig(duration_minutes=1, minimum_windows=1), fixture=True)
    target = tmp_path / "trial_summary.json"
    target.write_text(target.read_text() + " ")
    try:
        verify_artifact_bundle(tmp_path)
    except ValueError as exc:
        assert "digest mismatch" in str(exc)
    else:
        raise AssertionError("tampering was not detected")
