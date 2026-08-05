"""Crash-consistent append-only recovery for sequential paper execution.

The recovery journal is written before either economic fill is applied.  On
restart it replays the portfolio journal, verifies the recovery hash chain,
finishes any prepared durable boundary idempotently, and reconstructs pending
or terminal orchestration without refetching or reapplying the first leg.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .broker import PublicOrderBook, SimulatedBroker
from .engine import PendingPaperExecution
from .execution import (
    PendingSequentialExecution,
    SequentialExecutionResult,
    SequentialLegRecord,
    SequentialPaperExecutor,
)
from .fees import FeeSchedule
from .models import (
    PaperDecision,
    PaperFill,
    PaperOrderIntent,
    PaperRiskDecision,
    canonical_json_bytes,
    sha256_json,
)
from .portfolio import PaperPortfolio

RECOVERY_SCHEMA_VERSION = "senex-sequential-recovery-v1"
FIRST_LEG_PREPARED = "FIRST_LEG_PREPARED"
FIRST_LEG_COMMITTED = "FIRST_LEG_COMMITTED"
SECOND_LEG_PREPARED = "SECOND_LEG_PREPARED"
WINDOW_TERMINAL = "WINDOW_TERMINAL"
PROCESS_START = "PROCESS_START"


class SequentialRecoveryError(RuntimeError):
    """Fail-closed recovery error."""


def _hash_record(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(dict(value))).hexdigest()


def execution_key(*, window_slug: str, decision_id: str) -> str:
    return "sequential_window_" + sha256_json({"window_slug": window_slug, "decision_id": decision_id})[:32]


def _book_to_dict(book: PublicOrderBook) -> dict[str, Any]:
    return {
        "market_id": book.market_id,
        "token_id": book.token_id,
        "timestamp_utc": book.timestamp_utc,
        "received_timestamp_utc": book.received_timestamp_utc,
        "bids": [list(level) for level in book.bids],
        "asks": [list(level) for level in book.asks],
        "source_evidence_hash": book.source_evidence_hash,
        "source_timestamp_provenance": book.source_timestamp_provenance,
        "source_time_contract_version": book.source_time_contract_version,
    }


def _book_from_dict(value: Mapping[str, Any]) -> PublicOrderBook:
    return PublicOrderBook(
        market_id=str(value["market_id"]),
        token_id=str(value["token_id"]),
        timestamp_utc=str(value["timestamp_utc"]),
        received_timestamp_utc=str(value["received_timestamp_utc"]),
        bids=tuple((float(p), float(s)) for p, s in value.get("bids", [])),
        asks=tuple((float(p), float(s)) for p, s in value.get("asks", [])),
        source_evidence_hash=str(value["source_evidence_hash"]),
        source_timestamp_provenance=str(value.get("source_timestamp_provenance", "SOURCE_PAYLOAD")),
        source_time_contract_version=str(value.get("source_time_contract_version", "SENEX_SOURCE_TIME_V2")),
    )


def _schedule_to_dict(schedule: FeeSchedule) -> dict[str, Any]:
    return schedule.to_evidence()


def _schedule_from_dict(value: Mapping[str, Any]) -> FeeSchedule:
    return FeeSchedule.from_market_info(
        condition_id=str(value["condition_id"]),
        payload={"fd": dict(value["raw_schedule"]), "itode": value.get("itode")},
        source_evidence_hash=str(value["source_evidence_hash"]),
    )


def _decision_from_dict(value: Mapping[str, Any]) -> PaperDecision:
    payload = dict(value)
    payload["token_ids"] = tuple(payload.get("token_ids") or ())
    payload["reason_codes"] = tuple(payload.get("reason_codes") or ())
    return PaperDecision(**payload)


def _intent_from_dict(value: Mapping[str, Any]) -> PaperOrderIntent:
    return PaperOrderIntent(**dict(value))


def _risk_from_dict(value: Mapping[str, Any]) -> PaperRiskDecision:
    payload = dict(value)
    payload["reason_codes"] = tuple(payload.get("reason_codes") or ())
    return PaperRiskDecision(**payload)


def _fill_from_dict(value: Mapping[str, Any] | None) -> PaperFill | None:
    return None if value is None else PaperFill(**dict(value))


def _result_from_dict(value: Mapping[str, Any]) -> SequentialExecutionResult:
    payload = dict(value)
    payload["first_leg"] = SequentialLegRecord(**dict(payload["first_leg"]))
    payload["second_leg"] = SequentialLegRecord(**dict(payload["second_leg"]))
    payload["fills"] = tuple(PaperFill(**dict(item)) for item in payload.get("fills", []))
    return SequentialExecutionResult(**payload)


def serialize_pending(pending: PendingPaperExecution) -> dict[str, Any]:
    return {
        "decision": pending.decision.to_dict(),
        "intents": [intent.to_dict() for intent in pending.intents],
        "risk_decision": pending.risk_decision.to_dict(),
        "configured_transport_delay_ms": pending.executor.configured_transport_delay_ms,
        "maximum_pair_skew_ms": pending.executor.maximum_pair_skew_ms,
        "pending_execution": {
            "first_books": {token: _book_to_dict(book) for token, book in pending.pending_execution.first_books.items()},
            "fee_schedules": {token: _schedule_to_dict(schedule) for token, schedule in pending.pending_execution.fee_schedules.items()},
            "first_now_utc": pending.pending_execution.first_now_utc,
            "first_leg": pending.pending_execution.first_leg.to_dict(),
            "first_fill": None if pending.pending_execution.first_fill is None else pending.pending_execution.first_fill.to_dict(),
        },
    }


def restore_pending(
    value: Mapping[str, Any],
    *,
    broker: SimulatedBroker,
    portfolio: PaperPortfolio,
) -> PendingPaperExecution:
    decision = _decision_from_dict(value["decision"])
    intents = tuple(_intent_from_dict(item) for item in value["intents"])
    if len(intents) != 2:
        raise SequentialRecoveryError("RECOVERY_INTENT_CARDINALITY_INVALID")
    risk = _risk_from_dict(value["risk_decision"])
    raw = value["pending_execution"]
    first_books = {str(token): _book_from_dict(book) for token, book in raw["first_books"].items()}
    schedules = {str(token): _schedule_from_dict(schedule) for token, schedule in raw["fee_schedules"].items()}
    first_fill = _fill_from_dict(raw.get("first_fill"))
    pending_execution = PendingSequentialExecution(
        intents=intents,  # type: ignore[arg-type]
        first_books=first_books,
        fee_schedules=schedules,
        first_now_utc=str(raw["first_now_utc"]),
        first_leg=SequentialLegRecord(**dict(raw["first_leg"])),
        first_fill=first_fill,
    )
    executor = SequentialPaperExecutor(
        broker=broker,
        configured_transport_delay_ms=int(value["configured_transport_delay_ms"]),
        maximum_pair_skew_ms=float(value["maximum_pair_skew_ms"]),
    )
    return PendingPaperExecution(
        decision=decision,
        intents=intents,  # type: ignore[arg-type]
        risk_decision=risk,
        executor=executor,
        pending_execution=pending_execution,
    )


@dataclass
class RecoveredSequentialState:
    pending: dict[str, PendingPaperExecution]
    pending_metadata: dict[str, dict[str, Any]]
    terminal_windows: set[str]
    decisions: list[dict[str, Any]]
    orders: list[dict[str, Any]]
    risk_decisions: list[dict[str, Any]]
    sequential_results: list[dict[str, Any]]
    runner_records: list[dict[str, Any]]
    event_counts: dict[str, int]
    restart_count: int


class SequentialRecoveryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read_verified(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        previous = "GENESIS"
        for index, line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SequentialRecoveryError("RECOVERY_JOURNAL_MALFORMED_JSON") from exc
            material = {key: value for key, value in record.items() if key != "record_hash"}
            if material.get("schema_version") != RECOVERY_SCHEMA_VERSION:
                raise SequentialRecoveryError("RECOVERY_SCHEMA_MISMATCH")
            if material.get("sequence") != index or material.get("previous_hash") != previous:
                raise SequentialRecoveryError("RECOVERY_CHAIN_DISCONTINUITY")
            expected = _hash_record(material)
            if record.get("record_hash") != expected:
                raise SequentialRecoveryError("RECOVERY_HASH_MISMATCH")
            previous = expected
            records.append(record)
        return records

    def append(self, *, event_type: str, execution_key_value: str, window_slug: str, payload: Mapping[str, Any]) -> bool:
        records = self._read_verified()
        payload_dict = dict(payload)
        payload_hash = sha256_json(payload_dict)
        for record in records:
            if record.get("event_type") == event_type and record.get("execution_key") == execution_key_value:
                if record.get("payload_hash") == payload_hash:
                    return False
                raise SequentialRecoveryError("RECOVERY_CONFLICTING_DUPLICATE_EVENT")
        material = {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "sequence": len(records),
            "previous_hash": records[-1]["record_hash"] if records else "GENESIS",
            "event_type": event_type,
            "execution_key": execution_key_value,
            "window_slug": window_slug,
            "payload_hash": payload_hash,
            "payload": payload_dict,
        }
        record = dict(material)
        record["record_hash"] = _hash_record(material)
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            os.write(fd, canonical_json_bytes(record))
            os.fsync(fd)
        finally:
            os.close(fd)
        dir_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return True

    def record_process_start(self, *, timestamp_utc: str, code_sha: str, config_sha: str) -> None:
        records = self._read_verified()
        key = f"process_start_{len(records):08d}"
        self.append(
            event_type=PROCESS_START,
            execution_key_value=key,
            window_slug="__PROCESS__",
            payload={"timestamp_utc": timestamp_utc, "code_sha": code_sha, "config_sha": config_sha},
        )

    def prepare_first(self, *, window_slug: str, pending: PendingPaperExecution, metadata: Mapping[str, Any]) -> str:
        key = execution_key(window_slug=window_slug, decision_id=pending.decision.deterministic_id)
        self.append(
            event_type=FIRST_LEG_PREPARED,
            execution_key_value=key,
            window_slug=window_slug,
            payload={"pending": serialize_pending(pending), "metadata": dict(metadata)},
        )
        return key

    def commit_first(self, *, execution_key_value: str, window_slug: str, first_fill_id: str) -> None:
        self.append(
            event_type=FIRST_LEG_COMMITTED,
            execution_key_value=execution_key_value,
            window_slug=window_slug,
            payload={"first_fill_id": first_fill_id},
        )

    def prepare_second(
        self,
        *,
        execution_key_value: str,
        window_slug: str,
        result: SequentialExecutionResult,
        runner_record: Mapping[str, Any],
    ) -> None:
        self.append(
            event_type=SECOND_LEG_PREPARED,
            execution_key_value=execution_key_value,
            window_slug=window_slug,
            payload={"sequential_result": result.to_dict(), "runner_record": dict(runner_record)},
        )

    def commit_terminal(self, *, execution_key_value: str, window_slug: str) -> None:
        self.append(
            event_type=WINDOW_TERMINAL,
            execution_key_value=execution_key_value,
            window_slug=window_slug,
            payload={"terminal": True},
        )

    def _states(self) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        records = self._read_verified()
        states: dict[str, dict[str, Any]] = {}
        window_keys: dict[str, str] = {}
        for record in records:
            event_type = str(record["event_type"])
            if event_type == PROCESS_START:
                continue
            key = str(record["execution_key"])
            window = str(record["window_slug"])
            if window in window_keys and window_keys[window] != key:
                raise SequentialRecoveryError("RECOVERY_WINDOW_IDENTITY_CONFLICT")
            window_keys[window] = key
            state = states.setdefault(key, {"window_slug": window})
            if event_type in state:
                raise SequentialRecoveryError("RECOVERY_DUPLICATE_STATE_EVENT")
            state[event_type] = dict(record["payload"])
            if event_type == FIRST_LEG_COMMITTED and FIRST_LEG_PREPARED not in state:
                raise SequentialRecoveryError("RECOVERY_FIRST_COMMIT_WITHOUT_PREPARE")
            if event_type == SECOND_LEG_PREPARED and FIRST_LEG_COMMITTED not in state:
                raise SequentialRecoveryError("RECOVERY_SECOND_PREPARE_WITHOUT_FIRST_COMMIT")
            if event_type == WINDOW_TERMINAL and SECOND_LEG_PREPARED not in state:
                raise SequentialRecoveryError("RECOVERY_TERMINAL_WITHOUT_SECOND_PREPARE")
        return states, records

    def recover(self, *, portfolio: PaperPortfolio, broker: SimulatedBroker) -> RecoveredSequentialState:
        states, _ = self._states()
        repaired = False
        for key, state in states.items():
            window = str(state["window_slug"])
            first_payload = state.get(FIRST_LEG_PREPARED)
            if first_payload is None:
                raise SequentialRecoveryError("RECOVERY_MISSING_FIRST_PREPARE")
            pending = restore_pending(first_payload["pending"], broker=broker, portfolio=portfolio)
            first_fill = pending.pending_execution.first_fill
            if first_fill is None:
                raise SequentialRecoveryError("RECOVERY_FIRST_FILL_MISSING")
            if FIRST_LEG_COMMITTED not in state:
                portfolio.apply_fill(first_fill)
                self.commit_first(execution_key_value=key, window_slug=window, first_fill_id=first_fill.deterministic_id)
                repaired = True
            second_payload = state.get(SECOND_LEG_PREPARED)
            if second_payload is not None and WINDOW_TERMINAL not in state:
                result = _result_from_dict(second_payload["sequential_result"])
                for fill in result.fills:
                    portfolio.apply_fill(fill)
                self.commit_terminal(execution_key_value=key, window_slug=window)
                repaired = True
        if repaired:
            states, records = self._states()
        else:
            states, records = self._states()

        pending: dict[str, PendingPaperExecution] = {}
        pending_metadata: dict[str, dict[str, Any]] = {}
        terminal_windows: set[str] = set()
        decisions: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        risk_decisions: list[dict[str, Any]] = []
        sequential_results: list[dict[str, Any]] = []
        runner_records: list[dict[str, Any]] = []
        for key, state in states.items():
            window = str(state["window_slug"])
            first_payload = state[FIRST_LEG_PREPARED]
            restored = restore_pending(first_payload["pending"], broker=broker, portfolio=portfolio)
            decisions.append(restored.decision.to_dict())
            orders.extend(intent.to_dict() for intent in restored.intents)
            risk_decisions.append(restored.risk_decision.to_dict())
            if WINDOW_TERMINAL in state:
                terminal_windows.add(window)
                second_payload = state[SECOND_LEG_PREPARED]
                sequential_results.append(dict(second_payload["sequential_result"]))
                runner_records.append(dict(second_payload["runner_record"]))
            elif FIRST_LEG_COMMITTED in state:
                pending[window] = restored
                metadata = dict(first_payload["metadata"])
                metadata["execution_key"] = key
                pending_metadata[window] = metadata
        event_counts: dict[str, int] = {}
        for record in records:
            event = str(record["event_type"])
            event_counts[event] = event_counts.get(event, 0) + 1
        process_starts = event_counts.get(PROCESS_START, 0)
        return RecoveredSequentialState(
            pending=pending,
            pending_metadata=pending_metadata,
            terminal_windows=terminal_windows,
            decisions=decisions,
            orders=orders,
            risk_decisions=risk_decisions,
            sequential_results=sequential_results,
            runner_records=runner_records,
            event_counts=event_counts,
            restart_count=max(0, process_starts - 1),
        )

    def summary(self) -> dict[str, Any]:
        states, records = self._states()
        event_counts: dict[str, int] = {}
        for record in records:
            event = str(record["event_type"])
            event_counts[event] = event_counts.get(event, 0) + 1
        terminal = sorted(str(state["window_slug"]) for state in states.values() if WINDOW_TERMINAL in state)
        pending = sorted(str(state["window_slug"]) for state in states.values() if FIRST_LEG_COMMITTED in state and WINDOW_TERMINAL not in state)
        result_hashes = sorted(
            sha256_json(state[SECOND_LEG_PREPARED]["sequential_result"])
            for state in states.values()
            if SECOND_LEG_PREPARED in state
        )
        return {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "chain_verified": True,
            "event_counts": event_counts,
            "restart_count": max(0, event_counts.get(PROCESS_START, 0) - 1),
            "terminal_windows": terminal,
            "pending_windows": pending,
            "sequential_result_hashes": result_hashes,
            "record_count": len(records),
        }
