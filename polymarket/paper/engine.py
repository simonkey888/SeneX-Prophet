"""Paper-only integration of the existing H011 deterministic shadow signal."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .broker import BrokerRejection, PublicOrderBook, SimulatedBroker
from .models import PaperDecision, PaperFill, PaperOrderIntent, PaperRiskDecision, sha256_json
from .portfolio import PaperPortfolio
from .risk import PaperRiskEngine


@dataclass
class PaperEngineResult:
    decision: PaperDecision
    intents: list[PaperOrderIntent] = field(default_factory=list)
    risk_decision: PaperRiskDecision | None = None
    fills: list[PaperFill] = field(default_factory=list)
    abstention_reasons: list[str] = field(default_factory=list)


class PaperEngine:
    def __init__(self, *, broker: SimulatedBroker, risk: PaperRiskEngine, portfolio: PaperPortfolio):
        self.broker = broker
        self.risk = risk
        self.portfolio = portfolio

    @staticmethod
    def _decision_from_h011(
        *,
        record: Mapping[str, Any],
        timestamp_utc: str,
        code_sha: str,
        config_sha: str,
        source_evidence_hash: str,
        requested_shares: float,
    ) -> PaperDecision:
        market_id = str(record.get("market_id") or record.get("condition_id") or record.get("conditionId") or "")
        condition_id = str(record.get("condition_id") or record.get("conditionId") or market_id)
        tokens = record.get("token_ids") or record.get("clob_token_ids") or []
        shadow = record.get("shadow_execution") or {}
        status = str(shadow.get("status") or record.get("record_status") or "")
        net_edge_raw = shadow.get("net_edge")
        try:
            net_edge = float(net_edge_raw) if net_edge_raw is not None else None
        except (TypeError, ValueError):
            net_edge = None
        reasons: list[str] = []
        if not record.get("evidence_verified", True):
            action = "INSUFFICIENT_EVIDENCE"
            reasons.append("EVIDENCE_UNVERIFIED")
        elif not record.get("raw_chain_verified", True) or not record.get("replay_verified", True):
            action = "INTEGRITY_FAILURE"
            reasons.append("INTEGRITY_FAILURE")
        elif record.get("stale_data"):
            action = "STALE_DATA"
            reasons.append("STALE_DATA")
        elif record.get("regime_known") is False:
            action = "REGIME_UNKNOWN"
            reasons.append("REGIME_UNKNOWN")
        elif status == "SHADOW_EXECUTABLE" and net_edge is not None and net_edge > 0 and len(tokens) == 2:
            action = "LONG"
            reasons.append("H011_COMPLETE_SET_EDGE")
        else:
            action = "NO_TRADE"
            reasons.append("INSUFFICIENT_EDGE" if net_edge is not None else "INSUFFICIENT_EVIDENCE")
        return PaperDecision.build(
            timestamp_utc=timestamp_utc,
            code_sha=code_sha,
            config_sha=config_sha,
            source_evidence_hash=source_evidence_hash,
            market_id=market_id,
            condition_id=condition_id,
            token_ids=[str(token) for token in tokens],
            action=action,
            reason_codes=reasons,
            requested_shares=requested_shares,
            expected_edge=net_edge,
            signal_payload=dict(record),
        )

    def process_h011_record(
        self,
        *,
        record: Mapping[str, Any],
        books: Mapping[str, PublicOrderBook],
        outcomes: Mapping[str, str],
        timestamp_utc: str,
        code_sha: str,
        config_sha: str,
        requested_shares: float,
        max_notional_per_leg_usd: float,
    ) -> PaperEngineResult:
        evidence_hash = sha256_json({
            "record": dict(record),
            "books": {token: {
                "market_id": book.market_id,
                "token_id": book.token_id,
                "timestamp_utc": book.timestamp_utc,
                "bids": list(book.bids),
                "asks": list(book.asks),
                "source_evidence_hash": book.source_evidence_hash,
            } for token, book in sorted(books.items())},
        })
        decision = self._decision_from_h011(
            record=record,
            timestamp_utc=timestamp_utc,
            code_sha=code_sha,
            config_sha=config_sha,
            source_evidence_hash=evidence_hash,
            requested_shares=requested_shares,
        )
        result = PaperEngineResult(decision=decision)
        if decision.action != "LONG":
            result.abstention_reasons.extend(decision.reason_codes)
            return result
        missing = [token for token in decision.token_ids if token not in books]
        if missing:
            result.abstention_reasons.append("INVALID_BOOK")
            return result
        intents = [
            PaperOrderIntent.build(
                decision=decision,
                token_id=token,
                outcome=outcomes.get(token, "UNKNOWN"),
                side="BUY",
                requested_shares=requested_shares,
                max_notional_usd=max_notional_per_leg_usd,
            )
            for token in decision.token_ids
        ]
        result.intents = intents
        initial = self.portfolio.snapshot(
            timestamp_utc=timestamp_utc,
            code_sha=code_sha,
            config_sha=config_sha,
            source_evidence_hash=evidence_hash,
            prices={},
        )
        book_valid = True
        for token in decision.token_ids:
            try:
                books[token].validate(now_utc=timestamp_utc, staleness_seconds=self.broker.book_staleness_seconds)
            except BrokerRejection:
                book_valid = False
        risk_decision = self.risk.evaluate(
            decision=decision,
            intents=intents,
            portfolio=initial,
            paper_only=True,
            evidence_verified=bool(record.get("evidence_verified", True)),
            raw_chain_verified=bool(record.get("raw_chain_verified", True)),
            replay_verified=bool(record.get("replay_verified", True)),
            regime_known=bool(record.get("regime_known", True)),
            book_valid=book_valid,
            consecutive_losses=self.portfolio.consecutive_losses,
        )
        result.risk_decision = risk_decision
        if not risk_decision.allowed:
            result.abstention_reasons.extend(risk_decision.reason_codes)
            return result
        try:
            proposed = [
                self.broker.simulate(intent=intent, book=books[intent.token_id], now_utc=timestamp_utc)
                for intent in intents
            ]
        except BrokerRejection as exc:
            result.abstention_reasons.append(exc.reason)
            return result
        executable_shares = min(fill.filled_shares for fill in proposed)
        if executable_shares <= 0:
            result.abstention_reasons.append("EMPTY_LIQUIDITY")
            return result
        # Complete-set paper execution is applied symmetrically. If one leg is
        # thinner, both legs are deterministically re-simulated at the common
        # observed quantity; no fabricated liquidity and no leg imbalance.
        common_intents = [
            PaperOrderIntent.build(
                decision=decision,
                token_id=intent.token_id,
                outcome=intent.outcome,
                side=intent.side,
                requested_shares=executable_shares,
                max_notional_usd=intent.max_notional_usd,
            )
            for intent in intents
        ]
        fills = [
            self.broker.simulate(intent=intent, book=books[intent.token_id], now_utc=timestamp_utc)
            for intent in common_intents
        ]
        if any(abs(fill.filled_shares - executable_shares) > 1e-9 for fill in fills):
            result.abstention_reasons.append("INVALID_BOOK")
            return result
        for fill in fills:
            self.portfolio.apply_fill(fill)
        result.intents = common_intents
        result.fills = fills
        return result
