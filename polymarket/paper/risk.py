"""Fail-closed risk controls for simulated paper intents."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import PaperDecision, PaperOrderIntent, PaperPortfolioSnapshot, PaperRiskDecision, deterministic_id


@dataclass(frozen=True)
class PaperRiskConfig:
    virtual_starting_equity_usd: float = 10_000.0
    max_order_notional_pct: float = 1.0
    max_gross_exposure_pct: float = 5.0
    max_single_market_exposure_pct: float = 2.0
    max_session_drawdown_pct: float = 2.0
    max_consecutive_losses: int = 5
    book_staleness_seconds: float = 15.0
    slippage_bps_floor: float = 5.0
    minimum_expected_edge: float = 0.0


class PaperRiskEngine:
    def __init__(self, config: PaperRiskConfig):
        self.config = config
        self.seen_decisions: set[str] = set()

    def evaluate(
        self,
        *,
        decision: PaperDecision,
        intents: Iterable[PaperOrderIntent],
        portfolio: PaperPortfolioSnapshot,
        paper_only: bool,
        evidence_verified: bool,
        raw_chain_verified: bool,
        replay_verified: bool,
        regime_known: bool,
        book_valid: bool,
        consecutive_losses: int,
    ) -> PaperRiskDecision:
        intents = tuple(intents)
        requested = sum(intent.max_notional_usd for intent in intents)
        projected = (portfolio.gross_exposure_usd or 0.0) + requested
        reasons: list[str] = []
        if not paper_only:
            reasons.append("PAPER_ONLY_DISABLED")
        if not evidence_verified:
            reasons.append("EVIDENCE_UNVERIFIED")
        if not raw_chain_verified:
            reasons.append("RAW_CHAIN_INVALID")
        if not replay_verified:
            reasons.append("REPLAY_UNVERIFIED")
        if not regime_known:
            reasons.append("REGIME_UNKNOWN")
        if not book_valid:
            reasons.append("INVALID_BOOK")
        if decision.expected_edge is None or decision.expected_edge <= self.config.minimum_expected_edge:
            reasons.append("INSUFFICIENT_EDGE")
        if decision.deterministic_id in self.seen_decisions:
            reasons.append("DUPLICATE_DECISION")
        if portfolio.equity_usd is None or not portfolio.equity_known:
            reasons.append("VALUATION_UNKNOWN")
        equity = max(portfolio.equity_usd or portfolio.cash_usd, 1e-9)
        if requested / equity * 100.0 > self.config.max_order_notional_pct + 1e-9:
            reasons.append("EXPOSURE_LIMIT")
        if projected / equity * 100.0 > self.config.max_gross_exposure_pct + 1e-9:
            reasons.append("EXPOSURE_LIMIT")
        by_market: dict[str, float] = {}
        for intent in intents:
            by_market[intent.market_id] = by_market.get(intent.market_id, 0.0) + intent.max_notional_usd
        if any(value / equity * 100.0 > self.config.max_single_market_exposure_pct + 1e-9 for value in by_market.values()):
            reasons.append("EXPOSURE_LIMIT")
        if portfolio.max_drawdown_pct >= self.config.max_session_drawdown_pct:
            reasons.append("DRAWDOWN_LIMIT")
        if consecutive_losses >= self.config.max_consecutive_losses:
            reasons.append("CONSECUTIVE_LOSS_LIMIT")
        if decision.action in {"FLAT", "NO_TRADE", "INSUFFICIENT_EVIDENCE", "STALE_DATA", "REGIME_UNKNOWN", "RISK_LIMIT", "INTEGRITY_FAILURE"}:
            reasons.append(decision.action)
        reasons = sorted(set(reasons))
        allowed = not reasons
        if allowed:
            self.seen_decisions.add(decision.deterministic_id)
        payload = {
            "decision_id": decision.deterministic_id,
            "allowed": allowed,
            "reasons": reasons,
            "requested": round(requested, 12),
            "projected": round(projected, 12),
        }
        return PaperRiskDecision(
            schema_version=decision.schema_version,
            timestamp_utc=decision.timestamp_utc,
            code_sha=decision.code_sha,
            config_sha=decision.config_sha,
            source_evidence_hash=decision.source_evidence_hash,
            deterministic_id=deterministic_id("risk", payload),
            provenance="FAIL_CLOSED_PAPER_RISK",
            decision_id=decision.deterministic_id,
            allowed=allowed,
            reason_codes=tuple(reasons),
            requested_notional_usd=round(requested, 12),
            projected_gross_exposure_usd=round(projected, 12),
        )
