from __future__ import annotations

import inspect

import pytest

from polymarket.research_pack.cross_market import DisabledCrossMarketAdapter
from polymarket.research_pack.external_schema import normalize_public_event, projection_hash
from polymarket.research_pack.fill_model import FillModelConfig, PaperFillRequest, simulate_paper_fill
from polymarket.research_pack.replay_v2 import ReplayConfig, deterministic_replay, same_input_same_output_hash
from polymarket.research_pack.terminal import data_quality_badges, microstructure_terminal_projection, visual_smoke_fragment
from polymarket.signal_lab.contracts import RawEvent


def _event(event_id: str, event_time: str, cursor: int, event_type: str = "BOOK_SNAPSHOT", payload=None) -> RawEvent:
    return RawEvent.build(
        event_id=event_id,
        event_type=event_type,
        market_id="m1",
        token_id="yes",
        event_time=event_time,
        received_time=event_time,
        sequence_or_source_cursor=cursor,
        source="POLYMARKET_CLOB_PUBLIC",
        payload=payload or {"bids": [{"price": "0.49", "size": "5"}], "asks": [{"price": "0.51", "size": "4"}], "tick_size": "0.01", "min_order_size": "1", "neg_risk": False, "hash": "book-hash"},
    )


def test_cap_a_projection_preserves_public_schema_without_provenance_invention():
    event = _event("e1", "2026-08-07T00:00:00Z", 1)
    projected = normalize_public_event(event)
    assert projected.best_bid == 0.49
    assert projected.best_ask == 0.51
    assert projected.tick_size == 0.01
    assert projected.min_order_size == 1.0
    assert projected.neg_risk is False
    assert projected.book_hash == "book-hash"
    assert len(projection_hash(event)) == 64
    assert "queue" not in projected.to_dict()


def test_cap_b_full_partial_no_fill_and_stale_fail_closed():
    cfg = FillModelConfig(max_book_age_ms=1000)
    full = simulate_paper_fill(PaperFillRequest("BUY", 3, 0.52, 100, ((0.51, 4), (0.52, 3))), cfg)
    assert full.status == "FILLED" and full.filled_qty == 3
    assert full.queue_position_exact is False
    partial = simulate_paper_fill(PaperFillRequest("BUY", 6, 0.51, 100, ((0.51, 4), (0.52, 9))), cfg)
    assert partial.status == "PARTIAL_FILL" and partial.filled_qty == 4
    no_fill = simulate_paper_fill(PaperFillRequest("BUY", 2, 0.50, 100, ((0.51, 4),)), cfg)
    assert no_fill.status == "NO_FILL" and no_fill.no_fill is True
    stale = simulate_paper_fill(PaperFillRequest("SELL", 2, 0.48, 1001, ((0.49, 4),)), cfg)
    assert stale.status == "NO_FILL_STALE_BOOK" and stale.filled_qty == 0


def test_cap_b_adverse_selection_is_directional_and_deterministic():
    req = PaperFillRequest("BUY", 2, 0.52, 10, ((0.51, 2),), 0.49)
    one = simulate_paper_fill(req)
    two = simulate_paper_fill(req)
    assert one == two
    assert one.adverse_selection == pytest.approx(-0.02)


def test_cap_c_same_input_same_output_and_strict_pit_cutoff():
    events = [
        _event("future", "2026-08-07T00:02:00Z", 3),
        _event("b", "2026-08-07T00:01:00Z", 2),
        _event("a", "2026-08-07T00:00:00Z", 1),
    ]
    cfg = ReplayConfig("2026-08-07T00:01:00Z", "2026-08-07T00:01:00Z", seed=7)
    result = deterministic_replay(events, cfg)
    assert result["event_ids"] == ["a", "b"]
    assert "future" not in result["event_ids"]
    assert result["external_live_reads"] is False
    assert result["chain_verified"] is True
    assert same_input_same_output_hash(events, cfg) is True


def test_cap_c_seed_and_clock_are_explicit_and_schema_pinned():
    event = _event("a", "2026-08-07T00:00:00Z", 1)
    a = deterministic_replay([event], ReplayConfig("2026-08-07T00:00:00Z", "2026-08-07T00:00:00Z", seed=1))
    b = deterministic_replay([event], ReplayConfig("2026-08-07T00:00:00Z", "2026-08-07T00:00:00Z", seed=2))
    assert a["output_hash"] != b["output_hash"]
    with pytest.raises(ValueError, match="EXTERNAL_LIVE_READ"):
        ReplayConfig("2026-08-07T00:00:00Z", "2026-08-07T00:00:00Z", external_live_reads=True)


def test_cap_d_external_live_cross_market_adapter_is_hard_disabled():
    adapter = DisabledCrossMarketAdapter()
    assert adapter.enabled is False
    assert adapter.external_network_reads == 0
    with pytest.raises(RuntimeError, match="DISABLED"):
        adapter.fetch_live("BTC")
    fixture = adapter.from_fixture({"source": "SYNTHETIC", "instrument": "BTCUSDT", "observed_at": "2026-08-07T00:00:00Z", "fields": {"mid": 100.0}})
    assert fixture.provenance == "FIXTURE_OR_SYNTHETIC_ONLY"


def test_cap_e_terminal_projection_has_data_quality_and_no_trading_controls():
    projection = microstructure_terminal_projection({"microprice": 0.5, "paper_fill_quality": "PARTIAL_FILL"})
    assert projection["paper_only"] is True
    assert projection["orders_enabled"] is False
    assert projection["execution_controls"] is False
    badges = data_quality_badges(stale=False, sequence_gaps=0, replay_verified=True)
    assert {x["status"] for x in badges} >= {"FRESH", "CONTIGUOUS", "VERIFIED", "AGGREGATE_ONLY"}
    html = visual_smoke_fragment().lower()
    for forbidden in ("connect wallet", "private key", "place order", "buy now", "sell now", "deposit", "withdraw"):
        assert forbidden not in html
    assert "paper fill quality" in html


def test_research_pack_has_no_order_wallet_or_signing_surface():
    import polymarket.research_pack as pack
    names = "\n".join(name.lower() for name, _ in inspect.getmembers(pack))
    for forbidden in ("create_order", "place_order", "cancel_order", "sign_transaction", "private_key", "wallet"):
        assert forbidden not in names
