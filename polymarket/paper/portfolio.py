"""Append-only virtual portfolio with deterministic ledger replay."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import (
    PaperFill,
    PaperPortfolioSnapshot,
    PaperPosition,
    deterministic_id,
)


@dataclass
class _MutablePosition:
    market_id: str
    token_id: str
    outcome: str
    quantity: float = 0.0
    average_price: float = 0.0
    realized_pnl: float = 0.0


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
        self.max_equity = float(starting_equity_usd)
        self.max_drawdown_pct = 0.0
        self.positions: dict[str, _MutablePosition] = {}
        self.ledger: list[dict[str, Any]] = []
        self.journal = journal
        self.consecutive_losses = 0

    def _record(self, fill: PaperFill) -> None:
        record = {"type": "PAPER_FILL", "fill": fill.to_dict()}
        self.ledger.append(record)
        if self.journal is not None:
            self.journal.append(record)

    def apply_fill(self, fill: PaperFill, *, record: bool = True) -> None:
        position = self.positions.setdefault(
            fill.token_id,
            _MutablePosition(fill.market_id, fill.token_id, fill.outcome),
        )
        quantity = fill.filled_shares
        if fill.side == "BUY":
            total_cost = fill.gross_notional_usd + fill.fee_usd
            if total_cost > self.cash_usd + 1e-9:
                raise ValueError("virtual cash insufficient")
            new_quantity = position.quantity + quantity
            if new_quantity <= 0:
                raise ValueError("invalid resulting position")
            position.average_price = (
                (position.quantity * position.average_price) + fill.gross_notional_usd
            ) / new_quantity
            position.quantity = new_quantity
            self.cash_usd -= total_cost
        elif fill.side == "SELL":
            if quantity > position.quantity + 1e-9:
                raise ValueError("paper sell exceeds virtual holdings")
            proceeds = fill.gross_notional_usd - fill.fee_usd
            pnl = (fill.fill_price - position.average_price) * quantity - fill.fee_usd
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
        else:
            raise ValueError(f"unsupported side {fill.side}")
        if record:
            self._record(fill)

    def mark_to_market(self, prices: Mapping[str, float]) -> tuple[float, float, float]:
        market_value = 0.0
        unrealized = 0.0
        for token_id, position in self.positions.items():
            if position.quantity <= 0:
                continue
            price = float(prices.get(token_id, position.average_price))
            market_value += position.quantity * price
            unrealized += position.quantity * (price - position.average_price)
        equity = self.cash_usd + market_value
        self.max_equity = max(self.max_equity, equity)
        if self.max_equity > 0:
            drawdown = max(0.0, (self.max_equity - equity) / self.max_equity * 100.0)
            self.max_drawdown_pct = max(self.max_drawdown_pct, drawdown)
        return equity, unrealized, market_value

    def snapshot(
        self,
        *,
        timestamp_utc: str,
        code_sha: str,
        config_sha: str,
        source_evidence_hash: str,
        prices: Mapping[str, float],
    ) -> PaperPortfolioSnapshot:
        equity, unrealized, market_value = self.mark_to_market(prices)
        positions = tuple(
            PaperPosition(
                market_id=value.market_id,
                token_id=value.token_id,
                outcome=value.outcome,
                quantity=round(value.quantity, 12),
                average_price=round(value.average_price, 12),
                realized_pnl=round(value.realized_pnl, 12),
            )
            for _, value in sorted(self.positions.items())
            if value.quantity > 1e-12 or abs(value.realized_pnl) > 1e-12
        )
        payload = {
            "cash": round(self.cash_usd, 12),
            "equity": round(equity, 12),
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
            unrealized_pnl=round(unrealized, 12),
            equity_usd=round(equity, 12),
            gross_exposure_usd=round(market_value, 12),
            max_drawdown_pct=round(self.max_drawdown_pct, 12),
            positions=positions,
        )

    @classmethod
    def replay(cls, *, starting_equity_usd: float, records: Iterable[Mapping[str, Any]]) -> "PaperPortfolio":
        portfolio = cls(starting_equity_usd=starting_equity_usd)
        for record in records:
            if record.get("type") != "PAPER_FILL":
                continue
            payload = record["fill"]
            fill = PaperFill(**payload)
            portfolio.apply_fill(fill, record=False)
            portfolio.ledger.append(dict(record))
        return portfolio
