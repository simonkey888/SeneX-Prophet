"""Read-only Boros market context.

Boros is auxiliary real funding/yield context only. It does not create a BTC
directional signal in SENEX v1 because its funding/APR horizon is not the
canonical SENEX 1h outcome horizon and is not the Polymarket 5m horizon.

Public source: https://api-boros.pendle.finance/apis/v1/markets
No wallet, account, agent, signer, calldata, or transaction endpoints are used.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import threading
import time
from typing import Any

import httpx

log = logging.getLogger("senecio.boros")

BOROS_MARKETS_URL = "https://api-boros.pendle.finance/apis/v1/markets"
REFRESH_S = 60


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_market_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    return [x for x in results if isinstance(x, dict)] if isinstance(results, list) else []


def normalize_boros_market(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize the current `/v1/markets` schema used by Boros SDK/examples."""
    im_data = raw.get("imData") if isinstance(raw.get("imData"), dict) else {}
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    ext = raw.get("extConfig") if isinstance(raw.get("extConfig"), dict) else {}
    underlying = metadata.get("underlyingSymbol")
    funding_symbol = metadata.get("fundingRateSymbol")
    symbol = underlying or funding_symbol or im_data.get("symbol") or im_data.get("name")
    return {
        "market_id": raw.get("marketId"),
        "name": im_data.get("name"),
        "symbol": symbol,
        "underlying_symbol": underlying,
        "funding_rate_symbol": funding_symbol,
        "maturity": im_data.get("maturity"),
        "payment_period_s": ext.get("paymentPeriod"),
        "mid_apr": _as_float(data.get("midApr")),
        "mark_apr": _as_float(data.get("markApr")),
        "volume_24h": _as_float(data.get("volume24h")),
        "open_interest_notional": _as_float(data.get("notionalOI")),
        "asset_mark_price": _as_float(data.get("assetMarkPrice")),
        "next_settlement_time": data.get("nextSettlementTime"),
        "time_to_maturity": data.get("timeToMaturity"),
        "max_leverage": metadata.get("maxLeverage"),
        "is_ui_whitelisted": metadata.get("isUiWhitelisted"),
    }


def _is_relevant(market: dict[str, Any]) -> bool:
    text = " ".join(
        str(market.get(k) or "")
        for k in ("name", "symbol", "underlying_symbol", "funding_rate_symbol")
    ).upper()
    return any(token in text for token in ("BTC", "ETH", "BITCOIN", "ETHEREUM"))


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
                log.warning("Boros refresh error: %s", type(exc).__name__)
                with self._lock:
                    self._last_error = type(exc).__name__

    async def refresh(self) -> None:
        payload = None
        error: str | None = None
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as client:
            try:
                response = await client.get(
                    BOROS_MARKETS_URL,
                    params={"isMatured": "false", "isUiWhitelisted": "true", "limit": 100},
                )
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
        if markets and (age is None or age <= REFRESH_S * 2.5):
            status = "LIVE"
        elif error:
            status = "UNAVAILABLE"
        else:
            status = "EMPTY"
        return {
            "source": "BOROS_PUBLIC_API",
            "status": status,
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
