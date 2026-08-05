"""Deterministic simulated broker using public read-only order-book snapshots."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

from .fees import FeeSchedule, calculate_fee
from .models import PaperFill, PaperOrderIntent, deterministic_id

SOURCE_TIME_CONTRACT_VERSION = "SENEX_SOURCE_TIME_V2"


class BrokerRejection(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_source_timestamp(value: Any) -> str:
    if value is None or isinstance(value, bool):
        raise BrokerRejection("MISSING_SOURCE_TIMESTAMP")
    parsed: datetime
    if isinstance(value, (int, float, Decimal)) or (isinstance(value, str) and value.strip().replace(".", "", 1).isdigit()):
        try:
            numeric = Decimal(str(value).strip())
        except Exception as exc:
            raise BrokerRejection("MALFORMED_SOURCE_TIMESTAMP") from exc
        # Millisecond epoch values are currently 13 digits; tolerate larger
        # microsecond values by reducing until seconds are plausible.
        while abs(numeric) >= Decimal("100000000000"):
            numeric /= Decimal("1000")
        try:
            parsed = datetime.fromtimestamp(float(numeric), tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise BrokerRejection("MALFORMED_SOURCE_TIMESTAMP") from exc
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise BrokerRejection("MALFORMED_SOURCE_TIMESTAMP") from exc
        if parsed.tzinfo is None:
            raise BrokerRejection("MALFORMED_SOURCE_TIMESTAMP")
    else:
        raise BrokerRejection("MALFORMED_SOURCE_TIMESTAMP")
    return _iso(parsed)


@dataclass(frozen=True)
class PublicOrderBook:
    market_id: str
    token_id: str
    timestamp_utc: str  # source timestamp; retained for compatibility
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    source_evidence_hash: str
    received_timestamp_utc: str = "UNKNOWN"
    source_timestamp_provenance: str = "SOURCE_PAYLOAD"
    source_time_contract_version: str = SOURCE_TIME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.received_timestamp_utc == "UNKNOWN":
            object.__setattr__(self, "received_timestamp_utc", self.timestamp_utc)

    @property
    def source_timestamp_utc(self) -> str:
        return self.timestamp_utc

    @classmethod
    def from_payload(
        cls,
        *,
        market_id: str,
        token_id: str,
        timestamp_utc: str,
        payload: dict,
        source_evidence_hash: str,
        fixture_timestamp_utc: str | None = None,
    ) -> "PublicOrderBook":
        returned = str(payload.get("asset_id") or payload.get("assetId") or token_id)
        if returned != str(token_id):
            raise BrokerRejection("UNBOUND_BOOK")
        raw_source_time = payload.get("timestamp")
        provenance = "SOURCE_PAYLOAD"
        if raw_source_time is None:
            if fixture_timestamp_utc is None:
                raise BrokerRejection("MISSING_SOURCE_TIMESTAMP")
            raw_source_time = fixture_timestamp_utc
            provenance = "FIXTURE_EXPLICIT"
        source_timestamp = parse_source_timestamp(raw_source_time)
        received_timestamp = parse_source_timestamp(timestamp_utc)

        def levels(values: Iterable[dict], *, reverse: bool) -> tuple[tuple[float, float], ...]:
            clean: list[tuple[float, float]] = []
            for value in values:
                try:
                    price = float(value["price"])
                    size = float(value["size"])
                except (KeyError, TypeError, ValueError):
                    continue
                if 0.0 < price < 1.0 and size > 0.0:
                    clean.append((price, size))
            return tuple(sorted(clean, key=lambda item: item[0], reverse=reverse))

        return cls(
            market_id=str(market_id),
            token_id=str(token_id),
            timestamp_utc=source_timestamp,
            received_timestamp_utc=received_timestamp,
            bids=levels(payload.get("bids", []), reverse=True),
            asks=levels(payload.get("asks", []), reverse=False),
            source_evidence_hash=source_evidence_hash,
            source_timestamp_provenance=provenance,
        )

    def source_age_ms(self, now_utc: str | None = None) -> float:
        received = datetime.fromisoformat((now_utc or self.received_timestamp_utc).replace("Z", "+00:00"))
        source = datetime.fromisoformat(self.timestamp_utc.replace("Z", "+00:00"))
        if received.tzinfo is None or source.tzinfo is None:
            raise BrokerRejection("INVALID_BOOK_TIMESTAMP")
        return (received - source).total_seconds() * 1000.0

    def age_seconds(self, now_utc: str) -> float:
        return self.source_age_ms(now_utc) / 1000.0

    def validate(
        self,
        *,
        now_utc: str,
        staleness_seconds: float,
        future_tolerance_seconds: float = 1.0,
        allow_fixture: bool = True,
    ) -> None:
        if self.source_timestamp_provenance == "FIXTURE_EXPLICIT" and not allow_fixture:
            raise BrokerRejection("FIXTURE_TIMESTAMP_NOT_OBSERVED")
        age = self.source_age_ms(now_utc)
        if age < -max(0.0, future_tolerance_seconds) * 1000.0:
            raise BrokerRejection("FUTURE_SOURCE_TIMESTAMP")
        if age > staleness_seconds * 1000.0:
            raise BrokerRejection("STALE_DATA")
        if not self.bids or not self.asks:
            raise BrokerRejection("EMPTY_LIQUIDITY")
        if self.bids[0][0] >= self.asks[0][0]:
            raise BrokerRejection("INVALID_BOOK")


def validate_pair_skew(first: PublicOrderBook, second: PublicOrderBook, *, maximum_skew_ms: float) -> float:
    one = datetime.fromisoformat(first.source_timestamp_utc.replace("Z", "+00:00"))
    two = datetime.fromisoformat(second.source_timestamp_utc.replace("Z", "+00:00"))
    skew = abs((one - two).total_seconds() * 1000.0)
    if skew > maximum_skew_ms:
        raise BrokerRejection("PAIR_TIMESTAMP_SKEW_EXCEEDED")
    return skew


@dataclass(frozen=True)
class SimulatedBroker:
    fee_bps: float = 0.0  # compatibility-only deterministic fixture fallback
    slippage_bps_floor: float = 5.0
    book_staleness_seconds: float = 15.0
    future_tolerance_seconds: float = 1.0

    @staticmethod
    def _walk(levels: Sequence[tuple[float, float]], requested: float) -> tuple[float, float, float]:
        remaining = max(0.0, float(requested))
        quantity = 0.0
        gross = 0.0
        available_total = 0.0
        for price, available in levels:
            available_total += available
            if remaining <= 1e-12:
                break
            fill = min(remaining, available)
            quantity += fill
            gross += fill * price
            remaining -= fill
        average = gross / quantity if quantity > 0 else 0.0
        return quantity, average, available_total

    def simulate(
        self,
        *,
        intent: PaperOrderIntent,
        book: PublicOrderBook,
        now_utc: str,
        fee_schedule: FeeSchedule | None = None,
        liquidity_classification: str = "TAKER",
    ) -> PaperFill:
        if intent.token_id != book.token_id or intent.market_id != book.market_id:
            raise BrokerRejection("UNBOUND_BOOK")
        book.validate(
            now_utc=now_utc,
            staleness_seconds=self.book_staleness_seconds,
            future_tolerance_seconds=self.future_tolerance_seconds,
        )
        side = intent.side.upper()
        if side not in {"BUY", "SELL"}:
            raise BrokerRejection("INVALID_SIDE")
        levels = book.asks if side == "BUY" else book.bids
        filled, average, available_total = self._walk(levels, intent.requested_shares)
        if filled <= 0:
            raise BrokerRejection("EMPTY_LIQUIDITY")
        slip = max(0.0, self.slippage_bps_floor) / 10_000.0
        price = average * (1.0 + slip if side == "BUY" else 1.0 - slip)
        price = min(0.999999, max(0.000001, price))
        gross = filled * price
        if gross > intent.max_notional_usd + 1e-9:
            affordable = intent.max_notional_usd / price if price > 0 else 0.0
            filled = min(filled, affordable)
            gross = filled * price
        if filled <= 0:
            raise BrokerRejection("EXPOSURE_LIMIT")
        if fee_schedule is None:
            # Explicit fixture compatibility only. Observed execution must pass
            # a public market-info bound schedule.
            fee_schedule = FeeSchedule.deterministic_fixture(
                condition_id=intent.market_id,
                fee_rate=format(max(0.0, self.fee_bps) / 10_000.0, "f"),
                enabled=self.fee_bps > 0,
                itode=False,
            )
        fee_result = calculate_fee(
            shares=Decimal(str(filled)),
            price=Decimal(str(price)),
            schedule=fee_schedule,
            liquidity_classification=liquidity_classification,
        )
        fee = float(fee_result.fee_usd)
        payload = {
            "order_intent_id": intent.deterministic_id,
            "token_id": intent.token_id,
            "side": side,
            "filled_shares": round(filled, 12),
            "fill_price": round(price, 12),
            "book_timestamp_utc": book.timestamp_utc,
            "fee_schedule_hash": fee_schedule.raw_schedule_hash,
        }
        return PaperFill(
            schema_version=intent.schema_version,
            timestamp_utc=now_utc,
            code_sha=intent.code_sha,
            config_sha=intent.config_sha,
            source_evidence_hash=book.source_evidence_hash,
            deterministic_id=deterministic_id("fill", payload),
            provenance="PUBLIC_BOOK_SIMULATION_ONLY",
            order_intent_id=intent.deterministic_id,
            market_id=intent.market_id,
            token_id=intent.token_id,
            outcome=intent.outcome,
            side=side,
            requested_shares=intent.requested_shares,
            filled_shares=round(filled, 12),
            fill_price=round(price, 12),
            observed_available_size=round(available_total, 12),
            gross_notional_usd=round(gross, 12),
            fee_usd=round(fee, 12),
            partial=filled + 1e-9 < intent.requested_shares,
            book_timestamp_utc=book.timestamp_utc,
            received_timestamp_utc=book.received_timestamp_utc,
            source_age_ms=round(book.source_age_ms(now_utc), 6),
            fee_model_version=fee_result.model_version,
            fee_schedule_hash=fee_result.schedule_hash,
            liquidity_classification=fee_result.liquidity_classification,
            condition_id=intent.condition_id,
        )
