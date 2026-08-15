#!/usr/bin/env python3
"""AUD-063 pre-fix read-only runtime baseline and independent starvation reproduction.

This script never authenticates to Supabase and never writes to production. It
uses only the public dashboard API plus an in-memory fake PostgREST boundary to
exercise the exact main-branch fetch_pending_outcomes() implementation.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE = os.environ.get(
    "AUD063_PUBLIC_BASE",
    "https://h011-web--senecio-h011--wbjggn89fnf8.code.run",
).rstrip("/")
OUT = Path(os.environ.get("AUD063_OUT", "aud063_bootstrap"))
OUT.mkdir(parents=True, exist_ok=True)


def _get_json(path: str) -> tuple[int | None, Any, str | None]:
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={"User-Agent": "SENEX-AUD-063-read-only/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8")), None
    except Exception as exc:  # public runtime may be unavailable; preserve fact
        return None, None, f"{type(exc).__name__}:{exc}"


def _parse_ts(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _direction(row: dict[str, Any]) -> str:
    return str(row.get("prediction") or "UNKNOWN").upper()


def _symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or "UNKNOWN").upper().replace("/", "").replace("-", "")


def build_runtime_baseline() -> dict[str, Any]:
    captured = datetime.now(timezone.utc)
    health_status, health, health_error = _get_json("/api/health")
    state_status, state, state_error = _get_json("/api/oracle/state")
    pred_status, pred_payload, pred_error = _get_json("/api/oracle/predictions/db?limit=500")
    score_payload: dict[str, Any] = {}
    score_http: dict[str, Any] = {}
    for sym in ("BTCUSDT", "ETHUSDT"):
        status, payload, error = _get_json(f"/api/oracle/score?symbol={sym}")
        score_http[sym] = {"http_status": status, "error": error}
        score_payload[sym] = payload

    rows: list[dict[str, Any]] = []
    if isinstance(pred_payload, list):
        rows = [r for r in pred_payload if isinstance(r, dict)]
    elif isinstance(pred_payload, dict):
        for key in ("predictions", "rows", "data"):
            candidate = pred_payload.get(key)
            if isinstance(candidate, list):
                rows = [r for r in candidate if isinstance(r, dict)]
                break

    null_rows = [r for r in rows if r.get("outcome") is None]
    cutoff = captured - timedelta(hours=1)
    directional_null = [
        r for r in null_rows
        if _direction(r) in {"LONG", "SHORT"}
        and (_parse_ts(r.get("ts") or r.get("timestamp")) or captured) <= cutoff
    ]
    null_sorted = sorted(
        null_rows,
        key=lambda r: (
            _parse_ts(r.get("ts") or r.get("timestamp")) or datetime.max.replace(tzinfo=timezone.utc),
            str(r.get("id") or ""),
        ),
    )
    directional_sorted = sorted(
        directional_null,
        key=lambda r: (
            _parse_ts(r.get("ts") or r.get("timestamp")) or datetime.max.replace(tzinfo=timezone.utc),
            str(r.get("id") or ""),
        ),
    )
    oldest = directional_sorted[0] if directional_sorted else None
    oldest_ts = _parse_ts((oldest or {}).get("ts") or (oldest or {}).get("timestamp"))
    flats_ahead = 0
    if oldest_ts is not None:
        flats_ahead = sum(
            1 for r in null_sorted
            if _direction(r) == "FLAT"
            and (_parse_ts(r.get("ts") or r.get("timestamp")) or captured) < oldest_ts
        )

    by_symbol_age: dict[str, dict[str, int]] = defaultdict(lambda: {"1_2h": 0, "2_6h": 0, "6_24h": 0, "gt_24h": 0})
    for row in directional_null:
        ts = _parse_ts(row.get("ts") or row.get("timestamp"))
        if ts is None:
            continue
        age_h = (captured - ts).total_seconds() / 3600.0
        bucket = "1_2h" if age_h < 2 else "2_6h" if age_h < 6 else "6_24h" if age_h < 24 else "gt_24h"
        by_symbol_age[_symbol(row)][bucket] += 1

    def score_minimized(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"available": False}
        return {
            "available": True,
            "score_status": payload.get("score_status"),
            "authoritative_score_pct": payload.get("authoritative_score_pct"),
            "proof_qualified_rows_raw": payload.get("proof_qualified_rows_raw"),
            "independent_1h_rows": payload.get("independent_1h_rows") or payload.get("verified"),
            "authority_cohort": payload.get("authority_cohort"),
        }

    verifier = {}
    if isinstance(state, dict):
        for key in (
            "last_verify_at", "last_verify_count", "last_verify_ids", "verified_total",
            "cycles_failed", "last_error", "cycles_run", "last_cycle_at",
        ):
            verifier[key] = state.get(key)

    return {
        "order": "AUD-063",
        "source_class": "PUBLIC_READ_ONLY_HTTP",
        "public_base_origin": BASE,
        "captured_at_utc": captured.isoformat(),
        "http": {
            "health": {"status": health_status, "error": health_error},
            "state": {"status": state_status, "error": state_error},
            "predictions_db_limit_500": {"status": pred_status, "error": pred_error},
            "scores": score_http,
        },
        "counts": {
            "total_rows_visible": len(rows),
            "outcome_null": len(null_rows),
            "null_by_direction": dict(Counter(_direction(r) for r in null_rows)),
            "eligible_directional_null_gt_1h": len(directional_null),
            "first_100_null_by_direction": dict(Counter(_direction(r) for r in null_sorted[:100])),
            "old_flat_null_rows_ahead_of_oldest_directional": flats_ahead,
            "directional_backlog_by_symbol_age_bucket": dict(by_symbol_age),
        },
        "oldest_eligible_directional": None if oldest is None else {
            "id": oldest.get("id"),
            "ts": oldest.get("ts") or oldest.get("timestamp"),
            "symbol": _symbol(oldest),
            "prediction": _direction(oldest),
        },
        "scores": {sym: score_minimized(score_payload.get(sym)) for sym in ("BTCUSDT", "ETHUSDT")},
        "verifier": verifier,
        "health_minimized": {
            key: health.get(key) for key in ("status", "version", "predictions_count", "cycles_run", "cycles_failed", "last_error")
        } if isinstance(health, dict) else None,
        "sanitization": "ALLOWLISTED_FIELDS_ONLY_NO_AUTH_HEADERS_NO_RAW_PAYLOAD",
    }


class _FakeResponse:
    def __init__(self, rows: list[dict[str, Any]]):
        self.status_code = 200
        self._rows = rows
        self.text = ""
    def json(self):
        return self._rows


class _FakePostgrest:
    """Minimal PostgREST semantics needed by pre-fix fetch_pending_outcomes."""
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.calls: list[dict[str, Any]] = []
        self.is_closed = False
    async def get(self, path: str, params: dict[str, str] | None = None, **kwargs):
        params = dict(params or {})
        self.calls.append(params)
        selected = [r for r in self.rows if r.get("outcome") is None]
        cutoff_text = str(params.get("ts", ""))
        if cutoff_text.startswith("lt."):
            cutoff = _parse_ts(cutoff_text[3:])
            if cutoff:
                selected = [r for r in selected if (_parse_ts(r.get("ts")) or cutoff) < cutoff]
        # Crucially: apply a direction predicate only if the production query sent one.
        pred_filter = params.get("prediction")
        if pred_filter == "in.(LONG,SHORT)":
            selected = [r for r in selected if _direction(r) in {"LONG", "SHORT"}]
        selected.sort(key=lambda r: (_parse_ts(r.get("ts")) or datetime.max.replace(tzinfo=timezone.utc), str(r.get("id"))))
        return _FakeResponse(selected[: int(params.get("limit", "100"))])


async def reproduce_prefx_starvation() -> dict[str, Any]:
    from backend import supabase_client

    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for i in range(125):
        rows.append({
            "id": i + 1,
            "ts": (now - timedelta(hours=6) + timedelta(seconds=i)).isoformat(),
            "symbol": "BTCUSDT",
            "prediction": "FLAT",
            "outcome": None,
            "price_now": 100.0,
            "exchange_used": "okx",
            "audit": {},
        })
    rows.append({
        "id": 1000,
        "ts": (now - timedelta(hours=5)).isoformat(),
        "symbol": "BTCUSDT",
        "prediction": "LONG",
        "outcome": None,
        "price_now": 100.0,
        "exchange_used": "okx",
        "audit": {"origin_price_v1": {"version": "origin-price-v1", "price": 100.0, "timestamp": (now - timedelta(hours=5)).isoformat(), "source": "okx"}},
    })
    fake = _FakePostgrest(rows)
    original = supabase_client._get_client
    supabase_client._get_client = lambda: fake
    try:
        first = await supabase_client.fetch_pending_outcomes(older_than_seconds=3600, limit=100)
        second = await supabase_client.fetch_pending_outcomes(older_than_seconds=3600, limit=100)
    finally:
        supabase_client._get_client = original

    first_dirs = Counter(_direction(r) for r in first)
    second_dirs = Counter(_direction(r) for r in second)
    calls = fake.calls
    no_server_side_direction_filter = bool(calls) and all("prediction" not in call for call in calls)
    reproduced = (
        len(first) == 100 and len(second) == 100
        and first_dirs == {"FLAT": 100}
        and second_dirs == {"FLAT": 100}
        and not any(r.get("id") == 1000 for r in first + second)
        and no_server_side_direction_filter
    )
    if not reproduced:
        raise AssertionError({"first": dict(first_dirs), "second": dict(second_dirs), "calls": calls})
    return {
        "order": "AUD-063",
        "source_class": "EXACT_MAIN_IMPLEMENTATION_WITH_IN_MEMORY_POSTGREST_BOUNDARY",
        "base_sha": "49c5f0a69609c005da80e48b585e91d8582a5ac6",
        "generated_at_utc": now.isoformat(),
        "fixture": {"old_flat_null": 125, "later_directional_null": 1, "batch_limit": 100},
        "actual_query_params": calls,
        "first_page_by_direction": dict(first_dirs),
        "second_page_by_direction": dict(second_dirs),
        "later_directional_id_seen": any(r.get("id") == 1000 for r in first + second),
        "server_side_direction_filter_present": not no_server_side_direction_filter,
        "starvation_reproduced": True,
    }


def main() -> None:
    baseline = build_runtime_baseline()
    reproduction = asyncio.run(reproduce_prefx_starvation())
    (OUT / "aud-063-runtime-baseline.json").write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (OUT / "aud-063-starvation-reproduction.json").write_text(json.dumps(reproduction, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"runtime_baseline": baseline["counts"], "reproduction": reproduction}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
