"""Read-only Kalshi BTC 15-minute prediction-market context.

The adapter deliberately uses only public REST market-data endpoints. It does
not create or load an API key and never touches portfolio/order endpoints.

Kalshi's KXBTC15M horizon differs from SENEX's authoritative 1h score and from
Polymarket's 5m market, so this adapter is cross-venue evidence only in v1.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import threading
import time
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger("senecio.kalshi")

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
SERIES_TICKER = "KXBTC15M"
REFRESH_S = 10


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ts(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _choose_market(markets: list[dict[str, Any]], now_ts: float | None = None) -> dict[str, Any] | None:
    now_ts = time.time() if now_ts is None else now_ts
    open_markets = [m for m in markets if isinstance(m, dict) and str(m.get("status") or "").lower() in {"open", "active"}]
    if not open_markets:
        return None

    def rank(m: dict[str, Any]) -> tuple[int, float]:
        close = _ts(m.get("close_time") or m.get("expected_expiration_time") or m.get("expiration_time"))
        still_open = 1 if close is None or close >= now_ts else 0
        distance = abs((close or now_ts) - now_ts)
        return (-still_open, distance)

    return sorted(open_markets, key=rank)[0]


def normalize_market(market: dict[str, Any], exchange_status: dict[str, Any] | None = None) -> dict[str, Any]:
    yes_bid = _float(market.get("yes_bid_dollars"))
    yes_ask = _float(market.get("yes_ask_dollars"))
    no_bid = _float(market.get("no_bid_dollars"))
    no_ask = _float(market.get("no_ask_dollars"))
    last = _float(market.get("last_price_dollars"))
    if yes_bid is not None and yes_ask is not None and yes_ask >= yes_bid:
        yes_mid = (yes_bid + yes_ask) / 2.0
    else:
        yes_mid = last
    yes_probability = max(0.0, min(1.0, yes_mid)) if yes_mid is not None else None
    no_probability = 1.0 - yes_probability if yes_probability is not None else None
    spread = (yes_ask - yes_bid) if yes_bid is not None and yes_ask is not None else None
    close_ts = _ts(market.get("close_time") or market.get("expected_expiration_time") or market.get("expiration_time"))
    now = time.time()
    return {
        "source": "KALSHI_PUBLIC_REST",
        "series_ticker": SERIES_TICKER,
        "ticker": market.get("ticker"),
        "event_ticker": market.get("event_ticker"),
        "title": market.get("title"),
        "subtitle": market.get("subtitle"),
        "status": market.get("status"),
        "yes_probability": round(yes_probability, 6) if yes_probability is not None else None,
        "no_probability": round(no_probability, 6) if no_probability is not None else None,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "yes_spread": round(spread, 6) if spread is not None else None,
        "last_price": last,
        "volume": _float(market.get("volume_fp")),
        "volume_24h": _float(market.get("volume_24h_fp")),
        "open_interest": _float(market.get("open_interest_fp")),
        "liquidity_dollars": _float(market.get("liquidity_dollars")),
        "close_time": market.get("close_time"),
        "seconds_to_close": max(0, int(close_ts - now)) if close_ts is not None else None,
        "exchange_active": (exchange_status or {}).get("exchange_active"),
        "trading_active": (exchange_status or {}).get("trading_active"),
        "directional_use": False,
        "horizon": "15m",
    }


class KalshiMarketAdapter:
    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self._lock = threading.RLock()
        self._market: dict[str, Any] | None = None
        self._last_refresh: float | None = None
        self._last_error: str | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self.refresh()
        self._task = asyncio.create_task(self._loop(), name="kalshi_market_context")

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
                log.warning("Kalshi refresh error: %s", type(exc).__name__)
                with self._lock:
                    self._last_error = type(exc).__name__

    async def refresh(self) -> None:
        normalized: dict[str, Any] | None = None
        error: str | None = None
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as client:
            try:
                market_response, status_response = await asyncio.gather(
                    client.get(
                        f"{BASE_URL}/markets",
                        params={"series_ticker": SERIES_TICKER, "status": "open", "limit": 20},
                    ),
                    client.get(f"{BASE_URL}/exchange/status"),
                )
                if market_response.status_code != 200:
                    error = f"MARKETS_HTTP_{market_response.status_code}"
                else:
                    payload = market_response.json()
                    markets = payload.get("markets") if isinstance(payload, dict) else []
                    chosen = _choose_market(markets if isinstance(markets, list) else [])
                    exchange_status = status_response.json() if status_response.status_code == 200 else {}
                    if chosen:
                        normalized = normalize_market(chosen, exchange_status if isinstance(exchange_status, dict) else {})
                    else:
                        error = "NO_OPEN_KXBTC15M"
            except Exception as exc:
                error = type(exc).__name__

        with self._lock:
            if normalized is not None:
                self._market = normalized
                self._last_refresh = time.monotonic()
                self._last_error = None
            else:
                self._last_error = error

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            market = copy.deepcopy(self._market)
            last_refresh = self._last_refresh
            error = self._last_error
        age = time.monotonic() - last_refresh if last_refresh is not None else None
        live = bool(market) and (age is None or age <= REFRESH_S * 3)
        return {
            "source": "KALSHI_PUBLIC_REST",
            "status": "LIVE" if live else ("UNAVAILABLE" if error else "EMPTY"),
            "read_only": True,
            "directional_use": False,
            "purpose": "cross_venue_prediction_market_context",
            "horizon": "15m",
            "market": market,
            "freshness_s": round(age, 3) if age is not None else None,
            "last_error": error,
        }


_ADAPTER = KalshiMarketAdapter()


def get_kalshi_adapter() -> KalshiMarketAdapter:
    return _ADAPTER


def get_kalshi_snapshot() -> dict[str, Any]:
    return _ADAPTER.snapshot()
