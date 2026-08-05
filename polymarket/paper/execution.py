"""Non-atomic sequential complete-set paper execution truth model."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .broker import BrokerRejection, PublicOrderBook, SimulatedBroker, validate_pair_skew
from .fees import FeeModelError, FeeSchedule
from .models import PaperFill, PaperOrderIntent, deterministic_id
from .portfolio import PaperPortfolio

EXECUTION_MODEL_VERSION = "TAKER_COMPLETE_SET_SEQUENTIAL_PAPER_V1"


@dataclass(frozen=True)
class SequentialLegRecord:
    leg_number: int
    token_id: str
    outcome: str
    status: str
    requested_shares: float
    filled_shares: float
    fill_price: float | None
    fee_usd: float | None
    source_timestamp_utc: str | None
    received_timestamp_utc: str | None
    source_age_ms: float | None
    fee_schedule_hash: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class SequentialExecutionResult:
    execution_id: str
    execution_model_version: str
    first_leg_token_id: str
    second_leg_token_id: str
    configured_transport_delay_ms: int
    protocol_taker_delay_enabled: bool | None
    protocol_taker_delay_ms: int
    pair_skew_ms: float | None
    first_leg: SequentialLegRecord
    second_leg: SequentialLegRecord
    leg_imbalance_shares: float
    second_leg_repricing: float | None
    completion_status: str
    paper_unwind_outcome: str
    fills: tuple[PaperFill, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "execution_model_version": self.execution_model_version,
            "first_leg_token_id": self.first_leg_token_id,
            "second_leg_token_id": self.second_leg_token_id,
            "configured_transport_delay_ms": self.configured_transport_delay_ms,
            "protocol_taker_delay_enabled": self.protocol_taker_delay_enabled,
            "protocol_taker_delay_ms": self.protocol_taker_delay_ms,
            "pair_skew_ms": self.pair_skew_ms,
            "first_leg": self.first_leg.to_dict(),
            "second_leg": self.second_leg.to_dict(),
            "leg_imbalance_shares": self.leg_imbalance_shares,
            "second_leg_repricing": self.second_leg_repricing,
            "completion_status": self.completion_status,
            "paper_unwind_outcome": self.paper_unwind_outcome,
            "fills": [fill.to_dict() for fill in self.fills],
        }


@dataclass(frozen=True)
class PendingSequentialExecution:
    intents: tuple[PaperOrderIntent, PaperOrderIntent]
    first_books: Mapping[str, PublicOrderBook]
    fee_schedules: Mapping[str, FeeSchedule]
    first_now_utc: str
    first_leg: SequentialLegRecord
    first_fill: PaperFill | None


def _failed_leg(number: int, intent: PaperOrderIntent, schedule: FeeSchedule | None, reason: str) -> SequentialLegRecord:
    return SequentialLegRecord(
        leg_number=number,
        token_id=intent.token_id,
        outcome=intent.outcome,
        status="FAILED",
        requested_shares=intent.requested_shares,
        filled_shares=0.0,
        fill_price=None,
        fee_usd=None,
        source_timestamp_utc=None,
        received_timestamp_utc=None,
        source_age_ms=None,
        fee_schedule_hash=schedule.raw_schedule_hash if schedule else "UNVERIFIED",
        reason=reason,
    )


def _filled_leg(number: int, fill: PaperFill) -> SequentialLegRecord:
    return SequentialLegRecord(
        leg_number=number,
        token_id=fill.token_id,
        outcome=fill.outcome,
        status="PARTIAL" if fill.partial else "FILLED",
        requested_shares=fill.requested_shares,
        filled_shares=fill.filled_shares,
        fill_price=fill.fill_price,
        fee_usd=fill.fee_usd,
        source_timestamp_utc=fill.book_timestamp_utc,
        received_timestamp_utc=fill.received_timestamp_utc,
        source_age_ms=fill.source_age_ms,
        fee_schedule_hash=fill.fee_schedule_hash,
    )


class SequentialPaperExecutor:
    def __init__(
        self,
        *,
        broker: SimulatedBroker,
        configured_transport_delay_ms: int = 500,
        maximum_pair_skew_ms: float = 1_000.0,
    ):
        self.broker = broker
        self.configured_transport_delay_ms = max(0, int(configured_transport_delay_ms))
        self.maximum_pair_skew_ms = max(0.0, float(maximum_pair_skew_ms))

    @staticmethod
    def _timestamp_epoch(value: str) -> float:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()

    def begin(
        self,
        *,
        intents: tuple[PaperOrderIntent, PaperOrderIntent],
        first_books: Mapping[str, PublicOrderBook],
        fee_schedules: Mapping[str, FeeSchedule],
        first_now_utc: str,
        portfolio: PaperPortfolio,
    ) -> PendingSequentialExecution:
        ordered = tuple(intents)
        first_intent = ordered[0]
        first_schedule = fee_schedules.get(first_intent.token_id)
        first_fill: PaperFill | None = None
        if first_schedule is None:
            first_leg = _failed_leg(1, first_intent, None, "FEE_MODEL_UNVERIFIED")
        else:
            try:
                first_fill = self.broker.simulate(
                    intent=first_intent,
                    book=first_books[first_intent.token_id],
                    now_utc=first_now_utc,
                    fee_schedule=first_schedule,
                    liquidity_classification="TAKER",
                )
            except (KeyError, BrokerRejection, FeeModelError) as exc:
                first_leg = _failed_leg(
                    1,
                    first_intent,
                    first_schedule,
                    getattr(exc, "reason", type(exc).__name__),
                )
            else:
                portfolio.apply_fill(first_fill)
                first_leg = _filled_leg(1, first_fill)
        return PendingSequentialExecution(
            intents=ordered,
            first_books=dict(first_books),
            fee_schedules=dict(fee_schedules),
            first_now_utc=first_now_utc,
            first_leg=first_leg,
            first_fill=first_fill,
        )

    def complete(
        self,
        *,
        pending: PendingSequentialExecution,
        second_books: Mapping[str, PublicOrderBook],
        second_now_utc: str,
        portfolio: PaperPortfolio,
        window_end_epoch: float | None = None,
        second_epoch: float | None = None,
        require_distinct_snapshot: bool = False,
        second_failure_reason: str | None = None,
    ) -> SequentialExecutionResult:
        first_intent, second_intent = pending.intents
        first_schedule = pending.fee_schedules.get(first_intent.token_id)
        second_schedule = pending.fee_schedules.get(second_intent.token_id)
        first_fill = pending.first_fill
        second_fill: PaperFill | None = None
        pair_skew: float | None = None
        repricing: float | None = None
        first_leg = pending.first_leg
        if first_fill is None:
            second_leg = _failed_leg(2, second_intent, second_schedule, "FIRST_LEG_NOT_EXECUTED")
        elif second_failure_reason is not None:
            second_leg = _failed_leg(2, second_intent, second_schedule, second_failure_reason)
        elif window_end_epoch is not None and second_epoch is not None and second_epoch >= window_end_epoch:
            second_leg = _failed_leg(2, second_intent, second_schedule, "WINDOW_CLOSES_BETWEEN_LEGS")
        elif second_schedule is None:
            second_leg = _failed_leg(2, second_intent, None, "FEE_MODEL_UNVERIFIED")
        else:
            try:
                first_leg_book = pending.first_books[first_intent.token_id]
                first_second_book = pending.first_books[second_intent.token_id]
                second_book = second_books[second_intent.token_id]
                if second_book.source_age_ms(second_now_utc) > self.broker.book_staleness_seconds * 1000.0:
                    raise BrokerRejection("STALE_DATA")
                if require_distinct_snapshot:
                    if second_book.source_evidence_hash == first_second_book.source_evidence_hash:
                        raise BrokerRejection("SECOND_SNAPSHOT_NOT_DISTINCT")
                    source_later = self._timestamp_epoch(second_book.source_timestamp_utc) > self._timestamp_epoch(first_second_book.source_timestamp_utc)
                    receive_later = self._timestamp_epoch(second_book.received_timestamp_utc) > self._timestamp_epoch(first_second_book.received_timestamp_utc)
                    if not (source_later or receive_later):
                        raise BrokerRejection("SECOND_SNAPSHOT_NOT_LATER")
                pair_skew = validate_pair_skew(
                    first_leg_book,
                    second_book,
                    maximum_skew_ms=self.maximum_pair_skew_ms,
                )
                second_fill = self.broker.simulate(
                    intent=second_intent,
                    book=second_book,
                    now_utc=second_now_utc,
                    fee_schedule=second_schedule,
                    liquidity_classification="TAKER",
                )
            except (KeyError, BrokerRejection, FeeModelError, ValueError) as exc:
                second_leg = _failed_leg(
                    2,
                    second_intent,
                    second_schedule,
                    getattr(exc, "reason", type(exc).__name__),
                )
            else:
                portfolio.apply_fill(second_fill)
                second_leg = _filled_leg(2, second_fill)
                first_best = first_second_book.asks[0][0] if first_second_book.asks else None
                if first_best is not None:
                    repricing = round(second_fill.fill_price - first_best, 12)
        fills = tuple(fill for fill in (first_fill, second_fill) if fill is not None)
        first_qty = first_fill.filled_shares if first_fill else 0.0
        second_qty = second_fill.filled_shares if second_fill else 0.0
        imbalance = round(first_qty - second_qty, 12)
        if first_fill and second_fill:
            status = "BOTH_LEGS_FILL" if not first_fill.partial and not second_fill.partial else "PARTIAL_COMPLETION"
            unwind = "NOT_REQUIRED" if abs(imbalance) <= 1e-12 else "PAPER_UNWIND_PENDING"
        elif first_fill:
            status = "SECOND_LEG_FAILED_AFTER_FIRST_FILL"
            unwind = "PAPER_UNWIND_PENDING"
        else:
            status = "NO_LEG_FILLED"
            unwind = "NOT_REQUIRED"
        payload = {
            "intents": [item.deterministic_id for item in pending.intents],
            "first": first_leg.to_dict(),
            "second": second_leg.to_dict(),
            "delay_ms": self.configured_transport_delay_ms,
            "pair_skew_ms": pair_skew,
            "status": status,
        }
        protocol_enabled = first_schedule.itode if first_schedule else None
        return SequentialExecutionResult(
            execution_id=deterministic_id("sequential_execution", payload),
            execution_model_version=EXECUTION_MODEL_VERSION,
            first_leg_token_id=first_intent.token_id,
            second_leg_token_id=second_intent.token_id,
            configured_transport_delay_ms=self.configured_transport_delay_ms,
            protocol_taker_delay_enabled=protocol_enabled,
            protocol_taker_delay_ms=250 if protocol_enabled else 0,
            pair_skew_ms=pair_skew,
            first_leg=first_leg,
            second_leg=second_leg,
            leg_imbalance_shares=imbalance,
            second_leg_repricing=repricing,
            completion_status=status,
            paper_unwind_outcome=unwind,
            fills=fills,
        )

    def execute(
        self,
        *,
        intents: tuple[PaperOrderIntent, PaperOrderIntent],
        first_books: Mapping[str, PublicOrderBook],
        second_books: Mapping[str, PublicOrderBook],
        fee_schedules: Mapping[str, FeeSchedule],
        first_now_utc: str,
        second_now_utc: str,
        portfolio: PaperPortfolio,
        window_end_epoch: float | None = None,
        second_epoch: float | None = None,
    ) -> SequentialExecutionResult:
        pending = self.begin(
            intents=intents,
            first_books=first_books,
            fee_schedules=fee_schedules,
            first_now_utc=first_now_utc,
            portfolio=portfolio,
        )
        return self.complete(
            pending=pending,
            second_books=second_books,
            second_now_utc=second_now_utc,
            portfolio=portfolio,
            window_end_epoch=window_end_epoch,
            second_epoch=second_epoch,
        )
