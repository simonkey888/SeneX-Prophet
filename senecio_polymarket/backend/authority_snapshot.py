"""Atomic, content-addressed authority snapshot for ORDER-070 public truth surfaces."""
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


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _canonical_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [json.loads(_canonical_json(row).decode()) for row in rows]
    return sorted(
        normalized,
        key=lambda row: (
            str(row.get("ts") or ""),
            str(row.get("id") or ""),
            _canonical_json(row),
        ),
    )


def _last_cursor(rows: list[dict[str, Any]]) -> dict[str, str] | None:
    if not rows:
        return None
    last = rows[-1]
    ts = str(last.get("ts") or "")
    row_id = str(last.get("id") or "")
    if not ts and not row_id:
        return None
    return {"ts": ts, "id": row_id}


class AuthoritySnapshotRefreshError(RuntimeError):
    """A complete authority generation could not be captured."""


@dataclass(frozen=True)
class AuthoritySnapshot:
    snapshot_id: str
    generation: int
    captured_at: str
    captured_monotonic: float
    canonical_sha256: str
    symbol: str
    authority_history_complete: bool
    authority_history_rows: int
    exact_total_predictions: int | None
    exact_count_complete: bool
    last_cursor_or_equivalent: dict[str, str] | None
    score: dict[str, Any]
    live_gate: dict[str, Any]
    provenance: dict[str, Any]
    failure_reason: str | None = None

    def to_dict(self, refresh_status: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "snapshot_id": self.snapshot_id,
            "generation": self.generation,
            "captured_at": self.captured_at,
            "canonical_sha256": self.canonical_sha256,
            "symbol": self.symbol,
            "authority_history_complete": self.authority_history_complete,
            "authority_history_rows": self.authority_history_rows,
            "exact_total_predictions": self.exact_total_predictions,
            "exact_count_complete": self.exact_count_complete,
            "last_cursor_or_equivalent": copy.deepcopy(self.last_cursor_or_equivalent),
            "failure_reason": self.failure_reason,
            "score": copy.deepcopy(self.score),
            "live_gate": copy.deepcopy(self.live_gate),
            "provenance": copy.deepcopy(self.provenance),
            "trade_mode": "PAPER",
            "orders_enabled": False,
            "live_capital_locked": True,
        }
        if refresh_status:
            payload.update(copy.deepcopy(refresh_status))
        return payload


@dataclass(frozen=True)
class _CapturedAuthority:
    captured_at: str
    captured_monotonic: float
    canonical_sha256: str
    canonical_hex: str
    symbol: str
    authority_history_rows: int
    exact_total_predictions: int
    last_cursor_or_equivalent: dict[str, str] | None
    score: dict[str, Any]
    live_gate: dict[str, Any]
    provenance: dict[str, Any]
    recent_predictions: tuple[dict[str, Any], ...]


class AuthoritySnapshotStore:
    """Publishes immutable last-known-good generations and tracks refresh state separately."""

    def __init__(self, ttl_s: float | None = None) -> None:
        self._runtime_policy = ttl_s is None
        self._refresh_period_s = max(
            300.0, float(os.environ.get("SENEX_AUTHORITY_REFRESH_INTERVAL_SEC", "300"))
        )
        if ttl_s is None:
            configured_ttl = float(os.environ.get("SENEX_AUTHORITY_SNAPSHOT_TTL_SEC", "600"))
            self.ttl_s = max(configured_ttl, self._refresh_period_s + 120.0)
        else:
            self.ttl_s = float(ttl_s)
        self._cache: dict[str, AuthoritySnapshot] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._generation: dict[str, int] = {}
        self._refresh: dict[str, dict[str, Any]] = {}
        self._recent: dict[str, tuple[dict[str, Any], ...]] = {}

    def clear(self) -> None:
        self._cache.clear()
        self._locks.clear()
        self._generation.clear()
        self._refresh.clear()
        self._recent.clear()

    def refresh_interval_s(self) -> float:
        if self._runtime_policy:
            return self._refresh_period_s
        ttl = max(0.0, self.ttl_s)
        if ttl <= 0.0:
            return 1.0
        return max(1.0, min(5.0, ttl / 2.0))

    def _refresh_state(self, symbol: str) -> dict[str, Any]:
        return self._refresh.setdefault(
            symbol,
            {
                "last_refresh_attempt_at": None,
                "last_refresh_attempt_monotonic": None,
                "last_refresh_success_at": None,
                "last_refresh_success_monotonic": None,
                "last_refresh_error": None,
                "refresh_in_progress": False,
            },
        )

    def _mark_attempt(self, symbol: str) -> None:
        state = self._refresh_state(symbol)
        state["last_refresh_attempt_at"] = _utcnow()
        state["last_refresh_attempt_monotonic"] = time.monotonic()
        state["refresh_in_progress"] = True

    def _mark_success(self, symbol: str) -> None:
        state = self._refresh_state(symbol)
        state["last_refresh_success_at"] = _utcnow()
        state["last_refresh_success_monotonic"] = time.monotonic()
        state["last_refresh_error"] = None
        state["refresh_in_progress"] = False

    def _mark_failure(self, symbol: str, exc: BaseException) -> None:
        state = self._refresh_state(symbol)
        state["last_refresh_error"] = str(exc) or type(exc).__name__
        state["refresh_in_progress"] = False

    def refresh_status(self, symbol: str) -> dict[str, Any]:
        normalized = normalize_symbol(symbol)
        snapshot = self._cache.get(normalized)
        state = self._refresh.get(normalized) or {}
        success_monotonic = state.get("last_refresh_success_monotonic")
        if success_monotonic is None:
            snapshot_age_s = None
            snapshot_stale = True
        else:
            snapshot_age_s = max(0.0, time.monotonic() - float(success_monotonic))
            snapshot_stale = snapshot is None or snapshot_age_s > max(0.0, self.ttl_s)
        return {
            "last_refresh_attempt_at": state.get("last_refresh_attempt_at"),
            "last_refresh_success_at": state.get("last_refresh_success_at"),
            "last_refresh_error": state.get("last_refresh_error"),
            "refresh_in_progress": bool(state.get("refresh_in_progress", False)),
            "snapshot_age_s": round(snapshot_age_s, 6) if snapshot_age_s is not None else None,
            "snapshot_stale": bool(snapshot_stale),
        }

    def observe(self, symbol: str) -> tuple[AuthoritySnapshot | None, dict[str, Any]]:
        """Read the current generation and refresh state without network I/O or mutation."""
        normalized = normalize_symbol(symbol)
        if not normalized:
            raise ValueError("AUTHORITY_SYMBOL_REQUIRED")
        return self._cache.get(normalized), self.refresh_status(normalized)

    def recent_predictions(self, symbol: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return a bounded copy of the current lifecycle-captured rows; no I/O or mutation."""
        normalized = normalize_symbol(symbol)
        bounded = max(1, min(int(limit), 50))
        return copy.deepcopy(list(self._recent.get(normalized, ()))[:bounded])

    def _fresh(self, symbol: str) -> bool:
        status = self.refresh_status(symbol)
        return (
            self._cache.get(symbol) is not None
            and not status["snapshot_stale"]
            and status["last_refresh_error"] is None
        )

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
        if cached is not None and not force and self._fresh(normalized):
            return cached

        lock = self._locks.setdefault(normalized, asyncio.Lock())
        async with lock:
            cached = self._cache.get(normalized)
            if cached is not None and not force and self._fresh(normalized):
                return cached

            self._mark_attempt(normalized)
            try:
                captured = await self._capture_complete(normalized, live_gate_builder)
            except Exception as exc:
                error = (
                    exc
                    if isinstance(exc, AuthoritySnapshotRefreshError)
                    else AuthoritySnapshotRefreshError(type(exc).__name__)
                )
                self._mark_failure(normalized, error)
                # Failed/partial refresh never replaces the immutable last-good generation.
                if cached is not None:
                    return cached
                raise error from exc

            self._recent[normalized] = tuple(copy.deepcopy(captured.recent_predictions))
            if cached is not None and cached.canonical_sha256 == captured.canonical_sha256:
                # Byte-equivalent authority content revalidates freshness without rotating identity.
                self._mark_success(normalized)
                return cached

            generation = self._generation.get(normalized, 0) + 1
            score = copy.deepcopy(captured.score)
            live_gate = copy.deepcopy(captured.live_gate)
            snapshot_id = captured.canonical_hex
            for payload in (score, live_gate):
                payload["authority_snapshot_id"] = snapshot_id
                payload["authority_generation"] = generation
                payload["authority_canonical_sha256"] = captured.canonical_sha256

            snapshot = AuthoritySnapshot(
                snapshot_id=snapshot_id,
                generation=generation,
                captured_at=captured.captured_at,
                captured_monotonic=captured.captured_monotonic,
                canonical_sha256=captured.canonical_sha256,
                symbol=normalized,
                authority_history_complete=True,
                authority_history_rows=captured.authority_history_rows,
                exact_total_predictions=captured.exact_total_predictions,
                exact_count_complete=True,
                last_cursor_or_equivalent=copy.deepcopy(captured.last_cursor_or_equivalent),
                score=score,
                live_gate=live_gate,
                provenance=copy.deepcopy(captured.provenance),
                failure_reason=None,
            )
            self._cache[normalized] = snapshot
            self._generation[normalized] = generation
            self._mark_success(normalized)
            return snapshot

    async def _capture_complete(
        self,
        symbol: str,
        live_gate_builder: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> _CapturedAuthority:
        captured_at = _utcnow()
        captured_monotonic = time.monotonic()
        history_result, count_result = await asyncio.gather(
            supabase_client.fetch_authority_history(symbol=symbol),
            supabase_client.count_predictions_exact(),
            return_exceptions=True,
        )
        failures: list[str] = []
        if isinstance(history_result, BaseException):
            failures.append(f"AUTHORITY_HISTORY:{type(history_result).__name__}")
        if isinstance(count_result, BaseException):
            failures.append(f"EXACT_COUNT:{type(count_result).__name__}")
        if failures:
            raise AuthoritySnapshotRefreshError(";".join(failures))
        if not isinstance(history_result, list):
            raise AuthoritySnapshotRefreshError("AUTHORITY_HISTORY:RESPONSE_NOT_LIST")
        if isinstance(count_result, bool) or not isinstance(count_result, int):
            raise AuthoritySnapshotRefreshError("EXACT_COUNT:RESPONSE_NOT_INT")

        rows = _canonical_rows(history_result)
        recent_predictions = tuple(copy.deepcopy(list(reversed(rows[-50:]))))
        exact_total = int(count_result)
        score = build_authoritative_score(rows, symbol=symbol)
        score["authority_history_complete"] = True
        score["authority_history_rows"] = len(rows)
        score["exact_total_predictions"] = exact_total
        score["exact_count_complete"] = True

        try:
            live_gate = live_gate_builder(copy.deepcopy(score))
        except Exception as exc:
            raise AuthoritySnapshotRefreshError(
                f"LIVE_GATE:{type(exc).__name__}"
            ) from exc
        if not isinstance(live_gate, dict):
            raise AuthoritySnapshotRefreshError("LIVE_GATE:RESPONSE_NOT_OBJECT")
        live_gate = copy.deepcopy(live_gate)
        live_gate["authority_history_complete"] = True
        live_gate["authority_history_rows"] = len(rows)

        provenance = runtime_provenance()
        last_cursor = _last_cursor(rows)
        canonical_authority = {
            "contract": "senex-authority-snapshot-v2",
            "symbol": symbol,
            "authority_rows": rows,
            "authority_history_rows": len(rows),
            "last_cursor_or_equivalent": last_cursor,
            "exact_total_predictions": exact_total,
            "score": score,
            "live_gate": live_gate,
            "provenance": provenance,
        }
        canonical_hex = hashlib.sha256(_canonical_json(canonical_authority)).hexdigest()
        canonical_sha256 = "sha256:" + canonical_hex
        return _CapturedAuthority(
            captured_at=captured_at,
            captured_monotonic=captured_monotonic,
            canonical_sha256=canonical_sha256,
            canonical_hex=canonical_hex,
            symbol=symbol,
            authority_history_rows=len(rows),
            exact_total_predictions=exact_total,
            last_cursor_or_equivalent=last_cursor,
            score=score,
            live_gate=live_gate,
            provenance=provenance,
            recent_predictions=recent_predictions,
        )


STORE = AuthoritySnapshotStore()
