import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from oracle_runtime import institutional_core as learning


BASE_TS = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)


def _row(idx: int, *, outcome: str = "LOSS", symbol: str = "BTCUSDT", valid: bool = True):
    ts = (BASE_TS + timedelta(minutes=idx)).isoformat()
    direction = "LONG"
    origin = 100.0
    later = 101.0 if outcome == "WIN" else 99.0
    audit = {
        "origin_price_v1": {
            "version": "origin-price-v1",
            "price": origin,
            "timestamp": ts,
            "source": "okx",
        },
        "outcomes_dual": {
            "outcome_15m": outcome,
            "outcome_1h": outcome,
            "price_15m_later": later,
            "price_1h_later": later,
            "primary_window": "1h",
        },
        "pipeline": {
            "step2_features": {
                "conviction": 0.60,
                "regime_4h": "NEUTRAL",
                "pressures": {
                    "orderflow": 0.40,
                    "volume_delta": 0.10,
                    "bidask": 0.20,
                    "funding": 0.10,
                    "oi": 0.10,
                    "price_momentum": 0.10,
                },
            }
        },
    }
    if not valid:
        audit.pop("outcomes_dual")
    return {
        "id": idx,
        "ts": ts,
        "symbol": symbol,
        "prediction": direction,
        "confidence": 0.60,
        "price_now": origin,
        "outcome": outcome,
        "audit": audit,
    }


def _core():
    return learning.SingleDecisionCore(
        max_drawdown=0.12,
        ruin_probability_threshold=0.05,
        hard_stop=True,
        max_position_pct=0.25,
        max_leverage=1,
        min_confidence=0.40,
        min_ev_to_trade=0.001,
        no_trade_noise=0.60,
        initial_capital=1000.0,
    )


class AuthoritativeLearningTests(unittest.TestCase):
    def test_warmup_does_not_mutate_weights(self):
        core = _core()
        before = dict(core.weights)
        state = learning.replay_authoritative_learning(
            core, [_row(i) for i in range(1, 10)], "BTCUSDT"
        )
        self.assertEqual(state["status"], "WARMUP")
        self.assertEqual(state["proof_qualified_n"], 9)
        self.assertEqual(core.weights, before)

    def test_raw_unverified_win_is_rejected(self):
        core = _core()
        before = dict(core.weights)
        rows = [_row(i) for i in range(1, 10)] + [_row(10, outcome="WIN", valid=False)]
        state = learning.replay_authoritative_learning(core, rows, "BTCUSDT")
        self.assertEqual(state["proof_qualified_n"], 9)
        self.assertEqual(state["status"], "WARMUP")
        self.assertEqual(core.weights, before)

    def test_losses_penalize_all_mapped_agreeing_pressures_with_drift_bound(self):
        core = _core()
        before = dict(core.weights)
        state = learning.replay_authoritative_learning(
            core, [_row(i) for i in range(1, 11)], "BTCUSDT"
        )
        self.assertEqual(state["status"], "ACTIVE")
        self.assertEqual(state["proof_qualified_n"], 10)
        self.assertGreater(state["mutations"], 0)
        for name in (
            "orderflow",
            "volume_delta",
            "bidask_imbalance",
            "funding_signal",
            "oi_momentum",
            "price_momentum",
        ):
            self.assertLess(core.weights[name], before[name], name)
            self.assertGreaterEqual(core.weights[name], before[name] * 0.75, name)

    def test_replay_is_deterministic(self):
        rows = [_row(i, outcome="WIN" if i % 3 == 0 else "LOSS") for i in range(1, 31)]
        a = _core()
        b = _core()
        state_a = learning.replay_authoritative_learning(a, rows, "BTCUSDT")
        state_b = learning.replay_authoritative_learning(b, rows, "BTCUSDT")
        self.assertEqual(a.weights, b.weights)
        self.assertEqual(state_a["effective_weights"], state_b["effective_weights"])
        self.assertEqual(state_a["source_prediction_ids"], state_b["source_prediction_ids"])

    def test_learning_state_is_attached_to_prediction_pipeline(self):
        rows = [_row(i) for i in range(1, 11)]
        core = _core()
        candles = []
        for i in range(100):
            p = 100.0 + i * 0.01
            candles.append([i * 900_000, p, p + 0.1, p - 0.1, p + 0.02, 1000.0 + i])
        market = {
            "symbol": "BTC/USDT",
            "timeframe": "15m",
            "ohlcv": candles,
            "ticker": {"bid": 101.0, "ask": 101.01, "spread_pct": 0.0001, "spread_bps": 1.0},
            "orderbook": {"bid_depth": 120.0, "ask_depth": 100.0},
            "funding": {"rate": 0.0},
            "open_interest": {"oi_change_24h_pct": 0.0},
            "liquidity_quality": 0.99,
        }
        risk = {"drawdown": 0.0, "var": 0.0, "loss_streak": 0, "capital": 1000.0}
        execution = {"liquidity_quality": 0.99, "slippage_bps": 1.0, "latency_ms": 150.0, "spread_bps": 1.0}

        with mock.patch.dict(os.environ, {"SUPABASE_URL": "https://example.invalid", "SUPABASE_KEY": "sb_secret_test"}, clear=False):
            with mock.patch.object(learning, "fetch_authoritative_rows", return_value=rows):
                decision = core.decide(market, risk, execution)

        state = decision["pipeline"]["step2_features"]["learning_state_v1"]
        self.assertEqual(state["status"], "ACTIVE")
        self.assertEqual(state["proof_qualified_n"], 10)
        self.assertNotIn("sb_secret_test", repr(state))


if __name__ == "__main__":
    unittest.main()
