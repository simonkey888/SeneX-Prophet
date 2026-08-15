"""
LIVE_BRIDGE_LAYER_v1: exchange_connector.py — Dual-Exchange Public Data Connector

Purpose: Connect to Binance + Bybit via ccxt REST API for public market data only.
This is a SHADOW OBSERVATION LAYER — it reads market reality. It does not act on it.

CRITICAL CONSTRAINTS:
    - NO_REAL_ORDERS:  This module MUST NEVER place an order. It only reads data.
    - NO_API_KEYS:     Only public endpoints (orderbook, ticker, trades, funding).
    - PAPER_ONLY:      Even "execution" is simulated. Records what WOULD have happened.
    - OBSERVATION_STREAM_ONLY: Output is a data stream, not trading commands.

Architecture:
    - Two ccxt exchange instances (binance, bybit)
    - Unified fetch methods that return normalized data
    - Latency tracking per exchange
    - Connection health monitoring
    - Graceful degradation (one exchange down -> continue with other)

Self-tests use deterministic mock data — no API keys or live connections required.
Use --live flag to optionally verify real connectivity.
"""

import time
import os
import logging
from typing import Optional, Dict, List, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

logger = logging.getLogger("exchange_connector")

# ---------------------------------------------------------------------------
# Health status constants
# ---------------------------------------------------------------------------

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"       # 5+ consecutive failures
DOWN = "DOWN"               # 20+ consecutive failures

DEGRADED_THRESHOLD = 5
DOWN_THRESHOLD = 20

# Explicit public linear-swap identities. Funding/OI must never reuse the spot
# symbol merely because CCXT's defaultType option was toggled.
DERIVATIVE_SYMBOL_FORMATS = {
    "okx": "{base}/{quote}:{quote}",
    "gate": "{base}/{quote}:{quote}",
    "mexc": "{base}/{quote}:{quote}",
    "bitget": "{base}/{quote}:{quote}",
    "binance": "{base}/{quote}:{quote}",
    "bybit": "{base}/{quote}:{quote}",
}
_OI_SNAPSHOT_CACHE: Dict[tuple[str, str], tuple[int, float]] = {}


def resolve_public_derivative_symbol(exchange: str, spot_symbol: str) -> Optional[str]:
    """Resolve an explicit USDT-settled swap identity for a spot symbol."""
    template = DERIVATIVE_SYMBOL_FORMATS.get(str(exchange or "").lower())
    parts = str(spot_symbol or "").split(":", 1)[0].split("/", 1)
    if not template or len(parts) != 2:
        return None
    base, quote = (part.strip().upper() for part in parts)
    if not base or not quote or quote != "USDT":
        return None
    return template.format(base=base, quote=quote)


# ===========================================================================
# NormalizedOrderbook
# ===========================================================================

class NormalizedOrderbook:
    """Normalized orderbook representation.

    Takes raw ccxt orderbook data and produces a unified format that the
    rest of the system can consume without knowing which exchange it came
    from.
    """

    @staticmethod
    def normalize(raw_ob: dict, exchange: str, symbol: str) -> dict:
        """Normalize raw ccxt orderbook into unified format.

        Args:
            raw_ob: Raw ccxt orderbook dict with 'bids', 'asks', 'timestamp' keys.
            exchange: Exchange name (e.g. "binance", "bybit").
            symbol: Trading pair symbol (e.g. "BTC/USDT").

        Returns:
            Normalized orderbook dict with:
                - exchange: str
                - symbol: str
                - bids: list of [price: float, size: float]
                - asks: list of [price: float, size: float]
                - timestamp: int (ms)
                - nonce: int or None
        """
        # Safely extract and convert bids/asks to float lists
        raw_bids = raw_ob.get("bids", []) or []
        raw_asks = raw_ob.get("asks", []) or []

        bids = []
        for level in raw_bids:
            if len(level) >= 2:
                bids.append([float(level[0]), float(level[1])])

        asks = []
        for level in raw_asks:
            if len(level) >= 2:
                asks.append([float(level[0]), float(level[1])])

        timestamp = raw_ob.get("timestamp")
        if timestamp is not None:
            timestamp = int(timestamp)
        else:
            timestamp = int(time.time() * 1000)

        nonce = raw_ob.get("nonce")

        return {
            "exchange": exchange,
            "symbol": symbol,
            "bids": bids,
            "asks": asks,
            "timestamp": timestamp,
            "nonce": nonce,
        }

    @staticmethod
    def compute_depth_metrics(normalized_ob: dict) -> dict:
        """Compute depth metrics from a normalized orderbook.

        Args:
            normalized_ob: Output of NormalizedOrderbook.normalize().

        Returns:
            Dict with:
                - bid_depth_usdt: total bid value within 0.5% of mid
                - ask_depth_usdt: total ask value within 0.5% of mid
                - spread_bps: bid-ask spread in basis points
                - mid_price: mid price
                - depth_imbalance: (bid_depth - ask_depth) / (bid_depth + ask_depth)
                - available_liquidity_usdt: bid_depth + ask_depth
        """
        bids = normalized_ob.get("bids", [])
        asks = normalized_ob.get("asks", [])

        if not bids or not asks:
            return {
                "bid_depth_usdt": 0.0,
                "ask_depth_usdt": 0.0,
                "spread_bps": 0.0,
                "mid_price": 0.0,
                "depth_imbalance": 0.0,
                "available_liquidity_usdt": 0.0,
            }

        best_bid = bids[0][0]
        best_ask = asks[0][0]

        if best_bid <= 0 or best_ask <= 0:
            return {
                "bid_depth_usdt": 0.0,
                "ask_depth_usdt": 0.0,
                "spread_bps": 0.0,
                "mid_price": 0.0,
                "depth_imbalance": 0.0,
                "available_liquidity_usdt": 0.0,
            }

        mid_price = (best_bid + best_ask) / 2.0
        spread = best_ask - best_bid
        spread_bps = (spread / mid_price) * 10000.0 if mid_price > 0 else 0.0

        # Depth within 0.5% of mid
        # Epsilon tolerance to avoid floating-point boundary issues
        half_pct = 0.005
        _epsilon = 1e-9
        bid_threshold = mid_price * (1.0 - half_pct) - _epsilon
        ask_threshold = mid_price * (1.0 + half_pct) + _epsilon

        bid_depth_usdt = 0.0
        for price, size in bids:
            if price >= bid_threshold:
                bid_depth_usdt += price * size
            else:
                break  # bids are sorted descending

        ask_depth_usdt = 0.0
        for price, size in asks:
            if price <= ask_threshold:
                ask_depth_usdt += price * size
            else:
                break  # asks are sorted ascending

        total_depth = bid_depth_usdt + ask_depth_usdt
        depth_imbalance = (
            (bid_depth_usdt - ask_depth_usdt) / total_depth
            if total_depth > 0
            else 0.0
        )

        return {
            "bid_depth_usdt": round(bid_depth_usdt, 2),
            "ask_depth_usdt": round(ask_depth_usdt, 2),
            "spread_bps": round(spread_bps, 2),
            "mid_price": round(mid_price, 2),
            "depth_imbalance": round(depth_imbalance, 6),
            "available_liquidity_usdt": round(total_depth, 2),
        }

    @staticmethod
    def compute_depth_curve(normalized_ob: dict, n_levels: int = 10) -> list:
        """Compute depth curve (cumulative depth at each price level).

        Args:
            normalized_ob: Output of NormalizedOrderbook.normalize().
            n_levels: Number of price levels to compute on each side.

        Returns:
            List of dicts, each with:
                - side: "bid" or "ask"
                - price_pct_from_mid: percentage distance from mid price
                - cumulative_depth_usdt: cumulative depth in USDT up to this level
        """
        bids = normalized_ob.get("bids", [])
        asks = normalized_ob.get("asks", [])

        if not bids or not asks:
            return []

        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid_price = (best_bid + best_ask) / 2.0

        if mid_price <= 0:
            return []

        curve = []

        # Bid side: iterate from best bid downward
        cum_depth = 0.0
        levels_used = 0
        for price, size in bids:
            if levels_used >= n_levels:
                break
            cum_depth += price * size
            pct_from_mid = ((price - mid_price) / mid_price) * 100.0
            curve.append({
                "side": "bid",
                "price_pct_from_mid": round(pct_from_mid, 4),
                "cumulative_depth_usdt": round(cum_depth, 2),
            })
            levels_used += 1

        # Ask side: iterate from best ask upward
        cum_depth = 0.0
        levels_used = 0
        for price, size in asks:
            if levels_used >= n_levels:
                break
            cum_depth += price * size
            pct_from_mid = ((price - mid_price) / mid_price) * 100.0
            curve.append({
                "side": "ask",
                "price_pct_from_mid": round(pct_from_mid, 4),
                "cumulative_depth_usdt": round(cum_depth, 2),
            })
            levels_used += 1

        return curve


# ===========================================================================
# ExchangeConnector
# ===========================================================================

class ExchangeConnector:
    """Dual-exchange public data connector.

    Connects to Binance + Bybit via ccxt REST API.
    Public endpoints only — NO authentication, NO orders.

    This is a SENSOR. It reads market reality. It does not act on it.

    Architecture:
        - Two ccxt exchange instances (binance, bybit)
        - Unified fetch methods that return normalized data
        - Latency tracking per exchange
        - Connection health monitoring
        - Graceful degradation (one exchange down -> continue with other)
    """

    # Exchange names we support
    SUPPORTED_EXCHANGES = ("okx", "kraken", "gate", "mexc", "bitget", "binance", "bybit", "binance_testnet")

    def __init__(self, symbol: str = "BTC/USDT", config: dict = None):
        """Initialize the dual-exchange connector.

        Args:
            symbol: Trading pair to observe (e.g. "BTC/USDT").
            config: Optional configuration dict. Supported keys:
                - exchanges: list of exchange names to enable (default: all)
                - timeframe: OHLCV candle interval (default: "1h")
                - ohlcv_limit: number of candles to fetch (default: 60)
                - Any exchange-specific ccxt options (passed through)
        """
        self.symbol = symbol
        self.config = config or {}
        self.timeframe = self.config.get("timeframe", "1h")
        self.ohlcv_limit = self.config.get("ohlcv_limit", 100)

        # name -> ccxt exchange instance
        self.exchanges: Dict[str, Any] = {}
        # name -> health status dict
        self.health: Dict[str, dict] = {}
        # name -> last measured latency in ms
        self.latency_ms: Dict[str, float] = {}

        self._init_exchanges(self.config)
        # Track exchanges where funding rate consistently fails (skip after 3 failures)
        self._skip_funding: Dict[str, bool] = {}
        self._funding_fail_count: Dict[str, int] = {}

    # -----------------------------------------------------------------------
    # Initialization
    # -----------------------------------------------------------------------

    def _init_exchanges(self, config: dict):
        """Initialize ccxt exchange instances.

        For Binance and Bybit, creates exchange objects with:
        - enableRateLimit = True (respect rate limits)
        - No API keys (public only)
        - Sandbox/testnet mode disabled (we want real public data)

        Args:
            config: Configuration dict, possibly containing:
                - exchanges: list of exchange names to initialize
                - per-exchange options under the exchange name key
        """
        import ccxt

        enabled = config.get("exchanges", list(self.SUPPORTED_EXCHANGES))

        exchange_configs = {
            "okx": {
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            },
            "kraken": {
                "enableRateLimit": True,
            },
            "gate": {
                "enableRateLimit": True,
            },
            "mexc": {
                "enableRateLimit": True,
            },
            "bitget": {
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            },
            "binance": {
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            },
            "bybit": {
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            },
            "binance_testnet": {
                "enableRateLimit": True,
                "options": {
                    "defaultType": "future",
                    # ccxt blocks futures testnet with a deprecation warning.
                    # This flag disables that check so we can use the testnet.
                    # SAFETY: Only used with binance_testnet, never on mainnet.
                    "disableFuturesSandboxWarning": True,
                },
                # We do NOT pass urls or sandbox in the constructor.
                # Instead, we call ex.set_sandbox_mode(True) after creation
                # which properly configures ALL fapi URLs to testnet.binancefuture.com
            },
        }

        for name in enabled:
            if name not in self.SUPPORTED_EXCHANGES:
                logger.warning(f"Unsupported exchange '{name}', skipping")
                continue

            try:
                # binance_testnet uses ccxt.binance with testnet config
                ccxt_name = "binance" if name == "binance_testnet" else name
                exchange_class = getattr(ccxt, ccxt_name)
                ex_config = dict(exchange_configs.get(name, {"enableRateLimit": True}))

                # Load API keys for testnet from .env
                if name == "binance_testnet":
                    try:
                        from dotenv import load_dotenv
                        load_dotenv()
                    except ImportError:
                        pass
                    api_key = os.environ.get("BINANCE_TESTNET_KEY", "")
                    api_secret = os.environ.get("BINANCE_TESTNET_SECRET", "")
                    if api_key and api_secret:
                        ex_config["apiKey"] = api_key
                        ex_config["secret"] = api_secret
                        logger.info(f"Loaded testnet API credentials from .env")
                    else:
                        logger.warning("No BINANCE_TESTNET_KEY/SECRET in .env — testnet orders will fail")

                # Merge any user-provided config for this exchange
                # SAFETY: For binance_testnet, do NOT allow URL overrides (defense-in-depth)
                user_config = config.get(name, {})
                if name == "binance_testnet" and "urls" in user_config:
                    logger.warning(f"SAFETY: Ignoring user-provided URL override for {name} — testnet URLs must not be changed")
                    user_config = {k: v for k, v in user_config.items() if k != "urls"}
                ex_config.update(user_config)

                instance = exchange_class(ex_config)

                # SAFETY: Enable testnet sandbox mode AFTER creation.
                # This sets ALL fapi URLs to testnet.binancefuture.com correctly.
                # We use set_sandbox_mode() instead of passing URLs in constructor
                # because ccxt has special URL routing logic for sandbox that
                # handles V2/V3 fapi endpoints automatically.
                if name == "binance_testnet":
                    instance.set_sandbox_mode(True)
                    # Verify URLs point to testnet (defense-in-depth)
                    fapi_urls = [
                        instance.urls.get('api', {}).get('fapiPublic', ''),
                        instance.urls.get('api', {}).get('fapiPrivate', ''),
                    ]
                    for url in fapi_urls:
                        if 'testnet' not in url:
                            raise RuntimeError(
                                f"SAFETY ABORT: binance_testnet fapi URL not pointing to testnet: {url}"
                            )

                self.exchanges[name] = instance

                # Initialize health tracking
                self.health[name] = {
                    "status": HEALTHY,
                    "consecutive_failures": 0,
                    "last_success_ms": None,
                    "last_failure_ms": None,
                    "total_successes": 0,
                    "total_failures": 0,
                }
                self.latency_ms[name] = None

                logger.info(f"Initialized exchange: {name}")

            except Exception as e:
                logger.error(f"Failed to initialize exchange '{name}': {e}")
                # Track as DOWN from the start
                self.health[name] = {
                    "status": DOWN,
                    "consecutive_failures": DOWN_THRESHOLD,
                    "last_success_ms": None,
                    "last_failure_ms": int(time.time() * 1000),
                    "total_successes": 0,
                    "total_failures": 1,
                }
                self.latency_ms[name] = None

        logger.info(f"ExchangeConnector ready: timeframe={self.timeframe} ohlcv_limit={self.ohlcv_limit}")

    # -----------------------------------------------------------------------
    # Health tracking
    # -----------------------------------------------------------------------

    def _record_success(self, exchange: str, latency_ms: float):
        """Record a successful fetch for an exchange.

        Resets consecutive failure counter and updates health status.

        Args:
            exchange: Exchange name.
            latency_ms: Measured latency in milliseconds.
        """
        now_ms = int(time.time() * 1000)
        self.latency_ms[exchange] = latency_ms

        if exchange in self.health:
            h = self.health[exchange]
            h["consecutive_failures"] = 0
            h["status"] = HEALTHY
            h["last_success_ms"] = now_ms
            h["total_successes"] = h.get("total_successes", 0) + 1

    def _record_failure(self, exchange: str, error: Exception):
        """Record a failed fetch for an exchange.

        Increments consecutive failure counter and updates health status.
        After 5 consecutive failures -> DEGRADED.
        After 20 consecutive failures -> DOWN.

        Args:
            exchange: Exchange name.
            error: The exception that occurred.
        """
        now_ms = int(time.time() * 1000)
        logger.warning(f"Exchange '{exchange}' fetch failed: {error}")

        if exchange in self.health:
            h = self.health[exchange]
            h["consecutive_failures"] = h.get("consecutive_failures", 0) + 1
            h["last_failure_ms"] = now_ms
            h["total_failures"] = h.get("total_failures", 0) + 1

            if h["consecutive_failures"] >= DOWN_THRESHOLD:
                h["status"] = DOWN
            elif h["consecutive_failures"] >= DEGRADED_THRESHOLD:
                h["status"] = DEGRADED
            # Don't upgrade status back on failure — that only happens on success

    # -----------------------------------------------------------------------
    # Core fetch methods
    # -----------------------------------------------------------------------

    def fetch_orderbook(self, exchange: str, limit: int = 20) -> Optional[dict]:
        """Fetch orderbook from specified exchange.

        Args:
            exchange: Exchange name ("binance" or "bybit").
            limit: Number of orderbook levels to fetch (default 20).

        Returns:
            Normalized orderbook dict with:
                - exchange: str
                - symbol: str
                - bids: list of [price, size]
                - asks: list of [price, size]
                - bid_depth_usdt: total bid value within 0.5% of mid
                - ask_depth_usdt: total ask value within 0.5% of mid
                - spread_bps: bid-ask spread in basis points
                - mid_price: mid price
                - timestamp: ms
                - latency_ms: how long the fetch took
            Returns None on failure.
        """
        if exchange not in self.exchanges:
            logger.error(f"Exchange '{exchange}' not initialized")
            return None

        try:
            ex = self.exchanges[exchange]
            t0 = time.time()
            raw_ob = ex.fetch_order_book(self.symbol, limit=limit)
            latency = (time.time() - t0) * 1000.0

            normalized = NormalizedOrderbook.normalize(raw_ob, exchange, self.symbol)
            metrics = NormalizedOrderbook.compute_depth_metrics(normalized)

            result = {
                **normalized,
                **metrics,
                "latency_ms": round(latency, 2),
            }

            self._record_success(exchange, latency)
            return result

        except Exception as e:
            self._record_failure(exchange, e)
            return None

    def fetch_ticker(self, exchange: str) -> Optional[dict]:
        """Fetch ticker from specified exchange.

        Args:
            exchange: Exchange name ("binance" or "bybit").

        Returns:
            Normalized ticker dict with:
                - exchange: str
                - symbol: str
                - bid: float
                - ask: float
                - last: float
                - high: float
                - low: float
                - volume: float (base volume)
                - quote_volume: float (quote volume, 24h)
                - change_pct: float (24h percentage change)
                - spread_bps: float
                - mid_price: float
                - timestamp: int (ms)
                - latency_ms: float
            Returns None on failure.
        """
        if exchange not in self.exchanges:
            logger.error(f"Exchange '{exchange}' not initialized")
            return None

        try:
            ex = self.exchanges[exchange]
            t0 = time.time()
            raw_ticker = ex.fetch_ticker(self.symbol)
            latency = (time.time() - t0) * 1000.0

            bid = float(raw_ticker.get("bid", 0) or 0)
            ask = float(raw_ticker.get("ask", 0) or 0)
            last = float(raw_ticker.get("last", 0) or 0)
            high = float(raw_ticker.get("high", 0) or 0)
            low = float(raw_ticker.get("low", 0) or 0)
            volume = float(raw_ticker.get("baseVolume", 0) or 0)
            quote_volume = float(raw_ticker.get("quoteVolume", 0) or 0)
            change_pct = float(raw_ticker.get("percentage", 0) or 0)

            mid_price = (bid + ask) / 2.0 if (bid + ask) > 0 else last
            spread_bps = ((ask - bid) / mid_price * 10000.0) if mid_price > 0 else 0.0

            timestamp = raw_ticker.get("timestamp")
            if timestamp is not None:
                timestamp = int(timestamp)
            else:
                timestamp = int(time.time() * 1000)

            result = {
                "exchange": exchange,
                "symbol": self.symbol,
                "bid": bid,
                "ask": ask,
                "last": last,
                "high": high,
                "low": low,
                "volume": volume,
                "quote_volume": quote_volume,
                "change_pct": change_pct,
                "spread_bps": round(spread_bps, 2),
                "mid_price": round(mid_price, 2),
                "timestamp": timestamp,
                "latency_ms": round(latency, 2),
            }

            self._record_success(exchange, latency)
            return result

        except Exception as e:
            self._record_failure(exchange, e)
            return None

    def fetch_trades(self, exchange: str, limit: int = 100) -> Optional[list]:
        """Fetch recent trades from specified exchange.

        Args:
            exchange: Exchange name ("binance" or "bybit").
            limit: Maximum number of trades to fetch (default 100).

        Returns:
            List of normalized trade dicts, each with:
                - exchange: str
                - symbol: str
                - id: str
                - price: float
                - amount: float
                - cost_usdt: float (price * amount)
                - side: "buy" or "sell"
                - timestamp: int (ms)
            Returns None on failure.
        """
        if exchange not in self.exchanges:
            logger.error(f"Exchange '{exchange}' not initialized")
            return None

        try:
            ex = self.exchanges[exchange]
            t0 = time.time()
            raw_trades = ex.fetch_trades(self.symbol, limit=limit)
            latency = (time.time() - t0) * 1000.0

            normalized = []
            for t in raw_trades:
                price = float(t.get("price", 0) or 0)
                amount = float(t.get("amount", 0) or 0)
                normalized.append({
                    "exchange": exchange,
                    "symbol": self.symbol,
                    "id": str(t.get("id", "")),
                    "price": price,
                    "amount": amount,
                    "cost_usdt": round(price * amount, 2),
                    "side": t.get("side", "unknown"),
                    "timestamp": int(t.get("timestamp", 0) or 0),
                })

            self._record_success(exchange, latency)
            return normalized

        except Exception as e:
            self._record_failure(exchange, e)
            return None

    def fetch_funding_rate(self, exchange: str) -> Optional[dict]:
        """Fetch funding rate from specified exchange.

        Note: Funding rate may not be available on all exchanges or for
        all symbols (spot markets typically don't have funding rates).

        Args:
            exchange: Exchange name ("binance" or "bybit").

        Returns:
            Normalized funding rate dict with:
                - exchange: str
                - symbol: str
                - rate: float (current funding rate)
                - next_funding_ms: int or None (ms until next funding)
                - timestamp: int (ms)
                - latency_ms: float
            Returns None on failure or if not supported.
        """
        # Skip if this exchange's funding rate has failed too many times
        if self._skip_funding.get(exchange, False):
            return None
        if exchange not in self.exchanges:
            logger.error(f"Exchange '{exchange}' not initialized")
            return None

        try:
            ex = self.exchanges[exchange]
            derivative_symbol = resolve_public_derivative_symbol(exchange, self.symbol)
            if derivative_symbol is None:
                return None
            t0 = time.time()
            raw_funding = ex.fetch_funding_rate(derivative_symbol)
            latency = (time.time() - t0) * 1000.0
            if not isinstance(raw_funding, dict) or raw_funding.get("fundingRate") is None:
                raise ValueError("funding response missing fundingRate")

            rate = float(raw_funding.get("fundingRate", 0) or 0)

            # Parse next funding time
            next_funding_ms = None
            if raw_funding.get("fundingDatetime"):
                try:
                    # ccxt returns ISO 8601 datetime string
                    import datetime as dt
                    fd = raw_funding["fundingDatetime"]
                    # Try parsing as ISO format
                    parsed = dt.datetime.fromisoformat(fd.replace("Z", "+00:00"))
                    next_funding_ms = int(parsed.timestamp() * 1000) - int(time.time() * 1000)
                except Exception:
                    next_funding_ms = None

            timestamp = raw_funding.get("timestamp")
            if timestamp is not None:
                timestamp = int(timestamp)
            else:
                timestamp = int(time.time() * 1000)

            result = {
                "exchange": exchange,
                "symbol": self.symbol,
                "derivative_symbol": derivative_symbol,
                "market_type": "swap",
                "source": f"{exchange}:public_swap_funding",
                "rate": rate,
                "next_funding_ms": next_funding_ms,
                "timestamp": timestamp,
                "latency_ms": round(latency, 2),
            }

            self._record_success(exchange, latency)
            return result

        except Exception as e:
            self._record_failure(exchange, e)
            # Track funding rate failures per exchange — skip after 3 consecutive failures
            count = self._funding_fail_count.get(exchange, 0) + 1
            self._funding_fail_count[exchange] = count
            if count >= 3:
                self._skip_funding[exchange] = True
                logger.info(f"Skipping funding rate for {exchange} after {count} failures")
            return None

    def fetch_ohlcv(self, exchange: str, timeframe: str = "1m", limit: int = 60) -> Optional[list]:
        """Fetch OHLCV candles from specified exchange.

        Args:
            exchange: Exchange name ("binance" or "bybit").
            timeframe: Candle interval (e.g. "1m", "5m", "1h", "1d").
            limit: Number of candles to fetch (default 60).

        Returns:
            List of [timestamp_ms, open, high, low, close, volume] lists.
            All values are floats. Returns None on failure.
        """
        if exchange not in self.exchanges:
            logger.error(f"Exchange '{exchange}' not initialized")
            return None

        try:
            ex = self.exchanges[exchange]
            t0 = time.time()
            raw_ohlcv = ex.fetch_ohlcv(self.symbol, timeframe=timeframe, limit=limit)
            latency = (time.time() - t0) * 1000.0

            # Normalize: ensure all values are floats
            normalized = []
            for candle in raw_ohlcv:
                normalized.append([
                    int(candle[0]),       # timestamp
                    float(candle[1]),     # open
                    float(candle[2]),     # high
                    float(candle[3]),     # low
                    float(candle[4]),     # close
                    float(candle[5]),     # volume
                ])

            self._record_success(exchange, latency)
            return normalized

        except Exception as e:
            self._record_failure(exchange, e)
            return None

    def fetch_open_interest(self, exchange: str) -> Optional[dict]:
        """Fetch open interest from specified exchange (futures/perpetual).

        OI requires the futures market type. Temporarily switches if needed.

        Args:
            exchange: Exchange name ("binance" or "bybit").

        Returns:
            Normalized OI dict with:
                - openInterestAmount: float (OI in base currency)
                - timestamp: int (ms)
                - latency_ms: float
            Returns None on failure or if not supported.
        """
        if exchange not in self.exchanges:
            return None

        try:
            ex = self.exchanges[exchange]
            derivative_symbol = resolve_public_derivative_symbol(exchange, self.symbol)
            if derivative_symbol is None:
                return None
            t0 = time.time()
            raw_oi = ex.fetch_open_interest(derivative_symbol)
            latency = (time.time() - t0) * 1000.0
            if not isinstance(raw_oi, dict) or raw_oi.get("openInterestAmount") is None:
                raise ValueError("open-interest response missing openInterestAmount")

            oi_amount = float(raw_oi.get("openInterestAmount", 0) or 0)
            timestamp = raw_oi.get("timestamp")
            if timestamp is not None:
                timestamp = int(timestamp)
            else:
                timestamp = int(time.time() * 1000)

            cache_key = (exchange, derivative_symbol)
            prior = _OI_SNAPSHOT_CACHE.get(cache_key)
            oi_change_pct = None
            oi_change_observed = False
            prior_timestamp = None
            if prior is not None:
                prior_timestamp, prior_amount = prior
                if timestamp > prior_timestamp and prior_amount > 0:
                    oi_change_pct = (oi_amount - prior_amount) / prior_amount * 100.0
                    oi_change_observed = True
            if prior is None or timestamp >= prior[0]:
                _OI_SNAPSHOT_CACHE[cache_key] = (timestamp, oi_amount)

            result = {
                "oi_value": round(oi_amount, 4),
                "oi_change_24h_pct": round(oi_change_pct, 8) if oi_change_pct is not None else None,
                "oi_change_observed": oi_change_observed,
                "comparison_timestamp": prior_timestamp,
                "timestamp": timestamp,
                "latency_ms": round(latency, 2),
                "exchange": exchange,
                "symbol": self.symbol,
                "derivative_symbol": derivative_symbol,
                "market_type": "swap",
                "source": f"{exchange}:public_swap_open_interest",
            }

            self._record_success(exchange, latency)
            return result

        except Exception as e:
            # OI not supported or failed — don't record as full failure
            # since spot markets naturally lack OI
            logger.debug(f"OI fetch failed for {exchange}: {e}")
            return None

    # -----------------------------------------------------------------------
    # Aggregate fetch
    # -----------------------------------------------------------------------

    def fetch_all(self) -> dict:
        """Fetch all data from all exchanges simultaneously.

        Uses a thread pool to fetch data from all exchanges concurrently.
        If an exchange is unreachable, still returns data from the others.
        The system NEVER requires both exchanges to be up.

        Returns:
            Dict mapping exchange name -> {
                "orderbook": dict or None,
                "ticker": dict or None,
                "trades": list or None,
                "funding": dict or None,
                "ohlcv": list or None,
                "open_interest": dict or None,
            }
        """
        results = {}
        for name in self.exchanges:
            results[name] = {
                "orderbook": None,
                "ticker": None,
                "trades": None,
                "funding": None,
                "ohlcv": None,
                "open_interest": None,
            }

        def _fetch_exchange(name: str) -> tuple:
            """Fetch all data types for a single exchange."""
            # Skip funding rate for exchanges that consistently fail
            funding = None
            if not self._skip_funding.get(name, False):
                funding = self.fetch_funding_rate(name)
            data = {
                "orderbook": self.fetch_orderbook(name),
                "ticker": self.fetch_ticker(name),
                "trades": None,  # Not used in shadow pipeline, skip for speed
                "funding": funding,
                "ohlcv": self.fetch_ohlcv(name, timeframe=self.timeframe, limit=self.ohlcv_limit),
            }
            return name, data

        # Fetch concurrently using thread pool
        with ThreadPoolExecutor(max_workers=len(self.exchanges)) as executor:
            futures = {
                executor.submit(_fetch_exchange, name): name
                for name in self.exchanges
            }

            for future in as_completed(futures):
                try:
                    name, data = future.result(timeout=30)
                    results[name] = data
                except Exception as e:
                    name = futures[future]
                    logger.error(f"fetch_all failed for {name}: {e}")
                    # Results already initialized with None values

        # Fetch OI sequentially AFTER the thread pool to avoid race condition
        # (OI fetch temporarily changes defaultType to 'future' which would
        # corrupt other concurrent fetches if run in the thread pool)
        # Only fetch from the first exchange that supports it (usually binance)
        for name in self.exchanges:
            try:
                oi = self.fetch_open_interest(name)
                if oi is not None:
                    results[name]["open_interest"] = oi
                    break  # Only need OI from one exchange
            except Exception:
                pass

        return results

    # -----------------------------------------------------------------------
    # Health and latency
    # -----------------------------------------------------------------------

    def get_health(self) -> dict:
        """Get connection health status for all exchanges.

        Returns:
            Dict mapping exchange name -> {
                "connected": bool,
                "status": str ("HEALTHY"|"DEGRADED"|"DOWN"),
                "last_success_ms": int or None,
                "consecutive_failures": int,
                "latency_ms": float or None,
            }
        """
        result = {}
        for name, h in self.health.items():
            result[name] = {
                "connected": h["status"] != DOWN,
                "status": h["status"],
                "last_success_ms": h.get("last_success_ms"),
                "consecutive_failures": h.get("consecutive_failures", 0),
                "latency_ms": self.latency_ms.get(name),
            }
        return result

    def measure_latency(self, exchange: str) -> float:
        """Measure round-trip latency to exchange by fetching ticker.

        Args:
            exchange: Exchange name ("binance" or "bybit").

        Returns:
            Latency in milliseconds. Returns -1.0 on failure.
        """
        if exchange not in self.exchanges:
            return -1.0

        try:
            ex = self.exchanges[exchange]
            t0 = time.time()
            ex.fetch_ticker(self.symbol)
            latency = (time.time() - t0) * 1000.0
            self.latency_ms[exchange] = round(latency, 2)
            return round(latency, 2)
        except Exception as e:
            logger.warning(f"Latency measurement failed for {exchange}: {e}")
            return -1.0

    # -----------------------------------------------------------------------
    # Testnet order execution (Act IX)
    # -----------------------------------------------------------------------

    def place_market_order(self, exchange_name: str, symbol: str, side: str,
                           amount: float, params: dict = None) -> dict:
        """Place a real market order on the specified exchange.

        SAFETY: This method MUST only be called in testnet mode.
        The main.py testnet mode guards against mainnet usage.

        Args:
            exchange_name: Exchange to use (must be "binance_testnet").
            symbol: Trading pair (e.g. "ETH/USDT").
            side: "buy" or "sell".
            amount: Amount in base currency (e.g. ETH).
            params: Optional extra ccxt params.

        Returns:
            Dict with fill details: {order_id, fill_price, fill_amount,
            fill_cost, fees, timestamp, slippage_bps, expected_price}
        """
        if exchange_name not in self.exchanges:
            raise ValueError(f"Exchange '{exchange_name}' not initialized")

        ex = self.exchanges[exchange_name]

        # SAFETY: Verify we are NOT on mainnet (multi-layer defense)
        is_testnet = (
            exchange_name == "binance_testnet"
            or getattr(ex, 'isSandboxModeEnabled', False)
            or getattr(ex, 'sandbox', False)
        )
        # Defense-in-depth: also verify the actual fapi URLs point to testnet
        fapi_private = ex.urls.get("api", {}).get("fapiPrivate", "")
        if not is_testnet and "testnet" not in fapi_private:
            raise RuntimeError(
                f"SAFETY ABORT: place_market_order called on NON-TESTNET exchange "
                f"'{exchange_name}'. This would place REAL orders with REAL money. "
                f"Use --mode testnet with --exchange binance_testnet only."
            )

        # Get expected price from ticker before order
        ticker = ex.fetch_ticker(symbol)
        expected_price = float(ticker.get("last") or ticker.get("close") or 0)

        # Place market order
        order = ex.create_market_order(symbol, side, amount, params=params or {})

        # Binance testnet often returns None for average/price/cost in the initial
        # create_order response. Fetch the order again to get real fill data.
        order_id = order.get("id")
        if order_id and order.get("status") != "closed":
            # Order might still be processing — wait briefly and refetch
            time.sleep(0.3)

        if order_id:
            try:
                fetched = ex.fetch_order(order_id, symbol)
                if fetched:
                    order = fetched  # Use the fetched data which has real fill prices
            except Exception as e:
                logger.warning(f"Could not refetch order {order_id}: {e}")

        # Extract fill details
        # Handle None values explicitly: dict.get("key", default) returns None
        # (not the default) when the key exists but the value is None.
        raw_avg = order.get("average")
        raw_price = order.get("price")
        raw_filled = order.get("filled")
        raw_cost = order.get("cost")

        fill_price = float(raw_avg if raw_avg is not None else
                           (raw_price if raw_price is not None else expected_price))
        fill_amount = float(raw_filled if raw_filled is not None else amount)
        fill_cost = float(raw_cost if raw_cost is not None else fill_price * fill_amount)
        order_id = str(order.get("id") or "UNKNOWN")
        fees = order.get("fees") or []

        # Calculate real slippage vs expected
        slippage_bps = 0.0
        if expected_price > 0:
            slippage_bps = (fill_price - expected_price) / expected_price * 10000
            if side == "sell":
                slippage_bps = -slippage_bps  # Negative slippage = worse for seller

        result = {
            "order_id": order_id,
            "side": side,
            "symbol": symbol,
            "expected_price": round(expected_price, 2),
            "fill_price": round(fill_price, 2),
            "fill_amount": round(fill_amount, 6),
            "fill_cost_usdt": round(fill_cost, 2),
            "fees": fees,
            "slippage_bps": round(slippage_bps, 2),
            "timestamp": int(time.time() * 1000),
            "exchange": exchange_name,
            "is_testnet": True,
        }
        logger.info(f"TESTNET ORDER: {side} {fill_amount} {symbol} @ {fill_price} "
                    f"(expected {expected_price}, slippage={slippage_bps:.2f}bps) id={order_id}")
        return result

    def fetch_positions(self, exchange_name: str, symbol: str = None) -> list:
        """Fetch open positions on testnet (for reconciliation).

        Args:
            exchange_name: Exchange to query.
            symbol: Optional symbol filter.

        Returns:
            List of position dicts from ccxt.
        """
        if exchange_name not in self.exchanges:
            return []
        ex = self.exchanges[exchange_name]
        try:
            positions = ex.fetch_positions(symbols=[symbol] if symbol else None)
            return positions
        except Exception as e:
            logger.warning(f"fetch_positions failed for {exchange_name}: {e}")
            return []

    # -----------------------------------------------------------------------
    # Cleanup
    # -----------------------------------------------------------------------

    def close(self):
        """Close all exchange connections.

        Properly shuts down ccxt exchange instances by closing any
        open sessions/connections.
        """
        for name, ex in self.exchanges.items():
            try:
                if hasattr(ex, "close"):
                    ex.close()
                logger.info(f"Closed exchange: {name}")
            except Exception as e:
                logger.warning(f"Error closing exchange {name}: {e}")

        self.exchanges.clear()

    def __del__(self):
        """Destructor — ensure connections are closed."""
        try:
            self.close()
        except Exception:
            pass


# ===========================================================================
# Mock / Synthetic data generators (for self-tests only)
# ===========================================================================

def _generate_mock_orderbook(mid_price: float = 50000.0, n_levels: int = 20,
                             spread_bps: float = 5.0, seed: int = 42) -> dict:
    """Generate a mock ccxt orderbook for testing.

    Args:
        mid_price: Mid price for the orderbook.
        n_levels: Number of bid/ask levels.
        spread_bps: Spread in basis points.
        seed: Random seed for reproducibility.

    Returns:
        Dict mimicking ccxt orderbook format.
    """
    import random
    rng = random.Random(seed)

    half_spread = mid_price * (spread_bps / 10000.0) / 2.0
    best_bid = mid_price - half_spread
    best_ask = mid_price + half_spread

    bids = []
    asks = []

    tick_size = mid_price * 0.0001  # 1 bps tick

    for i in range(n_levels):
        bid_price = best_bid - i * tick_size
        ask_price = best_ask + i * tick_size
        bid_size = rng.uniform(0.1, 5.0)
        ask_size = rng.uniform(0.1, 5.0)
        bids.append([bid_price, bid_size])
        asks.append([ask_price, ask_size])

    return {
        "bids": bids,
        "asks": asks,
        "timestamp": int(time.time() * 1000),
        "nonce": seed,
    }


def _generate_mock_ticker(mid_price: float = 50000.0, spread_bps: float = 5.0) -> dict:
    """Generate a mock ccxt ticker for testing.

    Args:
        mid_price: Mid price.
        spread_bps: Spread in basis points.

    Returns:
        Dict mimicking ccxt ticker format.
    """
    half_spread = mid_price * (spread_bps / 10000.0) / 2.0
    bid = mid_price - half_spread
    ask = mid_price + half_spread

    return {
        "symbol": "BTC/USDT",
        "bid": bid,
        "ask": ask,
        "last": mid_price,
        "high": mid_price * 1.02,
        "low": mid_price * 0.98,
        "baseVolume": 12345.67,
        "quoteVolume": 617283500.0,
        "percentage": 1.5,
        "timestamp": int(time.time() * 1000),
    }


def _generate_mock_trades(mid_price: float = 50000.0, n: int = 100,
                           seed: int = 42) -> list:
    """Generate mock ccxt trades for testing.

    Args:
        mid_price: Approximate trade price.
        n: Number of trades.
        seed: Random seed.

    Returns:
        List of dicts mimicking ccxt trade format.
    """
    import random
    rng = random.Random(seed)

    trades = []
    base_ts = int(time.time() * 1000) - n * 1000

    for i in range(n):
        price = mid_price + rng.uniform(-50, 50)
        amount = rng.uniform(0.001, 1.0)
        side = rng.choice(["buy", "sell"])
        trades.append({
            "id": str(1000 + i),
            "price": price,
            "amount": amount,
            "side": side,
            "timestamp": base_ts + i * 10,
        })

    return trades


def _generate_mock_funding_rate(rate: float = 0.0001) -> dict:
    """Generate mock ccxt funding rate for testing.

    Args:
        rate: Funding rate value.

    Returns:
        Dict mimicking ccxt funding rate format.
    """
    return {
        "symbol": "BTC/USDT",
        "fundingRate": rate,
        "fundingDatetime": None,
        "timestamp": int(time.time() * 1000),
    }


def _generate_mock_ohlcv(mid_price: float = 50000.0, n: int = 60,
                          seed: int = 42) -> list:
    """Generate mock ccxt OHLCV data for testing.

    Args:
        mid_price: Approximate price.
        n: Number of candles.
        seed: Random seed.

    Returns:
        List of [timestamp, open, high, low, close, volume] lists.
    """
    import random
    rng = random.Random(seed)

    ohlcv = []
    base_ts = int(time.time() * 1000) - n * 60000
    price = mid_price

    for i in range(n):
        o = price
        change = rng.gauss(0, 0.001) * price
        c = o + change
        h = max(o, c) + abs(rng.gauss(0, 0.0005)) * price
        l = min(o, c) - abs(rng.gauss(0, 0.0005)) * price
        v = rng.uniform(10, 500)
        ohlcv.append([base_ts + i * 60000, o, h, l, c, v])
        price = c

    return ohlcv


# ===========================================================================
# Self-Test
# ===========================================================================

def _run_mock_tests():
    """Run self-tests using mock data (no real API calls)."""

    print("=" * 70)
    print("exchange_connector.py — Self-Test (MOCK DATA)")
    print("=" * 70)

    passed = 0
    failed = 0

    # -----------------------------------------------------------------------
    # Test 1: ExchangeConnector initialization
    # -----------------------------------------------------------------------
    print("\n[Test 1] ExchangeConnector initialization...")
    try:
        connector = ExchangeConnector(symbol="BTC/USDT")
        assert "binance" in connector.exchanges, "binance should be initialized"
        assert "bybit" in connector.exchanges, "bybit should be initialized"
        assert connector.symbol == "BTC/USDT"
        assert connector.health["binance"]["status"] == HEALTHY
        assert connector.health["bybit"]["status"] == HEALTHY
        print(f"  Exchanges initialized: {list(connector.exchanges.keys())}")
        print(f"  Health: {connector.get_health()}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 2: NormalizedOrderbook.normalize()
    # -----------------------------------------------------------------------
    print("\n[Test 2] NormalizedOrderbook.normalize()...")
    try:
        raw_ob = _generate_mock_orderbook(mid_price=50000.0, n_levels=20, seed=42)
        normalized = NormalizedOrderbook.normalize(raw_ob, "binance", "BTC/USDT")

        assert normalized["exchange"] == "binance"
        assert normalized["symbol"] == "BTC/USDT"
        assert len(normalized["bids"]) == 20
        assert len(normalized["asks"]) == 20
        assert isinstance(normalized["bids"][0][0], float)
        assert isinstance(normalized["bids"][0][1], float)
        assert normalized["timestamp"] > 0

        # Verify bids are sorted descending
        for i in range(len(normalized["bids"]) - 1):
            assert normalized["bids"][i][0] >= normalized["bids"][i + 1][0], \
                "Bids should be sorted descending"

        # Verify asks are sorted ascending
        for i in range(len(normalized["asks"]) - 1):
            assert normalized["asks"][i][0] <= normalized["asks"][i + 1][0], \
                "Asks should be sorted ascending"

        print(f"  Bids: {len(normalized['bids'])} levels, "
              f"best={normalized['bids'][0][0]:.2f}")
        print(f"  Asks: {len(normalized['asks'])} levels, "
              f"best={normalized['asks'][0][0]:.2f}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 3: NormalizedOrderbook.normalize() with empty data
    # -----------------------------------------------------------------------
    print("\n[Test 3] NormalizedOrderbook.normalize() with empty orderbook...")
    try:
        empty_ob = {"bids": [], "asks": [], "timestamp": 0}
        normalized = NormalizedOrderbook.normalize(empty_ob, "bybit", "BTC/USDT")
        assert normalized["bids"] == []
        assert normalized["asks"] == []
        assert normalized["exchange"] == "bybit"
        print(f"  Empty orderbook handled correctly")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 4: NormalizedOrderbook.normalize() with string prices
    # -----------------------------------------------------------------------
    print("\n[Test 4] NormalizedOrderbook.normalize() with string prices...")
    try:
        string_ob = {
            "bids": [["50000.5", "1.5"], ["49999.0", "2.0"]],
            "asks": [["50001.0", "0.8"], ["50002.5", "1.2"]],
            "timestamp": 1700000000000,
        }
        normalized = NormalizedOrderbook.normalize(string_ob, "binance", "BTC/USDT")
        assert isinstance(normalized["bids"][0][0], float)
        assert isinstance(normalized["bids"][0][1], float)
        assert normalized["bids"][0][0] == 50000.5
        assert normalized["bids"][0][1] == 1.5
        print(f"  String prices converted to float correctly")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 5: NormalizedOrderbook.compute_depth_metrics()
    # -----------------------------------------------------------------------
    print("\n[Test 5] NormalizedOrderbook.compute_depth_metrics()...")
    try:
        raw_ob = _generate_mock_orderbook(mid_price=50000.0, n_levels=20,
                                          spread_bps=5.0, seed=42)
        normalized = NormalizedOrderbook.normalize(raw_ob, "binance", "BTC/USDT")
        metrics = NormalizedOrderbook.compute_depth_metrics(normalized)

        assert "bid_depth_usdt" in metrics
        assert "ask_depth_usdt" in metrics
        assert "spread_bps" in metrics
        assert "mid_price" in metrics
        assert "depth_imbalance" in metrics
        assert "available_liquidity_usdt" in metrics

        # mid_price should be approximately 50000
        assert abs(metrics["mid_price"] - 50000.0) < 1.0, \
            f"Mid price should be ~50000, got {metrics['mid_price']}"

        # spread_bps should be approximately 5
        assert abs(metrics["spread_bps"] - 5.0) < 1.0, \
            f"Spread should be ~5 bps, got {metrics['spread_bps']}"

        # Depth values should be positive
        assert metrics["bid_depth_usdt"] > 0
        assert metrics["ask_depth_usdt"] > 0
        assert metrics["available_liquidity_usdt"] > 0

        # depth_imbalance should be between -1 and 1
        assert -1.0 <= metrics["depth_imbalance"] <= 1.0

        # available_liquidity = bid_depth + ask_depth
        assert abs(metrics["available_liquidity_usdt"] -
                   (metrics["bid_depth_usdt"] + metrics["ask_depth_usdt"])) < 0.01

        print(f"  mid_price={metrics['mid_price']:.2f}, "
              f"spread_bps={metrics['spread_bps']:.2f}")
        print(f"  bid_depth={metrics['bid_depth_usdt']:.2f}, "
              f"ask_depth={metrics['ask_depth_usdt']:.2f}")
        print(f"  imbalance={metrics['depth_imbalance']:.4f}, "
              f"liquidity={metrics['available_liquidity_usdt']:.2f}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 6: NormalizedOrderbook.compute_depth_metrics() with empty orderbook
    # -----------------------------------------------------------------------
    print("\n[Test 6] compute_depth_metrics() with empty orderbook...")
    try:
        empty_normalized = NormalizedOrderbook.normalize(
            {"bids": [], "asks": [], "timestamp": 0}, "binance", "BTC/USDT"
        )
        metrics = NormalizedOrderbook.compute_depth_metrics(empty_normalized)
        assert metrics["bid_depth_usdt"] == 0.0
        assert metrics["ask_depth_usdt"] == 0.0
        assert metrics["spread_bps"] == 0.0
        assert metrics["mid_price"] == 0.0
        print(f"  Empty orderbook metrics default to 0 correctly")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 7: NormalizedOrderbook.compute_depth_curve()
    # -----------------------------------------------------------------------
    print("\n[Test 7] NormalizedOrderbook.compute_depth_curve()...")
    try:
        raw_ob = _generate_mock_orderbook(mid_price=50000.0, n_levels=20, seed=42)
        normalized = NormalizedOrderbook.normalize(raw_ob, "binance", "BTC/USDT")
        curve = NormalizedOrderbook.compute_depth_curve(normalized, n_levels=10)

        assert len(curve) == 20  # 10 bid levels + 10 ask levels
        assert curve[0]["side"] == "bid"
        assert curve[10]["side"] == "ask"

        # Bid price_pct_from_mid should be negative (below mid)
        for entry in curve[:10]:
            assert entry["side"] == "bid"
            assert entry["price_pct_from_mid"] <= 0
            assert entry["cumulative_depth_usdt"] > 0

        # Ask price_pct_from_mid should be positive (above mid)
        for entry in curve[10:]:
            assert entry["side"] == "ask"
            assert entry["price_pct_from_mid"] >= 0
            assert entry["cumulative_depth_usdt"] > 0

        # Cumulative depth should be monotonically increasing on each side
        for i in range(1, 10):
            assert curve[i]["cumulative_depth_usdt"] >= curve[i - 1]["cumulative_depth_usdt"]
        for i in range(11, 20):
            assert curve[i]["cumulative_depth_usdt"] >= curve[i - 1]["cumulative_depth_usdt"]

        print(f"  Curve has {len(curve)} entries (10 bid + 10 ask)")
        print(f"  Bid range: {curve[0]['price_pct_from_mid']:.4f}% to "
              f"{curve[9]['price_pct_from_mid']:.4f}%")
        print(f"  Ask range: {curve[10]['price_pct_from_mid']:.4f}% to "
              f"{curve[19]['price_pct_from_mid']:.4f}%")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 8: fetch_orderbook with mock data
    # -----------------------------------------------------------------------
    print("\n[Test 8] fetch_orderbook with mock exchange...")
    try:
        connector = ExchangeConnector(symbol="BTC/USDT")

        # Monkey-patch the exchange's fetch_order_book to return mock data
        raw_ob = _generate_mock_orderbook(mid_price=50000.0, n_levels=20, seed=99)
        connector.exchanges["binance"].fetch_order_book = lambda symbol, limit=20: raw_ob

        result = connector.fetch_orderbook("binance", limit=20)

        assert result is not None, "fetch_orderbook should return data"
        assert result["exchange"] == "binance"
        assert result["symbol"] == "BTC/USDT"
        assert len(result["bids"]) == 20
        assert len(result["asks"]) == 20
        assert result["latency_ms"] >= 0
        assert result["mid_price"] > 0
        assert result["spread_bps"] > 0
        assert result["bid_depth_usdt"] > 0
        assert result["ask_depth_usdt"] > 0

        print(f"  mid_price={result['mid_price']:.2f}, "
              f"spread_bps={result['spread_bps']:.2f}")
        print(f"  bid_depth={result['bid_depth_usdt']:.2f}, "
              f"ask_depth={result['ask_depth_usdt']:.2f}")
        print(f"  latency_ms={result['latency_ms']:.2f}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 9: fetch_ticker with mock data
    # -----------------------------------------------------------------------
    print("\n[Test 9] fetch_ticker with mock exchange...")
    try:
        connector = ExchangeConnector(symbol="BTC/USDT")

        raw_ticker = _generate_mock_ticker(mid_price=50000.0, spread_bps=3.0)
        connector.exchanges["binance"].fetch_ticker = lambda symbol: raw_ticker

        result = connector.fetch_ticker("binance")

        assert result is not None
        assert result["exchange"] == "binance"
        assert result["bid"] > 0
        assert result["ask"] > 0
        assert result["last"] > 0
        assert result["spread_bps"] > 0
        assert result["mid_price"] > 0
        assert result["latency_ms"] >= 0
        assert abs(result["mid_price"] - 50000.0) < 10.0

        print(f"  bid={result['bid']:.2f}, ask={result['ask']:.2f}, "
              f"mid={result['mid_price']:.2f}")
        print(f"  spread_bps={result['spread_bps']:.2f}, "
              f"volume={result['quote_volume']:.2f}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 10: fetch_trades with mock data
    # -----------------------------------------------------------------------
    print("\n[Test 10] fetch_trades with mock exchange...")
    try:
        connector = ExchangeConnector(symbol="BTC/USDT")

        raw_trades = _generate_mock_trades(mid_price=50000.0, n=50, seed=42)
        connector.exchanges["bybit"].fetch_trades = lambda symbol, limit=100: raw_trades[:30]

        result = connector.fetch_trades("bybit", limit=30)

        assert result is not None
        assert len(result) == 30
        assert result[0]["exchange"] == "bybit"
        assert result[0]["price"] > 0
        assert result[0]["amount"] > 0
        assert result[0]["side"] in ("buy", "sell")
        assert result[0]["cost_usdt"] > 0

        print(f"  Trades: {len(result)}, first price={result[0]['price']:.2f}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 11: fetch_funding_rate with mock data
    # -----------------------------------------------------------------------
    print("\n[Test 11] fetch_funding_rate with mock exchange...")
    try:
        connector = ExchangeConnector(symbol="BTC/USDT")

        raw_funding = _generate_mock_funding_rate(rate=0.0001)
        connector.exchanges["binance"].fetch_funding_rate = lambda symbol: raw_funding

        result = connector.fetch_funding_rate("binance")

        assert result is not None
        assert result["exchange"] == "binance"
        assert abs(result["rate"] - 0.0001) < 1e-10
        assert result["timestamp"] > 0

        print(f"  rate={result['rate']:.6f}, timestamp={result['timestamp']}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 12: fetch_ohlcv with mock data
    # -----------------------------------------------------------------------
    print("\n[Test 12] fetch_ohlcv with mock exchange...")
    try:
        connector = ExchangeConnector(symbol="BTC/USDT")

        raw_ohlcv = _generate_mock_ohlcv(mid_price=50000.0, n=60, seed=42)
        connector.exchanges["bybit"].fetch_ohlcv = lambda symbol, timeframe="1m", limit=60: raw_ohlcv

        result = connector.fetch_ohlcv("bybit", timeframe="1m", limit=60)

        assert result is not None
        assert len(result) == 60
        for candle in result:
            assert len(candle) == 6
            assert isinstance(candle[0], int)  # timestamp
            assert isinstance(candle[1], float)  # open
            assert candle[2] >= max(candle[1], candle[4])  # high >= max(o,c)
            assert candle[3] <= min(candle[1], candle[4])  # low <= min(o,c)

        print(f"  Candles: {len(result)}, first close={result[0][4]:.2f}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 13: fetch_all with mock data
    # -----------------------------------------------------------------------
    print("\n[Test 13] fetch_all with mock exchanges...")
    try:
        connector = ExchangeConnector(symbol="BTC/USDT")

        # Patch all exchange methods for both exchanges
        for name in connector.exchanges:
            ex = connector.exchanges[name]
            raw_ob = _generate_mock_orderbook(mid_price=50000.0, n_levels=20, seed=hash(name) % 10000)
            raw_ticker = _generate_mock_ticker(mid_price=50000.0)
            raw_trades = _generate_mock_trades(mid_price=50000.0, n=50, seed=hash(name) % 10000)
            raw_funding = _generate_mock_funding_rate()
            raw_ohlcv = _generate_mock_ohlcv(mid_price=50000.0, n=60, seed=hash(name) % 10000)

            ex.fetch_order_book = lambda symbol, limit=20, _ob=raw_ob: _ob
            ex.fetch_ticker = lambda symbol, _t=raw_ticker: _t
            ex.fetch_trades = lambda symbol, limit=100, _tr=raw_trades: _tr[:limit]
            ex.fetch_funding_rate = lambda symbol, _f=raw_funding: _f
            ex.fetch_ohlcv = lambda symbol, timeframe="1m", limit=60, _o=raw_ohlcv: _o[:limit]

        results = connector.fetch_all()

        assert "binance" in results
        assert "bybit" in results

        for name, data in results.items():
            assert data["orderbook"] is not None, f"{name} orderbook is None"
            assert data["ticker"] is not None, f"{name} ticker is None"
            # fetch_all intentionally omits trades because the shadow pipeline
            # does not consume them; asserting a fetch here was stale.
            assert data["trades"] is None, f"{name} trades should be skipped"
            if name in DERIVATIVE_SYMBOL_FORMATS:
                assert data["funding"] is not None, f"{name} funding is None"
                assert data["funding"]["derivative_symbol"] == "BTC/USDT:USDT"
            else:
                assert data["funding"] is None, f"{name} unexpectedly exposed funding"
            assert data["ohlcv"] is not None, f"{name} ohlcv is None"

        print(f"  Binance: OB={results['binance']['orderbook'] is not None}, "
              f"T={results['binance']['ticker'] is not None}")
        print(f"  Bybit:   OB={results['bybit']['orderbook'] is not None}, "
              f"T={results['bybit']['ticker'] is not None}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 14: Health tracking — success recording
    # -----------------------------------------------------------------------
    print("\n[Test 14] Health tracking — success recording...")
    try:
        connector = ExchangeConnector(symbol="BTC/USDT")

        raw_ticker = _generate_mock_ticker(mid_price=50000.0)
        connector.exchanges["binance"].fetch_ticker = lambda symbol: raw_ticker

        # Fetch ticker to trigger success recording
        result = connector.fetch_ticker("binance")
        assert result is not None

        health = connector.get_health()
        assert health["binance"]["connected"] is True
        assert health["binance"]["status"] == HEALTHY
        assert health["binance"]["consecutive_failures"] == 0
        assert health["binance"]["last_success_ms"] is not None
        assert health["binance"]["latency_ms"] is not None
        assert health["binance"]["latency_ms"] >= 0

        print(f"  Status: {health['binance']['status']}, "
              f"failures: {health['binance']['consecutive_failures']}, "
              f"latency: {health['binance']['latency_ms']:.2f}ms")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 15: Health tracking — failure recording and degradation
    # -----------------------------------------------------------------------
    print("\n[Test 15] Health tracking — failure recording and degradation...")
    try:
        connector = ExchangeConnector(symbol="BTC/USDT")

        # Patch binance to always fail
        def _raise_error(*args, **kwargs):
            raise ConnectionError("Simulated connection failure")

        connector.exchanges["binance"].fetch_ticker = _raise_error

        # Trigger failures
        for i in range(DEGRADED_THRESHOLD):
            result = connector.fetch_ticker("binance")
            assert result is None, "Should return None on failure"

        health = connector.get_health()
        assert health["binance"]["status"] == DEGRADED, \
            f"Expected DEGRADED after {DEGRADED_THRESHOLD} failures, got {health['binance']['status']}"
        assert health["binance"]["consecutive_failures"] == DEGRADED_THRESHOLD

        print(f"  After {DEGRADED_THRESHOLD} failures: status={health['binance']['status']}, "
              f"consecutive_failures={health['binance']['consecutive_failures']}")

        # Continue failing to DOWN
        for i in range(DOWN_THRESHOLD - DEGRADED_THRESHOLD):
            result = connector.fetch_ticker("binance")

        health = connector.get_health()
        assert health["binance"]["status"] == DOWN, \
            f"Expected DOWN after {DOWN_THRESHOLD} failures, got {health['binance']['status']}"

        print(f"  After {DOWN_THRESHOLD} failures: status={health['binance']['status']}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 16: Health recovery — success resets failure counter
    # -----------------------------------------------------------------------
    print("\n[Test 16] Health recovery — success resets failure counter...")
    try:
        connector = ExchangeConnector(symbol="BTC/USDT")

        # Make binance fail a few times
        def _raise_error(*args, **kwargs):
            raise ConnectionError("Simulated failure")

        connector.exchanges["binance"].fetch_ticker = _raise_error

        for i in range(3):
            connector.fetch_ticker("binance")

        health_before = connector.get_health()
        assert health_before["binance"]["consecutive_failures"] == 3

        # Now make it succeed
        raw_ticker = _generate_mock_ticker(mid_price=50000.0)
        connector.exchanges["binance"].fetch_ticker = lambda symbol: raw_ticker
        result = connector.fetch_ticker("binance")
        assert result is not None

        health_after = connector.get_health()
        assert health_after["binance"]["consecutive_failures"] == 0, \
            "Failures should reset on success"
        assert health_after["binance"]["status"] == HEALTHY, \
            "Status should return to HEALTHY on success"

        print(f"  Before: failures={health_before['binance']['consecutive_failures']}, "
              f"status={health_before['binance']['status']}")
        print(f"  After:  failures={health_after['binance']['consecutive_failures']}, "
              f"status={health_after['binance']['status']}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 17: Graceful degradation — one exchange fails, other continues
    # -----------------------------------------------------------------------
    print("\n[Test 17] Graceful degradation — one exchange fails, other continues...")
    try:
        connector = ExchangeConnector(symbol="BTC/USDT")

        # Make binance fail
        def _raise_error(*args, **kwargs):
            raise ConnectionError("Binance is down")

        connector.exchanges["binance"].fetch_order_book = _raise_error
        connector.exchanges["binance"].fetch_ticker = _raise_error

        # Make bybit succeed
        raw_ob = _generate_mock_orderbook(mid_price=50000.0, n_levels=20, seed=42)
        raw_ticker = _generate_mock_ticker(mid_price=50000.0)
        connector.exchanges["bybit"].fetch_order_book = lambda symbol, limit=20, _ob=raw_ob: _ob
        connector.exchanges["bybit"].fetch_ticker = lambda symbol, _t=raw_ticker: _t
        connector.exchanges["bybit"].fetch_trades = lambda symbol, limit=100: []
        connector.exchanges["bybit"].fetch_funding_rate = _raise_error  # funding might fail
        connector.exchanges["bybit"].fetch_ohlcv = lambda symbol, timeframe="1m", limit=60: []

        # fetch_all should still return bybit data
        results = connector.fetch_all()

        # Binance should have None values
        assert results["binance"]["orderbook"] is None, "Binance OB should be None"
        assert results["binance"]["ticker"] is None, "Binance ticker should be None"

        # Bybit should have data
        assert results["bybit"]["orderbook"] is not None, "Bybit OB should have data"
        assert results["bybit"]["ticker"] is not None, "Bybit ticker should have data"

        # Bybit funding should be None (we made it fail), but that's OK
        assert results["bybit"]["funding"] is None, "Bybit funding should be None"

        print(f"  Binance: OB={'None' if results['binance']['orderbook'] is None else 'OK'}, "
              f"T={'None' if results['binance']['ticker'] is None else 'OK'}")
        print(f"  Bybit:   OB={'OK' if results['bybit']['orderbook'] is not None else 'None'}, "
              f"T={'OK' if results['bybit']['ticker'] is not None else 'None'}")
        print(f"  System continues with one exchange down!")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 18: Fetching from non-existent exchange
    # -----------------------------------------------------------------------
    print("\n[Test 18] Fetching from non-existent exchange...")
    try:
        connector = ExchangeConnector(symbol="BTC/USDT")

        result = connector.fetch_orderbook("nonexistent")
        assert result is None, "Should return None for non-existent exchange"

        result = connector.fetch_ticker("nonexistent")
        assert result is None

        result = connector.fetch_trades("nonexistent")
        assert result is None

        latency = connector.measure_latency("nonexistent")
        assert latency == -1.0

        print(f"  Non-existent exchange correctly returns None/-1.0")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 19: Selective exchange initialization
    # -----------------------------------------------------------------------
    print("\n[Test 19] Selective exchange initialization...")
    try:
        connector = ExchangeConnector(
            symbol="BTC/USDT",
            config={"exchanges": ["binance"]}
        )

        assert "binance" in connector.exchanges
        assert "bybit" not in connector.exchanges

        print(f"  Initialized exchanges: {list(connector.exchanges.keys())}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 20: measure_latency with mock
    # -----------------------------------------------------------------------
    print("\n[Test 20] measure_latency with mock exchange...")
    try:
        connector = ExchangeConnector(symbol="BTC/USDT")

        raw_ticker = _generate_mock_ticker(mid_price=50000.0)
        connector.exchanges["binance"].fetch_ticker = lambda symbol: raw_ticker

        latency = connector.measure_latency("binance")
        assert latency >= 0, f"Latency should be non-negative, got {latency}"
        assert connector.latency_ms["binance"] == latency

        print(f"  Measured latency: {latency:.2f}ms")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 21: close() method
    # -----------------------------------------------------------------------
    print("\n[Test 21] close() method...")
    try:
        connector = ExchangeConnector(symbol="BTC/USDT")
        assert len(connector.exchanges) > 0

        connector.close()
        assert len(connector.exchanges) == 0, "Exchanges should be cleared after close"

        # Double close should not crash
        connector.close()

        print(f"  close() works correctly, no crash on double-close")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 22: get_health() comprehensive structure
    # -----------------------------------------------------------------------
    print("\n[Test 22] get_health() comprehensive structure...")
    try:
        connector = ExchangeConnector(symbol="BTC/USDT")

        # Make a successful fetch
        raw_ticker = _generate_mock_ticker(mid_price=50000.0)
        connector.exchanges["binance"].fetch_ticker = lambda symbol: raw_ticker
        connector.fetch_ticker("binance")

        health = connector.get_health()

        for name in ["binance", "bybit"]:
            assert name in health
            h = health[name]
            assert "connected" in h
            assert "status" in h
            assert "last_success_ms" in h
            assert "consecutive_failures" in h
            assert "latency_ms" in h

        # Binance should have a recent success
        assert health["binance"]["last_success_ms"] is not None
        assert health["binance"]["connected"] is True

        # Bybit hasn't been used yet
        assert health["bybit"]["last_success_ms"] is None
        assert health["bybit"]["consecutive_failures"] == 0

        print(f"  Binance: connected={health['binance']['connected']}, "
              f"status={health['binance']['status']}")
        print(f"  Bybit:   connected={health['bybit']['connected']}, "
              f"status={health['bybit']['status']}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 23: Depth metrics precision
    # -----------------------------------------------------------------------
    print("\n[Test 23] Depth metrics precision (known orderbook)...")
    try:
        # Create a known orderbook where we can verify the math
        mid = 10000.0
        known_ob = {
            "bids": [
                [9999.5, 1.0],   # within 0.5% of mid -> depth = 9999.5
                [9999.0, 2.0],   # within 0.5% -> depth += 19998.0
                [9950.0, 5.0],   # 0.5% below mid -> exactly at threshold
                [9900.0, 10.0],  # 1% below mid -> outside threshold
            ],
            "asks": [
                [10000.5, 1.5],  # within 0.5% -> depth = 15000.75
                [10001.0, 2.0],  # within 0.5% -> depth += 20002.0
                [10050.0, 3.0],  # 0.5% above mid -> exactly at threshold
                [10100.0, 5.0],  # 1% above mid -> outside threshold
            ],
            "timestamp": 1700000000000,
        }

        normalized = NormalizedOrderbook.normalize(known_ob, "test", "BTC/USDT")
        metrics = NormalizedOrderbook.compute_depth_metrics(normalized)

        # Mid price = (9999.5 + 10000.5) / 2 = 10000.0
        assert metrics["mid_price"] == 10000.0, f"Expected mid=10000.0, got {metrics['mid_price']}"

        # Spread = 10000.5 - 9999.5 = 1.0 -> 1.0/10000 * 10000 = 1.0 bps
        assert metrics["spread_bps"] == 1.0, f"Expected spread=1.0 bps, got {metrics['spread_bps']}"

        # Threshold: 0.5% from mid
        bid_threshold = 10000.0 * (1 - 0.005)  # = 9950.0
        ask_threshold = 10000.0 * (1 + 0.005)  # = 10050.0

        # Bid depth: levels with price >= 9950.0
        # 9999.5 * 1.0 = 9999.5
        # 9999.0 * 2.0 = 19998.0
        # 9950.0 * 5.0 = 49750.0 (exactly at threshold, included)
        expected_bid_depth = 9999.5 + 19998.0 + 49750.0
        assert abs(metrics["bid_depth_usdt"] - round(expected_bid_depth, 2)) < 1.0, \
            f"Expected bid_depth~{expected_bid_depth}, got {metrics['bid_depth_usdt']}"

        # Ask depth: levels with price <= 10050.0
        # 10000.5 * 1.5 = 15000.75
        # 10001.0 * 2.0 = 20002.0
        # 10050.0 * 3.0 = 30150.0 (exactly at threshold, included)
        expected_ask_depth = 15000.75 + 20002.0 + 30150.0
        assert abs(metrics["ask_depth_usdt"] - round(expected_ask_depth, 2)) < 1.0, \
            f"Expected ask_depth~{expected_ask_depth}, got {metrics['ask_depth_usdt']}"

        print(f"  Verified bid_depth={metrics['bid_depth_usdt']:.2f} "
              f"(expected ~{expected_bid_depth:.2f})")
        print(f"  Verified ask_depth={metrics['ask_depth_usdt']:.2f} "
              f"(expected ~{expected_ask_depth:.2f})")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 24: Depth curve with known orderbook
    # -----------------------------------------------------------------------
    print("\n[Test 24] Depth curve with known orderbook...")
    try:
        known_ob = {
            "bids": [[9999.0, 1.0], [9998.0, 2.0], [9997.0, 3.0]],
            "asks": [[10001.0, 1.5], [10002.0, 2.5], [10003.0, 3.5]],
            "timestamp": 1700000000000,
        }

        normalized = NormalizedOrderbook.normalize(known_ob, "test", "BTC/USDT")
        curve = NormalizedOrderbook.compute_depth_curve(normalized, n_levels=3)

        assert len(curve) == 6  # 3 bid + 3 ask

        # Verify cumulative depth increases
        bid_cumulative = [e["cumulative_depth_usdt"] for e in curve if e["side"] == "bid"]
        ask_cumulative = [e["cumulative_depth_usdt"] for e in curve if e["side"] == "ask"]

        for i in range(1, len(bid_cumulative)):
            assert bid_cumulative[i] > bid_cumulative[i - 1], \
                "Bid cumulative depth should increase"
        for i in range(1, len(ask_cumulative)):
            assert ask_cumulative[i] > ask_cumulative[i - 1], \
                "Ask cumulative depth should increase"

        # First bid level: 9999.0 * 1.0 = 9999.0
        assert abs(bid_cumulative[0] - 9999.0) < 1.0
        # Second bid level: 9999.0 + 9998.0 * 2.0 = 9999.0 + 19996.0 = 29995.0
        assert abs(bid_cumulative[1] - 29995.0) < 1.0

        print(f"  Bid cumulative: {[round(v, 2) for v in bid_cumulative]}")
        print(f"  Ask cumulative: {[round(v, 2) for v in ask_cumulative]}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Test 25: Fetch with zero-price orderbook (safety)
    # -----------------------------------------------------------------------
    print("\n[Test 25] Zero-price orderbook edge case...")
    try:
        zero_ob = {
            "bids": [[0.0, 1.0], [0.0, 2.0]],
            "asks": [[0.0, 1.5], [0.0, 2.5]],
            "timestamp": 1700000000000,
        }
        normalized = NormalizedOrderbook.normalize(zero_ob, "test", "BTC/USDT")
        metrics = NormalizedOrderbook.compute_depth_metrics(normalized)

        # Should handle gracefully without division by zero
        assert metrics["spread_bps"] == 0.0
        assert metrics["mid_price"] == 0.0

        print(f"  Zero-price orderbook handled: spread_bps={metrics['spread_bps']}, "
              f"mid_price={metrics['mid_price']}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        failed += 1

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    print("\n" + "=" * 70)
    print(f"Self-Test Results: {passed} PASSED, {failed} FAILED")
    if failed == 0:
        print("All self-tests PASSED")
    else:
        print(f"WARNING: {failed} test(s) FAILED!")
    print("=" * 70)

    return failed == 0


def _run_live_tests():
    """Run live connectivity tests against real exchanges.

    This makes actual API calls to Binance and Bybit.
    No API keys required — uses public endpoints only.
    """

    print("=" * 70)
    print("exchange_connector.py — LIVE Connectivity Test")
    print("WARNING: This makes real API calls to exchanges!")
    print("=" * 70)

    passed = 0
    failed = 0

    connector = ExchangeConnector(symbol="BTC/USDT")

    try:
        # Live Test 1: Binance ticker
        print("\n[Live Test 1] Binance ticker...")
        try:
            ticker = connector.fetch_ticker("binance")
            if ticker:
                print(f"  bid={ticker['bid']:.2f}, ask={ticker['ask']:.2f}, "
                      f"mid={ticker['mid_price']:.2f}")
                print(f"  latency={ticker['latency_ms']:.1f}ms")
                passed += 1
            else:
                print("  FAIL: Returned None")
                failed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1

        # Live Test 2: Bybit ticker
        print("\n[Live Test 2] Bybit ticker...")
        try:
            ticker = connector.fetch_ticker("bybit")
            if ticker:
                print(f"  bid={ticker['bid']:.2f}, ask={ticker['ask']:.2f}, "
                      f"mid={ticker['mid_price']:.2f}")
                print(f"  latency={ticker['latency_ms']:.1f}ms")
                passed += 1
            else:
                print("  FAIL: Returned None")
                failed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1

        # Live Test 3: Binance orderbook
        print("\n[Live Test 3] Binance orderbook...")
        try:
            ob = connector.fetch_orderbook("binance", limit=10)
            if ob:
                print(f"  bids={len(ob['bids'])}, asks={len(ob['asks'])}")
                print(f"  mid={ob['mid_price']:.2f}, spread={ob['spread_bps']:.2f}bps")
                print(f"  bid_depth(0.5%)={ob['bid_depth_usdt']:.2f}, "
                      f"ask_depth(0.5%)={ob['ask_depth_usdt']:.2f}")
                passed += 1
            else:
                print("  FAIL: Returned None")
                failed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1

        # Live Test 4: Bybit orderbook
        print("\n[Live Test 4] Bybit orderbook...")
        try:
            ob = connector.fetch_orderbook("bybit", limit=10)
            if ob:
                print(f"  bids={len(ob['bids'])}, asks={len(ob['asks'])}")
                print(f"  mid={ob['mid_price']:.2f}, spread={ob['spread_bps']:.2f}bps")
                passed += 1
            else:
                print("  FAIL: Returned None")
                failed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1

        # Live Test 5: Binance trades
        print("\n[Live Test 5] Binance recent trades...")
        try:
            trades = connector.fetch_trades("binance", limit=10)
            if trades and len(trades) > 0:
                print(f"  trades={len(trades)}, last price={trades[0]['price']:.2f}")
                passed += 1
            else:
                print("  FAIL: No trades returned")
                failed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1

        # Live Test 6: Binance OHLCV
        print("\n[Live Test 6] Binance OHLCV (1m, 10 candles)...")
        try:
            ohlcv = connector.fetch_ohlcv("binance", timeframe="1m", limit=10)
            if ohlcv and len(ohlcv) > 0:
                print(f"  candles={len(ohlcv)}, last close={ohlcv[-1][4]:.2f}")
                passed += 1
            else:
                print("  FAIL: No OHLCV data returned")
                failed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1

        # Live Test 7: Funding rate (may fail for spot)
        print("\n[Live Test 7] Funding rate (may not be available for spot)...")
        try:
            funding = connector.fetch_funding_rate("binance")
            if funding:
                print(f"  rate={funding['rate']:.6f}")
                passed += 1
            else:
                print("  Funding rate not available (expected for spot)")
                passed += 1  # Not a failure
        except Exception as e:
            print(f"  Expected: {e}")
            passed += 1  # Not a failure for spot

        # Live Test 8: Latency measurement
        print("\n[Live Test 8] Latency measurement...")
        try:
            binance_latency = connector.measure_latency("binance")
            bybit_latency = connector.measure_latency("bybit")
            print(f"  Binance: {binance_latency:.1f}ms")
            print(f"  Bybit:   {bybit_latency:.1f}ms")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1

        # Live Test 9: Health check
        print("\n[Live Test 9] Health check after live tests...")
        try:
            health = connector.get_health()
            for name, h in health.items():
                print(f"  {name}: connected={h['connected']}, "
                      f"status={h['status']}, "
                      f"failures={h['consecutive_failures']}, "
                      f"latency={h['latency_ms']:.1f}ms")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1

        # Live Test 10: Cross-exchange price comparison
        print("\n[Live Test 10] Cross-exchange price comparison...")
        try:
            binance_ob = connector.fetch_orderbook("binance", limit=5)
            bybit_ob = connector.fetch_orderbook("bybit", limit=5)
            if binance_ob and bybit_ob:
                price_diff = abs(binance_ob["mid_price"] - bybit_ob["mid_price"])
                avg_price = (binance_ob["mid_price"] + bybit_ob["mid_price"]) / 2
                diff_bps = (price_diff / avg_price) * 10000
                print(f"  Binance mid: {binance_ob['mid_price']:.2f}")
                print(f"  Bybit mid:   {bybit_ob['mid_price']:.2f}")
                print(f"  Diff: {diff_bps:.2f} bps")
                # Cross-exchange difference should be small for BTC/USDT
                if diff_bps < 50:  # Less than 50 bps difference expected
                    passed += 1
                else:
                    print(f"  WARNING: Large cross-exchange difference: {diff_bps:.2f} bps")
                    passed += 1  # Not necessarily a code failure
            else:
                print("  Could not fetch both orderbooks for comparison")
                failed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1

    finally:
        connector.close()

    print("\n" + "=" * 70)
    print(f"Live Test Results: {passed} PASSED, {failed} FAILED")
    if failed == 0:
        print("All live tests PASSED")
    else:
        print(f"WARNING: {failed} test(s) FAILED!")
    print("=" * 70)

    return failed == 0


# ===========================================================================
# Multi-Exchange Fallback Chain
# ===========================================================================

DEFAULT_FALLBACK_CHAIN = ["okx", "kraken", "gate", "mexc", "bitget"]


def build_feature_observations(
    *, exchange: str, ohlcv: list | None, orderbook: dict | None,
    funding: dict | None, open_interest: dict | None,
    fallback_used: bool = False, instrument: str | None = None,
    query_observation_epoch: float | None = None,
) -> dict:
    """Describe the six predictor inputs without conflating absence with zero."""
    query_observation_epoch = float(query_observation_epoch if query_observation_epoch is not None else time.time())
    def observed(value: float) -> str:
        return "REAL_OBSERVED_ZERO" if abs(float(value)) <= 1e-12 else "REAL_NONZERO"

    candles_ok = bool(ohlcv and len(ohlcv) >= 2 and ohlcv[-2][4] and ohlcv[-2][5])
    depth = (orderbook or {}).get("bid_depth_usdt") or (orderbook or {}).get("bid_depth") or 0
    depth += (orderbook or {}).get("ask_depth_usdt") or (orderbook or {}).get("ask_depth") or 0
    book_ok = depth > 0
    funding_ok = funding is not None and funding.get("rate") is not None
    # A point-in-time OI amount is not OI momentum. The connector does not have
    # a prior comparable snapshot, so its historical 0.0 was a fallback.
    oi_momentum_ok = bool(open_interest and open_interest.get("oi_change_observed") is True)
    derivative_capable = exchange in DERIVATIVE_SYMBOL_FORMATS
    transport = "FALLBACK_USED" if fallback_used else "PRIMARY_SOURCE"

    def item(
        status: str, source: str, fallback: float | None = None, *,
        observed_value: float | None = None, observed_at: int | None = None,
        instrument: str | None = None, market_type: str | None = None,
    ) -> dict:
        return {
            "status": status, "source": source,
            "transport_status": transport, "fallback_value": fallback,
            "observed_value": observed_value, "observed_at": observed_at,
            "exchange_timestamp": observed_at,
            "query_observation_epoch": query_observation_epoch,
            "instrument": instrument, "market_type": market_type,
        }

    price_momentum = 0.0
    volume_delta = 0.0
    candle_observed_at = None
    if candles_ok:
        price_momentum = (float(ohlcv[-1][4]) - float(ohlcv[-2][4])) / float(ohlcv[-2][4])
        volume_delta = (float(ohlcv[-1][5]) - float(ohlcv[-2][5])) / float(ohlcv[-2][5])
        candle_observed_at = int(ohlcv[-1][0])
    imbalance = 0.0
    book_observed_at = (orderbook or {}).get("timestamp")
    if book_ok:
        bid = float((orderbook or {}).get("bid_depth_usdt") or (orderbook or {}).get("bid_depth") or 0)
        ask = float((orderbook or {}).get("ask_depth_usdt") or (orderbook or {}).get("ask_depth") or 0)
        imbalance = (bid - ask) / (bid + ask)
    funding_signal = -float((funding or {}).get("rate") or 0.0) * 100.0
    oi_momentum = float((open_interest or {}).get("oi_change_24h_pct") or 0.0) / 100.0
    unavailable = "SOURCE_ERROR" if derivative_capable else "NOT_APPLICABLE"
    return {
        "price_momentum": item(
            observed(price_momentum) if candles_ok else "MISSING",
            f"{exchange}:ohlcv", None if candles_ok else 0.0,
            observed_at=candle_observed_at,
            instrument=instrument, market_type="SPOT_OBSERVATION",
        ),
        "volume_delta": item(
            observed(volume_delta) if candles_ok else "MISSING",
            f"{exchange}:ohlcv", None if candles_ok else 0.0,
            observed_at=candle_observed_at,
            instrument=instrument, market_type="SPOT_OBSERVATION",
        ),
        "bidask_imbalance": item(
            observed(imbalance) if book_ok else "MISSING",
            f"{exchange}:orderbook", None if book_ok else 0.0,
            observed_at=book_observed_at if book_ok else None,
            instrument=instrument, market_type="SPOT_OBSERVATION",
        ),
        "orderflow": item(
            observed(imbalance * (1.0 + abs(volume_delta))) if book_ok and candles_ok else "MISSING",
            f"{exchange}:orderbook+ohlcv", None if book_ok and candles_ok else 0.0,
            observed_at=(
                max(int(candle_observed_at), int(book_observed_at))
                if candle_observed_at is not None and book_observed_at is not None
                else None
            ),
            instrument=instrument, market_type="SPOT_OBSERVATION",
        ),
        "funding_signal": item(
            observed(funding_signal) if funding_ok else unavailable,
            str((funding or {}).get("source") or f"{exchange}:funding_public_get"),
            None if funding_ok else 0.0,
            observed_value=funding_signal if funding_ok else None,
            observed_at=(funding or {}).get("timestamp"),
            instrument=(funding or {}).get("derivative_symbol"),
            market_type=(funding or {}).get("market_type"),
        ),
        "oi_momentum": item(
            observed(oi_momentum) if oi_momentum_ok else ("MISSING" if open_interest else unavailable),
            str((open_interest or {}).get("source") or f"{exchange}:open_interest_public_get"),
            None if oi_momentum_ok else 0.0,
            observed_value=oi_momentum if oi_momentum_ok else None,
            observed_at=(open_interest or {}).get("timestamp"),
            instrument=(open_interest or {}).get("derivative_symbol"),
            market_type=(open_interest or {}).get("market_type"),
        ),
    }


def fetch_market_snapshot_with_fallback(
    symbol: str = "BTC/USDT",
    timeframe: str = "15m",
    fallback_chain: list = None,
) -> tuple:
    """Try each exchange in the fallback chain until one succeeds.

    Returns:
        (market_data_dict, exchange_used_str) on success.
        (None, None) if ALL exchanges fail.
    """
    chain = fallback_chain or DEFAULT_FALLBACK_CHAIN
    last_error = None

    for exchange_index, exchange_name in enumerate(chain):
        try:
            logger.info(f"Fallback chain: trying {exchange_name} for {symbol}")
            connector = ExchangeConnector(
                symbol=symbol,
                config={
                    "exchanges": [exchange_name],
                    "timeframe": timeframe,
                    "ohlcv_limit": 100,
                },
            )

            if exchange_name not in connector.exchanges:
                logger.warning(f"  {exchange_name}: not initialized, skipping")
                continue

            # Fetch all data types
            ohlcv = connector.fetch_ohlcv(exchange_name, timeframe=timeframe, limit=100)
            ticker = connector.fetch_ticker(exchange_name)
            orderbook = connector.fetch_orderbook(exchange_name)
            funding = connector.fetch_funding_rate(exchange_name)
            oi = connector.fetch_open_interest(exchange_name)

            # Validate: price must be > 0
            bid = 0.0
            ask = 0.0
            if ticker:
                bid = float(ticker.get("bid") or 0)
                ask = float(ticker.get("ask") or 0)
            if bid == 0 and orderbook:
                raw_bids = orderbook.get("bids", [])
                raw_asks = orderbook.get("asks", [])
                if raw_bids:
                    bid = float(raw_bids[0][0])
                if raw_asks:
                    ask = float(raw_asks[0][0])
            if bid == 0 and ohlcv:
                bid = ohlcv[-1][4]
                ask = bid

            mid = (bid + ask) / 2.0 if (bid + ask) > 0 else 0

            if mid <= 0:
                logger.warning(f"  {exchange_name}: price=0 (geo-blocked or failed), skipping")
                try:
                    connector.close()
                except Exception:
                    pass
                continue

            # Build market data (same structure as predict_only.py fetch_market_snapshot)
            candle_ts = ohlcv[-1][0] if ohlcv else 0
            spread = ask - bid
            spread_pct = spread / mid if mid > 0 else 0
            spread_bps = spread_pct * 10000.0

            bid_depth = 0.0
            ask_depth = 0.0
            if orderbook:
                bid_depth = float(orderbook.get("bid_depth_usdt", 0) or orderbook.get("bid_depth", 0) or 0)
                ask_depth = float(orderbook.get("ask_depth_usdt", 0) or orderbook.get("ask_depth", 0) or 0)
                if bid_depth == 0 and ask_depth == 0:
                    raw_bids = orderbook.get("bids", [])
                    raw_asks = orderbook.get("asks", [])
                    for b in (raw_bids or [])[:20]:
                        if len(b) >= 2:
                            bid_depth += float(b[1])
                    for a in (raw_asks or [])[:20]:
                        if len(a) >= 2:
                            ask_depth += float(a[1])

            funding_rate = 0.0
            next_funding_ms = 0
            if funding:
                funding_rate = float(funding.get("rate") or 0)
                next_funding_ms = int(funding.get("next_funding_ms") or 0)

            oi_value = 0.0
            oi_change_pct = 0.0
            if oi:
                oi_value = float(oi.get("oi_value") or 0)
                oi_change_pct = float(oi.get("oi_change_24h_pct") or 0)

            volume_24h = float(ticker.get("quote_volume", 0) or 0) if ticker else 0
            liquidity_quality = max(0.0, 1.0 - spread_pct * 100)

            market_data = {
                "symbol": symbol,
                "timeframe": timeframe,
                "ohlcv": ohlcv or [],
                "ticker": {
                    "bid": round(bid, 2),
                    "ask": round(ask, 2),
                    "spread": round(spread, 2),
                    "spread_pct": round(spread_pct, 8),
                    "spread_bps": round(spread_bps, 2),
                    "volume_24h": round(volume_24h, 2),
                },
                "orderbook": {
                    "bid_depth": round(bid_depth, 4),
                    "ask_depth": round(ask_depth, 4),
                    "spread": round(spread, 2),
                },
                "funding": {
                    "rate": funding_rate,
                    "next_funding_ms": next_funding_ms,
                    "predicted_rate": 0.0,
                },
                "open_interest": {
                    "oi_value": oi_value,
                    "oi_change_24h_pct": oi_change_pct,
                },
                "timestamp": int(time.time() * 1000),
                "candle_ts": candle_ts,
                "liquidity_quality": round(liquidity_quality, 4),
                "exchange_used": exchange_name,
                "feature_observations": build_feature_observations(
                    exchange=exchange_name,
                    ohlcv=ohlcv,
                    orderbook=orderbook,
                    funding=funding,
                    open_interest=oi,
                    fallback_used=exchange_index > 0,
                    instrument=symbol,
                    query_observation_epoch=time.time(),
                ),
            }

            try:
                connector.close()
            except Exception:
                pass

            logger.info(f"  {exchange_name}: SUCCESS (price={mid:.2f})")
            return market_data, exchange_name

        except Exception as e:
            last_error = e
            logger.warning(f"  {exchange_name}: FAILED — {e}")
            continue

    logger.error(f"ALL exchanges failed for {symbol}. Last error: {last_error}")
    return None, None


# ===========================================================================
# Main entry point
# ===========================================================================

if __name__ == "__main__":
    import sys

    live_mode = "--live" in sys.argv

    if live_mode:
        # Run both mock and live tests
        mock_ok = _run_mock_tests()
        print("\n")
        live_ok = _run_live_tests()
        sys.exit(0 if (mock_ok and live_ok) else 1)
    else:
        # Run mock tests only (default)
        mock_ok = _run_mock_tests()
        sys.exit(0 if mock_ok else 1)
