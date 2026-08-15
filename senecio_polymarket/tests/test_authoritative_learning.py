import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from oracle_runtime import institutional_core as learning
from oracle_runtime import institutional_core_real as real_learning
from oracle_runtime import predict_only as runtime_predictor


from tests.aud063_fixture_support import upgrade_proof_row

def _aud063_upgrade(fn):
    def wrapped(*args, **kwargs):
        return upgrade_proof_row(fn(*args, **kwargs))
    return wrapped

BASE_TS = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)


@_aud063_upgrade
def _row(idx: int, *, outcome: str = "LOSS", symbol: str = "BTCUSDT", valid: bool = True):
    ts = (BASE_TS + timedelta(minutes=idx * 61)).isoformat()
    settled_at = (BASE_TS + timedelta(minutes=idx * 61 + 60)).isoformat()
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
            "settled_at": settled_at,
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
        self.assertEqual(state["authority_cohort"], "INDEPENDENT_NONOVERLAP_1H")
        for key in ("source_evidence_hash", "effective_weights_hash", "code_hash", "config_hash"):
            self.assertEqual(len(state[key]), 64, key)
        self.assertNotIn("sb_secret_test", repr(state))

    def test_runtime_predictor_forces_real_learning_core_for_base_import(self):
        previous = sys.modules.get("institutional_core")
        observed = {}

        def fake_base_run_prediction(_market):
            observed["core_module"] = sys.modules.get("institutional_core")
            return {"prediction": "FLAT"}

        with mock.patch.object(runtime_predictor._base, "run_prediction", side_effect=fake_base_run_prediction):
            result = runtime_predictor.run_prediction({"symbol": "BTC/USDT"})

        self.assertEqual(result["prediction"], "FLAT")
        self.assertIn("external_markets_v1", result["_audit"])
        self.assertIs(observed["core_module"], real_learning)
        self.assertIs(sys.modules.get("institutional_core"), previous)

    def test_dockerfile_installs_bridge_at_historical_predictor_path(self):
        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("mv /app/oracle/predict_only.py /app/oracle/predict_only_base.py", dockerfile)
        self.assertIn("cp /app/oracle_runtime/predict_only.py /app/oracle/predict_only.py", dockerfile)


if __name__ == "__main__":
    unittest.main()
