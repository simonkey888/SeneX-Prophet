"""Read-only live Polymarket adapter for the current BTC Up/Down 5m market.

No wallet, signer, API key, order placement, or transaction code exists here.
Public sources only:
- Gamma API for event/market discovery
- CLOB REST for initial order books
- CLOB public market WebSocket for live book / BBO / trade updates
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import httpx
import websockets

log = logging.getLogger("senecio.polymarket")

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WINDOW_S = 300
DISCOVERY_INTERVAL_S = 15
TEXT_HEARTBEAT_S = 9


def _jsonish(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def current_window_start(now_ts: int | None = None) -> int:
    now_ts = int(time.time() if now_ts is None else now_ts)
    return (now_ts // WINDOW_S) * WINDOW_S


def candidate_slugs(now_ts: int | None = None) -> list[str]:
    start = current_window_start(now_ts)
    offsets = (0, WINDOW_S, -WINDOW_S, -2 * WINDOW_S, 2 * WINDOW_S, -3 * WINDOW_S, 3 * WINDOW_S)
    return [f"btc-updown-5m-{start + offset}" for offset in offsets]


def _levels(raw: Any, *, bids: bool) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict):
            p = _float(item.get("price"), None)
            s = _float(item.get("size"), None)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            p, s = _float(item[0], None), _float(item[1], None)
        else:
            continue
        if p is None or s is None:
            continue
        out.append({"price": p, "size": s})
    out.sort(key=lambda x: x["price"], reverse=bids)
    return out


def book_metrics(book: dict[str, Any] | None) -> dict[str, Any]:
    book = book or {}
    bids = _levels(book.get("bids"), bids=True)
    asks = _levels(book.get("asks"), bids=False)
    best_bid = bids[0]["price"] if bids else _float(book.get("best_bid"), None)
    best_ask = asks[0]["price"] if asks else _float(book.get("best_ask"), None)
    mid = None
    spread = None
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2.0
        spread = max(0.0, best_ask - best_bid)
    bid_depth = sum(x["size"] for x in bids[:5])
    ask_depth = sum(x["size"] for x in asks[:5])
    denom = bid_depth + ask_depth
    imbalance = (bid_depth - ask_depth) / denom if denom > 0 else 0.0
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": spread,
        "bid_depth_5": round(bid_depth, 6),
        "ask_depth_5": round(ask_depth, 6),
        "depth_imbalance": round(imbalance, 6),
        "last_trade_price": _float(book.get("last_trade_price"), None),
        "timestamp": book.get("timestamp"),
        "hash": book.get("hash"),
    }


def _event_market(event: dict[str, Any]) -> dict[str, Any] | None:
    markets = event.get("markets") or []
    if not isinstance(markets, list):
        return None
    candidates: list[dict[str, Any]] = []
    for market in markets:
        if not isinstance(market, dict):
            continue
        if market.get("closed") is True or market.get("active") is False:
            continue
        candidates.append(market)
    if not candidates:
        return None
    candidates.sort(key=lambda m: bool(m.get("acceptingOrders") or m.get("accepting_orders")), reverse=True)
    return candidates[0]


def normalize_event(event: dict[str, Any], now_ts: int | None = None) -> dict[str, Any] | None:
    now_ts = int(time.time() if now_ts is None else now_ts)
    market = _event_market(event)
    if market is None:
        return None
    outcomes = _jsonish(market.get("outcomes"))
    token_ids = _jsonish(market.get("clobTokenIds") or market.get("clob_token_ids"))
    if len(outcomes) < 2 or len(token_ids) < 2:
        return None
    mapping = {str(label).strip().lower(): str(token_ids[i]) for i, label in enumerate(outcomes[: len(token_ids)])}
    up_id = mapping.get("up") or mapping.get("yes") or str(token_ids[0])
    down_id = mapping.get("down") or mapping.get("no") or str(token_ids[1])

    slug = event.get("slug") or market.get("slug")
    suffix_ts = None
    try:
        suffix_ts = int(str(slug).rsplit("-", 1)[1])
    except Exception:
        pass

    start_ts = suffix_ts
    end_ts = suffix_ts + WINDOW_S if suffix_ts is not None else None
    if end_ts is None:
        end_raw = market.get("endDate") or event.get("endDate")
        if end_raw:
            try:
                end_ts = int(datetime.fromisoformat(str(end_raw).replace("Z", "+00:00")).timestamp())
            except Exception:
                end_ts = None

    return {
        "event_id": event.get("id"),
        "market_id": market.get("id"),
        "condition_id": market.get("conditionId") or market.get("condition_id"),
        "slug": slug,
        "question": market.get("question") or event.get("title") or "BTC Up or Down - 5 Minutes",
        "start_ts": start_ts,
        "end_ts": end_ts,
        "active": market.get("active") is not False and event.get("active") is not False,
        "closed": bool(market.get("closed") or event.get("closed")),
        "accepting_orders": bool(market.get("acceptingOrders") or market.get("accepting_orders")),
        "outcomes": outcomes,
        "up_token_id": up_id,
        "down_token_id": down_id,
        "resolution_source": event.get("resolutionSource") or market.get("resolutionSource"),
        "gamma_best_bid": _float(market.get("bestBid"), None),
        "gamma_best_ask": _float(market.get("bestAsk"), None),
        "gamma_last_trade": _float(market.get("lastTradePrice"), None),
        "liquidity": _float(market.get("liquidity"), None),
        "volume": _float(market.get("volume"), None),
    }


class PolymarketMarketAdapter:
    def __init__(self) -> None:
        self._running = False
        self._supervisor_task: asyncio.Task | None = None
        self._ws_task: asyncio.Task | None = None
        self._lock = threading.RLock()
        self._market: dict[str, Any] | None = None
        self._books: dict[str, dict[str, Any]] = {"UP": {}, "DOWN": {}}
        self._events: deque[dict[str, Any]] = deque(maxlen=80)
        self._ws_connected = False
        self._last_update_monotonic: float | None = None
        self._last_error: str | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._refresh_market()
        self._supervisor_task = asyncio.create_task(self._supervisor(), name="polymarket_market_supervisor")

    async def stop(self) -> None:
        self._running = False
        for task in (self._ws_task, self._supervisor_task):
            if task:
                task.cancel()
        for task in (self._ws_task, self._supervisor_task):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._ws_task = None
        self._supervisor_task = None
        with self._lock:
            self._ws_connected = False

    async def _supervisor(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(DISCOVERY_INTERVAL_S)
                await self._refresh_market()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Polymarket discovery loop error: %s", type(exc).__name__)
                with self._lock:
                    self._last_error = type(exc).__name__

    async def _refresh_market(self) -> None:
        now_ts = int(time.time())
        found: dict[str, Any] | None = None
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as client:
            for slug in candidate_slugs(now_ts):
                try:
                    response = await client.get(f"{GAMMA_BASE}/events/slug/{slug}")
                except Exception:
                    continue
                if response.status_code != 200:
                    continue
                try:
                    event = response.json()
                except Exception:
                    continue
                normalized = normalize_event(event, now_ts)
                if not normalized:
                    continue
                start_ts = normalized.get("start_ts")
                end_ts = normalized.get("end_ts")
                if start_ts is not None and start_ts > now_ts + 30:
                    continue
                if end_ts is not None and end_ts < now_ts - 20:
                    continue
                if normalized.get("closed"):
                    continue
                found = normalized
                break

            if found is None:
                with self._lock:
                    self._last_error = "NO_CURRENT_BTC_5M_MARKET"
                return

            with self._lock:
                previous_tokens = None
                if self._market:
                    previous_tokens = (self._market.get("up_token_id"), self._market.get("down_token_id"))
            new_tokens = (found["up_token_id"], found["down_token_id"])

            if previous_tokens != new_tokens:
                bootstrap: dict[str, dict[str, Any]] = {}
                for label, token_id in (("UP", found["up_token_id"]), ("DOWN", found["down_token_id"])):
                    try:
                        book_response = await client.get(f"{CLOB_BASE}/book", params={"token_id": token_id})
                        bootstrap[label] = book_response.json() if book_response.status_code == 200 else {}
                    except Exception:
                        bootstrap[label] = {}
                with self._lock:
                    self._market = found
                    self._books = bootstrap
                    self._last_error = None
                    self._last_update_monotonic = time.monotonic()
                    self._events.clear()
                if self._ws_task:
                    self._ws_task.cancel()
                    try:
                        await self._ws_task
                    except asyncio.CancelledError:
                        pass
                self._ws_task = asyncio.create_task(
                    self._ws_loop(found["up_token_id"], found["down_token_id"]),
                    name="polymarket_clob_ws",
                )
            else:
                with self._lock:
                    self._market = found
                    self._last_error = None

    async def _ws_loop(self, up_token_id: str, down_token_id: str) -> None:
        token_to_label = {str(up_token_id): "UP", str(down_token_id): "DOWN"}
        while self._running:
            with self._lock:
                current = self._market or {}
                if (current.get("up_token_id"), current.get("down_token_id")) != (up_token_id, down_token_id):
                    return
            try:
                async with websockets.connect(CLOB_WS, ping_interval=None, close_timeout=5) as ws:
                    await ws.send(json.dumps({
                        "assets_ids": [up_token_id, down_token_id],
                        "type": "market",
                        "custom_feature_enabled": True,
                    }))
                    last_ping = time.monotonic()
                    with self._lock:
                        self._ws_connected = True
                        self._last_error = None
                    while self._running:
                        now = time.monotonic()
                        if now - last_ping >= TEXT_HEARTBEAT_S:
                            await ws.send("PING")
                            last_ping = now
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                        except asyncio.TimeoutError:
                            continue
                        if raw in ("PONG", "PING"):
                            continue
                        try:
                            payload = json.loads(raw)
                        except Exception:
                            continue
                        messages = payload if isinstance(payload, list) else [payload]
                        for msg in messages:
                            if isinstance(msg, dict):
                                self._handle_ws_message(msg, token_to_label)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with self._lock:
                    self._ws_connected = False
                    self._last_error = f"WS_{type(exc).__name__}"
                await asyncio.sleep(2.0)
            finally:
                with self._lock:
                    self._ws_connected = False

    def _handle_ws_message(self, msg: dict[str, Any], token_to_label: dict[str, str]) -> None:
        event_type = str(msg.get("event_type") or "unknown")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if event_type == "book":
                asset = str(msg.get("asset_id") or "")
                label = token_to_label.get(asset)
                if label:
                    self._books[label] = msg
            elif event_type == "best_bid_ask":
                asset = str(msg.get("asset_id") or "")
                label = token_to_label.get(asset)
                if label:
                    book = dict(self._books.get(label) or {})
                    book["best_bid"] = msg.get("best_bid")
                    book["best_ask"] = msg.get("best_ask")
                    self._books[label] = book
            elif event_type == "price_change":
                changes = msg.get("price_changes") or []
                if isinstance(changes, list):
                    for change in changes:
                        if not isinstance(change, dict):
                            continue
                        asset = str(change.get("asset_id") or "")
                        label = token_to_label.get(asset)
                        if label:
                            book = dict(self._books.get(label) or {})
                            if change.get("best_bid") is not None:
                                book["best_bid"] = change.get("best_bid")
                            if change.get("best_ask") is not None:
                                book["best_ask"] = change.get("best_ask")
                            self._books[label] = book
            elif event_type == "last_trade_price":
                asset = str(msg.get("asset_id") or "")
                label = token_to_label.get(asset)
                if label:
                    book = dict(self._books.get(label) or {})
                    book["last_trade_price"] = msg.get("price")
                    self._books[label] = book
            self._events.appendleft({
                "event_type": event_type,
                "outcome": token_to_label.get(str(msg.get("asset_id") or "")),
                "timestamp": msg.get("timestamp") or now,
                "best_bid": msg.get("best_bid"),
                "best_ask": msg.get("best_ask"),
                "price": msg.get("price"),
                "side": msg.get("side"),
            })
            self._last_update_monotonic = time.monotonic()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            market = copy.deepcopy(self._market)
            books = copy.deepcopy(self._books)
            events = list(copy.deepcopy(self._events))
            ws_connected = self._ws_connected
            last_update = self._last_update_monotonic
            last_error = self._last_error

        now_ts = int(time.time())
        if not market:
            return {
                "source": "POLYMARKET_PUBLIC",
                "status": "UNAVAILABLE",
                "read_only": True,
                "ws_connected": False,
                "eligible_for_prediction": False,
                "last_error": last_error,
                "recent_events": events[:20],
            }

        up = book_metrics(books.get("UP"))
        down = book_metrics(books.get("DOWN"))
        up_mid = up.get("mid")
        down_mid = down.get("mid")
        if up_mid is None:
            up_mid = up.get("last_trade_price") or market.get("gamma_last_trade")
        if down_mid is None and up_mid is not None:
            down_mid = max(0.0, min(1.0, 1.0 - float(up_mid)))
        denom = float(up_mid or 0) + float(down_mid or 0)
        up_probability = float(up_mid) / denom if up_mid is not None and denom > 0 else None
        down_probability = 1.0 - up_probability if up_probability is not None else None

        prob_pressure = (2.0 * up_probability - 1.0) if up_probability is not None else 0.0
        depth_pressure = (float(up.get("depth_imbalance") or 0) - float(down.get("depth_imbalance") or 0)) / 2.0
        directional_pressure = max(-1.0, min(1.0, 0.75 * prob_pressure + 0.25 * depth_pressure))
        freshness_s = (time.monotonic() - last_update) if last_update is not None else None
        seconds_to_close = max(0, int(market["end_ts"] - now_ts)) if market.get("end_ts") else None
        live = (
            bool(market.get("active"))
            and not bool(market.get("closed"))
            and bool(market.get("accepting_orders"))
            and (seconds_to_close is None or seconds_to_close > 0)
        )
        status = "LIVE_WS" if live and ws_connected else ("LIVE_REST" if live else "STALE")
        eligible = bool(
            live
            and up_probability is not None
            and (freshness_s is None or freshness_s <= 45)
            and (seconds_to_close is None or seconds_to_close >= 20)
        )

        return {
            "source": "POLYMARKET_PUBLIC",
            "status": status,
            "read_only": True,
            "ws_connected": ws_connected,
            "market": market,
            "up": up,
            "down": down,
            "up_probability": round(up_probability, 6) if up_probability is not None else None,
            "down_probability": round(down_probability, 6) if down_probability is not None else None,
            "directional_pressure": round(directional_pressure, 6),
            "eligible_for_prediction": eligible,
            "seconds_to_close": seconds_to_close,
            "freshness_s": round(freshness_s, 3) if freshness_s is not None else None,
            "recent_events": events[:20],
            "last_error": last_error,
        }


_ADAPTER = PolymarketMarketAdapter()


def get_polymarket_adapter() -> PolymarketMarketAdapter:
    return _ADAPTER


def get_polymarket_snapshot() -> dict[str, Any]:
    return _ADAPTER.snapshot()
