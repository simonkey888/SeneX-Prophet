from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

EVENT_TYPES = frozenset({
    "MARKET_META",
    "BOOK_SNAPSHOT",
    "BOOK_DELTA",
    "TRADE",
    "BEST_BID_ASK",
    "LAST_TRADE_PRICE",
    "MARKET_CLOSE",
    "RESOLUTION_METADATA",
    "RELATED_MARKET_LINK",
    "DATA_HEALTH_EVENT",
})

FEATURE_IDS = tuple(f"F{i:02d}" for i in range(1, 16))


def parse_time(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RawEvent:
    event_id: str
    event_type: str
    market_id: str
    token_id: str | None
    event_time: str
    received_time: str
    sequence_or_source_cursor: str | int | None
    source: str
    payload_hash: str
    schema_version: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported event_type: {self.event_type}")
        if not self.event_id or not self.market_id or not self.source:
            raise ValueError("event_id, market_id and source are required")
        event_dt = parse_time(self.event_time)
        received_dt = parse_time(self.received_time)
        if event_dt > received_dt:
            raise ValueError("NEGATIVE_LAG_CANARY_DETECTED")
        observed = sha256_json(dict(self.payload))
        if observed != self.payload_hash:
            raise ValueError("PAYLOAD_HASH_MISMATCH")

    @property
    def event_dt(self) -> datetime:
        return parse_time(self.event_time)

    @property
    def received_dt(self) -> datetime:
        return parse_time(self.received_time)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["payload"] = dict(self.payload)
        return value

    @classmethod
    def build(
        cls,
        *,
        event_id: str,
        event_type: str,
        market_id: str,
        token_id: str | None,
        event_time: str,
        received_time: str,
        sequence_or_source_cursor: str | int | None,
        source: str,
        payload: Mapping[str, Any],
        schema_version: str = "senex-signal-lab-event-v1",
    ) -> "RawEvent":
        normalized = dict(payload)
        return cls(
            event_id=event_id,
            event_type=event_type,
            market_id=market_id,
            token_id=token_id,
            event_time=event_time,
            received_time=received_time,
            sequence_or_source_cursor=sequence_or_source_cursor,
            source=source,
            payload_hash=sha256_json(normalized),
            schema_version=schema_version,
            payload=normalized,
        )


@dataclass(frozen=True)
class FeatureValue:
    feature_id: str
    feature_version: str
    market_id: str
    value: float | None
    as_of_event_time: str
    input_event_max_time: str | None
    sample_support: int
    data_quality: str

    def __post_init__(self) -> None:
        if self.feature_id not in FEATURE_IDS:
            raise ValueError(f"unsupported feature_id: {self.feature_id}")
        if self.sample_support < 0:
            raise ValueError("sample_support must be non-negative")
        if self.input_event_max_time is not None:
            if parse_time(self.input_event_max_time) > parse_time(self.as_of_event_time):
                raise ValueError("FEATURE_FUTURE_INPUT_DETECTED")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
