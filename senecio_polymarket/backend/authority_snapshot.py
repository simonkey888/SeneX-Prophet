"""Atomic, shared authority snapshot for ORDER-070 public truth surfaces."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .authoritative_score import build_authoritative_score
from .runtime_provenance import runtime_provenance
from . import supabase_client


def normalize_symbol(value: str | None) -> str:
    return str(value or "").upper().replace("/", "").replace("-", "").strip()


@dataclass(frozen=True)
class AuthoritySnapshot:
    snapshot_id: str
    captured_at: str
    captured_monotonic: float
    symbol: str
    authority_history_complete: bool
    authority_history_rows: int
    exact_total_predictions: int | None
    exact_count_complete: bool
    score: dict[str, Any]
    live_gate: dict[str, Any]
    provenance: dict[str, Any]
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "captured_at": self.captured_at,
            "symbol": self.symbol,
            "authority_history_complete": self.authority_history_complete,
            "authority_history_rows": self.authority_history_rows,
            "exact_total_predictions": self.exact_total_predictions,
            "exact_count_complete": self.exact_count_complete,
            "score": copy.deepcopy(self.score),
            "live_gate": copy.deepcopy(self.live_gate),
            "provenance": copy.deepcopy(self.provenance),
            "failure_reason": self.failure_reason,
            "trade_mode": "PAPER",
            "orders_enabled": False,
            "live_capital_locked": True,
        }


class AuthoritySnapshotStore:
    def __init__(self, ttl_s: float | None = None) -> None:
        self.ttl_s = float(ttl_s if ttl_s is not None else os.environ.get("SENEX_AUTHORITY_SNAPSHOT_TTL_SEC", "10"))
        self._cache: dict[str, AuthoritySnapshot] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def clear(self) -> None:
        self._cache.clear()

    def _fresh(self, snapshot: AuthoritySnapshot) -> bool:
        return time.monotonic() - snapshot.captured_monotonic <= max(0.0, self.ttl_s)

    async def get(
        self,
        symbol: str,
        *,
        live_gate_builder: Callable[[dict[str, Any]], dict[str, Any]],
        force: bool = False,
    ) -> AuthoritySnapshot:
        normalized = normalize_symbol(symbol)
        if not normalized:
            raise ValueError("AUTHORITY_SYMBOL_REQUIRED")
        cached = self._cache.get(normalized)
        if cached is not None and not force and self._fresh(cached):
            return cached
        lock = self._locks.setdefault(normalized, asyncio.Lock())
        async with lock:
            cached = self._cache.get(normalized)
            if cached is not None and not force and self._fresh(cached):
                return cached
            snapshot = await self._capture(normalized, live_gate_builder)
            self._cache[normalized] = snapshot
            return snapshot

    async def _capture(
        self,
        symbol: str,
        live_gate_builder: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> AuthoritySnapshot:
        captured_at = datetime.now(timezone.utc).isoformat()
        captured_monotonic = time.monotonic()
        rows: list[dict[str, Any]] = []
        history_complete = False
        exact_total: int | None = None
        exact_count_complete = False
        failures: list[str] = []
        try:
            rows = await supabase_client.fetch_authority_history(symbol=symbol)
            history_complete = True
        except Exception as exc:
            failures.append(f"AUTHORITY_HISTORY:{type(exc).__name__}")
        try:
            exact_total = await supabase_client.count_predictions_exact()
            exact_count_complete = True
        except Exception as exc:
            failures.append(f"EXACT_COUNT:{type(exc).__name__}")

        score = build_authoritative_score(rows, symbol=symbol)
        score["authority_history_complete"] = history_complete
        score["authority_history_rows"] = len(rows)
        score["exact_total_predictions"] = exact_total
        score["exact_count_complete"] = exact_count_complete
        if not history_complete:
            score["score_status"] = "UNKNOWN"
            score["authoritative_score_pct"] = None
            reasons = score.setdefault("reasons", [])
            if "AUTHORITY_HISTORY_INCOMPLETE" not in reasons:
                reasons.append("AUTHORITY_HISTORY_INCOMPLETE")

        live_gate = live_gate_builder(score)
        if not history_complete:
            live_gate["effective_gate"] = "LOCKED_BY_INCOMPLETE_AUTHORITY_HISTORY"
            live_gate["unlocked"] = False
            live_gate["trade_mode"] = "PAPER"
            live_gate["live_capital_locked"] = True
            failed = live_gate.setdefault("failed_reasons", [])
            if "AUTHORITY_HISTORY_INCOMPLETE" not in failed:
                failed.append("AUTHORITY_HISTORY_INCOMPLETE")
        live_gate["authority_history_complete"] = history_complete
        live_gate["authority_history_rows"] = len(rows)

        provenance = runtime_provenance()
        identity = {
            "captured_at": captured_at,
            "symbol": symbol,
            "row_keys": [(row.get("id"), row.get("ts"), row.get("outcome")) for row in rows],
            "authority_1h": score.get("authority_1h"),
            "exact_total_predictions": exact_total,
            "provenance": provenance,
            "failure_reason": ";".join(failures) if failures else None,
        }
        snapshot_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        score["authority_snapshot_id"] = snapshot_id
        live_gate["authority_snapshot_id"] = snapshot_id
        return AuthoritySnapshot(
            snapshot_id=snapshot_id,
            captured_at=captured_at,
            captured_monotonic=captured_monotonic,
            symbol=symbol,
            authority_history_complete=history_complete,
            authority_history_rows=len(rows),
            exact_total_predictions=exact_total,
            exact_count_complete=exact_count_complete,
            score=score,
            live_gate=live_gate,
            provenance=provenance,
            failure_reason=";".join(failures) if failures else None,
        )


STORE = AuthoritySnapshotStore()
