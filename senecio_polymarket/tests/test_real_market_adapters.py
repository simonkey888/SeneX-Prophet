import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from backend import boros_market_adapter as boros
from backend import kalshi_market_adapter as kalshi
from backend import polymarket_market_adapter as poly
from oracle_runtime import institutional_core_real as real_core
from oracle_runtime import predict_only as runtime_predictor


class PolymarketAdapterTests(unittest.TestCase):
    def test_candidate_slug_is_epoch_aligned_5m(self):
        slugs = poly.candidate_slugs(1_785_033_685)
        self.assertEqual(slugs[0], "btc-updown-5m-1785033600")

    def test_normalize_event_uses_slug_window_and_maps_up_down_tokens(self):
        event = {
            "id": "event-1",
            "slug": "btc-updown-5m-1785033600",
            "active": True,
            "closed": False,
            "resolutionSource": "https://data.chain.link/streams/btc-usd",
            # Deliberately coarse/wrong for the 5m window: slug must win.
            "endDate": "2026-07-26T00:00:00Z",
            "markets": [{
                "id": "market-1",
                "conditionId": "cond",
                "question": "Bitcoin Up or Down",
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "outcomes": '["Up", "Down"]',
                "clobTokenIds": '["up-token", "down-token"]',
            }],
        }
        result = poly.normalize_event(event, 1_785_033_685)
        self.assertIsNotNone(result)
        self.assertEqual(result["start_ts"], 1_785_033_600)
        self.assertEqual(result["end_ts"], 1_785_033_900)
        self.assertEqual(result["up_token_id"], "up-token")
        self.assertEqual(result["down_token_id"], "down-token")
        self.assertTrue(result["accepting_orders"])

    def test_book_metrics_use_top_five_real_levels(self):
        book = {
            "bids": [{"price": "0.40", "size": "5"}, {"price": "0.45", "size": "10"}],
            "asks": [{"price": "0.60", "size": "2"}, {"price": "0.55", "size": "8"}],
        }
        metrics = poly.book_metrics(book)
        self.assertAlmostEqual(metrics["best_bid"], 0.45)
        self.assertAlmostEqual(metrics["best_ask"], 0.55)
        self.assertAlmostEqual(metrics["spread"], 0.10)
        self.assertAlmostEqual(metrics["bid_depth_5"], 15.0)
        self.assertAlmostEqual(metrics["ask_depth_5"], 10.0)
        self.assertAlmostEqual(metrics["depth_imbalance"], 0.2)


class KalshiAdapterTests(unittest.TestCase):
    def test_current_series_is_btc_15m_and_context_only(self):
        self.assertEqual(kalshi.SERIES_TICKER, "KXBTC15M")
        market = kalshi.normalize_market({
            "ticker": "KXBTC15M-26AUG122345",
            "event_ticker": "KXBTC15M-26AUG122345",
            "title": "BTC 15 min · target",
            "status": "open",
            "yes_bid_dollars": "0.44",
            "yes_ask_dollars": "0.48",
            "no_bid_dollars": "0.52",
            "no_ask_dollars": "0.56",
            "last_price_dollars": "0.46",
            "volume_fp": "1234.00",
            "open_interest_fp": "456.00",
        }, {"exchange_active": True, "trading_active": True})
        self.assertAlmostEqual(market["yes_probability"], 0.46)
        self.assertAlmostEqual(market["no_probability"], 0.54)
        self.assertFalse(market["directional_use"])
        self.assertEqual(market["horizon"], "15m")
        self.assertTrue(market["exchange_active"])


class BorosAdapterTests(unittest.TestCase):
    def test_current_public_schema_is_normalized(self):
        result = boros.normalize_boros_market({
            "marketId": 24,
            "imData": {"name": "BTC Funding", "symbol": "BTC", "maturity": 1_800_000_000},
            "metadata": {
                "underlyingSymbol": "BTC",
                "fundingRateSymbol": "BTCUSDT",
                "maxLeverage": 10,
                "isUiWhitelisted": True,
            },
            "extConfig": {"paymentPeriod": 28_800},
            "data": {
                "midApr": 0.12,
                "markApr": 0.11,
                "volume24h": 250000,
                "notionalOI": 1000000,
                "assetMarkPrice": 64000,
            },
        })
        self.assertEqual(result["market_id"], 24)
        self.assertEqual(result["underlying_symbol"], "BTC")
        self.assertEqual(result["funding_rate_symbol"], "BTCUSDT")
        self.assertAlmostEqual(result["mid_apr"], 0.12)
        self.assertAlmostEqual(result["asset_mark_price"], 64000)


class RealMarketCoreTests(unittest.TestCase):
    @staticmethod
    def _market(poly_ctx):
        candles = []
        for i in range(16):
            candles.append([i, 100.1, 100.2, 99.8, 100.0, 1000.0])
        return {
            "symbol": "BTC/USDT",
            "timeframe": "15m",
            "ohlcv": candles,
            "ticker": {"bid": 100.0, "ask": 100.01, "spread_pct": 0.0001, "spread_bps": 1.0},
            "orderbook": {"bid_depth": 100.0, "ask_depth": 100.0},
            "funding": {"rate": 0.0},
            "open_interest": {"oi_change_24h_pct": 0.0},
            "liquidity_quality": 0.99,
            "polymarket_context": poly_ctx,
        }

    def test_polymarket_pressure_is_fixed_bounded_and_audited(self):
        core = real_core.SingleDecisionCore()
        market_state = core.ingest_market(self._market({
            "eligible_for_prediction": True,
            "status": "LIVE_WS",
            "slug": "btc-updown-5m-1785033600",
            "up_probability": 0.80,
            "down_probability": 0.20,
            "directional_pressure": 0.80,
            "seconds_to_close": 120,
            "freshness_s": 1.0,
            "ws_connected": True,
        }))
        features = core.compress_features(market_state)
        self.assertAlmostEqual(features["pressures"]["polymarket"], 0.20, places=6)
        self.assertAlmostEqual(
            features["total_pressure"] - features["base_total_pressure"],
            0.20,
            places=6,
        )
        ctx = features["polymarket_context_v1"]
        self.assertTrue(ctx["eligible"])
        self.assertEqual(ctx["fixed_weight"], 0.25)

    def test_stale_polymarket_context_has_zero_effect(self):
        core = real_core.SingleDecisionCore()
        market_state = core.ingest_market(self._market({
            "eligible_for_prediction": False,
            "status": "STALE",
            "directional_pressure": 1.0,
        }))
        features = core.compress_features(market_state)
        self.assertEqual(features["pressures"]["polymarket"], 0.0)
        self.assertAlmostEqual(features["total_pressure"], features["base_total_pressure"], places=6)


class RuntimeWiringTests(unittest.TestCase):
    def test_runtime_predictor_attaches_three_real_sources(self):
        seen = {}

        def fake_base(market):
            seen.update(market)
            return {
                "prediction": "LONG",
                "_audit": {"pipeline": {"step2_features": {"up_prob": 0.61}}},
            }

        poly_ctx = {
            "source": "POLYMARKET_PUBLIC",
            "status": "LIVE_WS",
            "eligible_for_prediction": True,
            "up_probability": 0.55,
            "down_probability": 0.45,
            "directional_pressure": 0.1,
        }
        kalshi_ctx = {
            "source": "KALSHI_PUBLIC_REST",
            "status": "LIVE",
            "directional_use": False,
            "market": {"yes_probability": 0.58},
        }
        boros_ctx = {
            "source": "BOROS_PUBLIC_API",
            "status": "LIVE",
            "directional_use": False,
            "markets": [],
        }
        with mock.patch.object(runtime_predictor, "_poly_snapshot_for_prediction", return_value=poly_ctx), \
             mock.patch.object(runtime_predictor, "_kalshi_snapshot_for_audit", return_value=kalshi_ctx), \
             mock.patch.object(runtime_predictor, "_boros_snapshot_for_audit", return_value=boros_ctx), \
             mock.patch.object(runtime_predictor._base, "run_prediction", side_effect=fake_base):
            result = runtime_predictor.run_prediction({"symbol": "BTC/USDT"})

        self.assertIs(seen["polymarket_context"], poly_ctx)
        self.assertFalse(seen["kalshi_context"]["directional_use"])
        self.assertFalse(seen["boros_context"]["directional_use"])
        ext = result["_audit"]["external_markets_v1"]
        self.assertIn("polymarket", ext)
        self.assertIn("kalshi", ext)
        self.assertIn("boros", ext)
        self.assertTrue(ext["kalshi_cross_venue_v1"]["horizon_mismatch"])

    def test_production_launcher_targets_real_runtime_and_synthetic_default_is_off(self):
        root = Path(__file__).resolve().parents[1]
        launcher = (root / "start_single_authority.sh").read_text(encoding="utf-8")
        self.assertIn("uvicorn backend.main_real:app", launcher)
        self.assertNotIn("uvicorn backend.main:app", launcher)

        from backend import main_real
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SENEX_ENABLE_SYNTHETIC_DEMO", None)
            self.assertFalse(main_real.synthetic_demo_enabled())

    def test_real_adapters_have_no_trading_credentials_or_order_routes(self):
        root = Path(__file__).resolve().parents[1] / "backend"
        for name in ("polymarket_market_adapter.py", "kalshi_market_adapter.py", "boros_market_adapter.py"):
            text = (root / name).read_text(encoding="utf-8").lower()
            self.assertNotIn("private_key", text)
            self.assertNotIn("kalshi-access-key", text)
            self.assertNotIn("portfolio/orders", text)
            self.assertNotIn("place-orders", text)
            self.assertNotIn("send-txs", text)


if __name__ == "__main__":
    unittest.main()
