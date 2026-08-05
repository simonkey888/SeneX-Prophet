"""Paper-only integration of the existing H011 deterministic shadow signal."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .broker import BrokerRejection, PublicOrderBook, SimulatedBroker
from .execution import SequentialExecutionResult, SequentialPaperExecutor
from .fees import FeeSchedule
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
    sequential_execution: SequentialExecutionResult | None = None


class PaperEngine:
    def __init__(
        self,
        *,
        broker: SimulatedBroker,
        risk: PaperRiskEngine,
        portfolio: PaperPortfolio,
        sequential_executor: SequentialPaperExecutor | None = None,
    ):
        self.broker = broker
        self.risk = risk
        self.portfolio = portfolio
        self.sequential_executor = sequential_executor or SequentialPaperExecutor(broker=broker)

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
        elif record.get("fee_model_verified") is False:
            action = "NO_TRADE"
            reasons.append("FEE_MODEL_UNVERIFIED")
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
        fee_schedules: Mapping[str, FeeSchedule] | None = None,
        second_leg_books: Mapping[str, PublicOrderBook] | None = None,
        second_timestamp_utc: str | None = None,
        configured_transport_delay_ms: int = 500,
        maximum_pair_skew_ms: float = 1_000.0,
        window_end_epoch: float | None = None,
        second_epoch: float | None = None,
    ) -> PaperEngineResult:
        evidence_hash = sha256_json({
            "record": dict(record),
            "books": {
                token: {
                    "market_id": book.market_id,
                    "token_id": book.token_id,
                    "source_timestamp_utc": book.source_timestamp_utc,
                    "received_timestamp_utc": book.received_timestamp_utc,
                    "bids": list(book.bids),
                    "asks": list(book.asks),
                    "source_evidence_hash": book.source_evidence_hash,
                }
                for token, book in sorted(books.items())
            },
            "fee_schedules": {
                token: schedule.to_evidence() for token, schedule in sorted((fee_schedules or {}).items())
            },
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
        fixture_books = all(book.source_timestamp_provenance == "FIXTURE_EXPLICIT" for book in books.values())
        if not fee_schedules and fixture_books:
            schedule = FeeSchedule.deterministic_fixture(condition_id=decision.condition_id, fee_rate="0.07", enabled=True)
            fee_schedules = {token: schedule for token in decision.token_ids}
        if not fee_schedules or any(token not in fee_schedules for token in decision.token_ids):
            result.abstention_reasons.append("FEE_MODEL_UNVERIFIED")
            return result
        if second_leg_books is None and fixture_books:
            second_leg_books = books
            second_timestamp_utc = timestamp_utc
        if second_leg_books is None or second_timestamp_utc is None:
            result.abstention_reasons.append("SECOND_LEG_SNAPSHOT_REQUIRED")
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
        executor = SequentialPaperExecutor(
            broker=self.broker,
            configured_transport_delay_ms=configured_transport_delay_ms,
            maximum_pair_skew_ms=maximum_pair_skew_ms,
        )
        execution = executor.execute(
            intents=(intents[0], intents[1]),
            first_books=books,
            second_books=second_leg_books,
            fee_schedules=fee_schedules,
            first_now_utc=timestamp_utc,
            second_now_utc=second_timestamp_utc,
            portfolio=self.portfolio,
            window_end_epoch=window_end_epoch,
            second_epoch=second_epoch,
        )
        result.sequential_execution = execution
        result.fills = list(execution.fills)
        if execution.completion_status != "BOTH_LEGS_FILL":
            reason = execution.second_leg.reason or execution.first_leg.reason or execution.completion_status
            result.abstention_reasons.append(reason)
        return result
