from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .contracts import RawEvent, canonical_json, parse_time


@dataclass(frozen=True)
class ChainEntry:
    sequence: int
    previous_hash: str
    event_hash: str
    entry_hash: str
    event: RawEvent

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "previous_hash": self.previous_hash,
            "event_hash": self.event_hash,
            "entry_hash": self.entry_hash,
            "event": self.event.to_dict(),
        }


class RawAppendOnlyChain:
    """Small compatible extension layer for point-in-time research evidence.

    Existing H-011 raw evidence remains authoritative. This chain does not edit
    or rewrite it; it provides the same append-only/hash-continuity property for
    Signal Lab events and can be replayed deterministically.
    """

    GENESIS = "0" * 64

    def __init__(self, path: Path | None = None):
        self.path = None if path is None else Path(path)
        self._entries: list[ChainEntry] = []
        self._event_ids: set[str] = set()
        if self.path and self.path.exists():
            self._load()

    @staticmethod
    def _entry_digest(sequence: int, previous_hash: str, event: RawEvent) -> tuple[str, str]:
        event_hash = hashlib.sha256(canonical_json(event.to_dict()).encode("utf-8")).hexdigest()
        envelope = {
            "sequence": sequence,
            "previous_hash": previous_hash,
            "event_hash": event_hash,
        }
        return event_hash, hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()

    def append(self, event: RawEvent) -> ChainEntry:
        if event.event_id in self._event_ids:
            raise ValueError("DUPLICATE_EVENT_ID")
        sequence = len(self._entries) + 1
        previous = self._entries[-1].entry_hash if self._entries else self.GENESIS
        event_hash, entry_hash = self._entry_digest(sequence, previous, event)
        entry = ChainEntry(sequence, previous, event_hash, entry_hash, event)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = canonical_json(entry.to_dict()) + "\n"
            fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                os.write(fd, payload.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        self._entries.append(entry)
        self._event_ids.add(event.event_id)
        return entry

    def _load(self) -> None:
        previous = self.GENESIS
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            event = RawEvent(**raw["event"])
            sequence = len(self._entries) + 1
            event_hash, entry_hash = self._entry_digest(sequence, previous, event)
            if raw.get("sequence") != sequence:
                raise ValueError("RAW_CHAIN_SEQUENCE_GAP")
            if raw.get("previous_hash") != previous:
                raise ValueError("RAW_CHAIN_PREVIOUS_HASH_MISMATCH")
            if raw.get("event_hash") != event_hash or raw.get("entry_hash") != entry_hash:
                raise ValueError("RAW_CHAIN_HASH_MISMATCH")
            self._entries.append(ChainEntry(sequence, previous, event_hash, entry_hash, event))
            self._event_ids.add(event.event_id)
            previous = entry_hash

    @property
    def entries(self) -> tuple[ChainEntry, ...]:
        return tuple(self._entries)

    @property
    def tip_hash(self) -> str:
        return self._entries[-1].entry_hash if self._entries else self.GENESIS

    def verify(self) -> bool:
        previous = self.GENESIS
        seen: set[str] = set()
        for sequence, entry in enumerate(self._entries, 1):
            if entry.sequence != sequence or entry.previous_hash != previous:
                return False
            if entry.event.event_id in seen:
                return False
            event_hash, entry_hash = self._entry_digest(sequence, previous, entry.event)
            if entry.event_hash != event_hash or entry.entry_hash != entry_hash:
                return False
            seen.add(entry.event.event_id)
            previous = entry_hash
        return True

    def replay_hash(self) -> str:
        canonical = [entry.to_dict() for entry in self._entries]
        return hashlib.sha256(canonical_json(canonical).encode("utf-8")).hexdigest()


class PointInTimeStore:
    """Deterministic ASOF store. Future event_time is never visible at time t."""

    def __init__(self, chain: RawAppendOnlyChain | None = None):
        self.chain = chain or RawAppendOnlyChain()

    def ingest(self, event: RawEvent) -> ChainEntry:
        return self.chain.append(event)

    def ingest_many(self, events: Iterable[RawEvent]) -> None:
        for event in events:
            self.ingest(event)

    def events_as_of(
        self,
        as_of_event_time: str,
        *,
        market_id: str | None = None,
        token_id: str | None = None,
        event_types: set[str] | None = None,
    ) -> list[RawEvent]:
        cutoff = parse_time(as_of_event_time)
        visible = []
        for entry in self.chain.entries:
            event = entry.event
            if event.event_dt > cutoff:
                continue
            if market_id is not None and event.market_id != market_id:
                continue
            if token_id is not None and event.token_id != token_id:
                continue
            if event_types is not None and event.event_type not in event_types:
                continue
            visible.append(event)
        visible.sort(key=lambda item: (item.event_dt, item.received_dt, item.event_id))
        return visible

    def latest_by_type(self, as_of_event_time: str, market_id: str) -> dict[str, RawEvent]:
        latest: dict[str, RawEvent] = {}
        for event in self.events_as_of(as_of_event_time, market_id=market_id):
            latest[event.event_type] = event
        return latest

    def state_hash(self, as_of_event_time: str, market_id: str | None = None) -> str:
        payload = [event.to_dict() for event in self.events_as_of(as_of_event_time, market_id=market_id)]
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    def sequence_gaps(self) -> int:
        entries = self.chain.entries
        if not entries:
            return 0
        expected = list(range(1, len(entries) + 1))
        observed = [entry.sequence for entry in entries]
        return sum(1 for a, b in zip(expected, observed) if a != b)
