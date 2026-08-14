from __future__ import annotations

import asyncio
import copy
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend import oracle_runner, supabase_client
from backend.authoritative_score import build_authoritative_score

BASE_TS = datetime(2026, 8, 2, 0, 0, tzinfo=timezone.utc)


def _proof_row(idx: int, symbol: str, direction: str, outcome: str, *, confidence: float = 0.60) -> dict:
    ts = (BASE_TS + timedelta(minutes=idx)).isoformat()
    origin = 100.0
    if direction == "LONG":
        later = 101.0 if outcome == "WIN" else 99.0
    elif direction == "SHORT":
        later = 99.0 if outcome == "WIN" else 101.0
    else:
        raise ValueError(direction)
    return {
        "id": idx,
        "ts": ts,
        "symbol": symbol,
        "prediction": direction,
        "confidence": confidence,
        "price_now": origin,
        "outcome": outcome,
        "audit": {
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
        },
    }


def _cohort(symbol: str, long_outcomes: list[str], short_outcomes: list[str], start: int) -> list[dict]:
    rows: list[dict] = []
    idx = start
    for outcome in long_outcomes:
        rows.append(_proof_row(idx, symbol, "LONG", outcome))
        idx += 1
    for outcome in short_outcomes:
        rows.append(_proof_row(idx, symbol, "SHORT", outcome))
        idx += 1
    return rows


BTC5 = _cohort("BTCUSDT", ["WIN", "LOSS", "LOSS"], ["WIN", "LOSS"], 1)
ETH11 = _cohort(
    "ETHUSDT",
    ["WIN", "WIN", "LOSS", "LOSS", "LOSS"],
    ["WIN", "WIN", "WIN", "LOSS", "LOSS", "LOSS"],
    100,
)


class ScoreByWindowIsolationTests(unittest.TestCase):
    def test_btc5_eth11_returns_btc_only_by_window(self):
        poisoned_runner = {
            "directional_stats": {
                "aggregate_diagnostic": {"by_window": {"1h": {"global": {"verified": 999}}}}
            }
        }
        score = build_authoritative_score(BTC5 + ETH11, poisoned_runner, symbol="BTCUSDT")
        self.assertEqual(score["proof_qualified_rows"], 5)
        self.assertEqual(score["by_window"]["1h"]["global"]["verified"], 5)
        self.assertEqual(score["by_window"]["1h"]["LONG"]["verified"], 3)
        self.assertEqual(score["by_window"]["1h"]["SHORT"]["verified"], 2)
        self.assertEqual(score["by_window"]["15m"]["global"]["verified"], 5)

    def test_eth_rows_cannot_modify_btc_score_or_by_window(self):
        solo = build_authoritative_score(BTC5, symbol="BTCUSDT")
        mixed = build_authoritative_score(BTC5 + ETH11, symbol="BTCUSDT")
        for key in (
            "score_status",
            "authoritative_score_pct",
            "observed_win_rate_pct",
            "proof_qualified_rows",
            "verified",
            "wins",
            "losses",
            "posterior_accuracy",
            "by_direction",
            "by_window",
            "gates",
            "quality",
            "reasons",
            "short_only_paper_mode",
        ):
            self.assertEqual(solo[key], mixed[key], key)


class RuntimeDirectionalIsolationTests(unittest.TestCase):
    def setUp(self):
        self._saved_state = copy.deepcopy(oracle_runner._state)

    def tearDown(self):
        oracle_runner._state.clear()
        oracle_runner._state.update(self._saved_state)

    def _refresh(self, rows: list[dict]) -> dict:
        with patch.object(supabase_client, "fetch_predictions", new=AsyncMock(return_value=rows)):
            asyncio.run(oracle_runner._refresh_directional_stats())
        return oracle_runner._state["directional_stats"]

    def test_directional_stats_are_partitioned_by_symbol(self):
        stats = self._refresh(BTC5 + ETH11)
        self.assertNotIn("by_window", stats)
        self.assertEqual(stats["per_symbol"]["BTCUSDT"]["by_window"]["1h"]["global"]["verified"], 5)
        self.assertEqual(stats["per_symbol"]["ETHUSDT"]["by_window"]["1h"]["global"]["verified"], 11)
        self.assertEqual(stats["aggregate_diagnostic"]["by_window"]["1h"]["global"]["verified"], 16)

    def test_eth_raw_overlap_cannot_trigger_control_short_only(self):
        btc = BTC5
        eth = _cohort("ETHUSDT", ["LOSS"] * 30, ["WIN"] * 30, 500)
        stats = self._refresh(btc + eth)
        self.assertFalse(stats["per_symbol"]["BTCUSDT"]["short_only_paper_mode"])
        self.assertFalse(stats["per_symbol"]["ETHUSDT"]["short_only_paper_mode"])
        self.assertEqual(
            stats["per_symbol"]["ETHUSDT"]["by_window"]["1h"]["SHORT"]["verified"],
            30,
        )
        self.assertEqual(
            stats["per_symbol"]["ETHUSDT"]["authority_1h"]["SHORT"]["verified"],
            0,
        )
        self.assertFalse(stats["per_symbol"]["BTCUSDT"]["gates"]["short_1h"]["pass"])
        self.assertFalse(stats["per_symbol"]["ETHUSDT"]["gates"]["short_1h"]["pass"])


class _ConfigSink:
    def __init__(self):
        self.calls: list[dict] = []

    def update_config(self, **kwargs):
        self.calls.append(dict(kwargs))


class _FakeCoordinator:
    def __init__(self):
        self.portfolio_engine = _ConfigSink()
        self.risk_kernel = _ConfigSink()
        self.execution_engine = _ConfigSink()
        self.ingested: list[dict] = []

    async def ingest_prediction(self, **kwargs):
        self.ingested.append(dict(kwargs))
        return {"skipped": "fixture", "reason": "AUD-057"}


class PortfolioSymbolIsolationTests(unittest.TestCase):
    def setUp(self):
        self._saved_state = copy.deepcopy(oracle_runner._state)
        oracle_runner._state["trade_mode"] = "PAPER"
        oracle_runner._state["live_capital_locked"] = True
        oracle_runner._state["directional_stats"] = {
            "per_symbol": {
                "BTCUSDT": {
                    "by_window": {"1h": {"LONG": {"win_rate_pct": 33.33}, "SHORT": {"win_rate_pct": 50.0}}},
                    "authority_1h": {"LONG": {"win_rate_pct": 33.33}, "SHORT": {"win_rate_pct": 50.0}},
                    "short_only_paper_mode": False,
                },
                "ETHUSDT": {
                    "by_window": {"1h": {"LONG": {"win_rate_pct": 40.0}, "SHORT": {"win_rate_pct": 70.0}}},
                    "authority_1h": {"LONG": {"win_rate_pct": 40.0}, "SHORT": {"win_rate_pct": 70.0}},
                    "short_only_paper_mode": True,
                },
            },
            "aggregate_diagnostic": {
                "by_window": {"1h": {"LONG": {"win_rate_pct": 99.0}, "SHORT": {"win_rate_pct": 99.0}}}
            },
        }

    def tearDown(self):
        oracle_runner._state.clear()
        oracle_runner._state.update(self._saved_state)

    def _route(self, symbol: str) -> _FakeCoordinator:
        coord = _FakeCoordinator()
        with patch.object(oracle_runner, "_get_portfolio_coordinator", return_value=coord):
            asyncio.run(oracle_runner._route_to_portfolio({"symbol": symbol, "price_now": 100.0}, {}))
        return coord

    def test_btc_portfolio_uses_only_btc_stats_and_remains_paper_locked(self):
        coord = self._route("BTCUSDT")
        self.assertEqual(coord.ingested[0]["win_rate_by_direction"], {"LONG": 0.3333, "SHORT": 0.5})
        self.assertFalse(coord.portfolio_engine.calls[-1]["short_only_paper_mode"])
        self.assertEqual(coord.risk_kernel.calls[-1]["trade_mode"], "PAPER")
        self.assertTrue(coord.risk_kernel.calls[-1]["live_capital_locked"])
        self.assertEqual(coord.execution_engine.calls[-1], {"trade_mode": "PAPER", "allow_live": False})

    def test_eth_portfolio_uses_only_eth_stats(self):
        coord = self._route("ETHUSDT")
        self.assertEqual(coord.ingested[0]["win_rate_by_direction"], {"LONG": 0.4, "SHORT": 0.7})
        self.assertTrue(coord.portfolio_engine.calls[-1]["short_only_paper_mode"])


class ContractPreservationTests(unittest.TestCase):
    def test_learning_per_symbol_guards_remain_in_source(self):
        root = Path(__file__).resolve().parents[1]
        learning = (root / "oracle_runtime" / "institutional_core.py").read_text(encoding="utf-8")
        self.assertIn('"symbol": f"eq.{normalized}"', learning)
        self.assertIn('_normalize_symbol(str(row.get("symbol") or "")) == normalized', learning)
        self.assertIn('and _proof_gate(row)', learning)

    def test_semgrep_workflow_remains_absent(self):
        repo_root = Path(__file__).resolve().parents[2]
        self.assertFalse((repo_root / ".github" / "workflows" / "semgrep.yml").exists())

    def test_global_paper_lock_defaults_remain_closed(self):
        self.assertEqual(oracle_runner._state["trade_mode"], "PAPER")
        self.assertTrue(oracle_runner._state["live_capital_locked"])


if __name__ == "__main__":
    unittest.main()
