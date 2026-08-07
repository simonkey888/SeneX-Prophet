from __future__ import annotations

from pathlib import Path

import pytest

from polymarket.signal_lab.contracts import RawEvent, parse_time
from polymarket.signal_lab.features import FeatureEngine
from polymarket.signal_lab.registry import ContradictionLedger, ExperimentRegistry
from polymarket.signal_lab.store import PointInTimeStore, RawAppendOnlyChain


def event(
    event_id: str,
    event_type: str,
    event_time: str,
    payload: dict,
    *,
    market: str = "m1",
    token: str | None = "yes",
    received_time: str | None = None,
) -> RawEvent:
    return RawEvent.build(
        event_id=event_id,
        event_type=event_type,
        market_id=market,
        token_id=token,
        event_time=event_time,
        received_time=received_time or event_time,
        sequence_or_source_cursor=event_id,
        source="POLYMARKET_CLOB_WEBSOCKET",
        payload=payload,
    )


def populated_store() -> PointInTimeStore:
    store = PointInTimeStore()
    store.ingest_many([
        event("meta", "MARKET_META", "2026-08-07T00:00:00Z", {
            "title": "BTC Up?", "category": "CRYPTO", "end_time": "2026-08-07T01:00:00Z",
            "outcome_prices": [0.49, 0.51],
        }),
        event("b1", "BOOK_SNAPSHOT", "2026-08-07T00:10:00Z", {
            "bids": [{"price": "0.48", "size": "100"}, {"price": "0.47", "size": "80"}],
            "asks": [{"price": "0.52", "size": "60"}, {"price": "0.53", "size": "90"}],
        }),
        event("t1", "TRADE", "2026-08-07T00:11:00Z", {"side": "BUY", "size": "10", "price": "0.52"}),
        event("b2", "BOOK_SNAPSHOT", "2026-08-07T00:12:00Z", {
            "bids": [{"price": "0.49", "size": "70"}, {"price": "0.48", "size": "50"}],
            "asks": [{"price": "0.51", "size": "50"}, {"price": "0.52", "size": "40"}],
        }),
        event("link", "RELATED_MARKET_LINK", "2026-08-07T00:12:30Z", {"peer_market_id": "m2", "peer_mid": 0.505}, token=None),
    ])
    return store


def test_future_event_injection_does_not_change_state_at_t():
    store = populated_store()
    cutoff = "2026-08-07T00:12:30Z"
    before = store.state_hash(cutoff, "m1")
    store.ingest(event("future", "TRADE", "2026-08-07T00:30:00Z", {"side": "SELL", "size": 999}))
    assert store.state_hash(cutoff, "m1") == before
    assert all(parse_time(item.event_time) <= parse_time(cutoff) for item in store.events_as_of(cutoff))


def test_negative_lag_canary_detected():
    with pytest.raises(ValueError, match="NEGATIVE_LAG_CANARY_DETECTED"):
        event(
            "bad", "TRADE", "2026-08-07T00:10:01Z", {"side": "BUY", "size": 1},
            received_time="2026-08-07T00:10:00Z",
        )


def test_resolution_data_not_available_before_resolution():
    store = populated_store()
    store.ingest(event("resolve", "RESOLUTION_METADATA", "2026-08-07T01:00:01Z", {"winner": "YES"}))
    earlier = store.events_as_of("2026-08-07T00:59:59Z", event_types={"RESOLUTION_METADATA"})
    later = store.events_as_of("2026-08-07T01:00:01Z", event_types={"RESOLUTION_METADATA"})
    assert earlier == []
    assert [item.event_id for item in later] == ["resolve"]


def test_raw_chain_hash_continuity_and_replay_determinism(tmp_path: Path):
    path = tmp_path / "signal.jsonl"
    chain = RawAppendOnlyChain(path)
    for item in populated_store().chain.entries:
        chain.append(item.event)
    assert chain.verify() is True
    replay_hash = chain.replay_hash()
    loaded = RawAppendOnlyChain(path)
    assert loaded.verify() is True
    assert loaded.tip_hash == chain.tip_hash
    assert loaded.replay_hash() == replay_hash


def test_feature_engine_f01_to_f15_is_point_in_time():
    store = populated_store()
    engine = FeatureEngine(store)
    values = engine.compute("m1", "2026-08-07T00:13:00Z")
    assert set(values) == {f"F{i:02d}" for i in range(1, 16)}
    assert values["F01"].value == pytest.approx((70 - 50) / 120)
    assert values["F04"].value == pytest.approx(0.02)
    assert values["F05"].value == pytest.approx(210)
    assert values["F08"].value == pytest.approx(1.0)
    assert values["F10"].value == pytest.approx(47 * 60)
    assert values["F13"].value == pytest.approx(0.5 - 0.505)
    assert values["F15"].value == pytest.approx(0.0)
    for value in values.values():
        if value.input_event_max_time:
            assert parse_time(value.input_event_max_time) <= parse_time(value.as_of_event_time)
    fair = engine.fair_value("m1", "2026-08-07T00:13:00Z")
    assert fair["status"] == "UNVALIDATED"
    assert fair["claim"] == "RESEARCH_ONLY_NOT_ALPHA"
    assert 0 <= fair["fair_value"] <= 1


def test_stale_book_flag_material_is_exposed():
    values = FeatureEngine(populated_store()).compute("m1", "2026-08-07T00:22:00Z")
    assert values["F09"].value == pytest.approx(600)


def test_experiment_registry_is_preregister_then_append_only(tmp_path: Path):
    path = tmp_path / "experiments.jsonl"
    registry = ExperimentRegistry(path)
    registered = registry.preregister(
        experiment_id="EXP-001",
        hypothesis="F01 improves calibration OOS",
        formula_or_feature_versions={"F01": "1.0.0"},
        dataset_hash="d" * 64,
        featureset_hash="f" * 64,
        pre_registered_metrics=["OOS_BRIER", "CALIBRATION_ERROR"],
        pre_registered_pass_fail_rule="PASS iff leakage gate PASS and OOS Brier < baseline Brier",
        start_time="2026-08-01T00:00:00Z",
        end_time="2026-08-02T00:00:00Z",
    )
    registry.record_result(
        experiment_id="EXP-001",
        oos_metrics={"OOS_BRIER": 0.20, "BASELINE_BRIER": 0.22},
        leakage_gate="PASS",
        result="PASS",
        reason="preregistered rule satisfied",
    )
    assert registry.verify() is True
    assert len(registry.records) == 2
    assert registry.records[1]["payload"]["supersedes"] == registered["record_hash"]
    with pytest.raises(ValueError, match="ALREADY"):
        registry.record_result(
            experiment_id="EXP-001", oos_metrics={}, leakage_gate="PASS", result="PASS", reason="rewrite"
        )
    assert ExperimentRegistry(path).verify() is True


def test_contradiction_ledger_is_append_only(tmp_path: Path):
    ledger = ContradictionLedger(tmp_path / "contradictions.jsonl")
    opened = ledger.open(
        contradiction_id="C-001",
        claim_a="public book exposes provenance",
        claim_b="public schema exposes only aggregated levels",
        evidence_refs=["official-clob-book-doc"],
    )
    resolved = ledger.resolve("C-001", "SENEX-MIRROR-001")
    assert opened["payload"]["status"] == "OPEN"
    assert resolved["payload"]["status"] == "RESOLVED"
    assert ledger.verify() is True
    assert len(ledger.records) == 2
