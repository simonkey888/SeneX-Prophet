from datetime import datetime, timezone

from senecio_polymarket.backend.engine_contracts import h011_contract, oracle_contract

NOW = datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc)


def test_oracle_abstention_is_explicit_and_hash_is_stable():
    payload = {
        "shadow_action": "FLAT", "source_confidence": 0.61,
        "gate_status": "REJECT", "source_ts": "2026-07-10T00:00:00Z",
        "reasons": ["EDGE_NOT_DEMONSTRATED_AT_95PCT"],
    }
    first = oracle_contract(payload)
    second = oracle_contract(payload)
    assert first.direction == "FLAT"
    assert first.abstain_reason == "EDGE_NOT_DEMONSTRATED_AT_95PCT"
    assert first.evidence_hash == second.evidence_hash


def test_h011_without_btc_is_non_executable():
    state = {
        "window_s": 300, "scan_id": NOW.isoformat(),
        "invariants": {"summary": {"unknown": 31, "fail": 0}},
        "source_health": {"clob": {"level": "UNKNOWN"}},
    }
    evidence = h011_contract(state, [{"question": "Will France win?"}], now=NOW)
    assert evidence.executable is False
    assert evidence.side == "FLAT"
    assert evidence.invariants_verified is False


def test_h011_btc_contract_preserves_identity_and_side():
    state = {
        "window_s": 300, "scan_id": NOW.isoformat(),
        "invariants": {"summary": {"unknown": 0, "fail": 0}},
        "source_health": {"clob": {"level": "HEALTHY"}},
        "aggregate_metrics": {"discovery": {
            "discovery_complete": True,
            "discovery_replay_verified": True,
        }},
    }
    evidence = h011_contract(state, [{
        "question": "Bitcoin Up or Down", "condition_id": "0xabc",
        "side": "UP", "net_edge": 0.012, "equal_fillable_quantity": 5,
        "record_status": "SHADOW_EXECUTABLE",
    }], now=NOW)
    assert evidence.executable is True
    assert evidence.side == "LONG"
    assert evidence.identity_verified is True
