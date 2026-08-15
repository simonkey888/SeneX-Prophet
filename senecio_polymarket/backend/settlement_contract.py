"""AUD-063 causal settlement primitives.

Pure validation lives here so the primary NULL->WIN/LOSS verifier and the
repair-only reconciler share one historical-price contract. Public exchange
reads only; no order, wallet, signer, or database write occurs in this module.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from typing import Any

CANDLE_INTERVAL_MS = 60_000
WINDOW_15M_S = 900
WINDOW_1H_S = 3600
ALLOWED_PUBLIC_EXCHANGES = frozenset({"okx", "kraken", "gate", "mexc", "bitget"})


def parse_utc(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_symbol(value: Any) -> str:
    text = str(value or "").upper().strip()
    if "/" in text:
        return text
    if text.endswith("USDT") and len(text) > 4:
        return f"{text[:-4]}/USDT"
    return text


def normalize_exchange(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in ALLOWED_PUBLIC_EXCHANGES else None


def target_epoch_ms(ts_iso: Any, window_seconds: int) -> int | None:
    if window_seconds not in (WINDOW_15M_S, WINDOW_1H_S):
        return None
    origin = parse_utc(ts_iso)
    if origin is None:
        return None
    return int((origin + timedelta(seconds=window_seconds)).timestamp() * 1000)


def select_containing_candle(
    candles: list[Any],
    *,
    target_ms: int,
    interval_ms: int = CANDLE_INTERVAL_MS,
) -> list[Any] | None:
    """Return the canonical candle containing target_ms, never nearest-current.

    A 1m OHLCV candle is admissible iff ``open_ms <= target < open_ms+60s``.
    This bounded rule rejects stale/adjacent candles instead of silently using
    whatever a venue returns around the target.
    """
    valid: list[list[Any]] = []
    for candle in candles or []:
        try:
            open_ms = int(candle[0])
            close = float(candle[4])
        except (TypeError, ValueError, IndexError):
            continue
        if close <= 0 or not math.isfinite(close):
            continue
        if open_ms <= target_ms < open_ms + interval_ms:
            valid.append(candle)
    if not valid:
        return None
    return max(valid, key=lambda item: int(item[0]))


def price_evidence_from_candles(
    *,
    candles: list[Any],
    exchange: Any,
    symbol: Any,
    ts_iso: Any,
    window_seconds: int,
    observed_at: Any | None = None,
) -> dict[str, Any] | None:
    source = normalize_exchange(exchange)
    normalized_symbol = normalize_symbol(symbol)
    target_ms = target_epoch_ms(ts_iso, window_seconds)
    if source is None or not normalized_symbol or target_ms is None:
        return None
    candle = select_containing_candle(candles, target_ms=target_ms)
    if candle is None:
        return None
    candle_open_ms = int(candle[0])
    candle_close_ms = candle_open_ms + CANDLE_INTERVAL_MS
    price = float(candle[4])
    observed = parse_utc(observed_at) if observed_at is not None else datetime.now(timezone.utc)
    if observed is None:
        return None
    observed_ms = int(observed.timestamp() * 1000)
    # The current/last OHLCV candle is provisional until its interval closes.
    # Never freeze a provisional close into the immutable settlement CAS.
    if observed_ms < candle_close_ms:
        return None
    return {
        "version": "historical-price-evidence-v1",
        "source": source,
        "symbol": normalized_symbol,
        "window_seconds": int(window_seconds),
        "target_epoch_ms": target_ms,
        "candle_open_epoch_ms": candle_open_ms,
        "candle_close_epoch_ms": candle_close_ms,
        "candle_interval_ms": CANDLE_INTERVAL_MS,
        "target_offset_from_candle_open_ms": target_ms - candle_open_ms,
        "price": price,
        "observed_at": observed.isoformat(),
        "selection_rule": "ONE_MINUTE_CANDLE_CONTAINING_EXACT_TARGET",
        "maturity_rule": "OBSERVED_AT_GTE_CANDLE_CLOSE_EPOCH_MS",
    }


def validate_price_evidence(
    evidence: Any,
    *,
    expected_exchange: Any,
    expected_symbol: Any,
    expected_ts: Any,
    expected_window_seconds: int,
) -> bool:
    if not isinstance(evidence, dict) or evidence.get("version") != "historical-price-evidence-v1":
        return False
    source = normalize_exchange(expected_exchange)
    symbol = normalize_symbol(expected_symbol)
    target_ms = target_epoch_ms(expected_ts, expected_window_seconds)
    if source is None or not symbol or target_ms is None:
        return False
    if evidence.get("source") != source or evidence.get("symbol") != symbol:
        return False
    try:
        window = int(evidence.get("window_seconds"))
        actual_target = int(evidence.get("target_epoch_ms"))
        open_ms = int(evidence.get("candle_open_epoch_ms"))
        close_ms = int(evidence.get("candle_close_epoch_ms"))
        interval_ms = int(evidence.get("candle_interval_ms"))
        price = float(evidence.get("price"))
    except (TypeError, ValueError):
        return False
    if window != expected_window_seconds or actual_target != target_ms:
        return False
    if interval_ms != CANDLE_INTERVAL_MS:
        return False
    if close_ms != open_ms + interval_ms:
        return False
    if not (open_ms <= target_ms < close_ms):
        return False
    if price <= 0 or not math.isfinite(price):
        return False
    observed = parse_utc(evidence.get("observed_at"))
    if observed is None:
        return False
    return int(observed.timestamp() * 1000) >= close_ms


def directional_outcome(direction: Any, origin: Any, later: Any) -> str | None:
    try:
        origin_f = float(origin)
        later_f = float(later)
    except (TypeError, ValueError):
        return None
    if origin_f <= 0 or later_f <= 0 or not math.isfinite(origin_f) or not math.isfinite(later_f):
        return None
    direction_s = str(direction or "").upper()
    if direction_s == "LONG":
        return "WIN" if later_f > origin_f else "LOSS"
    if direction_s == "SHORT":
        return "WIN" if later_f < origin_f else "LOSS"
    return None


def fetch_historical_price_evidence(
    exchange_name: Any,
    symbol: Any,
    ts_iso: Any,
    window_seconds: int,
) -> dict[str, Any] | None:
    """Fetch a same-source public 1m candle and return bounded causal evidence."""
    source = normalize_exchange(exchange_name)
    normalized_symbol = normalize_symbol(symbol)
    target_ms = target_epoch_ms(ts_iso, window_seconds)
    if source is None or not normalized_symbol or target_ms is None:
        return None
    try:
        import ccxt
        exchange_type = getattr(ccxt, source, None)
        if exchange_type is None:
            return None
        ex = exchange_type({"enableRateLimit": True})
        try:
            candles = ex.fetch_ohlcv(
                normalized_symbol,
                timeframe="1m",
                since=target_ms - CANDLE_INTERVAL_MS,
                limit=3,
            )
        finally:
            try:
                ex.close()
            except Exception:
                pass
    except Exception:
        return None
    return price_evidence_from_candles(
        candles=candles or [],
        exchange=source,
        symbol=normalized_symbol,
        ts_iso=ts_iso,
        window_seconds=window_seconds,
    )
