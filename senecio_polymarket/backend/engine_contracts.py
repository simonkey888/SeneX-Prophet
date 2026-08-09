"""Canonical contracts shared by SENECIO decision engines.

Inspired by the alpha-model separation in virattt/ai-hedge-fund v2 (MIT),
adapted for prediction markets: models form views, execution evidence remains
separate, and an arbiter is the only component allowed to combine them.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


Direction = Literal["LONG", "SHORT", "FLAT"]
ValidationState = Literal["PASS", "REJECT", "UNKNOWN"]


class AlphaSignal(BaseModel):
    engine_id: str
    instrument: str
    market_id: str | None = None
    horizon_s: int = Field(gt=0)
    direction: Direction
    confidence_raw: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence_calibrated: float | None = Field(default=None, ge=0.0, le=1.0)
    validation_state: ValidationState = "UNKNOWN"
    as_of: str | None = None
    data_cutoff: str | None = None
    abstain_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def evidence_hash(self) -> str:
        body = self.model_dump(mode="json", exclude={"metadata"})
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ExecutionEvidence(BaseModel):
    engine_id: str
    instrument: str
    market_id: str | None = None
    horizon_s: int = Field(gt=0)
    side: Direction = "FLAT"
    executable: bool = False
    identity_verified: bool = False
    source_health_verified: bool = False
    invariants_verified: bool = False
    discovery_verified: bool = False
    freshness_verified: bool = False
    net_edge: float | None = None
    equal_fillable_quantity: float | None = Field(default=None, ge=0.0)
    as_of: str | None = None
    rejection_reasons: list[str] = Field(default_factory=list)

    @property
    def evidence_hash(self) -> str:
        body = self.model_dump(mode="json")
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def oracle_contract(payload: dict[str, Any], *, horizon_s: int = 3600) -> AlphaSignal:
    action = str(payload.get("shadow_action") or "FLAT").upper()
    direction: Direction = action if action in {"LONG", "SHORT", "FLAT"} else "FLAT"  # type: ignore[assignment]
    calibrated_pct = payload.get("authoritative_score_pct")
    calibrated = (
        float(calibrated_pct) / 100.0
        if isinstance(calibrated_pct, (int, float)) and 0.0 <= float(calibrated_pct) <= 100.0
        else None
    )
    validation = str(payload.get("gate_status") or "UNKNOWN").upper()
    if validation not in {"PASS", "REJECT", "UNKNOWN"}:
        validation = "UNKNOWN"
    return AlphaSignal(
        engine_id="senecio-oracle-btc-v2",
        instrument="BTCUSD",
        horizon_s=horizon_s,
        direction=direction,
        confidence_raw=payload.get("source_confidence"),
        confidence_calibrated=calibrated,
        validation_state=validation,
        as_of=payload.get("source_ts"),
        data_cutoff=payload.get("source_ts"),
        abstain_reason=(payload.get("reasons") or [None])[0] if direction == "FLAT" else None,
    )


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def h011_contract(
    state: dict[str, Any],
    operations: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    max_age_s: int = 900,
) -> ExecutionEvidence:
    btc = [
        op for op in operations
        if any(mark in str(op.get("question") or "").lower() for mark in ("btc", "bitcoin"))
        and op.get("record_status") == "SHADOW_EXECUTABLE"
    ]
    unknown = int((((state.get("invariants") or {}).get("summary") or {}).get("unknown")) or 0)
    failed = int((((state.get("invariants") or {}).get("summary") or {}).get("fail")) or 0)
    health = state.get("source_health") or {}
    health_ok = bool(health) and all(
        ((item or {}).get("level") or (item or {}).get("status")) in {"HEALTHY", "NOT_USED"}
        for item in health.values()
    )
    discovery = ((state.get("aggregate_metrics") or {}).get("discovery") or {})
    discovery_ok = bool(
        discovery.get("discovery_complete")
        and discovery.get("discovery_replay_verified")
    )
    scan_at = _parse_ts(state.get("scan_id"))
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age_s = (observed_at - scan_at).total_seconds() if scan_at else None
    freshness_ok = age_s is not None and 0 <= age_s <= max_age_s
    op = btc[0] if btc else {}
    raw_side = str(op.get("direction") or op.get("side") or "FLAT").upper()
    side: Direction = "LONG" if raw_side in {"LONG", "UP"} else "SHORT" if raw_side in {"SHORT", "DOWN"} else "FLAT"
    try:
        net_edge = float(op.get("net_edge") or 0)
        fillable = float(op.get("equal_fillable_quantity") or 0)
    except (TypeError, ValueError):
        net_edge = 0.0
        fillable = 0.0
    rejection_reasons: list[str] = []
    if not btc:
        rejection_reasons.append("NO_EXECUTABLE_BTC_CLOB_OPERATION")
    if not discovery_ok:
        rejection_reasons.append("DISCOVERY_NOT_COMPLETE_AND_REPLAY_VERIFIED")
    if not health_ok:
        rejection_reasons.append("SOURCE_HEALTH_NOT_VERIFIED")
    if unknown or failed:
        rejection_reasons.append("INVARIANTS_NOT_VERIFIED")
    if not freshness_ok:
        rejection_reasons.append("H011_SNAPSHOT_STALE_OR_INVALID")

    identity_ok = bool(
        op.get("condition_id")
        and op.get("record_status") == "SHADOW_EXECUTABLE"
        and int(state.get("window_s") or 0) == 300
    )
    executable = bool(
        btc
        and identity_ok
        and discovery_ok
        and health_ok
        and unknown == 0
        and failed == 0
        and freshness_ok
        and net_edge > 0
        and fillable > 0
    )
    return ExecutionEvidence(
        engine_id="senecio-h011-v3",
        instrument="BTCUSD",
        market_id=op.get("condition_id"),
        horizon_s=int(op.get("window_s") or state.get("window_s") or 300),
        side=side,
        executable=executable,
        identity_verified=identity_ok,
        source_health_verified=health_ok,
        invariants_verified=unknown == 0 and failed == 0,
        discovery_verified=discovery_ok,
        freshness_verified=freshness_ok,
        net_edge=net_edge if op else None,
        equal_fillable_quantity=fillable if op else None,
        as_of=state.get("scan_id"),
        rejection_reasons=rejection_reasons,
    )
