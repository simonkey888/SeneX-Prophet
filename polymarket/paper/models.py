"""Immutable, deterministic records for the SENEX paper-only trial."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from decimal import Decimal
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "senex-paper-v1"


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def deterministic_id(kind: str, payload: Mapping[str, Any]) -> str:
    return f"{kind}_{sha256_json({'kind': kind, 'payload': payload})[:32]}"


def _to_primitive(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {field.name: _to_primitive(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, tuple):
        return [_to_primitive(item) for item in value]
    if isinstance(value, list):
        return [_to_primitive(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_primitive(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


class RecordMixin:
    def to_dict(self) -> dict[str, Any]:
        return _to_primitive(self)


@dataclass(frozen=True)
class PaperDecision(RecordMixin):
    schema_version: str
    timestamp_utc: str
    code_sha: str
    config_sha: str
    source_evidence_hash: str
    deterministic_id: str
    provenance: str
    market_id: str
    condition_id: str
    token_ids: tuple[str, ...]
    action: str
    reason_codes: tuple[str, ...]
    requested_shares: float
    expected_edge: float | None
    signal_payload_hash: str

    @classmethod
    def build(
        cls,
        *,
        timestamp_utc: str,
        code_sha: str,
        config_sha: str,
        source_evidence_hash: str,
        market_id: str,
        condition_id: str,
        token_ids: Sequence[str],
        action: str,
        reason_codes: Sequence[str],
        requested_shares: float,
        expected_edge: float | None,
        signal_payload: Mapping[str, Any],
        provenance: str = "H011_DETERMINISTIC_SHADOW",
    ) -> "PaperDecision":
        payload = {
            "timestamp_utc": timestamp_utc,
            "code_sha": code_sha,
            "config_sha": config_sha,
            "source_evidence_hash": source_evidence_hash,
            "market_id": market_id,
            "condition_id": condition_id,
            "token_ids": list(token_ids),
            "action": action,
            "reason_codes": list(reason_codes),
            "requested_shares": requested_shares,
            "expected_edge": expected_edge,
            "signal_payload_hash": sha256_json(signal_payload),
        }
        return cls(
            schema_version=SCHEMA_VERSION,
            timestamp_utc=timestamp_utc,
            code_sha=code_sha,
            config_sha=config_sha,
            source_evidence_hash=source_evidence_hash,
            deterministic_id=deterministic_id("decision", payload),
            provenance=provenance,
            market_id=market_id,
            condition_id=condition_id,
            token_ids=tuple(str(value) for value in token_ids),
            action=action,
            reason_codes=tuple(str(value) for value in reason_codes),
            requested_shares=float(requested_shares),
            expected_edge=None if expected_edge is None else float(expected_edge),
            signal_payload_hash=payload["signal_payload_hash"],
        )


@dataclass(frozen=True)
class PaperOrderIntent(RecordMixin):
    schema_version: str
    timestamp_utc: str
    code_sha: str
    config_sha: str
    source_evidence_hash: str
    deterministic_id: str
    provenance: str
    decision_id: str
    market_id: str
    token_id: str
    outcome: str
    side: str
    requested_shares: float
    max_notional_usd: float
    condition_id: str = "UNKNOWN"

    @classmethod
    def build(
        cls,
        *,
        decision: PaperDecision,
        token_id: str,
        outcome: str,
        side: str,
        requested_shares: float,
        max_notional_usd: float,
    ) -> "PaperOrderIntent":
        payload = {
            "decision_id": decision.deterministic_id,
            "token_id": token_id,
            "outcome": outcome,
            "side": side,
            "requested_shares": requested_shares,
            "max_notional_usd": max_notional_usd,
        }
        return cls(
            schema_version=SCHEMA_VERSION,
            timestamp_utc=decision.timestamp_utc,
            code_sha=decision.code_sha,
            config_sha=decision.config_sha,
            source_evidence_hash=decision.source_evidence_hash,
            deterministic_id=deterministic_id("order_intent", payload),
            provenance="SIMULATED_INTENT_ONLY",
            decision_id=decision.deterministic_id,
            market_id=decision.market_id,
            token_id=str(token_id),
            outcome=str(outcome),
            side=str(side).upper(),
            requested_shares=float(requested_shares),
            max_notional_usd=float(max_notional_usd),
            condition_id=decision.condition_id,
        )


@dataclass(frozen=True)
class PaperFill(RecordMixin):
    schema_version: str
    timestamp_utc: str
    code_sha: str
    config_sha: str
    source_evidence_hash: str
    deterministic_id: str
    provenance: str
    order_intent_id: str
    market_id: str
    token_id: str
    outcome: str
    side: str
    requested_shares: float
    filled_shares: float
    fill_price: float
    observed_available_size: float
    gross_notional_usd: float
    fee_usd: float
    partial: bool
    book_timestamp_utc: str
    received_timestamp_utc: str = "UNKNOWN"
    source_age_ms: float | None = None
    fee_model_version: str = "UNVERIFIED"
    fee_schedule_hash: str = "UNVERIFIED"
    liquidity_classification: str = "TAKER"
    condition_id: str = "UNKNOWN"


@dataclass(frozen=True)
class PaperPosition(RecordMixin):
    market_id: str
    token_id: str
    outcome: str
    quantity: float
    average_price: float
    realized_pnl: float
    condition_id: str = "UNKNOWN"
    settlement_state: str = "OPEN_UNMARKED"
    mark_price: float | None = None
    valuation_known: bool = False


@dataclass(frozen=True)
class PaperPortfolioSnapshot(RecordMixin):
    schema_version: str
    timestamp_utc: str
    code_sha: str
    config_sha: str
    source_evidence_hash: str
    deterministic_id: str
    provenance: str
    cash_usd: float
    realized_pnl: float
    unrealized_pnl: float | None
    equity_usd: float | None
    gross_exposure_usd: float | None
    max_drawdown_pct: float
    equity_known: bool = True
    unknown_valuation_positions: int = 0
    pending_settlement_count: int = 0
    realized_settled_pnl: float = 0.0
    marked_unsettled_pnl: float | None = None
    positions: tuple[PaperPosition, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PaperRiskDecision(RecordMixin):
    schema_version: str
    timestamp_utc: str
    code_sha: str
    config_sha: str
    source_evidence_hash: str
    deterministic_id: str
    provenance: str
    decision_id: str
    allowed: bool
    reason_codes: tuple[str, ...]
    requested_notional_usd: float
    projected_gross_exposure_usd: float


@dataclass(frozen=True)
class PaperTrialSummary(RecordMixin):
    schema_version: str
    timestamp_utc: str
    code_sha: str
    config_sha: str
    source_evidence_hash: str
    deterministic_id: str
    provenance: str
    trial_id: str
    start_utc: str
    end_utc: str
    duration_seconds: float
    gamma_discovered_windows: int
    valid_order_book_windows: int
    valid_order_books: int
    expected_closed_no_book_windows: int
    unexpected_source_failures: int
    windows_observed: int
    markets_observed: int
    decisions_total: int
    long_short_flat_counts: Mapping[str, int]
    abstention_counts: Mapping[str, int]
    order_intents: int
    fills: int
    partial_fills: int
    turnover: float
    realized_pnl: float
    unrealized_pnl: float | None
    ending_equity: float | None
    max_drawdown: float
    risk_rejections: int
    source_failures: int
    stale_data_events: int
    integrity_failures: int
    replay_result: str
    raw_chain_verified: bool
    replay_verified: bool
    legacy_mode: bool
    paper_only: bool
    orders_enabled: bool
    live_capital_locked: bool
    real_order_network_calls: int
    real_order_methods_reachable: int
    wallet_private_key_dependencies: int
    equity_known: bool = True
    unknown_valuation_positions: int = 0
    pending_settlement_count: int = 0
    realized_settled_pnl: float = 0.0
    marked_unsettled_pnl: float | None = None
    fees_applied_usd: float = 0.0
    sequential_executions: int = 0
    leg_risk_incidents: int = 0
