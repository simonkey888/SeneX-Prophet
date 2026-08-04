"""Deterministic simulated broker using public read-only order-book snapshots."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

from .models import PaperFill, PaperOrderIntent, deterministic_id


class BrokerRejection(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PublicOrderBook:
    market_id: str
    token_id: str
    timestamp_utc: str
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    source_evidence_hash: str

    @classmethod
    def from_payload(
        cls,
        *,
        market_id: str,
        token_id: str,
        timestamp_utc: str,
        payload: dict,
        source_evidence_hash: str,
    ) -> "PublicOrderBook":
        returned = str(payload.get("asset_id") or payload.get("assetId") or token_id)
        if returned != str(token_id):
            raise BrokerRejection("UNBOUND_BOOK")

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
            timestamp_utc=timestamp_utc,
            bids=levels(payload.get("bids", []), reverse=True),
            asks=levels(payload.get("asks", []), reverse=False),
            source_evidence_hash=source_evidence_hash,
        )

    def age_seconds(self, now_utc: str) -> float:
        now = datetime.fromisoformat(now_utc.replace("Z", "+00:00"))
        observed = datetime.fromisoformat(self.timestamp_utc.replace("Z", "+00:00"))
        if now.tzinfo is None or observed.tzinfo is None:
            raise BrokerRejection("INVALID_BOOK_TIMESTAMP")
        return max(0.0, (now - observed).total_seconds())

    def validate(self, *, now_utc: str, staleness_seconds: float) -> None:
        if self.age_seconds(now_utc) > staleness_seconds:
            raise BrokerRejection("STALE_DATA")
        if not self.bids or not self.asks:
            raise BrokerRejection("EMPTY_LIQUIDITY")
        if self.bids[0][0] >= self.asks[0][0]:
            raise BrokerRejection("INVALID_BOOK")


@dataclass(frozen=True)
class SimulatedBroker:
    fee_bps: float = 0.0
    slippage_bps_floor: float = 5.0
    book_staleness_seconds: float = 15.0

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

    def simulate(self, *, intent: PaperOrderIntent, book: PublicOrderBook, now_utc: str) -> PaperFill:
        if intent.token_id != book.token_id or intent.market_id != book.market_id:
            raise BrokerRejection("UNBOUND_BOOK")
        book.validate(now_utc=now_utc, staleness_seconds=self.book_staleness_seconds)
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
        fee = gross * max(0.0, self.fee_bps) / 10_000.0
        payload = {
            "order_intent_id": intent.deterministic_id,
            "token_id": intent.token_id,
            "side": side,
            "filled_shares": round(filled, 12),
            "fill_price": round(price, 12),
            "book_timestamp_utc": book.timestamp_utc,
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
        )
