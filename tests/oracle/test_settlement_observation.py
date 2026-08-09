import asyncio
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from senecio_polymarket.backend import oracle_runner


def _install_fake_ccxt(monkeypatch, candles):
    class FakeOkx:
        def __init__(self, config):
            self.config = config

        def fetch_ohlcv(self, symbol, timeframe, since, limit):
            assert timeframe == "1m"
            assert limit == 2
            return candles

    monkeypatch.setitem(sys.modules, "ccxt", SimpleNamespace(okx=FakeOkx))


def test_exact_containing_candle_emits_replayable_observation(monkeypatch):
    predicted_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    target_at = predicted_at + timedelta(hours=1, seconds=30)
    target_ms = int(target_at.timestamp() * 1000)
    open_ms = target_ms - target_ms % 60_000
    _install_fake_ccxt(monkeypatch, [
        [open_ms - 60_000, 99, 102, 98, 100, 1],
        [open_ms, 100, 102, 99, 101, 1],
    ])

    observation = asyncio.run(oracle_runner._fetch_price_at_time(
        "BTC/USDT", predicted_at.isoformat(), window_seconds=3630,
    ))

    assert observation["target_ts_ms"] == target_ms
    assert observation["candle_open_ts_ms"] == open_ms
    assert observation["candle_close_ts_ms"] == open_ms + 60_000
    assert observation["price"] == 101.0


def test_nearest_candle_fallback_is_forbidden(monkeypatch):
    predicted_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    target_at = predicted_at + timedelta(hours=1, seconds=30)
    target_ms = int(target_at.timestamp() * 1000)
    open_ms = target_ms - target_ms % 60_000
    _install_fake_ccxt(monkeypatch, [
        [open_ms - 120_000, 99, 102, 98, 100, 1],
        [open_ms + 60_000, 100, 102, 99, 101, 1],
    ])

    observation = asyncio.run(oracle_runner._fetch_price_at_time(
        "BTC/USDT", predicted_at.isoformat(), window_seconds=3630,
    ))

    assert observation is None
