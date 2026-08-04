from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from paper.broker import BrokerRejection, PublicOrderBook, SimulatedBroker
from paper.models import PaperDecision, PaperOrderIntent


def ts(offset: int = 0) -> str:
    return (datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc) + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")


def decision() -> PaperDecision:
    return PaperDecision.build(
        timestamp_utc=ts(), code_sha="a" * 40, config_sha="b" * 64,
        source_evidence_hash="c" * 64, market_id="m", condition_id="c",
        token_ids=["yes", "no"], action="LONG", reason_codes=["H011_COMPLETE_SET_EDGE"],
        requested_shares=10, expected_edge=0.1, signal_payload={"x": 1},
    )


def intent(side="BUY", shares=10, max_notional=100) -> PaperOrderIntent:
    return PaperOrderIntent.build(
        decision=decision(), token_id="yes", outcome="UP", side=side,
        requested_shares=shares, max_notional_usd=max_notional,
    )


def book(*, bids=None, asks=None, timestamp=None) -> PublicOrderBook:
    return PublicOrderBook.from_payload(
        market_id="m", token_id="yes", timestamp_utc=timestamp or ts(),
        source_evidence_hash="d" * 64,
        payload={"asset_id": "yes", "bids": bids or [{"price": "0.40", "size": "20"}], "asks": asks or [{"price": "0.50", "size": "20"}]},
    )


def test_decision_and_intent_ids_are_deterministic():
    assert decision().deterministic_id == decision().deterministic_id
    assert intent().deterministic_id == intent().deterministic_id


def test_buy_fill_is_deterministic_and_uses_ask():
    broker = SimulatedBroker(fee_bps=10, slippage_bps_floor=5)
    first = broker.simulate(intent=intent(), book=book(), now_utc=ts(1))
    second = broker.simulate(intent=intent(), book=book(), now_utc=ts(1))
    assert first == second
    assert first.fill_price == pytest.approx(0.50025)
    assert first.filled_shares == 10
    assert first.partial is False


def test_sell_fill_uses_bid():
    fill = SimulatedBroker(slippage_bps_floor=0).simulate(intent=intent(side="SELL"), book=book(), now_utc=ts(1))
    assert fill.fill_price == pytest.approx(0.40)


def test_partial_fill_never_fabricates_liquidity():
    fill = SimulatedBroker(slippage_bps_floor=0).simulate(
        intent=intent(shares=10),
        book=book(asks=[{"price": "0.5", "size": "3"}]),
        now_utc=ts(1),
    )
    assert fill.filled_shares == 3
    assert fill.partial is True
    assert fill.observed_available_size == 3


def test_stale_book_rejected():
    with pytest.raises(BrokerRejection, match="STALE_DATA"):
        SimulatedBroker(book_staleness_seconds=15).simulate(
            intent=intent(), book=book(timestamp=ts()), now_utc=ts(16),
        )


def test_crossed_book_rejected():
    crossed = book(bids=[{"price": "0.6", "size": "10"}], asks=[{"price": "0.5", "size": "10"}])
    with pytest.raises(BrokerRejection, match="INVALID_BOOK"):
        SimulatedBroker().simulate(intent=intent(), book=crossed, now_utc=ts(1))


def test_empty_liquidity_rejected():
    empty = PublicOrderBook(
        market_id="m", token_id="yes", timestamp_utc=ts(), bids=(), asks=(), source_evidence_hash="d" * 64,
    )
    with pytest.raises(BrokerRejection, match="EMPTY_LIQUIDITY"):
        SimulatedBroker().simulate(intent=intent(), book=empty, now_utc=ts(1))


def test_unbound_book_rejected():
    wrong = PublicOrderBook(
        market_id="other", token_id="yes", timestamp_utc=ts(), bids=((0.4, 10),), asks=((0.5, 10),), source_evidence_hash="d" * 64,
    )
    with pytest.raises(BrokerRejection, match="UNBOUND_BOOK"):
        SimulatedBroker().simulate(intent=intent(), book=wrong, now_utc=ts(1))
