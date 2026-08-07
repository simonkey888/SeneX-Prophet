from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from polymarket.signal_lab.contracts import RawEvent, parse_time, sha256_json
from polymarket.signal_lab.store import PointInTimeStore, RawAppendOnlyChain


@dataclass(frozen=True)
class ReplayConfig:
    as_of_event_time: str
    explicit_clock: str
    seed: int = 0
    event_schema_version: str = "senex-signal-lab-event-v1"
    replay_schema_version: str = "senex-replay-contract-v2"
    external_live_reads: bool = False

    def __post_init__(self) -> None:
        parse_time(self.as_of_event_time)
        parse_time(self.explicit_clock)
        if self.external_live_reads:
            raise ValueError("EXTERNAL_LIVE_READ_DURING_REPLAY_FORBIDDEN")


def _cursor_key(value: str | int | None) -> tuple[int, str]:
    if value is None:
        return (2, "")
    if isinstance(value, int):
        return (0, f"{value:030d}")
    text = str(value)
    if text.isdigit():
        return (0, f"{int(text):030d}")
    return (1, text)


def strict_event_order(events: Iterable[RawEvent]) -> list[RawEvent]:
    return sorted(
        events,
        key=lambda event: (
            event.event_dt,
            event.received_dt,
            _cursor_key(event.sequence_or_source_cursor),
            event.source,
            event.event_id,
        ),
    )


def deterministic_replay(events: Iterable[RawEvent], config: ReplayConfig) -> dict[str, Any]:
    """Replay a finite captured event set with no network or wall-clock input."""

    cutoff = parse_time(config.as_of_event_time)
    ordered = strict_event_order(events)
    visible = [event for event in ordered if event.event_dt <= cutoff]
    if any(event.schema_version != config.event_schema_version for event in visible):
        raise ValueError("EVENT_SCHEMA_VERSION_MISMATCH")

    rng = random.Random(config.seed)
    deterministic_nonce = rng.getrandbits(64)
    chain = RawAppendOnlyChain()
    store = PointInTimeStore(chain)
    store.ingest_many(visible)
    state = {
        "replay_schema_version": config.replay_schema_version,
        "event_schema_version": config.event_schema_version,
        "as_of_event_time": config.as_of_event_time,
        "explicit_clock": config.explicit_clock,
        "seed": config.seed,
        "deterministic_nonce": deterministic_nonce,
        "external_live_reads": False,
        "strict_event_ordering": True,
        "point_in_time_cutoff": True,
        "event_count": len(visible),
        "event_ids": [event.event_id for event in visible],
        "chain_tip_hash": chain.tip_hash,
        "chain_replay_hash": chain.replay_hash(),
        "chain_verified": chain.verify(),
    }
    state["output_hash"] = sha256_json(state)
    return state


def same_input_same_output_hash(events: Iterable[RawEvent], config: ReplayConfig) -> bool:
    material = list(events)
    return deterministic_replay(material, config)["output_hash"] == deterministic_replay(material, config)["output_hash"]
