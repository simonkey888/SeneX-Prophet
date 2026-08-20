"""SENECIO ShadowLive comparator.

NO REAL ORDERS are placed. Real-market data is read only. ORDER-069 adds an
optional public book provider while preserving the historical OKX fallback when
no provider is injected.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("senecio.shadow_live")

DEFAULTS: dict[str, Any] = {
    "duration_days": 7,
    "output_path": "data/journal/shadow_trades.jsonl",
    "report_path": "data/journal/shadow_report.json",
    "exchange": "okx",
    "fetch_real_book": True,
    "real_book_timeout_ms": 2000,
    "min_fills_for_report": 30,
    "thresholds": {
        "max_slippage_diff_bps": 3.0,
        "max_fee_diff_pct": 5.0,
        "max_latency_diff_ms": 100,
        "min_fill_match_pct": 85.0,
    },
}


@dataclass
class ShadowTrade:
    shadow_id: str
    paper_order_id: str
    symbol: str
    direction: str
    side: str
    expected_qty: float
    expected_price: float
    expected_slippage_bps: float
    expected_fee_usd: float
    expected_latency_ms: int
    real_mid_price: float = 0.0
    real_best_bid: float = 0.0
    real_best_ask: float = 0.0
    real_book_depth_usd: float = 0.0
    real_spread_bps: float = 0.0
    real_estimated_fill_price: float = 0.0
    real_estimated_fill_qty: float = 0.0
    real_fee_usd: float = 0.0
    real_latency_ms: int = 0
    slippage_diff_bps: float = 0.0
    fee_diff_usd: float = 0.0
    fee_diff_pct: float = 0.0
    latency_diff_ms: int = 0
    fill_match: bool = False
    paper_fill_ts: str = ""
    real_book_ts: str = ""
    audit_trail: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ShadowLive:
    """Shadow-mode comparator running alongside the PAPER ExecutionEngine.

    ``book_provider`` is optional and must be a callable or object exposing
    ``shadow_live_book(symbol)``. The default remains the pre-existing OKX
    public order-book fetch, preserving backward compatibility.
    """

    def __init__(self, config: Optional[dict[str, Any]] = None, book_provider: Any = None):
        self.cfg = {**DEFAULTS, **(config or {})}
        self.book_provider = book_provider
        self.path = Path(self.cfg["output_path"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path = Path(self.cfg["report_path"])
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.started_at = datetime.now(timezone.utc)
        self.ended_at: Optional[datetime] = None
        self._pending: dict[str, dict] = {}
        self._trades: list[ShadowTrade] = []
        log.info(
            "ShadowLive init: duration=%dd output=%s fetch_real_book=%s provider=%s",
            self.cfg["duration_days"], self.path, self.cfg["fetch_real_book"],
            type(book_provider).__name__ if book_provider is not None else "legacy-okx",
        )

    def on_audit_event(self, event: dict) -> None:
        try:
            if event.get("event") != "FILL":
                return
            fill = event.get("fill") or {}
            if not fill.get("order_id"):
                return
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(self._process_fill(fill))
            except RuntimeError:
                pass
        except Exception as exc:
            log.exception("ShadowLive on_audit_event error: %s", exc)

    async def _process_fill(self, fill: dict) -> None:
        symbol = fill.get("symbol", "")
        side = fill.get("side", "BUY")
        direction = "LONG" if side == "BUY" else "SHORT"
        expected_qty = float(fill.get("qty", 0))
        expected_price = float(fill.get("price", 0))
        expected_slip = float(fill.get("slippage_bps", 0))
        expected_fee = float(fill.get("fee_usd", 0))
        expected_latency = int(fill.get("latency_ms", 0))
        real_book: dict[str, Any] = {}
        if self.cfg["fetch_real_book"]:
            try:
                real_book = await asyncio.wait_for(self._fetch_real_book(symbol), timeout=self.cfg["real_book_timeout_ms"] / 1000.0)
            except asyncio.TimeoutError:
                log.warning("real book fetch timed out for %s", symbol)
            except Exception as exc:
                log.warning("real book fetch failed for %s: %s", symbol, exc)
        real_mid = real_book.get("mid", 0.0); real_bid = real_book.get("bid", 0.0); real_ask = real_book.get("ask", 0.0)
        real_depth = real_book.get("depth_usd", 0.0); real_spread_bps = real_book.get("spread_bps", 0.0); real_latency = real_book.get("fetch_latency_ms", 0)
        if side == "BUY":
            real_fill_price = real_ask or real_mid or expected_price
            real_fill_qty = min(expected_qty, real_depth / max(real_ask, 1e-9)) if real_depth > 0 else expected_qty
        else:
            real_fill_price = real_bid or real_mid or expected_price
            real_fill_qty = min(expected_qty, real_depth / max(real_bid, 1e-9)) if real_depth > 0 else expected_qty
        real_fee = real_fill_qty * real_fill_price * 5 / 10_000
        slippage_diff = expected_slip - real_spread_bps / 2
        fee_diff_usd = expected_fee - real_fee
        fee_diff_pct = abs(fee_diff_usd) / max(real_fee, 1e-9) * 100 if real_fee > 0 else 0
        latency_diff = expected_latency - real_latency
        th = self.cfg["thresholds"]
        fill_match = abs(slippage_diff) <= th["max_slippage_diff_bps"] and fee_diff_pct <= th["max_fee_diff_pct"] and abs(latency_diff) <= th["max_latency_diff_ms"]
        trade = ShadowTrade(
            shadow_id=f"sd-{uuid.uuid4().hex[:12]}", paper_order_id=fill.get("order_id", ""), symbol=symbol,
            direction=direction, side=side, expected_qty=round(expected_qty, 8), expected_price=round(expected_price, 6),
            expected_slippage_bps=round(expected_slip, 2), expected_fee_usd=round(expected_fee, 4), expected_latency_ms=expected_latency,
            real_mid_price=round(real_mid, 6), real_best_bid=round(real_bid, 6), real_best_ask=round(real_ask, 6),
            real_book_depth_usd=round(real_depth, 2), real_spread_bps=round(real_spread_bps, 2),
            real_estimated_fill_price=round(real_fill_price, 6), real_estimated_fill_qty=round(real_fill_qty, 8),
            real_fee_usd=round(real_fee, 4), real_latency_ms=real_latency, slippage_diff_bps=round(slippage_diff, 2),
            fee_diff_usd=round(fee_diff_usd, 4), fee_diff_pct=round(fee_diff_pct, 2), latency_diff_ms=latency_diff,
            fill_match=fill_match, paper_fill_ts=fill.get("ts", ""), real_book_ts=real_book.get("ts", ""),
            audit_trail=[{"fill": fill, "real_book": real_book}],
        )
        self._trades.append(trade); self._append(trade.to_dict())

    async def _fetch_real_book(self, symbol: str) -> dict[str, Any]:
        """Fetch a public book through injected provider or legacy OKX fallback."""
        if self.book_provider is not None:
            provider = self.book_provider
            fn = getattr(provider, "shadow_live_book", None)
            if fn is None and callable(provider): fn = provider
            if not callable(fn): raise TypeError("book_provider must be callable or expose shadow_live_book(symbol)")
            result = fn(symbol)
            if inspect.isawaitable(result): result = await result
            if not isinstance(result, dict): raise TypeError("book_provider result must be dict")
            return result
        def _fetch() -> dict[str, Any]:
            import ccxt
            import time as _time
            t0 = _time.time(); ex = ccxt.okx({"enableRateLimit": True}); ob = ex.fetch_order_book(symbol, limit=5)
            latency_ms = int((_time.time() - t0) * 1000); bids = ob.get("bids") or []; asks = ob.get("asks") or []
            best_bid = float(bids[0][0]) if bids else 0.0; best_ask = float(asks[0][0]) if asks else 0.0
            mid = (best_bid + best_ask) / 2 if (best_bid + best_ask) > 0 else 0.0
            spread_bps = ((best_ask - best_bid) / mid * 10_000) if mid > 0 else 0.0
            depth_usd = sum(float(lvl[0]) * float(lvl[1]) for lvl in bids[:5] + asks[:5])
            return {"bid": best_bid, "ask": best_ask, "mid": mid, "spread_bps": spread_bps, "depth_usd": depth_usd, "fetch_latency_ms": latency_ms, "ts": datetime.now(timezone.utc).isoformat()}
        return await asyncio.to_thread(_fetch)

    def _append(self, record: dict) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as handle: handle.write(json.dumps(record, default=str) + "\n")
        except Exception as exc: log.exception("shadow trade append failed: %s", exc)

    def is_active(self) -> bool:
        if self.ended_at is not None: return False
        return datetime.now(timezone.utc) - self.started_at < timedelta(days=self.cfg["duration_days"])

    def elapsed_days(self) -> float:
        return (datetime.now(timezone.utc) - self.started_at).total_seconds() / 86400.0

    def remaining_days(self) -> float:
        return max(0.0, self.cfg["duration_days"] - self.elapsed_days())

    def generate_report(self) -> dict[str, Any]:
        n = len(self._trades)
        if n < self.cfg["min_fills_for_report"]:
            return {"status": "insufficient_data", "n_fills": n, "min_required": self.cfg["min_fills_for_report"], "passed": False, "started_at": self.started_at.isoformat(), "ended_at": self.ended_at.isoformat() if self.ended_at else None}
        matches = sum(1 for trade in self._trades if trade.fill_match); match_pct = matches / n * 100; th = self.cfg["thresholds"]; passed = match_pct >= th["min_fill_match_pct"]
        avg_slip_diff = sum(t.slippage_diff_bps for t in self._trades) / n; avg_fee_diff_pct = sum(t.fee_diff_pct for t in self._trades) / n; avg_latency_diff = sum(t.latency_diff_ms for t in self._trades) / n
        max_slip_diff = max(abs(t.slippage_diff_bps) for t in self._trades); max_latency_diff = max(abs(t.latency_diff_ms) for t in self._trades)
        by_symbol: dict[str, dict] = {}
        for trade in self._trades:
            stats = by_symbol.setdefault(trade.symbol, {"n": 0, "matches": 0, "slip_diff_sum": 0.0}); stats["n"] += 1; stats["matches"] += int(trade.fill_match); stats["slip_diff_sum"] += trade.slippage_diff_bps
        for stats in by_symbol.values():
            stats["match_pct"] = round(stats["matches"] / stats["n"] * 100, 2) if stats["n"] else 0.0; stats["avg_slip_diff_bps"] = round(stats["slip_diff_sum"] / stats["n"], 2) if stats["n"] else 0.0; del stats["slip_diff_sum"]
        report = {"status": "complete", "n_fills": n, "matches": matches, "match_pct": round(match_pct, 2), "passed": passed, "thresholds": th, "started_at": self.started_at.isoformat(), "ended_at": self.ended_at.isoformat() if self.ended_at else datetime.now(timezone.utc).isoformat(), "duration_days": self.cfg["duration_days"], "elapsed_days": round(self.elapsed_days(), 2), "avg_slip_diff_bps": round(avg_slip_diff, 2), "avg_fee_diff_pct": round(avg_fee_diff_pct, 2), "avg_latency_diff_ms": round(avg_latency_diff, 1), "max_slip_diff_bps": round(max_slip_diff, 2), "max_latency_diff_ms": max_latency_diff, "by_symbol": by_symbol, "computed_at": datetime.now(timezone.utc).isoformat()}
        try:
            with open(self.report_path, "w", encoding="utf-8") as handle: json.dump(report, handle, indent=2, default=str)
        except Exception as exc: log.exception("shadow report write failed: %s", exc)
        return report

    def stop(self) -> dict[str, Any]:
        self.ended_at = datetime.now(timezone.utc); return self.generate_report()

    def fetch_trades(self, limit: int = 50) -> list[dict]:
        if not self.path.exists(): return []
        rows: list[dict] = []
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line: continue
                try: rows.append(json.loads(line))
                except Exception: continue
        rows.reverse(); return rows[:limit]

    def stats(self) -> dict[str, Any]:
        return {"active": self.is_active(), "elapsed_days": round(self.elapsed_days(), 2), "remaining_days": round(self.remaining_days(), 2), "n_trades": len(self._trades), "started_at": self.started_at.isoformat(), "ended_at": self.ended_at.isoformat() if self.ended_at else None}

    def update_config(self, **overrides: Any) -> None:
        self.cfg.update(overrides); log.info("ShadowLive config updated: %s", overrides)
