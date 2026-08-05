"""Replayable paper settlement bound to public or deterministic resolution evidence."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

SETTLEMENT_CONTRACT_VERSION = "SENEX_PAPER_SETTLEMENT_V1"


class SettlementError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class SettlementState(str, Enum):
    OPEN_UNMARKED = "OPEN_UNMARKED"
    OPEN_MARKED = "OPEN_MARKED"
    RESOLUTION_PENDING = "RESOLUTION_PENDING"
    RESOLVED_WIN = "RESOLVED_WIN"
    RESOLVED_LOSS = "RESOLVED_LOSS"
    SETTLED = "SETTLED"
    SETTLEMENT_EVIDENCE_UNVERIFIED = "SETTLEMENT_EVIDENCE_UNVERIFIED"


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


@dataclass(frozen=True)
class ResolutionEvidence:
    condition_id: str
    market_id: str
    token_ids: tuple[str, ...]
    winning_token_id: str
    payout_per_share: float
    resolved_timestamp_utc: str
    source: str
    source_evidence_hash: str
    raw_resolution_hash: str
    verified: bool
    contract_version: str = SETTLEMENT_CONTRACT_VERSION

    @classmethod
    def build(
        cls,
        *,
        condition_id: str,
        market_id: str,
        token_ids: Sequence[str],
        winning_token_id: str,
        payout_per_share: float,
        resolved_timestamp_utc: str,
        source: str,
        raw_resolution: Mapping[str, Any],
        source_evidence_hash: str,
        verified: bool = True,
    ) -> "ResolutionEvidence":
        payout = float(payout_per_share)
        tokens = tuple(str(item) for item in token_ids)
        winner = str(winning_token_id)
        if payout < 0 or payout > 1:
            raise SettlementError("INVALID_SETTLEMENT_PAYOUT")
        if len(tokens) < 2 or len(set(tokens)) != len(tokens) or winner not in tokens:
            raise SettlementError("SETTLEMENT_TOKEN_IDENTITY_INVALID")
        raw_hash = hashlib.sha256(_canonical(dict(raw_resolution))).hexdigest()
        return cls(
            condition_id=str(condition_id),
            market_id=str(market_id),
            token_ids=tokens,
            winning_token_id=winner,
            payout_per_share=payout,
            resolved_timestamp_utc=str(resolved_timestamp_utc),
            source=str(source),
            source_evidence_hash=str(source_evidence_hash),
            raw_resolution_hash=raw_hash,
            verified=bool(verified),
        )

    @classmethod
    def deterministic_fixture(
        cls,
        *,
        condition_id: str = "condition",
        market_id: str = "market",
        token_ids: Sequence[str] = ("yes", "no"),
        winning_token_id: str = "yes",
        resolved_timestamp_utc: str = "2026-08-04T13:00:00Z",
    ) -> "ResolutionEvidence":
        raw = {
            "condition_id": condition_id,
            "market_id": market_id,
            "token_ids": list(token_ids),
            "winning_token_id": winning_token_id,
            "payout": "1",
        }
        digest = hashlib.sha256(_canonical(raw)).hexdigest()
        return cls.build(
            condition_id=condition_id,
            market_id=market_id,
            token_ids=token_ids,
            winning_token_id=winning_token_id,
            payout_per_share=1.0,
            resolved_timestamp_utc=resolved_timestamp_utc,
            source="DETERMINISTIC_FIXTURE",
            raw_resolution=raw,
            source_evidence_hash=digest,
            verified=True,
        )

    @property
    def identity_hash(self) -> str:
        return hashlib.sha256(_canonical({
            "condition_id": self.condition_id,
            "market_id": self.market_id,
            "token_ids": list(self.token_ids),
            "winning_token_id": self.winning_token_id,
        })).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "market_id": self.market_id,
            "token_ids": list(self.token_ids),
            "winning_token_id": self.winning_token_id,
            "payout_per_share": self.payout_per_share,
            "resolved_timestamp_utc": self.resolved_timestamp_utc,
            "source": self.source,
            "source_evidence_hash": self.source_evidence_hash,
            "raw_resolution_hash": self.raw_resolution_hash,
            "identity_hash": self.identity_hash,
            "verified": self.verified,
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True)
class SettlementResult:
    settlement_id: str
    condition_id: str
    market_id: str
    token_id: str
    state: SettlementState
    quantity: float
    payout_usd: float
    settled_pnl_usd: float
    evidence_hash: str
    evidence_identity_hash: str
    idempotent_duplicate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "settlement_id": self.settlement_id,
            "condition_id": self.condition_id,
            "market_id": self.market_id,
            "token_id": self.token_id,
            "state": self.state.value,
            "quantity": self.quantity,
            "payout_usd": self.payout_usd,
            "settled_pnl_usd": self.settled_pnl_usd,
            "evidence_hash": self.evidence_hash,
            "evidence_identity_hash": self.evidence_identity_hash,
            "idempotent_duplicate": self.idempotent_duplicate,
        }
