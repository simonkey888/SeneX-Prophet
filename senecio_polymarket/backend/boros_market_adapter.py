"""Read-only Boros market context.

Boros is used only as auxiliary funding/yield context. It does not create a
BTC directional signal in SENEX v1 because implied/underlying APR is not a
validated causal mapping to 5-minute BTC direction.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import threading
import time
from typing import Any

import httpx

log = logging.getLogger("senecio.boros")

BOROS_MARKETS_URL = "https://api-boros.pendle.finance/apis/v1/markets"
REFRESH_S = 60


def _first(obj: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in obj and obj[name] is not None:
            return obj[name]
    return None


def _extract_market_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "data", "markets", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            nested = _extract_market_list(value)
            if nested:
                return nested
    return []


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_boros_market(raw: dict[str, Any]) -> dict[str, Any]:
    descriptor = raw.get("descriptor") if isinstance(raw.get("descriptor"), dict) else {}
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    state = raw.get("state") if isinstance(raw.get("state"), dict) else {}
    merged = {**descriptor, **metadata, **state, **raw}
    symbol = _first(
        merged,
        "fundingRateSymbol", "funding_rate_symbol", "symbol", "pair", "name", "marketName", "displayName",
    )
    return {
        "market_id": _first(merged, "marketId", "market_id", "id"),
        "symbol": symbol,
        "exchange": _first(merged, "exchange", "venue", "source", "underlyingExchange"),
        "maturity": _first(merged, "maturity", "expiry", "expiryTimestamp", "maturityTimestamp"),
        "mid_apr": _as_float(_first(merged, "midApr", "midAPR", "midRate", "mid")),
        "mark_apr": _as_float(_first(merged, "markApr", "markAPR", "markRate", "mark")),
        "last_traded_apr": _as_float(_first(merged, "lastTradedApr", "lastTradeApr", "lastRate", "lastTradedRate")),
        "underlying_apr": _as_float(_first(merged, "underlyingApr", "underlyingAPR", "underlyingRate", "underlying")),
        "best_bid_apr": _as_float(_first(merged, "bestBid", "bestBidApr", "bestBidAPR")),
        "best_ask_apr": _as_float(_first(merged, "bestAsk", "bestAskApr", "bestAskAPR")),
        "open_interest": _as_float(_first(merged, "openInterest", "open_interest", "oi")),
        "is_whitelisted": _first(merged, "isWhitelisted", "is_whitelisted"),
    }


def _is_relevant(market: dict[str, Any]) -> bool:
    text = " ".join(str(v or "") for v in (market.get("symbol"), market.get("exchange"))).upper()
    return "BTCUSDT" in text or "ETHUSDT" in text or "BTC/USDT" in text or "ETH/USDT" in text


class BorosMarketAdapter:
    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self._lock = threading.RLock()
        self._markets: list[dict[str, Any]] = []
        self._last_refresh: float | None = None
        self._last_error: str | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self.refresh()
        self._task = asyncio.create_task(self._loop(), name="boros_market_context")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(REFRESH_S)
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with self._lock:
                    self._last_error = type(exc).__name__

    async def refresh(self) -> None:
        payload = None
        error: str | None = None
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as client:
            try:
                response = await client.get(BOROS_MARKETS_URL, params={"limit": 100})
                if response.status_code in (400, 404, 422):
                    response = await client.get(BOROS_MARKETS_URL)
                if response.status_code == 200:
                    payload = response.json()
                else:
                    error = f"HTTP_{response.status_code}"
            except Exception as exc:
                error = type(exc).__name__

        raw_markets = _extract_market_list(payload)
        normalized = [normalize_boros_market(m) for m in raw_markets]
        relevant = [m for m in normalized if _is_relevant(m)]
        with self._lock:
            self._markets = relevant[:20]
            self._last_refresh = time.monotonic() if payload is not None else self._last_refresh
            self._last_error = error

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            markets = copy.deepcopy(self._markets)
            last_refresh = self._last_refresh
            error = self._last_error
        age = time.monotonic() - last_refresh if last_refresh is not None else None
        return {
            "source": "BOROS_PUBLIC_API",
            "status": "LIVE" if markets and (age is None or age <= REFRESH_S * 2.5) else ("EMPTY" if not error else "UNAVAILABLE"),
            "read_only": True,
            "directional_use": False,
            "purpose": "funding_yield_context_only",
            "markets": markets,
            "freshness_s": round(age, 3) if age is not None else None,
            "last_error": error,
        }


_ADAPTER = BorosMarketAdapter()


def get_boros_adapter() -> BorosMarketAdapter:
    return _ADAPTER


def get_boros_snapshot() -> dict[str, Any]:
    return _ADAPTER.snapshot()
