from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"replacement count {count} for {path}: {old[:120]!r}")
    p.write_text(text.replace(old, new, 1))


supa = Path("senecio_polymarket/backend/supabase_client.py")
text = supa.read_text()
anchor = "\n\nasync def count_predictions() -> int:\n"
if text.count(anchor) != 1:
    raise SystemExit("supabase insertion anchor mismatch")
addition = r'''

AUTHORITY_HISTORY_PAGE_SIZE_MAX = 500
AUTHORITY_HISTORY_MAX_PAGES = 10_000


class AuthorityHistoryIncompleteError(RuntimeError):
    """Raised when complete authority history cannot be proven retrieved."""


async def fetch_authority_history(
    symbol: Optional[str] = None,
    *,
    page_size: int = 250,
    max_pages: int = AUTHORITY_HISTORY_MAX_PAGES,
) -> list[dict]:
    """Fetch complete history with deterministic keyset pagination.

    Authority/control callers must use this instead of a newest-N query. Each
    request page is bounded, but the semantic result is the complete visible
    dataset. Any HTTP, response-shape, cursor, or page-cap failure raises rather
    than silently returning a truncated authority cohort.
    """
    normalized_symbol = (
        str(symbol).upper().replace("/", "").replace("-", "").strip()
        if symbol else None
    )
    bounded_page_size = max(1, min(int(page_size), AUTHORITY_HISTORY_PAGE_SIZE_MAX))
    bounded_max_pages = max(1, int(max_pages))
    c = _get_client()
    collected: list[dict] = []
    cursor: tuple[str, str] | None = None
    seen: set[tuple[str, str]] = set()

    for _ in range(bounded_max_pages):
        params = {
            "limit": str(bounded_page_size),
            "order": "ts.asc,id.asc",
        }
        if normalized_symbol:
            params["symbol"] = f"eq.{normalized_symbol}"
        if cursor is not None:
            cursor_ts, cursor_id = cursor
            params["or"] = f"(ts.gt.{cursor_ts},and(ts.eq.{cursor_ts},id.gt.{cursor_id}))"

        try:
            r = await c.get(f"/{SUPABASE_TABLE}", params=params)
        except Exception as exc:
            raise AuthorityHistoryIncompleteError(
                f"AUTHORITY_HISTORY_REQUEST_ERROR:{type(exc).__name__}"
            ) from exc
        if r.status_code != 200:
            raise AuthorityHistoryIncompleteError(
                f"AUTHORITY_HISTORY_HTTP_{r.status_code}"
            )
        page = r.json()
        if not isinstance(page, list):
            raise AuthorityHistoryIncompleteError("AUTHORITY_HISTORY_RESPONSE_NOT_LIST")

        for row in page:
            if not isinstance(row, dict):
                raise AuthorityHistoryIncompleteError("AUTHORITY_HISTORY_ROW_NOT_OBJECT")
            ts = str(row.get("ts") or "")
            row_id = str(row.get("id") or "")
            if not ts or not row_id:
                raise AuthorityHistoryIncompleteError("AUTHORITY_HISTORY_CURSOR_FIELD_MISSING")
            key = (ts, row_id)
            if key in seen:
                raise AuthorityHistoryIncompleteError("AUTHORITY_HISTORY_DUPLICATE_CURSOR")
            seen.add(key)
            collected.append(row)

        if len(page) < bounded_page_size:
            return collected
        last = page[-1]
        next_cursor = (str(last.get("ts") or ""), str(last.get("id") or ""))
        if not all(next_cursor) or next_cursor == cursor:
            raise AuthorityHistoryIncompleteError("AUTHORITY_HISTORY_CURSOR_STALLED")
        cursor = next_cursor

    raise AuthorityHistoryIncompleteError(
        f"AUTHORITY_HISTORY_PAGE_CAP_HIT:{bounded_max_pages}"
    )
'''
supa.write_text(text.replace(anchor, addition + anchor, 1))

replace_once(
    "senecio_polymarket/backend/main_real.py",
    "    rows = await supabase_client.fetch_predictions(limit=500, symbol=normalized_symbol)\n    return build_authoritative_score(\n        rows,\n        oracle_runner.get_state(),\n        symbol=normalized_symbol,\n    )\n",
    "    rows = await supabase_client.fetch_authority_history(symbol=normalized_symbol)\n    score = build_authoritative_score(\n        rows,\n        oracle_runner.get_state(),\n        symbol=normalized_symbol,\n    )\n    score[\"authority_history_complete\"] = True\n    score[\"authority_history_rows\"] = len(rows)\n    return score\n",
)

replace_once(
    "senecio_polymarket/backend/oracle_runner.py",
    "    qualified_by_symbol: dict[str, list[dict[str, Any]]] = {}\n    for symbol in symbols:\n        try:\n            # PostgREST applies this equality filter before the bounded limit,\n            # preserving an independent evidence window for each instrument.\n            symbol_rows = await supabase_client.fetch_predictions(\n                limit=500,\n                symbol=symbol,\n            )\n        except Exception as e:\n            log.warning(\"directional stats fetch failed for %s: %s\", symbol, e)\n            symbol_rows = []\n        qualified_by_symbol[symbol] = [\n",
    "    qualified_by_symbol: dict[str, list[dict[str, Any]]] = {}\n    history_complete_by_symbol: dict[str, bool] = {}\n    history_rows_by_symbol: dict[str, int] = {}\n    for symbol in symbols:\n        try:\n            symbol_rows = await supabase_client.fetch_authority_history(symbol=symbol)\n            history_complete_by_symbol[symbol] = True\n        except Exception as e:\n            log.warning(\"directional authority history fetch failed for %s: %s\", symbol, e)\n            symbol_rows = []\n            history_complete_by_symbol[symbol] = False\n        history_rows_by_symbol[symbol] = len(symbol_rows)\n        qualified_by_symbol[symbol] = [\n",
)
replace_once(
    "senecio_polymarket/backend/oracle_runner.py",
    "            \"authority_cohort\": score[\"authority_cohort\"],\n        }\n",
    "            \"authority_cohort\": score[\"authority_cohort\"],\n            \"authority_history_complete\": history_complete_by_symbol.get(symbol, False),\n            \"authority_history_rows\": history_rows_by_symbol.get(symbol, 0),\n        }\n",
)

replace_once(
    "senecio_polymarket/backend/main.py",
    "    rows = []\n    if normalized_symbol:\n        try:\n            rows = await supabase_client.fetch_predictions(\n                limit=500,\n                symbol=normalized_symbol,\n            )\n        except Exception as e:\n            log.warning(\"symbol-scoped live-gate fetch failed for %s: %s\", normalized_symbol, e)\n    return _paper_locked_live_gate_state(\n        coord,\n        rows,\n        symbol=normalized_symbol,\n    )\n",
    "    rows = []\n    history_complete = False\n    if normalized_symbol:\n        try:\n            rows = await supabase_client.fetch_authority_history(symbol=normalized_symbol)\n            history_complete = True\n        except Exception as e:\n            log.warning(\"symbol-scoped complete authority fetch failed for %s: %s\", normalized_symbol, e)\n    state = _paper_locked_live_gate_state(\n        coord,\n        rows,\n        symbol=normalized_symbol,\n    )\n    state[\"authority_history_complete\"] = history_complete\n    state[\"authority_history_rows\"] = len(rows)\n    if not history_complete:\n        state[\"effective_gate\"] = \"LOCKED_BY_INCOMPLETE_AUTHORITY_HISTORY\"\n        failed = state.setdefault(\"failed_reasons\", [])\n        if \"AUTHORITY_HISTORY_INCOMPLETE\" not in failed:\n            failed.append(\"AUTHORITY_HISTORY_INCOMPLETE\")\n    return state\n",
)

# AUD-059 tests previously mocked the bounded transport API because authority
# used it directly. Point only those authority-path mocks at the complete
# history boundary; the global newest-N diagnostic remains fetch_predictions.
test59 = Path("senecio_polymarket/tests/test_aud_059.py")
t = test59.read_text()
t = t.replace(
    'patch.object(supabase_client, "fetch_predictions", new=AsyncMock(return_value=rows))',
    'patch.object(supabase_client, "fetch_authority_history", new=AsyncMock(return_value=rows))',
)
t = t.replace(
    'patch.object(supabase_client, "fetch_predictions", new=boundary)',
    'patch.object(supabase_client, "fetch_authority_history", new=boundary)',
)
old_asserts = '''        self.assertIn({"limit": 500, "symbol": "BTCUSDT"}, mixed_calls)\n        self.assertIn({"limit": 500, "symbol": "ETHUSDT"}, mixed_calls)\n        self.assertIn({"limit": 500, "symbol": None}, mixed_calls)\n        self.assertIn({"limit": 500, "symbol": "BTCUSDT"}, solo_calls)\n'''
new_asserts = '''        self.assertIn({"limit": 50, "symbol": "BTCUSDT"}, mixed_calls)\n        self.assertIn({"limit": 50, "symbol": "ETHUSDT"}, mixed_calls)\n        self.assertNotIn({"limit": 50, "symbol": None}, mixed_calls)\n        self.assertIn({"limit": 50, "symbol": "BTCUSDT"}, solo_calls)\n'''
if old_asserts not in t:
    raise SystemExit("AUD059 R3 assertion block mismatch")
t = t.replace(old_asserts, new_asserts, 1)
t = t.replace(
    'self.assertEqual(calls, [{"limit": 500, "symbol": "BTCUSDT"}])',
    'self.assertEqual(calls, [{"limit": 50, "symbol": "BTCUSDT"}])',
)
test59.write_text(t)

Path("senecio_polymarket/tests/test_aud_065.py").write_text(r'''from __future__ import annotations

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
''')

Path("senecio_polymarket/docs/evidence/aud-065-post-merge-production-acceptance.md").write_text(r'''# AUD-065 post-merge/deploy production acceptance procedure

Do not execute without separate OWNER merge/deploy/restart authorization.

1. Resolve the final merged `main` SHA and tree and require the Northflank build/deployment to be pinned to that exact SHA; reject `latest` or any moving ref.
2. Verify runtime health, `cycles_failed=0`, `last_error=null`, and PAPER/live/order locks.
3. Read real Supabase per-symbol source totals with complete keyset pagination, never a newest-N authority query.
4. For BTCUSDT and ETHUSDT independently, reconcile source row count, canonical proof-qualified raw N, `INDEPENDENT_NONOVERLAP_1H` N, wins/losses, WR, Wilson/gates and score reasons against `/api/oracle/score?symbol=...`.
5. When either symbol exceeds 500 source rows, prove the API `input_rows/total_predictions` equals the complete source count and that an old valid proof row remains represented in authority after newer same-symbol rows are appended.
6. Reconcile runtime `directional_stats.per_symbol` and portfolio/research live-gate diagnostics to the same authority cohort.
7. Verify dashboard total input rows, raw proof N and independent authority N are derived from the reconciled score API and remain separately labeled.
8. Verify latest persisted decisions remain `learning_mutation_authority=SHADOW_ONLY`, `production_learning_mutation_enabled=false`, `mutations=0`, `uses_only_prior_settled_evidence=true`, and base/decision/effective weight hashes are identical.
9. Verify Polymarket/Kalshi/Boros are read-only with zero effective external directional contribution, no RUNTIME017 mutation, no data rewrite/backfill, no real trading and no wallet/private-key/order/capital path.
10. Materialize exact-SHA logs/artifacts and fail closed on any incomplete authority retrieval.
''')
