from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from paper.broker import BrokerRejection, PublicOrderBook, SimulatedBroker, parse_source_timestamp, validate_pair_skew
from paper.engine import PaperEngine
from paper.execution import SequentialPaperExecutor
from paper.fees import FeeModelError, FeeSchedule, calculate_fee, run_official_conformance
from paper.models import PaperDecision, PaperOrderIntent
from paper.portfolio import PaperPortfolio
from paper.risk import PaperRiskConfig, PaperRiskEngine
from paper.settlement import ResolutionEvidence, SettlementError, SettlementState

TS = "2026-08-04T12:00:00Z"
LATER = "2026-08-04T12:00:00.500000Z"


def decision() -> PaperDecision:
    return PaperDecision.build(
        timestamp_utc=TS,
        code_sha="a" * 40,
        config_sha="b" * 64,
        source_evidence_hash="c" * 64,
        market_id="m",
        condition_id="c",
        token_ids=["yes", "no"],
        action="LONG",
        reason_codes=["TEST"],
        requested_shares=2,
        expected_edge=0.1,
        signal_payload={"edge": 0.1},
    )


def intents(shares: float = 2.0):
    d = decision()
    return tuple(
        PaperOrderIntent.build(
            decision=d,
            token_id=token,
            outcome=outcome,
            side="BUY",
            requested_shares=shares,
            max_notional_usd=50,
        )
        for token, outcome in (("yes", "UP"), ("no", "DOWN"))
    )


def book(token: str, *, source=TS, received=TS, bid=0.44, ask=0.46, size=10.0) -> PublicOrderBook:
    return PublicOrderBook.from_payload(
        market_id="m",
        token_id=token,
        timestamp_utc=received,
        payload={
            "asset_id": token,
            "timestamp": source,
            "bids": [{"price": str(bid), "size": str(size)}] if size else [],
            "asks": [{"price": str(ask), "size": str(size)}] if size else [],
        },
        source_evidence_hash=hashlib.sha256(f"{token}:{source}:{ask}:{size}".encode()).hexdigest(),
    )


def schedules(*, conflict_second=False):
    verified = FeeSchedule.deterministic_fixture(condition_id="c", exponent=1)
    conflict = FeeSchedule.deterministic_fixture(condition_id="c", exponent=2)
    return {"yes": verified, "no": conflict if conflict_second else verified}


def execute(*, first=None, second=None, fee=None, shares=2.0, window_end=None, second_epoch=None, max_skew=1000):
    portfolio = PaperPortfolio(starting_equity_usd=10000)
    result = SequentialPaperExecutor(
        broker=SimulatedBroker(slippage_bps_floor=0, book_staleness_seconds=15),
        configured_transport_delay_ms=500,
        maximum_pair_skew_ms=max_skew,
    ).execute(
        intents=intents(shares),
        first_books=first or {"yes": book("yes"), "no": book("no")},
        second_books=second or {"yes": book("yes", received=LATER), "no": book("no", received=LATER)},
        fee_schedules=fee or schedules(),
        first_now_utc=TS,
        second_now_utc=LATER,
        portfolio=portfolio,
        window_end_epoch=window_end,
        second_epoch=second_epoch,
    )
    return result, portfolio


# Source-time truth

def test_source_timestamp_seconds_and_milliseconds_parse_identically():
    assert parse_source_timestamp("1785844800") == "2026-08-04T12:00:00Z"
    assert parse_source_timestamp("1785844800000") == "2026-08-04T12:00:00Z"


@pytest.mark.parametrize("value,reason", [(None, "MISSING_SOURCE_TIMESTAMP"), ("nonsense", "MALFORMED_SOURCE_TIMESTAMP")])
def test_missing_or_malformed_source_time_fails_closed(value, reason):
    with pytest.raises(BrokerRejection) as exc:
        parse_source_timestamp(value)
    assert exc.value.reason == reason


def test_future_source_time_and_stale_source_time_fail_even_when_received_now():
    future = book("yes", source="2026-08-04T12:00:02Z", received=TS)
    with pytest.raises(BrokerRejection, match="FUTURE_SOURCE_TIMESTAMP"):
        future.validate(now_utc=TS, staleness_seconds=15, future_tolerance_seconds=1)
    stale = book("yes", source="2026-08-04T11:59:00Z", received=TS)
    with pytest.raises(BrokerRejection, match="STALE_DATA"):
        stale.validate(now_utc=TS, staleness_seconds=15)


def test_pair_skew_and_fixture_provenance_fail_closed():
    one = book("yes", source=TS)
    two = book("no", source="2026-08-04T12:00:02Z", received="2026-08-04T12:00:02Z")
    with pytest.raises(BrokerRejection, match="PAIR_TIMESTAMP_SKEW_EXCEEDED"):
        validate_pair_skew(one, two, maximum_skew_ms=1000)
    fixture = PublicOrderBook.from_payload(
        market_id="m", token_id="yes", timestamp_utc=TS,
        payload={"asset_id": "yes", "bids": [{"price": "0.4", "size": "1"}], "asks": [{"price": "0.5", "size": "1"}]},
        source_evidence_hash="d" * 64, fixture_timestamp_utc=TS,
    )
    with pytest.raises(BrokerRejection, match="FIXTURE_TIMESTAMP_NOT_OBSERVED"):
        fixture.validate(now_utc=TS, staleness_seconds=15, allow_fixture=False)


# Fee truth

def test_official_fee_conformance_detects_docs_sdk_conflict_and_fails_closed():
    report = run_official_conformance()
    assert report["result"] == "PASS"
    assert report["official_source_conflict_detected"] is True
    assert report["fail_closed_verified"] is True


def test_fee_disabled_market_is_verified_zero_but_enabled_conflict_is_rejected():
    disabled = FeeSchedule.deterministic_fixture(enabled=False, exponent=2)
    assert calculate_fee(shares="100", price="0.5", schedule=disabled).fee_usd == Decimal("0")
    conflict = FeeSchedule.deterministic_fixture(enabled=True, exponent=2)
    with pytest.raises(FeeModelError, match="FEE_MODEL_UNVERIFIED"):
        calculate_fee(shares="100", price="0.5", schedule=conflict)


def test_broker_fill_consumes_the_same_fee_result_and_never_defaults_zero():
    intent = intents()[0]
    schedule = FeeSchedule.deterministic_fixture(exponent=1)
    expected = calculate_fee(shares="2", price="0.46", schedule=schedule)
    fill = SimulatedBroker(slippage_bps_floor=0).simulate(
        intent=intent, book=book(intent.token_id), now_utc=TS, fee_schedule=schedule,
    )
    assert fill.fee_usd == float(expected.fee_usd)
    assert fill.fee_schedule_hash == schedule.raw_schedule_hash
    assert fill.condition_id == "c"


# Sequential execution hostile scenarios

def test_both_legs_fill_without_forced_symmetry_claim():
    result, _ = execute()
    assert result.completion_status == "BOTH_LEGS_FILL"
    assert len(result.fills) == 2
    assert result.execution_model_version == "TAKER_COMPLETE_SET_SEQUENTIAL_PAPER_V1"


def test_first_leg_filled_second_leg_empty():
    second = {"yes": book("yes", received=LATER), "no": book("no", received=LATER, size=0)}
    result, portfolio = execute(second=second)
    assert result.completion_status == "SECOND_LEG_FAILED_AFTER_FIRST_FILL"
    assert result.second_leg.reason == "EMPTY_LIQUIDITY"
    assert result.leg_imbalance_shares > 0
    assert sum(p.quantity for p in portfolio.positions.values()) > 0


def test_first_leg_filled_second_leg_stale():
    second = {"yes": book("yes", received=LATER), "no": book("no", source="2026-08-04T11:59:00Z", received=LATER)}
    result, _ = execute(second=second)
    assert result.second_leg.reason == "STALE_DATA"


def test_second_leg_price_worsens_and_is_recorded():
    first = {"yes": book("yes"), "no": book("no", ask=0.46)}
    second = {"yes": book("yes", received=LATER), "no": book("no", received=LATER, ask=0.56)}
    result, _ = execute(first=first, second=second)
    assert result.second_leg_repricing is not None and result.second_leg_repricing > 0


def test_first_leg_partial_second_leg_different_depth_preserves_imbalance():
    first = {"yes": book("yes", size=10), "no": book("no", size=0.5)}
    second = {"yes": book("yes", received=LATER, size=10), "no": book("no", received=LATER, size=0.25)}
    result, _ = execute(first=first, second=second, shares=2)
    assert result.completion_status == "PARTIAL_COMPLETION"
    assert result.first_leg.filled_shares != result.second_leg.filled_shares
    assert result.leg_imbalance_shares != 0


def test_second_leg_fee_schedule_unverified():
    result, _ = execute(fee=schedules(conflict_second=True))
    assert result.second_leg.reason == "FEE_MODEL_UNVERIFIED"


def test_pair_timestamp_skew_exceeded_after_first_fill():
    second = {"yes": book("yes", received=LATER), "no": book("no", source="2026-08-04T12:00:03Z", received="2026-08-04T12:00:03Z")}
    result, _ = execute(second=second, max_skew=1000)
    assert result.second_leg.reason == "PAIR_TIMESTAMP_SKEW_EXCEEDED"


def test_window_closes_between_legs():
    result, _ = execute(window_end=100.0, second_epoch=100.0)
    assert result.second_leg.reason == "WINDOW_CLOSES_BETWEEN_LEGS"


# Settlement and valuation truth

def portfolio_with_two_positions() -> PaperPortfolio:
    portfolio = PaperPortfolio(starting_equity_usd=1000)
    broker = SimulatedBroker(slippage_bps_floor=0)
    schedule = FeeSchedule.deterministic_fixture(exponent=1)
    for intent in intents():
        portfolio.apply_fill(broker.simulate(intent=intent, book=book(intent.token_id), now_utc=TS, fee_schedule=schedule))
    return portfolio


def test_unresolved_positions_are_unknown_not_zero():
    portfolio = portfolio_with_two_positions()
    snapshot = portfolio.snapshot(timestamp_utc=TS, code_sha="a"*40, config_sha="b"*64, source_evidence_hash="c"*64, prices={})
    assert snapshot.equity_known is False
    assert snapshot.equity_usd is None
    assert snapshot.unrealized_pnl is None
    assert snapshot.pending_settlement_count == 2


def test_win_loss_settlement_is_idempotent_and_replayable():
    portfolio = portfolio_with_two_positions()
    evidence = ResolutionEvidence.deterministic_fixture(condition_id="c", market_id="m", token_ids=("yes", "no"), winning_token_id="yes")
    win = portfolio.apply_settlement(evidence=evidence, token_id="yes")
    loss = portfolio.apply_settlement(evidence=evidence, token_id="no")
    duplicate = portfolio.apply_settlement(evidence=evidence, token_id="yes")
    assert win.payout_usd > 0 and loss.payout_usd == 0
    assert duplicate.idempotent_duplicate is True
    replayed = PaperPortfolio.replay(starting_equity_usd=1000, records=portfolio.ledger)
    assert replayed.cash_usd == pytest.approx(portfolio.cash_usd)
    assert replayed.realized_settled_pnl == pytest.approx(portfolio.realized_settled_pnl)


def test_mismatched_resolution_identity_and_unverified_evidence_are_rejected():
    portfolio = portfolio_with_two_positions()
    wrong = ResolutionEvidence.deterministic_fixture(condition_id="wrong", market_id="m", token_ids=("yes", "no"), winning_token_id="yes")
    with pytest.raises(SettlementError, match="SETTLEMENT_CONDITION_MISMATCH"):
        portfolio.apply_settlement(evidence=wrong, token_id="yes")
    valid = ResolutionEvidence.deterministic_fixture(condition_id="c", market_id="m", token_ids=("yes", "no"), winning_token_id="yes")
    object.__setattr__(valid, "verified", False)
    with pytest.raises(SettlementError, match="SETTLEMENT_EVIDENCE_UNVERIFIED"):
        portfolio.apply_settlement(evidence=valid, token_id="yes")
