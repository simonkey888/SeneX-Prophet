from datetime import datetime, timezone

from senecio_polymarket.backend.arbiter_shadow_v3 import arbitrate

NOW = datetime.now(timezone.utc).isoformat()


def _state(records=None, unknown=0, healthy=True):
    level = "HEALTHY" if healthy else "UNKNOWN"
    return {
        "scan_id": NOW, "scan_status": "COMPLETE_VALIDATED", "window_s": 300,
        "market_records": records or [],
        "invariants": {"summary": {"unknown": unknown, "fail": 0}},
        "source_health": {"clob": {"level": level}},
        "aggregate_metrics": {"discovery": {
            "discovery_complete": True,
            "discovery_replay_verified": True,
        }},
    }


def test_real_current_scope_mismatch_fails_closed():
    oracle = {"gate_status": "PASS", "shadow_action": "LONG", "authoritative_score_pct": 61.0}
    state = _state([{"question": "Will France win the World Cup?"}], unknown=31, healthy=False)
    result = arbitrate(oracle, state, [])
    assert result["decision"] == "UNKNOWN"
    assert result["action"] == "FLAT"
    assert "H011_MARKET_SCOPE_MISMATCH_NO_BTC" in result["reasons"]


def test_apparent_agreement_cannot_bridge_1h_oracle_to_5m_market():
    oracle = {"gate_status": "PASS", "shadow_action": "SHORT", "authoritative_score_pct": 61.0}
    record = {
        "question": "Bitcoin Up or Down 5 minute", "side": "DOWN",
        "condition_id": "0xabc", "record_status": "SHADOW_EXECUTABLE",
        "net_edge": 0.01, "equal_fillable_quantity": 5,
    }
    result = arbitrate(oracle, _state([record]), [record])
    assert result["decision"] == "UNKNOWN"
    assert result["action"] == "FLAT"
    assert "HORIZON_MISMATCH" in result["reasons"]


def test_unproved_operation_never_becomes_a_conflict_score():
    oracle = {"gate_status": "PASS", "shadow_action": "LONG", "authoritative_score_pct": 61.0}
    record = {"question": "BTC Up or Down 5 minute", "side": "DOWN"}
    result = arbitrate(oracle, _state([record]), [record])
    assert result["decision"] == "UNKNOWN"
    assert result["action"] == "FLAT"
