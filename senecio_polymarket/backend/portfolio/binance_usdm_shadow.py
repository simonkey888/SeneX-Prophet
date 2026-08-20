"""ORDER-069 Binance USD-M public-market shadow adapter.

Credentialless and read-only by construction. This module cannot create, amend,
cancel or authenticate Binance orders. The authoritative shadow fill is a pure,
deterministic book-walk over frozen public response bytes.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN, getcontext
from typing import Any, Callable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

getcontext().prec = 40

BINANCE_USDM_BASE = "https://fapi.binance.com"
SYMBOL = "BTCUSDT"
CCXT_SYMBOL = "BTC/USDT:USDT"
ALLOWED_METHOD = "GET"
ALLOWED_PATHS = frozenset({
    "/fapi/v1/time",
    "/fapi/v1/exchangeInfo",
    "/fapi/v1/depth",
    "/fapi/v1/premiumIndex",
})
FORBIDDEN_OPERATION_NAMES = frozenset({
    "create_order", "create_market_order", "cancel_order", "edit_order",
    "set_leverage", "set_margin_mode", "set_position_mode", "transfer",
    "withdrawal", "withdraw",
})
TARGET_NOTIONAL_MIN = Decimal("10")
TARGET_NOTIONAL_CAP = Decimal("25")
MAX_BOOK_AGE_MS = 15_000


class ShadowBoundaryError(RuntimeError):
    """A request or operation is outside the immutable public-read boundary."""


class ShadowDataError(RuntimeError):
    """Public market data cannot support a safe deterministic shadow result."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def D(value: Any, *, name: str = "value") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ShadowDataError(f"INVALID_DECIMAL:{name}") from exc
    if not result.is_finite():
        raise ShadowDataError(f"NONFINITE_DECIMAL:{name}")
    return result


def decstr(value: Decimal) -> str:
    value = value.normalize()
    text = format(value, "f")
    return "0" if text in {"-0", ""} else text


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ShadowDataError("INVALID_STEP_SIZE")
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def _public_path(url_or_path: str) -> str:
    path = url_or_path.split("?", 1)[0]
    if path.startswith("http://") or path.startswith("https://"):
        marker = path.find("/fapi/")
        if marker < 0:
            raise ShadowBoundaryError("NON_BINANCE_USDM_PATH")
        path = path[marker:]
    return path


@dataclass(frozen=True)
class PublicReceipt:
    path: str
    method: str
    status: int
    body_sha256: str


class PublicGetTransport:
    """Strict GET-only transport. Validation happens before opener invocation."""

    def __init__(
        self,
        *,
        base_url: str = BINANCE_USDM_BASE,
        timeout_s: float = 8.0,
        retries: int = 2,
        opener: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = max(0.2, min(float(timeout_s), 20.0))
        self.retries = max(0, min(int(retries), 3))
        self.opener = opener or urlopen
        self.sleeper = sleeper
        self.receipts: list[dict[str, Any]] = []

    @staticmethod
    def assert_allowed(method: str, path: str) -> None:
        method = str(method).upper()
        path = _public_path(path)
        if method != ALLOWED_METHOD:
            raise ShadowBoundaryError(f"METHOD_FORBIDDEN:{method}")
        if path not in ALLOWED_PATHS:
            raise ShadowBoundaryError(f"PATH_FORBIDDEN:{path}")

    def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> tuple[Any, bytes]:
        self.assert_allowed("GET", path)
        query = urlencode([(str(k), str(v)) for k, v in sorted((params or {}).items())])
        url = self.base_url + path + ("?" + query if query else "")
        last: BaseException | None = None
        for attempt in range(self.retries + 1):
            try:
                req = Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "senex-order069-shadow/1"})
                with self.opener(req, timeout=self.timeout_s) as response:
                    status = int(getattr(response, "status", response.getcode()))
                    body = response.read(12_000_000)
                if status != 200:
                    raise ShadowDataError(f"HTTP_STATUS:{status}:{path}")
                try:
                    parsed = json.loads(body)
                except Exception as exc:
                    raise ShadowDataError(f"MALFORMED_JSON:{path}") from exc
                self.receipts.append(PublicReceipt(path, "GET", status, sha256_bytes(body)).__dict__)
                return parsed, body
            except ShadowDataError:
                raise
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                last = exc
                if attempt < self.retries:
                    self.sleeper(0.2 * (attempt + 1))
                    continue
                raise ShadowDataError(f"PUBLIC_READ_FAILED:{path}:{type(exc).__name__}") from exc
        raise ShadowDataError(f"PUBLIC_READ_FAILED:{path}:{type(last).__name__ if last else 'UNKNOWN'}")


class BinanceUsdMShadowProvider:
    """Credentialless provider for official Binance USD-M public market truth."""

    def __init__(self, *, transport: PublicGetTransport | None = None, symbol: str = SYMBOL, depth_limit: int = 100) -> None:
        if symbol != SYMBOL:
            raise ShadowBoundaryError(f"SYMBOL_NOT_ALLOWED:{symbol}")
        if depth_limit not in {5, 10, 20, 50, 100, 500, 1000}:
            raise ShadowBoundaryError("DEPTH_LIMIT_NOT_ALLOWED")
        self.transport = transport or PublicGetTransport()
        self.symbol = symbol
        self.depth_limit = depth_limit

    def capture(self) -> dict[str, Any]:
        server, server_raw = self.transport.get_json("/fapi/v1/time")
        info, info_raw = self.transport.get_json("/fapi/v1/exchangeInfo")
        depth, depth_raw = self.transport.get_json("/fapi/v1/depth", {"symbol": self.symbol, "limit": self.depth_limit})
        premium, premium_raw = self.transport.get_json("/fapi/v1/premiumIndex", {"symbol": self.symbol})
        capture = {
            "schema_version": "order069.binance-public-capture.v1",
            "symbol": self.symbol,
            "server_time": server,
            "exchange_info": info,
            "depth": depth,
            "premium_index": premium,
            "source_hashes": {
                "time_raw_sha256": sha256_bytes(server_raw),
                "exchange_info_raw_sha256": sha256_bytes(info_raw),
                "depth_raw_sha256": sha256_bytes(depth_raw),
                "premium_index_raw_sha256": sha256_bytes(premium_raw),
            },
            "network_receipts": list(self.transport.receipts),
        }
        validate_capture(capture)
        capture["canonical_sha256"] = sha256_json({k: v for k, v in capture.items() if k != "canonical_sha256"})
        return capture

    def shadow_live_book(self, symbol: str) -> dict[str, Any]:
        """Adapter used only when explicitly injected into legacy ShadowLive."""
        normalized = str(symbol).upper().replace("/", "").replace(":USDT", "").replace("-", "")
        if normalized != SYMBOL:
            raise ShadowBoundaryError(f"SYMBOL_NOT_ALLOWED:{symbol}")
        capture = self.capture()
        market = market_view(capture)
        bids, asks = market["bids"], market["asks"]
        bid = D(bids[0][0], name="best_bid")
        ask = D(asks[0][0], name="best_ask")
        mid = (bid + ask) / Decimal("2")
        depth_usd = sum(D(p) * D(q) for p, q in bids[:5] + asks[:5])
        spread_bps = ((ask - bid) / mid * Decimal("10000")) if mid > 0 else Decimal("0")
        return {
            "bid": float(bid), "ask": float(ask), "mid": float(mid),
            "spread_bps": float(spread_bps), "depth_usd": float(depth_usd),
            "fetch_latency_ms": 0, "ts": str(market["server_time_ms"]),
            "source": "binance-usdm-public", "capture_sha256": capture["canonical_sha256"],
        }


def _symbol_info(exchange_info: Mapping[str, Any]) -> Mapping[str, Any]:
    symbols = exchange_info.get("symbols")
    if not isinstance(symbols, list):
        raise ShadowDataError("EXCHANGE_INFO_SYMBOLS_MISSING")
    matches = [s for s in symbols if isinstance(s, dict) and s.get("symbol") == SYMBOL]
    if len(matches) != 1:
        raise ShadowDataError(f"BTCUSDT_SYMBOL_COUNT:{len(matches)}")
    return matches[0]


def _filter_map(info: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    filters = info.get("filters")
    if not isinstance(filters, list):
        raise ShadowDataError("FILTERS_MISSING")
    return {str(f.get("filterType")): f for f in filters if isinstance(f, dict) and f.get("filterType")}


def market_view(capture: Mapping[str, Any]) -> dict[str, Any]:
    validate_capture(capture)
    info = _symbol_info(capture["exchange_info"])
    depth = capture["depth"]
    premium = capture["premium_index"]
    server_time_ms = int(capture["server_time"]["serverTime"])
    bids = [[str(p), str(q)] for p, q, *_ in depth["bids"]]
    asks = [[str(p), str(q)] for p, q, *_ in depth["asks"]]
    return {
        "symbol": SYMBOL,
        "server_time_ms": server_time_ms,
        "status": info.get("status"),
        "contract_type": info.get("contractType"),
        "base_asset": info.get("baseAsset"),
        "quote_asset": info.get("quoteAsset"),
        "margin_asset": info.get("marginAsset"),
        "filters": _filter_map(info),
        "bids": bids,
        "asks": asks,
        "last_update_id": depth.get("lastUpdateId"),
        "book_event_time_ms": depth.get("E") or depth.get("T"),
        "mark_price": str(premium.get("markPrice")),
        "index_price": str(premium.get("indexPrice")),
        "funding_rate": str(premium.get("lastFundingRate")),
        "next_funding_time": premium.get("nextFundingTime"),
    }


def validate_capture(capture: Mapping[str, Any]) -> None:
    if capture.get("symbol") != SYMBOL:
        raise ShadowDataError("WRONG_CAPTURE_SYMBOL")
    for key in ("server_time", "exchange_info", "depth", "premium_index"):
        if not isinstance(capture.get(key), dict):
            raise ShadowDataError(f"CAPTURE_SECTION_MISSING:{key}")
    try:
        server_time = int(capture["server_time"].get("serverTime"))
    except Exception as exc:
        raise ShadowDataError("SERVER_TIME_INVALID") from exc
    info = _symbol_info(capture["exchange_info"])
    if info.get("status") != "TRADING":
        raise ShadowDataError("CONTRACT_NOT_TRADING")
    if info.get("contractType") != "PERPETUAL":
        raise ShadowDataError("CONTRACT_NOT_PERPETUAL")
    if info.get("quoteAsset") != "USDT" or info.get("marginAsset") != "USDT":
        raise ShadowDataError("WRONG_QUOTE_OR_SETTLE_ASSET")
    filters = _filter_map(info)
    lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE")
    if not lot:
        raise ShadowDataError("LOT_FILTER_MISSING")
    for name in ("stepSize", "minQty", "maxQty"):
        if D(lot.get(name), name=name) <= 0:
            raise ShadowDataError(f"LOT_FILTER_INVALID:{name}")
    depth = capture["depth"]
    bids, asks = depth.get("bids"), depth.get("asks")
    if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
        raise ShadowDataError("EMPTY_BOOK_SIDE")
    try:
        best_bid, best_ask = D(bids[0][0], name="best_bid"), D(asks[0][0], name="best_ask")
    except (IndexError, TypeError) as exc:
        raise ShadowDataError("MALFORMED_BOOK") from exc
    if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
        raise ShadowDataError("CROSSED_OR_INVALID_BOOK")
    event_ms = depth.get("E") or depth.get("T")
    if event_ms is not None:
        try:
            age = server_time - int(event_ms)
        except Exception as exc:
            raise ShadowDataError("BOOK_TIMESTAMP_INVALID") from exc
        if age < -5_000 or age > MAX_BOOK_AGE_MS:
            raise ShadowDataError(f"STALE_BOOK:{age}")
    premium = capture["premium_index"]
    if premium.get("symbol") != SYMBOL:
        raise ShadowDataError("PREMIUM_SYMBOL_MISMATCH")
    if D(premium.get("markPrice"), name="markPrice") <= 0 or D(premium.get("indexPrice"), name="indexPrice") <= 0:
        raise ShadowDataError("PREMIUM_PRICE_INVALID")


def minimum_notional(filters: Mapping[str, Mapping[str, Any]]) -> Decimal:
    f = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL")
    if not f:
        return TARGET_NOTIONAL_MIN
    raw = f.get("notional") if "notional" in f else f.get("minNotional")
    if raw is None:
        raise ShadowDataError("MIN_NOTIONAL_VALUE_MISSING")
    value = D(raw, name="min_notional")
    if value <= 0:
        raise ShadowDataError("MIN_NOTIONAL_INVALID")
    return value


def build_intent(decision_envelope: Mapping[str, Any], capture: Mapping[str, Any]) -> dict[str, Any]:
    market = market_view(capture)
    signal = str(decision_envelope.get("signal") or "").upper()
    decision_hash = sha256_json(decision_envelope)
    common = {
        "schema_version": "order069.binance-shadow-intent.v1", "symbol": SYMBOL,
        "decision_sha256": decision_hash,
        "market_capture_sha256": capture.get("canonical_sha256") or sha256_json(capture),
        "leverage": "1x_SIMULATED", "position_mode": "ONE_WAY_SIMULATED", "account_balance_semantics": "NOT_QUERIED",
    }
    if signal == "FLAT":
        result = {**common, "signal": signal, "side": "NO_ORDER", "quantity": "0", "target_notional_usdt": "0", "status": "NO_ORDER_FLAT"}
        result["intent_sha256"] = sha256_json(result)
        return result
    if signal not in {"LONG", "SHORT"}:
        raise ShadowDataError(f"UNSUPPORTED_SIGNAL:{signal}")
    filters = market["filters"]
    lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE")
    assert lot is not None
    min_notional = minimum_notional(filters)
    target = max(TARGET_NOTIONAL_MIN, min_notional)
    if target > TARGET_NOTIONAL_CAP:
        result = {**common, "signal": signal, "side": "BUY" if signal == "LONG" else "SELL", "quantity": "0", "target_notional_usdt": decstr(target), "status": "SHADOW_FILTER_INCOMPATIBLE", "reason": "MIN_NOTIONAL_EXCEEDS_25_USDT_CAP"}
        result["intent_sha256"] = sha256_json(result)
        return result
    mark = D(market["mark_price"], name="mark_price")
    step, min_qty, max_qty = D(lot["stepSize"], name="stepSize"), D(lot["minQty"], name="minQty"), D(lot["maxQty"], name="maxQty")
    raw_qty = target / mark
    qty = floor_step(raw_qty, step)
    if qty < min_qty:
        required = min_qty * mark
        result = {**common, "signal": signal, "side": "BUY" if signal == "LONG" else "SELL", "quantity": decstr(qty), "target_notional_usdt": decstr(target), "status": "SHADOW_FILTER_INCOMPATIBLE", "reason": "ROUNDED_QTY_BELOW_MIN_QTY", "required_notional_usdt": decstr(required)}
        result["intent_sha256"] = sha256_json(result)
        return result
    if qty > max_qty:
        raise ShadowDataError("QTY_ABOVE_MAX_QTY")
    actual = qty * mark
    if actual < min_notional:
        result = {**common, "signal": signal, "side": "BUY" if signal == "LONG" else "SELL", "quantity": decstr(qty), "target_notional_usdt": decstr(target), "status": "SHADOW_FILTER_INCOMPATIBLE", "reason": "POST_ROUND_NOTIONAL_BELOW_MINIMUM", "post_round_notional_usdt": decstr(actual)}
        result["intent_sha256"] = sha256_json(result)
        return result
    if actual > TARGET_NOTIONAL_CAP:
        raise ShadowDataError("POST_ROUND_NOTIONAL_EXCEEDS_CAP")
    result = {
        **common, "signal": signal, "side": "BUY" if signal == "LONG" else "SELL", "quantity": decstr(qty),
        "target_notional_usdt": decstr(target), "post_round_mark_notional_usdt": decstr(actual), "status": "SHADOW_INTENT_READY",
        "filter_provenance": {
            "lot_filter": "MARKET_LOT_SIZE" if "MARKET_LOT_SIZE" in filters else "LOT_SIZE",
            "step_size": decstr(step), "min_qty": decstr(min_qty), "max_qty": decstr(max_qty), "min_notional_usdt": decstr(min_notional),
        },
    }
    result["intent_sha256"] = sha256_json(result)
    return result


def deterministic_book_walk(intent: Mapping[str, Any], capture: Mapping[str, Any]) -> dict[str, Any]:
    if intent.get("status") != "SHADOW_INTENT_READY":
        raise ShadowDataError("INTENT_NOT_FILLABLE")
    market = market_view(capture)
    side = str(intent["side"])
    levels = market["asks"] if side == "BUY" else market["bids"]
    requested = D(intent["quantity"], name="intent_quantity")
    remaining, filled, notional = requested, Decimal("0"), Decimal("0")
    consumed: list[dict[str, str]] = []
    for price_raw, qty_raw in levels:
        price, available = D(price_raw, name="book_price"), D(qty_raw, name="book_qty")
        if price <= 0 or available <= 0:
            raise ShadowDataError("INVALID_BOOK_LEVEL")
        take = min(remaining, available)
        if take <= 0:
            continue
        filled += take; notional += take * price
        consumed.append({"price": decstr(price), "quantity": decstr(take)})
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0 or filled != requested:
        return {"schema_version": "order069.shadow-fill.v1", "status": "NO_SHADOW_FILL_INSUFFICIENT_DEPTH", "symbol": SYMBOL, "side": side, "requested_qty": decstr(requested), "filled_qty": decstr(filled), "depth_capture_sha256": capture.get("source_hashes", {}).get("depth_raw_sha256")}
    vwap = notional / filled
    bid, ask = D(market["bids"][0][0]), D(market["asks"][0][0])
    mid = (bid + ask) / Decimal("2"); mark = D(market["mark_price"])
    sign = Decimal("1") if side == "BUY" else Decimal("-1")
    slip_mid_bps = ((vwap - mid) / mid * Decimal("10000")) * sign
    slip_mark_bps = ((vwap - mark) / mark * Decimal("10000")) * sign
    fill = {
        "schema_version": "order069.shadow-fill.v1", "status": "SHADOW_FILL", "symbol": SYMBOL, "side": side,
        "requested_qty": decstr(requested), "filled_qty": decstr(filled), "vwap": decstr(vwap), "notional_usdt": decstr(notional),
        "best_bid": decstr(bid), "best_ask": decstr(ask), "mid": decstr(mid), "mark_price": decstr(mark),
        "spread_bps": decstr((ask-bid)/mid*Decimal("10000")), "slippage_vs_mid_bps": decstr(slip_mid_bps),
        "slippage_vs_mark_bps": decstr(slip_mark_bps), "levels_consumed": consumed, "levels_consumed_count": len(consumed),
        "last_update_id": market["last_update_id"], "book_event_time_ms": market["book_event_time_ms"], "server_time_ms": market["server_time_ms"],
        "depth_raw_sha256": capture.get("source_hashes", {}).get("depth_raw_sha256"), "intent_sha256": intent.get("intent_sha256"),
    }
    fill["fill_sha256"] = sha256_json(fill)
    return fill


def make_no_order(intent: Mapping[str, Any]) -> dict[str, Any]:
    result = {"schema_version": "order069.no-order.v1", "status": "NO_ORDER", "reason": intent.get("status"), "symbol": SYMBOL, "signal": intent.get("signal"), "intent_sha256": intent.get("intent_sha256"), "real_order_count": 0}
    result["no_order_sha256"] = sha256_json(result)
    return result


def shadow_ledger(decision: Mapping[str, Any], intent: Mapping[str, Any], outcome: Mapping[str, Any], capture: Mapping[str, Any]) -> dict[str, Any]:
    market = market_view(capture)
    if outcome.get("status") == "SHADOW_FILL":
        qty = D(outcome["filled_qty"]); entry = D(outcome["vwap"]); mark = D(market["mark_price"])
        direction = Decimal("1") if intent["side"] == "BUY" else Decimal("-1")
        unreal = (mark - entry) * qty * direction
        position = {"side": intent["side"], "quantity": decstr(qty), "entry_price": decstr(entry), "mark_price": decstr(mark)}
        unreal_text = decstr(unreal)
    else:
        position = {"side": "FLAT", "quantity": "0", "entry_price": None, "mark_price": str(market["mark_price"])}
        unreal_text = "0"
    ledger = {
        "schema_version": "order069.shadow-ledger.v1", "symbol": SYMBOL, "decision_sha256": sha256_json(decision),
        "intent_sha256": intent.get("intent_sha256"), "outcome_sha256": outcome.get("fill_sha256") or outcome.get("no_order_sha256") or sha256_json(outcome),
        "synthetic_position": position, "unrealized_pnl_usdt_diagnostic": unreal_text,
        "REALIZED_PNL": "NOT_APPLICABLE_REAL_CAPITAL", "REAL_CAPITAL_MOVEMENT": 0, "REAL_ORDER_COUNT": 0, "PRODUCTION_MUTATION_COUNT": 0,
    }
    ledger["ledger_sha256"] = sha256_json(ledger)
    return ledger


def route_shadow(decision_envelope: Mapping[str, Any], capture: Mapping[str, Any]) -> dict[str, Any]:
    validate_capture(capture)
    intent = build_intent(decision_envelope, capture)
    if intent["status"] != "SHADOW_INTENT_READY":
        outcome = make_no_order(intent)
    else:
        outcome = deterministic_book_walk(intent, capture)
        if outcome["status"] != "SHADOW_FILL":
            no_order = make_no_order({**intent, "status": outcome["status"]})
            no_order["depth_diagnostic"] = outcome
            no_order["no_order_sha256"] = sha256_json({k: v for k, v in no_order.items() if k != "no_order_sha256"})
            outcome = no_order
    ledger = shadow_ledger(decision_envelope, intent, outcome, capture)
    return {"intent": intent, "outcome": outcome, "ledger": ledger}


def fixture_capture(*, min_qty: str = "0.0001", step: str = "0.0001", min_notional: str = "5", event_time: int = 1_700_000_000_000) -> dict[str, Any]:
    info = {"symbols": [{"symbol": SYMBOL, "status": "TRADING", "contractType": "PERPETUAL", "baseAsset": "BTC", "quoteAsset": "USDT", "marginAsset": "USDT", "filters": [{"filterType": "MARKET_LOT_SIZE", "minQty": min_qty, "maxQty": "100", "stepSize": step}, {"filterType": "MIN_NOTIONAL", "notional": min_notional}]}]}
    capture = {
        "schema_version": "order069.binance-public-capture.v1", "symbol": SYMBOL,
        "server_time": {"serverTime": event_time + 100}, "exchange_info": info,
        "depth": {"lastUpdateId": 10, "E": event_time, "T": event_time, "bids": [["59990", "2"], ["59980", "2"]], "asks": [["60010", "2"], ["60020", "2"]]},
        "premium_index": {"symbol": SYMBOL, "markPrice": "60000", "indexPrice": "60001", "lastFundingRate": "0.0001", "nextFundingTime": event_time + 28_800_000},
        "source_hashes": {"time_raw_sha256": "a"*64, "exchange_info_raw_sha256": "b"*64, "depth_raw_sha256": "c"*64, "premium_index_raw_sha256": "d"*64},
        "network_receipts": [],
    }
    validate_capture(capture)
    capture["canonical_sha256"] = sha256_json(capture)
    return capture
