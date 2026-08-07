from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from polymarket.signal_lab.contracts import RawEvent, sha256_json


@dataclass(frozen=True)
class PublicSchemaProjection:
    """Backward-compatible projection of public Polymarket market data.

    This is deliberately an additive view over RawEvent. It never replaces or
    mutates the authoritative raw payload and never requires authenticated APIs.
    """

    market_id: str
    token_id: str | None
    event_type: str
    source: str
    source_cursor: str | int | None
    event_time: str
    received_time: str
    tick_size: float | None
    min_order_size: float | None
    neg_risk: bool | None
    book_hash: str | None
    best_bid: float | None
    best_ask: float | None
    schema_version: str = "senex-public-schema-crosscheck-v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "token_id": self.token_id,
            "event_type": self.event_type,
            "source": self.source,
            "source_cursor": self.source_cursor,
            "event_time": self.event_time,
            "received_time": self.received_time,
            "tick_size": self.tick_size,
            "min_order_size": self.min_order_size,
            "neg_risk": self.neg_risk,
            "book_hash": self.book_hash,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "schema_version": self.schema_version,
        }


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _level_price(level: Any) -> float | None:
    if isinstance(level, Mapping):
        return _number(level.get("price"))
    if isinstance(level, (list, tuple)) and level:
        return _number(level[0])
    return None


def normalize_public_event(event: RawEvent) -> PublicSchemaProjection:
    """Project known official public fields without manufacturing provenance."""

    payload = dict(event.payload)
    bids = payload.get("bids") or []
    asks = payload.get("asks") or []
    best_bid = max((_level_price(x) for x in bids), default=None, key=lambda x: float("-inf") if x is None else x)
    best_ask = min((_level_price(x) for x in asks), default=None, key=lambda x: float("inf") if x is None else x)
    if best_bid is None:
        best_bid = _number(payload.get("best_bid"))
    if best_ask is None:
        best_ask = _number(payload.get("best_ask"))
    raw_book_hash = payload.get("hash") or payload.get("book_hash")
    return PublicSchemaProjection(
        market_id=event.market_id,
        token_id=event.token_id,
        event_type=event.event_type,
        source=event.source,
        source_cursor=event.sequence_or_source_cursor,
        event_time=event.event_time,
        received_time=event.received_time,
        tick_size=_number(payload.get("tick_size") or payload.get("tickSize")),
        min_order_size=_number(payload.get("min_order_size") or payload.get("minOrderSize")),
        neg_risk=payload.get("neg_risk") if isinstance(payload.get("neg_risk"), bool) else payload.get("negRisk") if isinstance(payload.get("negRisk"), bool) else None,
        book_hash=str(raw_book_hash) if raw_book_hash is not None else None,
        best_bid=best_bid,
        best_ask=best_ask,
    )


def projection_hash(event: RawEvent) -> str:
    return sha256_json(normalize_public_event(event).to_dict())
