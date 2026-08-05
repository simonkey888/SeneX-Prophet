from __future__ import annotations

from pathlib import Path

import pytest

from paper.broker import PublicOrderBook, SimulatedBroker
from paper.models import PaperDecision, PaperOrderIntent
from paper.portfolio import AppendOnlyJournal, PaperPortfolio
from paper.risk import PaperRiskConfig, PaperRiskEngine

TS = "2026-08-04T12:00:00Z"


def make_decision(edge=0.1, action="LONG"):
    return PaperDecision.build(
        timestamp_utc=TS, code_sha="a" * 40, config_sha="b" * 64,
        source_evidence_hash="c" * 64, market_id="m", condition_id="m",
        token_ids=["yes", "no"], action=action, reason_codes=["TEST"],
        requested_shares=2, expected_edge=edge, signal_payload={"edge": edge},
    )


def make_intent(decision, token="yes", max_notional=50, side="BUY", shares=2):
    return PaperOrderIntent.build(
        decision=decision, token_id=token, outcome=token.upper(), side=side,
        requested_shares=shares, max_notional_usd=max_notional,
    )


def make_fill(intent, price=0.5):
    book = PublicOrderBook(
        market_id="m", token_id=intent.token_id, timestamp_utc=TS,
        bids=((price, 100),), asks=((price, 100),), source_evidence_hash="d" * 64,
    )
    # Use a non-crossed snapshot for validation.
    book = PublicOrderBook(
        market_id="m", token_id=intent.token_id, timestamp_utc=TS,
        bids=((max(0.01, price - 0.01), 100),), asks=((price, 100),), source_evidence_hash="d" * 64,
    )
    return SimulatedBroker(slippage_bps_floor=0).simulate(intent=intent, book=book, now_utc=TS)


def test_portfolio_replay_equivalence(tmp_path: Path):
    journal = AppendOnlyJournal(tmp_path / "ledger.jsonl")
    portfolio = PaperPortfolio(starting_equity_usd=1000, journal=journal)
    decision = make_decision()
    fill = make_fill(make_intent(decision))
    portfolio.apply_fill(fill)
    replayed = PaperPortfolio.replay(starting_equity_usd=1000, records=journal.read_all())
    assert replayed.cash_usd == pytest.approx(portfolio.cash_usd)
    assert replayed.positions["yes"].quantity == pytest.approx(portfolio.positions["yes"].quantity)


def test_crash_restart_journal_is_append_only(tmp_path: Path):
    path = tmp_path / "ledger.jsonl"
    journal = AppendOnlyJournal(path)
    portfolio = PaperPortfolio(starting_equity_usd=1000, journal=journal)
    decision = make_decision()
    portfolio.apply_fill(make_fill(make_intent(decision)))
    before = path.read_bytes()
    recovered = PaperPortfolio.replay(starting_equity_usd=1000, records=AppendOnlyJournal(path).read_all())
    assert path.read_bytes() == before
    assert recovered.cash_usd < 1000


def risk_snapshot(portfolio):
    return portfolio.snapshot(
        timestamp_utc=TS, code_sha="a" * 40, config_sha="b" * 64,
        source_evidence_hash="c" * 64, prices={},
    )


def test_risk_allows_safe_intent_and_rejects_duplicate():
    config = PaperRiskConfig(virtual_starting_equity_usd=10000)
    risk = PaperRiskEngine(config)
    portfolio = PaperPortfolio(starting_equity_usd=10000)
    decision = make_decision()
    intents = [make_intent(decision, max_notional=25), make_intent(decision, token="no", max_notional=25)]
    first = risk.evaluate(
        decision=decision, intents=intents, portfolio=risk_snapshot(portfolio), paper_only=True,
        evidence_verified=True, raw_chain_verified=True, replay_verified=True,
        regime_known=True, book_valid=True, consecutive_losses=0,
    )
    second = risk.evaluate(
        decision=decision, intents=intents, portfolio=risk_snapshot(portfolio), paper_only=True,
        evidence_verified=True, raw_chain_verified=True, replay_verified=True,
        regime_known=True, book_valid=True, consecutive_losses=0,
    )
    assert first.allowed is True
    assert second.allowed is False
    assert "DUPLICATE_DECISION" in second.reason_codes


def test_risk_denies_exposure_limit():
    config = PaperRiskConfig(max_order_notional_pct=1.0)
    decision = make_decision()
    result = PaperRiskEngine(config).evaluate(
        decision=decision, intents=[make_intent(decision, max_notional=200)],
        portfolio=risk_snapshot(PaperPortfolio(starting_equity_usd=10000)), paper_only=True,
        evidence_verified=True, raw_chain_verified=True, replay_verified=True,
        regime_known=True, book_valid=True, consecutive_losses=0,
    )
    assert result.allowed is False
    assert "EXPOSURE_LIMIT" in result.reason_codes


def test_risk_denies_drawdown_and_consecutive_losses():
    config = PaperRiskConfig(max_session_drawdown_pct=2, max_consecutive_losses=5)
    portfolio = PaperPortfolio(starting_equity_usd=10000)
    snapshot = risk_snapshot(portfolio)
    object.__setattr__(snapshot, "max_drawdown_pct", 2.0)
    decision = make_decision()
    result = PaperRiskEngine(config).evaluate(
        decision=decision, intents=[make_intent(decision, max_notional=10)], portfolio=snapshot,
        paper_only=True, evidence_verified=True, raw_chain_verified=True, replay_verified=True,
        regime_known=True, book_valid=True, consecutive_losses=5,
    )
    assert "DRAWDOWN_LIMIT" in result.reason_codes
    assert "CONSECUTIVE_LOSS_LIMIT" in result.reason_codes


def test_risk_denies_integrity_and_nonpaper():
    decision = make_decision()
    result = PaperRiskEngine(PaperRiskConfig()).evaluate(
        decision=decision, intents=[make_intent(decision, max_notional=10)],
        portfolio=risk_snapshot(PaperPortfolio(starting_equity_usd=10000)), paper_only=False,
        evidence_verified=False, raw_chain_verified=False, replay_verified=False,
        regime_known=False, book_valid=False, consecutive_losses=0,
    )
    assert set(("PAPER_ONLY_DISABLED", "EVIDENCE_UNVERIFIED", "RAW_CHAIN_INVALID", "REPLAY_UNVERIFIED", "REGIME_UNKNOWN", "INVALID_BOOK")).issubset(result.reason_codes)
