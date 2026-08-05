"""Append-only virtual portfolio with deterministic fill and settlement replay."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import PaperFill, PaperPortfolioSnapshot, PaperPosition, deterministic_id
from .settlement import ResolutionEvidence, SettlementError, SettlementResult, SettlementState


@dataclass
class _MutablePosition:
    market_id: str
    condition_id: str
    token_id: str
    outcome: str
    quantity: float = 0.0
    average_price: float = 0.0
    realized_pnl: float = 0.0
    settlement_state: SettlementState = SettlementState.OPEN_UNMARKED
    mark_price: float | None = None


class AppendOnlyJournal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: Mapping[str, Any]) -> None:
        payload = (json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        dir_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]


class PaperPortfolio:
    def __init__(self, *, starting_equity_usd: float, journal: AppendOnlyJournal | None = None):
        self.starting_equity_usd = float(starting_equity_usd)
        self.cash_usd = float(starting_equity_usd)
        self.realized_pnl = 0.0
        self.realized_settled_pnl = 0.0
        self.max_equity = float(starting_equity_usd)
        self.max_drawdown_pct = 0.0
        self.positions: dict[str, _MutablePosition] = {}
        self.ledger: list[dict[str, Any]] = []
        self.journal = journal
        self.consecutive_losses = 0
        self.applied_settlement_ids: set[str] = set()

    def _append(self, record: Mapping[str, Any]) -> None:
        item = dict(record)
        self.ledger.append(item)
        if self.journal is not None:
            self.journal.append(item)

    def apply_fill(self, fill: PaperFill, *, record: bool = True) -> None:
        position = self.positions.setdefault(fill.token_id, _MutablePosition(fill.market_id, fill.condition_id, fill.token_id, fill.outcome))
        quantity = fill.filled_shares
        if fill.side == "BUY":
            total_cost = fill.gross_notional_usd + fill.fee_usd
            if total_cost > self.cash_usd + 1e-9:
                raise ValueError("virtual cash insufficient")
            new_quantity = position.quantity + quantity
            if new_quantity <= 0:
                raise ValueError("invalid resulting position")
            # Entry fee is included in cost basis once, so settlement does not
            # charge or subtract it again.
            position.average_price = ((position.quantity * position.average_price) + total_cost) / new_quantity
            position.quantity = new_quantity
            position.settlement_state = SettlementState.OPEN_UNMARKED
            position.mark_price = None
            self.cash_usd -= total_cost
        elif fill.side == "SELL":
            if quantity > position.quantity + 1e-9:
                raise ValueError("paper sell exceeds virtual holdings")
            proceeds = fill.gross_notional_usd - fill.fee_usd
            pnl = proceeds - position.average_price * quantity
            position.quantity -= quantity
            position.realized_pnl += pnl
            self.realized_pnl += pnl
            self.cash_usd += proceeds
            if pnl < 0:
                self.consecutive_losses += 1
            elif pnl > 0:
                self.consecutive_losses = 0
            if position.quantity <= 1e-12:
                position.quantity = 0.0
                position.average_price = 0.0
                position.settlement_state = SettlementState.SETTLED
        else:
            raise ValueError(f"unsupported side {fill.side}")
        if record:
            self._append({"type": "PAPER_FILL", "fill": fill.to_dict()})

    def mark_position(self, *, token_id: str, price: float, record: bool = False, timestamp_utc: str | None = None, source_evidence_hash: str | None = None) -> None:
        if token_id not in self.positions:
            raise ValueError("unknown paper position")
        value = float(price)
        if not 0 <= value <= 1:
            raise ValueError("invalid mark price")
        position = self.positions[token_id]
        position.mark_price = value
        if position.quantity > 0:
            position.settlement_state = SettlementState.OPEN_MARKED
        if record:
            self._append({
                "type": "PAPER_MARK",
                "token_id": token_id,
                "price": value,
                "timestamp_utc": timestamp_utc,
                "source_evidence_hash": source_evidence_hash,
            })

    def mark_resolution_pending(self, *, token_id: str) -> None:
        if token_id in self.positions and self.positions[token_id].quantity > 0:
            self.positions[token_id].settlement_state = SettlementState.RESOLUTION_PENDING

    def apply_settlement(
        self,
        *,
        evidence: ResolutionEvidence,
        token_id: str,
        record: bool = True,
    ) -> SettlementResult:
        if not evidence.verified:
            raise SettlementError("SETTLEMENT_EVIDENCE_UNVERIFIED")
        position = self.positions.get(token_id)
        if position is None:
            raise SettlementError("SETTLEMENT_POSITION_NOT_FOUND")
        if position.market_id != evidence.market_id:
            raise SettlementError("SETTLEMENT_MARKET_MISMATCH")
        if position.condition_id != evidence.condition_id:
            raise SettlementError("SETTLEMENT_CONDITION_MISMATCH")
        if token_id not in evidence.token_ids:
            raise SettlementError("SETTLEMENT_TOKEN_MISMATCH")
        settlement_id = deterministic_id("settlement", {
            "market_id": evidence.market_id,
            "token_id": token_id,
            "raw_resolution_hash": evidence.raw_resolution_hash,
        })
        if settlement_id in self.applied_settlement_ids:
            return SettlementResult(
                settlement_id=settlement_id,
                condition_id=position.condition_id,
                market_id=position.market_id,
                token_id=token_id,
                state=SettlementState.SETTLED,
                quantity=0.0,
                payout_usd=0.0,
                settled_pnl_usd=0.0,
                evidence_hash=evidence.raw_resolution_hash,
                evidence_identity_hash=evidence.identity_hash,
                idempotent_duplicate=True,
            )
        quantity = position.quantity
        if quantity <= 0:
            raise SettlementError("SETTLEMENT_POSITION_EMPTY")
        is_winner = token_id == evidence.winning_token_id
        resolved_state = SettlementState.RESOLVED_WIN if is_winner else SettlementState.RESOLVED_LOSS
        position.settlement_state = resolved_state
        payout = quantity * evidence.payout_per_share if is_winner else 0.0
        settled_pnl = payout - quantity * position.average_price
        self.cash_usd += payout
        self.realized_pnl += settled_pnl
        self.realized_settled_pnl += settled_pnl
        position.realized_pnl += settled_pnl
        position.quantity = 0.0
        position.average_price = 0.0
        position.mark_price = None
        position.settlement_state = SettlementState.SETTLED
        self.applied_settlement_ids.add(settlement_id)
        result = SettlementResult(
            settlement_id=settlement_id,
            condition_id=position.condition_id,
            market_id=position.market_id,
            token_id=token_id,
            state=SettlementState.SETTLED,
            quantity=quantity,
            payout_usd=round(payout, 12),
            settled_pnl_usd=round(settled_pnl, 12),
            evidence_hash=evidence.raw_resolution_hash,
            evidence_identity_hash=evidence.identity_hash,
        )
        if record:
            self._append({"type": "PAPER_SETTLEMENT", "evidence": evidence.to_dict(), "result": result.to_dict()})
        return result

    def mark_to_market(self, prices: Mapping[str, float]) -> tuple[float | None, float | None, float | None, int]:
        market_value = 0.0
        unrealized = 0.0
        unknown = 0
        for token_id, position in self.positions.items():
            if position.quantity <= 0:
                continue
            if token_id in prices:
                self.mark_position(token_id=token_id, price=float(prices[token_id]))
            price = position.mark_price
            if price is None:
                unknown += 1
                continue
            market_value += position.quantity * price
            unrealized += position.quantity * (price - position.average_price)
        if unknown:
            return None, None, None, unknown
        equity = self.cash_usd + market_value
        self.max_equity = max(self.max_equity, equity)
        if self.max_equity > 0:
            drawdown = max(0.0, (self.max_equity - equity) / self.max_equity * 100.0)
            self.max_drawdown_pct = max(self.max_drawdown_pct, drawdown)
        return equity, unrealized, market_value, 0

    def snapshot(
        self,
        *,
        timestamp_utc: str,
        code_sha: str,
        config_sha: str,
        source_evidence_hash: str,
        prices: Mapping[str, float],
    ) -> PaperPortfolioSnapshot:
        equity, unrealized, market_value, unknown = self.mark_to_market(prices)
        positions = tuple(
            PaperPosition(
                market_id=value.market_id,
                token_id=value.token_id,
                outcome=value.outcome,
                quantity=round(value.quantity, 12),
                average_price=round(value.average_price, 12),
                realized_pnl=round(value.realized_pnl, 12),
                condition_id=value.condition_id,
                settlement_state=value.settlement_state.value,
                mark_price=None if value.mark_price is None else round(value.mark_price, 12),
                valuation_known=value.quantity <= 0 or value.mark_price is not None,
            )
            for _, value in sorted(self.positions.items())
            if value.quantity > 1e-12 or abs(value.realized_pnl) > 1e-12
        )
        pending = sum(1 for value in self.positions.values() if value.quantity > 0 and value.settlement_state in {SettlementState.OPEN_UNMARKED, SettlementState.OPEN_MARKED, SettlementState.RESOLUTION_PENDING})
        payload = {
            "cash": round(self.cash_usd, 12),
            "equity": None if equity is None else round(equity, 12),
            "positions": [position.to_dict() for position in positions],
            "timestamp_utc": timestamp_utc,
        }
        return PaperPortfolioSnapshot(
            schema_version="senex-paper-v1",
            timestamp_utc=timestamp_utc,
            code_sha=code_sha,
            config_sha=config_sha,
            source_evidence_hash=source_evidence_hash,
            deterministic_id=deterministic_id("portfolio_snapshot", payload),
            provenance="REPLAYABLE_VIRTUAL_LEDGER",
            cash_usd=round(self.cash_usd, 12),
            realized_pnl=round(self.realized_pnl, 12),
            unrealized_pnl=None if unrealized is None else round(unrealized, 12),
            equity_usd=None if equity is None else round(equity, 12),
            gross_exposure_usd=None if market_value is None else round(market_value, 12),
            max_drawdown_pct=round(self.max_drawdown_pct, 12),
            equity_known=unknown == 0,
            unknown_valuation_positions=unknown,
            pending_settlement_count=pending,
            realized_settled_pnl=round(self.realized_settled_pnl, 12),
            marked_unsettled_pnl=None if unrealized is None else round(unrealized, 12),
            positions=positions,
        )

    @classmethod
    def replay(cls, *, starting_equity_usd: float, records: Iterable[Mapping[str, Any]]) -> "PaperPortfolio":
        portfolio = cls(starting_equity_usd=starting_equity_usd)
        for record in records:
            if record.get("type") == "PAPER_FILL":
                fill = PaperFill(**record["fill"])
                portfolio.apply_fill(fill, record=False)
            elif record.get("type") == "PAPER_MARK":
                portfolio.mark_position(
                    token_id=str(record["token_id"]),
                    price=float(record["price"]),
                    record=False,
                )
            elif record.get("type") == "PAPER_SETTLEMENT":
                evidence_payload = dict(record["evidence"])
                evidence_payload.pop("identity_hash", None)
                evidence_payload["token_ids"] = tuple(evidence_payload.get("token_ids") or ())
                evidence = ResolutionEvidence(**evidence_payload)
                token_id = str(record["result"]["token_id"])
                portfolio.apply_settlement(evidence=evidence, token_id=token_id, record=False)
            else:
                continue
            portfolio.ledger.append(dict(record))
        return portfolio
