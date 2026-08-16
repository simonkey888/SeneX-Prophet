from __future__ import annotations

import asyncio
import copy
import re
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend import main, main_real, oracle_runner, supabase_client
from backend.authoritative_score import build_authoritative_score
from backend.portfolio.live_gate import GateStatus
from tests.test_aud_059 import proof_row


class _Response:
    status_code = 200

    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return copy.deepcopy(self._rows)


class _KeysetClient:
    def __init__(self, rows):
        self.rows = sorted(
            copy.deepcopy(rows),
            key=lambda row: (str(row.get("ts") or ""), int(row.get("id") or 0)),
        )
        self.calls = []

    async def get(self, path, params=None, headers=None):
        params = dict(params or {})
        self.calls.append(params)
        rows = self.rows
        symbol_filter = params.get("symbol")
        if symbol_filter and str(symbol_filter).startswith("eq."):
            symbol = str(symbol_filter)[3:]
            rows = [
                row for row in rows
                if str(row.get("symbol") or "").upper().replace("/", "").replace("-", "") == symbol
            ]
        cursor = params.get("or")
        if cursor:
            match = re.match(
                r"^\(ts\.gt\.(.*),and\(ts\.eq\.(.*),id\.gt\.([^)]*)\)\)$",
                str(cursor),
            )
            if not match or match.group(1) != match.group(2):
                raise AssertionError(f"bad cursor {cursor}")
            ts, _, raw_id = match.groups()
            cursor_id = int(raw_id)
            rows = [
                row for row in rows
                if str(row["ts"]) > ts
                or (str(row["ts"]) == ts and int(row["id"]) > cursor_id)
            ]
        return _Response(rows[: int(params.get("limit", 50))])


class _GateCoordinator:
    def __init__(self):
        self.score = None

    def evaluate_live_gate(self, oracle_score=None):
        self.score = oracle_score
        n = int((oracle_score or {}).get("independent_1h_rows") or 0)
        return GateStatus(
            unlocked=True,
            trade_mode="LIVE",
            live_capital_locked=False,
            conditions={
                "verified": {
                    "value": n,
                    "threshold": 300,
                    "op": ">=",
                    "pass": n >= 300,
                }
            },
        )


def _rows(n, *, symbol="BTCUSDT"):
    return [
        proof_row(
            i + 1,
            i * 15,
            symbol=symbol,
            outcome="WIN" if i % 3 else "LOSS",
        )
        for i in range(n)
    ]


class AuthorityHistoryBoundaryTests(unittest.TestCase):
    def test_same_symbol_499_500_501_and_620_are_complete(self):
        for n in (499, 500, 501, 620):
            rows = _rows(n)
            client = _KeysetClient(rows)
            with patch.object(supabase_client, "_get_client", return_value=client):
                fetched = asyncio.run(
                    supabase_client.fetch_authority_history("BTCUSDT", page_size=73)
                )
            self.assertEqual(len(fetched), n, n)
            self.assertEqual([row["id"] for row in fetched], [row["id"] for row in rows], n)

    def test_501_reproduces_old_loss_and_complete_history_repairs_it(self):
        rows = _rows(501)
        old = build_authoritative_score(rows[-500:], symbol="BTCUSDT")
        complete = build_authoritative_score(rows, symbol="BTCUSDT")
        self.assertEqual(old["input_rows"], 500)
        self.assertEqual(complete["input_rows"], 501)
        self.assertEqual(old["independent_1h_rows"], 125)
        self.assertEqual(complete["independent_1h_rows"], 126)

    def test_transport_page_size_invariance(self):
        rows = _rows(620)
        scores = []
        for page_size in (17, 73, 250, 500):
            client = _KeysetClient(rows)
            with patch.object(supabase_client, "_get_client", return_value=client):
                fetched = asyncio.run(
                    supabase_client.fetch_authority_history("BTC/USDT", page_size=page_size)
                )
            scores.append(build_authoritative_score(fetched, symbol="BTCUSDT"))
        keys = (
            "input_rows",
            "total_predictions",
            "proof_qualified_rows_raw",
            "independent_1h_rows",
            "wins",
            "losses",
            "gates",
            "quality",
            "reasons",
            "authority_1h",
        )
        for key in keys:
            self.assertTrue(all(score[key] == scores[0][key] for score in scores[1:]), key)

    def test_page_cap_fails_closed_instead_of_truncating(self):
        rows = _rows(620)
        client = _KeysetClient(rows)
        with patch.object(supabase_client, "_get_client", return_value=client):
            with self.assertRaises(supabase_client.AuthorityHistoryIncompleteError):
                asyncio.run(
                    supabase_client.fetch_authority_history(
                        "BTCUSDT", page_size=100, max_pages=5
                    )
                )


class AuthoritySurfaceReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.saved = copy.deepcopy(oracle_runner._state)

    def tearDown(self):
        oracle_runner._state.clear()
        oracle_runner._state.update(self.saved)

    def test_complete_history_oracle_score_matches_direct_builder_over_500(self):
        rows = _rows(620)
        expected = build_authoritative_score(rows, symbol="BTCUSDT")
        with patch.object(
            supabase_client,
            "fetch_authority_history",
            new=AsyncMock(return_value=rows),
        ) as fetch:
            actual = asyncio.run(main_real.authoritative_oracle_score(symbol="BTCUSDT"))
        fetch.assert_awaited_once_with(symbol="BTCUSDT")
        for key in (
            "input_rows",
            "total_predictions",
            "proof_qualified_rows_raw",
            "independent_1h_rows",
            "wins",
            "losses",
            "gates",
            "quality",
            "reasons",
            "authority_1h",
        ):
            self.assertEqual(actual[key], expected[key], key)
        self.assertTrue(actual["authority_history_complete"])
        self.assertEqual(actual["authority_history_rows"], 620)

    def test_runtime_per_symbol_authority_reconciles_with_canonical_score(self):
        btc = _rows(620, symbol="BTCUSDT")
        eth = _rows(605, symbol="ETHUSDT")

        async def complete(symbol=None, **kwargs):
            if symbol == "BTCUSDT":
                return btc
            if symbol == "ETHUSDT":
                return eth
            return btc + eth

        with (
            patch.object(supabase_client, "fetch_authority_history", new=complete),
            patch.object(
                supabase_client,
                "fetch_predictions",
                new=AsyncMock(return_value=(btc + eth)[-500:]),
            ),
        ):
            asyncio.run(oracle_runner._refresh_directional_stats())
        per_symbol = oracle_runner._state["directional_stats"]["per_symbol"]
        for symbol, rows in (("BTCUSDT", btc), ("ETHUSDT", eth)):
            expected = build_authoritative_score(rows, symbol=symbol)
            self.assertEqual(per_symbol[symbol]["authority_1h"], expected["authority_1h"])
            self.assertEqual(per_symbol[symbol]["gates"], expected["gates"])
            self.assertEqual(
                per_symbol[symbol]["independent_1h_rows"],
                expected["independent_1h_rows"],
            )
            self.assertTrue(per_symbol[symbol]["authority_history_complete"])
            self.assertEqual(per_symbol[symbol]["authority_history_rows"], len(rows))

    def test_portfolio_and_research_adapter_uses_complete_history_and_paper_lock(self):
        rows = _rows(620)
        coord = _GateCoordinator()
        with patch.object(
            supabase_client,
            "fetch_authority_history",
            new=AsyncMock(return_value=rows),
        ):
            state = asyncio.run(
                main._symbol_scoped_paper_live_gate_state(coord, symbol="BTC/USDT")
            )
        expected = build_authoritative_score(rows, symbol="BTCUSDT")
        self.assertEqual(coord.score["authority_1h"], expected["authority_1h"])
        self.assertEqual(state["verified"], expected["independent_1h_rows"])
        self.assertTrue(state["authority_history_complete"])
        self.assertEqual(state["authority_history_rows"], 620)
        self.assertFalse(state["unlocked"])
        self.assertEqual(state["trade_mode"], "PAPER")
        self.assertTrue(state["live_capital_locked"])

    def test_incomplete_live_gate_history_is_explicitly_fail_closed(self):
        coord = _GateCoordinator()
        with patch.object(
            supabase_client,
            "fetch_authority_history",
            new=AsyncMock(
                side_effect=supabase_client.AuthorityHistoryIncompleteError("test")
            ),
        ):
            state = asyncio.run(
                main._symbol_scoped_paper_live_gate_state(coord, symbol="BTCUSDT")
            )
        self.assertFalse(state["authority_history_complete"])
        self.assertEqual(state["effective_gate"], "LOCKED_BY_INCOMPLETE_AUTHORITY_HISTORY")
        self.assertIn("AUTHORITY_HISTORY_INCOMPLETE", state["failed_reasons"])
        self.assertFalse(state["unlocked"])
        self.assertEqual(state["verified"], 0)


class PreservationTests(unittest.TestCase):
    def test_dashboard_distinguishes_raw_and_authority_n(self):
        root = Path(__file__).resolve().parents[1]
        truth = (root / "frontend" / "dashboard_truth.js").read_text()
        app = (root / "frontend" / "app.js").read_text()
        self.assertIn(
            "proofQualifiedRaw: display(source.proof_qualified_rows_raw)", truth
        )
        self.assertIn(
            "independent1h: display(source.independent_1h_rows)", truth
        )
        self.assertIn(
            "$('#score-proof-raw').textContent = view.proofQualifiedRaw", app
        )
        self.assertIn(
            "$('#score-independent').textContent = view.independent1h", app
        )

    def test_paper_locks_remain_closed(self):
        score = build_authoritative_score(_rows(620), symbol="BTCUSDT")
        self.assertEqual(score["trade_mode"], "PAPER")
        self.assertFalse(score["orders_enabled"])
        self.assertTrue(score["live_capital_locked"])

    def test_no_authority_semantic_constants_changed_by_this_order(self):
        root = Path(__file__).resolve().parents[2]
        text = (root / "senecio_polymarket" / "backend" / "authoritative_score.py").read_text()
        self.assertIn("MIN_GLOBAL_N = 100", text)
        self.assertIn("MIN_DIRECTION_N = 30", text)
        self.assertIn("INDEPENDENT_HORIZON_S = 3600.0", text)
