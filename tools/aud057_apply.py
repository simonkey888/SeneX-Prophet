from pathlib import Path


def main() -> None:
    runner_path = Path("senecio_polymarket/backend/oracle_runner.py")
    text = runner_path.read_text(encoding="utf-8")

    primary = 'PRIMARY_WINDOW = "1h"   # gating source of truth per ACT XXIII directive 1\n'
    if "def _normalize_symbol(value: Any)" not in text:
        assert primary in text
        text = text.replace(
            primary,
            primary
            + "\n\ndef _normalize_symbol(value: Any) -> str:\n"
            + '    """Normalize runtime symbols for proof/gate/portfolio isolation."""\n'
            + '    return str(value or "").upper().replace("/", "").replace("-", "").strip()\n',
            1,
        )

    state_start = text.index("    # ACT XXIII: directional gate state")
    state_end = text.index('    "trade_mode": "PAPER",', state_start)
    state_replacement = '''    # AUD-057: proof-qualified directional stats and gates are symbol-scoped.
    # Any cross-symbol view is explicitly diagnostic and must never configure
    # a symbol-specific score, Kelly input, or PAPER portfolio gate.
    "directional_stats": {
        "per_symbol": {},
        "aggregate_diagnostic": {
            "by_window": {
                "15m": {"LONG": {}, "SHORT": {}, "FLAT": {}, "global": {}},
                "1h": {"LONG": {}, "SHORT": {}, "FLAT": {}, "global": {}},
            },
        },
    },
'''
    text = text[:state_start] + state_replacement + text[state_end:]

    refresh_start = text.index("async def _refresh_directional_stats() -> None:")
    refresh_end = text.index("\nasync def _oracle_loop() -> None:", refresh_start)
    refresh_replacement = '''async def _refresh_directional_stats() -> None:
    """Recompute proof-qualified directional stats independently per symbol.

    AUD-057 invariant: BTC evidence must never modify ETH gates and ETH
    evidence must never modify BTC gates. A cross-symbol aggregate is retained
    only under ``aggregate_diagnostic`` and is not consumed by portfolio or
    authoritative score routing.
    """
    try:
        from . import supabase_client
    except Exception as e:
        log.warning("supabase_client unavailable for directional stats: %s", e)
        return

    try:
        rows = await supabase_client.fetch_predictions(limit=500)
    except Exception as e:
        log.warning("directional stats fetch failed: %s", e)
        return

    def build_by_window(source_rows: list[dict[str, Any]]) -> dict[str, dict]:
        buckets: dict[str, dict[str, dict[str, int]]] = {
            "15m": {
                "LONG": {"WIN": 0, "LOSS": 0},
                "SHORT": {"WIN": 0, "LOSS": 0},
                "FLAT": {"WIN": 0, "LOSS": 0},
            },
            "1h": {
                "LONG": {"WIN": 0, "LOSS": 0},
                "SHORT": {"WIN": 0, "LOSS": 0},
                "FLAT": {"WIN": 0, "LOSS": 0},
            },
        }

        for row in source_rows:
            direction = (row.get("prediction") or "").upper()
            if direction not in ("LONG", "SHORT", "FLAT"):
                continue
            outcome_1h = row.get("outcome")
            if outcome_1h in ("WIN", "LOSS"):
                buckets["1h"][direction][outcome_1h] += 1

            audit = row.get("audit") or {}
            dual = audit.get("outcomes_dual") if isinstance(audit, dict) else {}
            if isinstance(dual, dict):
                outcome_15m = dual.get("outcome_15m")
                if outcome_15m in ("WIN", "LOSS"):
                    buckets["15m"][direction][outcome_15m] += 1

        by_window: dict[str, dict] = {}
        for window in ("15m", "1h"):
            by_window[window] = {}
            total_w = total_l = 0
            for direction in ("LONG", "SHORT", "FLAT"):
                wins = buckets[window][direction]["WIN"]
                losses = buckets[window][direction]["LOSS"]
                verified = wins + losses
                total_w += wins
                total_l += losses
                by_window[window][direction] = {
                    "verified": verified,
                    "wins": wins,
                    "losses": losses,
                    "win_rate_pct": round((wins / verified * 100) if verified else 0.0, 2),
                }
            total = total_w + total_l
            by_window[window]["global"] = {
                "verified": total,
                "wins": total_w,
                "losses": total_l,
                "win_rate_pct": round((total_w / total * 100) if total else 0.0, 2),
            }
        return by_window

    def build_gates(by_window: dict[str, dict]) -> dict[str, dict[str, Any]]:
        specs = {
            "long_1h": ("LONG", 50.0, 30),
            "short_1h": ("SHORT", 55.0, 30),
            "global_1h": ("global", 52.0, 100),
        }
        result: dict[str, dict[str, Any]] = {}
        for gate_name, (bucket_name, threshold_pct, min_n) in specs.items():
            bucket = (by_window.get("1h") or {}).get(bucket_name) or {}
            verified = int(bucket.get("verified") or 0)
            win_rate_pct = float(bucket.get("win_rate_pct") or 0.0)
            result[gate_name] = {
                "pass": bool(verified >= min_n and win_rate_pct >= threshold_pct),
                "win_rate_pct": win_rate_pct,
                "n": verified,
                "threshold_pct": threshold_pct,
                "min_n": min_n,
            }
        return result

    qualified_by_symbol: dict[str, list[dict[str, Any]]] = {}
    all_qualified: list[dict[str, Any]] = []
    for row in rows:
        if not is_proof_qualified(row):
            continue
        symbol = _normalize_symbol(row.get("symbol"))
        if not symbol:
            continue
        qualified_by_symbol.setdefault(symbol, []).append(row)
        all_qualified.append(row)

    configured_symbols = {_normalize_symbol(symbol) for symbol in SYMBOLS}
    symbols = sorted(configured_symbols | set(qualified_by_symbol))
    per_symbol: dict[str, dict[str, Any]] = {}

    for symbol in symbols:
        by_window = build_by_window(qualified_by_symbol.get(symbol, []))
        gates = build_gates(by_window)
        short_only = bool(
            gates["short_1h"]["pass"] and not gates["long_1h"]["pass"]
        )
        per_symbol[symbol] = {
            "by_window": by_window,
            "gates": gates,
            "short_only_paper_mode": short_only,
        }
        log.info(
            "directional gates symbol=%s LONG_1h=%s(wr=%.1f%% n=%d) "
            "SHORT_1h=%s(wr=%.1f%% n=%d) GLOBAL_1h=%s(wr=%.1f%% n=%d) "
            "short_only_paper_mode=%s",
            symbol,
            "PASS" if gates["long_1h"]["pass"] else "FAIL",
            gates["long_1h"]["win_rate_pct"], gates["long_1h"]["n"],
            "PASS" if gates["short_1h"]["pass"] else "FAIL",
            gates["short_1h"]["win_rate_pct"], gates["short_1h"]["n"],
            "PASS" if gates["global_1h"]["pass"] else "FAIL",
            gates["global_1h"]["win_rate_pct"], gates["global_1h"]["n"],
            short_only,
        )

    aggregate_by_window = build_by_window(all_qualified)
    _state["directional_stats"] = {
        "per_symbol": per_symbol,
        "aggregate_diagnostic": {"by_window": aggregate_by_window},
    }
    _state["verified_total"] = aggregate_by_window["1h"]["global"]["verified"]
'''
    text = text[:refresh_start] + refresh_replacement + text[refresh_end:]

    route_anchor = text.index("async def _route_to_portfolio(prediction: dict, market_data: dict) -> None:")
    route_start = text.index("    # Win-rate-by-direction passthrough (for Kelly)", route_anchor)
    route_end = text.index("    # ACT-XXVI: extract ohlcv + funding + OI", route_start)
    route_replacement = '''    # AUD-057: Kelly and short-only configuration are scoped to the
    # current prediction symbol. Cross-symbol aggregate diagnostics are never
    # consumed here.
    symbol = _normalize_symbol(prediction.get("symbol"))
    symbol_stats = (
        (_state.get("directional_stats") or {}).get("per_symbol") or {}
    ).get(symbol) or {}
    by_window = symbol_stats.get("by_window") or {}
    win_rate_by_dir = {}
    try:
        for direction in ("LONG", "SHORT"):
            direction_stat = (by_window.get("1h") or {}).get(direction) or {}
            win_rate_by_dir[direction] = float(direction_stat.get("win_rate_pct") or 0.0) / 100.0
    except Exception:
        win_rate_by_dir = {}

    short_only = bool(symbol_stats.get("short_only_paper_mode", False))
    coord.portfolio_engine.update_config(short_only_paper_mode=short_only)
    coord.risk_kernel.update_config(
        short_only_paper_mode=short_only,
        trade_mode=_state.get("trade_mode", "PAPER"),
        live_capital_locked=_state.get("live_capital_locked", True),
    )
    coord.execution_engine.update_config(
        trade_mode=_state.get("trade_mode", "PAPER"),
        allow_live=not _state.get("live_capital_locked", True),
    )

'''
    text = text[:route_start] + route_replacement + text[route_end:]
    runner_path.write_text(text, encoding="utf-8")

    score_path = Path("senecio_polymarket/backend/authoritative_score.py")
    score = score_path.read_text(encoding="utf-8")

    gate_marker = "\n\ndef _gate(bucket: dict[str, Any], *, min_n: int, threshold_pct: float) -> dict[str, Any]:\n"
    assert gate_marker in score
    window_helpers = '''

def _window_outcome(row: dict[str, Any], window: str) -> str | None:
    if window == "1h":
        return row.get("outcome")
    audit = row.get("audit") or {}
    dual = audit.get("outcomes_dual") if isinstance(audit, dict) else {}
    return dual.get("outcome_15m") if isinstance(dual, dict) else None


def _by_window(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build dual-window diagnostics from one already-scoped proof cohort."""
    result: dict[str, dict[str, Any]] = {}
    for window in ("15m", "1h"):
        result[window] = {}
        total_wins = total_losses = 0
        for direction in ("LONG", "SHORT", "FLAT"):
            direction_rows = [
                row for row in rows
                if str(row.get("prediction") or "").upper() == direction
            ]
            wins = sum(1 for row in direction_rows if _window_outcome(row, window) == "WIN")
            losses = sum(1 for row in direction_rows if _window_outcome(row, window) == "LOSS")
            verified = wins + losses
            total_wins += wins
            total_losses += losses
            result[window][direction] = {
                "verified": verified,
                "wins": wins,
                "losses": losses,
                "win_rate_pct": round((wins / verified * 100.0) if verified else 0.0, 2),
            }
        total = total_wins + total_losses
        result[window]["global"] = {
            "verified": total,
            "wins": total_wins,
            "losses": total_losses,
            "win_rate_pct": round((total_wins / total * 100.0) if total else 0.0, 2),
        }
    return result
'''
    score = score.replace(gate_marker, window_helpers + gate_marker, 1)

    selection_start = score.index("    requested_symbol = _normalize_symbol(symbol) if symbol else None")
    selection_end = score.index("    selected_payload = selected or _empty_symbol_score(requested_symbol)", selection_start)
    selection = '''    requested_symbol = _normalize_symbol(symbol) if symbol else None
    selected_rows: list[dict[str, Any]] = []
    if requested_symbol:
        selected_rows = grouped.get(requested_symbol, [])
        selected = by_symbol.get(requested_symbol) or _empty_symbol_score(requested_symbol)
        report_status = selected["score_status"]
        authoritative_score_pct = selected["authoritative_score_pct"]
    elif len(by_symbol) == 1:
        single_symbol = next(iter(by_symbol))
        selected_rows = grouped.get(single_symbol, [])
        selected = by_symbol[single_symbol]
        report_status = selected["score_status"]
        authoritative_score_pct = selected["authoritative_score_pct"]
    else:
        selected = None
        report_status = "MULTI_INSTRUMENT_REPORT" if by_symbol else "UNKNOWN"
        authoritative_score_pct = None

    # AUD-057: public by_window diagnostics are derived from the same
    # symbol-scoped proof cohort as the selected score. runner_state remains
    # accepted for API compatibility but cannot inject cross-symbol statistics.
    by_window = _by_window(selected_rows) if selected is not None else {}

'''
    score = score[:selection_start] + selection + score[selection_end:]
    score = score.replace('"version": "AUD-055-R1-score-truth-v2",', '"version": "AUD-057-score-truth-v3",', 1)
    score = score.replace(
        '        "proof_qualified_rows": len(proof_qualified),',
        '        "proof_qualified_rows": len(selected_rows) if selected is not None else len(proof_qualified),',
        1,
    )
    score_path.write_text(score, encoding="utf-8")

    test_path = Path("senecio_polymarket/tests/test_aud_057.py")
    test_path.write_text('''from __future__ import annotations

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

    def test_eth_short_only_gate_cannot_trigger_btc_short_only(self):
        btc = BTC5
        eth = _cohort("ETHUSDT", ["LOSS"] * 30, ["WIN"] * 30, 500)
        stats = self._refresh(btc + eth)
        self.assertFalse(stats["per_symbol"]["BTCUSDT"]["short_only_paper_mode"])
        self.assertTrue(stats["per_symbol"]["ETHUSDT"]["short_only_paper_mode"])
        self.assertFalse(stats["per_symbol"]["BTCUSDT"]["gates"]["short_1h"]["pass"])
        self.assertTrue(stats["per_symbol"]["ETHUSDT"]["gates"]["short_1h"]["pass"])


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
                    "short_only_paper_mode": False,
                },
                "ETHUSDT": {
                    "by_window": {"1h": {"LONG": {"win_rate_pct": 40.0}, "SHORT": {"win_rate_pct": 70.0}}},
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
''', encoding="utf-8")


if __name__ == "__main__":
    main()
