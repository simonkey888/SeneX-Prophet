import inspect
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.routing import APIRoute

from backend.authoritative_score import (
    MAX_BRIER,
    MAX_ECE,
    MIN_WILSON_LOWER,
    build_authoritative_score,
)


BASE_TS = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)


def _proof_row(
    idx: int,
    *,
    direction: str,
    outcome: str,
    symbol: str = "BTCUSDT",
    confidence: float | None = 0.60,
) -> dict:
    ts = (BASE_TS + timedelta(minutes=idx)).isoformat()
    origin = 100.0
    if direction == "LONG":
        later = 101.0 if outcome == "WIN" else 99.0
    elif direction == "SHORT":
        later = 99.0 if outcome == "WIN" else 101.0
    else:
        raise ValueError(direction)
    row = {
        "id": idx,
        "ts": ts,
        "symbol": symbol,
        "prediction": direction,
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
    if confidence is not None:
        row["confidence"] = confidence
    return row


def _rows(
    long_n: int,
    short_n: int,
    *,
    long_wins: int,
    short_wins: int,
    symbol: str = "BTCUSDT",
    confidence: float | None = 0.60,
    start_idx: int = 1,
) -> list[dict]:
    rows = []
    idx = start_idx
    for i in range(long_n):
        rows.append(_proof_row(
            idx,
            direction="LONG",
            outcome="WIN" if i < long_wins else "LOSS",
            symbol=symbol,
            confidence=confidence,
        ))
        idx += 1
    for i in range(short_n):
        rows.append(_proof_row(
            idx,
            direction="SHORT",
            outcome="WIN" if i < short_wins else "LOSS",
            symbol=symbol,
            confidence=confidence,
        ))
        idx += 1
    return rows


class AuthoritativeScoreTruthTests(unittest.TestCase):
    def test_n0_authoritative_score_is_null(self):
        score = build_authoritative_score([], symbol="BTCUSDT")
        self.assertEqual(score["score_status"], "UNKNOWN")
        self.assertIsNone(score["authoritative_score_pct"])
        self.assertIsNone(score["observed_win_rate_pct"])

    def test_n1_win_authoritative_score_is_null(self):
        score = build_authoritative_score(
            _rows(1, 0, long_wins=1, short_wins=0),
            symbol="BTCUSDT",
        )
        self.assertEqual(score["score_status"], "INSUFFICIENT_EVIDENCE")
        self.assertIsNone(score["authoritative_score_pct"])
        self.assertEqual(score["observed_win_rate_pct"], 100.0)
        self.assertTrue(score["observed_win_rate_diagnostic_only"])

    def test_n99_authoritative_score_is_null(self):
        score = build_authoritative_score(
            _rows(50, 49, long_wins=50, short_wins=49),
            symbol="BTCUSDT",
        )
        self.assertEqual(score["verified"], 99)
        self.assertEqual(score["score_status"], "INSUFFICIENT_EVIDENCE")
        self.assertIsNone(score["authoritative_score_pct"])
        self.assertEqual(score["minimum_evidence"]["global_n"], 100)
        self.assertEqual(score["minimum_evidence"]["direction_n"], 30)

    def test_calibrated_gate_only_exposes_numeric_authoritative_score(self):
        score = build_authoritative_score(
            _rows(50, 50, long_wins=30, short_wins=30, confidence=0.60),
            symbol="BTCUSDT",
        )
        self.assertEqual(score["verified"], 100)
        self.assertTrue(score["gates"]["long_1h"]["pass"])
        self.assertTrue(score["gates"]["short_1h"]["pass"])
        self.assertTrue(score["gates"]["global_1h"]["pass"])
        self.assertTrue(all(gate["pass"] for gate in score["quality"]["gates"].values()))
        self.assertEqual(score["score_status"], "CALIBRATED")
        self.assertIsInstance(score["authoritative_score_pct"], float)
        self.assertEqual(score["observed_win_rate_pct"], 60.0)

    def test_sufficient_sample_but_failed_directional_gate_is_rejected(self):
        score = build_authoritative_score(
            _rows(50, 50, long_wins=25, short_wins=20, confidence=0.45),
            symbol="BTCUSDT",
        )
        self.assertEqual(score["score_status"], "REJECTED")
        self.assertIsNone(score["authoritative_score_pct"])


class R1CalibrationQualityTests(unittest.TestCase):
    def test_n100_passing_win_rates_but_missing_confidence_fails_closed(self):
        score = build_authoritative_score(
            _rows(50, 50, long_wins=35, short_wins=35, confidence=None),
            symbol="BTCUSDT",
        )
        self.assertEqual(score["verified"], 100)
        self.assertTrue(all(gate["pass"] for gate in score["gates"].values()))
        self.assertFalse(score["quality"]["confidence_complete"])
        self.assertIn("INVALID_OR_MISSING_CONFIDENCE", score["reasons"])
        self.assertEqual(score["score_status"], "REJECTED")
        self.assertIsNone(score["authoritative_score_pct"])

    def test_wilson_lower_at_or_below_threshold_is_rejected(self):
        score = build_authoritative_score(
            _rows(50, 50, long_wins=27, short_wins=28, confidence=0.55),
            symbol="BTCUSDT",
        )
        self.assertTrue(all(gate["pass"] for gate in score["gates"].values()))
        self.assertLessEqual(score["quality"]["wilson_lower_95"], MIN_WILSON_LOWER)
        self.assertFalse(score["quality"]["gates"]["wilson_lower_95"]["pass"])
        self.assertEqual(score["score_status"], "REJECTED")
        self.assertIsNone(score["authoritative_score_pct"])

    def test_brier_at_or_above_ceiling_is_rejected(self):
        score = build_authoritative_score(
            _rows(50, 50, long_wins=35, short_wins=35, confidence=0.90),
            symbol="BTCUSDT",
        )
        self.assertGreaterEqual(score["quality"]["raw_confidence_brier"], MAX_BRIER)
        self.assertFalse(score["quality"]["gates"]["brier"]["pass"])
        self.assertEqual(score["score_status"], "REJECTED")
        self.assertIsNone(score["authoritative_score_pct"])

    def test_ece_above_ceiling_is_rejected_while_brier_can_pass(self):
        score = build_authoritative_score(
            _rows(50, 50, long_wins=35, short_wins=35, confidence=0.55),
            symbol="BTCUSDT",
        )
        self.assertLess(score["quality"]["raw_confidence_brier"], MAX_BRIER)
        self.assertGreater(score["quality"]["raw_confidence_ece"], MAX_ECE)
        self.assertTrue(score["quality"]["gates"]["brier"]["pass"])
        self.assertFalse(score["quality"]["gates"]["ece"]["pass"])
        self.assertEqual(score["score_status"], "REJECTED")
        self.assertIsNone(score["authoritative_score_pct"])

    def test_fully_proof_qualified_sample_and_quality_gates_calibrate(self):
        score = build_authoritative_score(
            _rows(50, 50, long_wins=35, short_wins=35, confidence=0.70),
            symbol="BTCUSDT",
        )
        self.assertEqual(score["score_status"], "CALIBRATED")
        self.assertIsNotNone(score["authoritative_score_pct"])
        self.assertEqual(score["observed_win_rate_pct"], 70.0)
        self.assertGreater(score["quality"]["wilson_lower_95"], MIN_WILSON_LOWER)
        self.assertLess(score["quality"]["raw_confidence_brier"], MAX_BRIER)
        self.assertLessEqual(score["quality"]["raw_confidence_ece"], MAX_ECE)


class R1PerSymbolIsolationTests(unittest.TestCase):
    def test_btc50_eth50_does_not_pool_to_calibrate_either_symbol(self):
        btc = _rows(25, 25, long_wins=20, short_wins=20, symbol="BTCUSDT", confidence=0.80)
        eth = _rows(
            25, 25,
            long_wins=20,
            short_wins=20,
            symbol="ETHUSDT",
            confidence=0.80,
            start_idx=1001,
        )
        mixed = btc + eth

        btc_score = build_authoritative_score(mixed, symbol="BTCUSDT")
        eth_score = build_authoritative_score(mixed, symbol="ETHUSDT")
        self.assertEqual(btc_score["verified"], 50)
        self.assertEqual(eth_score["verified"], 50)
        self.assertEqual(btc_score["score_status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(eth_score["score_status"], "INSUFFICIENT_EVIDENCE")
        self.assertIsNone(btc_score["authoritative_score_pct"])
        self.assertIsNone(eth_score["authoritative_score_pct"])
        self.assertEqual(btc_score["score_scope"], "PER_SYMBOL")
        self.assertEqual(set(btc_score["by_symbol"]), {"BTCUSDT", "ETHUSDT"})

    def test_btc_can_calibrate_independently_at_n100(self):
        btc = _rows(50, 50, long_wins=35, short_wins=35, symbol="BTCUSDT", confidence=0.70)
        score = build_authoritative_score(btc, symbol="BTCUSDT")
        self.assertEqual(score["verified"], 100)
        self.assertEqual(score["score_status"], "CALIBRATED")
        self.assertIsNotNone(score["authoritative_score_pct"])

    def test_btc_score_is_unchanged_when_eth_rows_are_added_or_removed(self):
        btc = _rows(50, 50, long_wins=35, short_wins=35, symbol="BTCUSDT", confidence=0.70)
        eth = _rows(
            50, 50,
            long_wins=10,
            short_wins=10,
            symbol="ETHUSDT",
            confidence=0.20,
            start_idx=2001,
        )
        solo = build_authoritative_score(btc, symbol="BTCUSDT")
        mixed = build_authoritative_score(btc + eth, symbol="BTCUSDT")
        for key in (
            "score_status",
            "authoritative_score_pct",
            "observed_win_rate_pct",
            "verified",
            "wins",
            "losses",
            "posterior_accuracy",
            "gates",
            "quality",
        ):
            self.assertEqual(solo[key], mixed[key], key)

    def test_multi_instrument_report_never_exposes_combined_authority(self):
        btc = _rows(50, 50, long_wins=35, short_wins=35, symbol="BTCUSDT", confidence=0.70)
        eth = _rows(
            50, 50,
            long_wins=35,
            short_wins=35,
            symbol="ETHUSDT",
            confidence=0.70,
            start_idx=3001,
        )
        report = build_authoritative_score(btc + eth)
        self.assertEqual(report["score_status"], "MULTI_INSTRUMENT_REPORT")
        self.assertIsNone(report["authoritative_score_pct"])
        self.assertEqual(set(report["by_symbol"]), {"BTCUSDT", "ETHUSDT"})


class ProductionScoreRouteTests(unittest.TestCase):
    def test_main_real_installs_exactly_one_truth_safe_score_route_with_symbol_parameter(self):
        from backend import main_real

        routes = [
            route for route in main_real.app.router.routes
            if isinstance(route, APIRoute)
            and route.path == "/api/oracle/score"
            and "GET" in route.methods
        ]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].name, "oracle_score_authoritative_aud055")
        self.assertIn("symbol", inspect.signature(routes[0].endpoint).parameters)

    def test_frontend_never_uses_observed_rate_as_authoritative_value(self):
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "frontend" / "app.js").read_text(encoding="utf-8")
        index = (root / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn("s.authoritative_score_pct", app_js)
        self.assertIn("observed_win_rate_pct", app_js)
        self.assertNotIn("s.verified ? `${Number(s.win_rate_pct", app_js)
        self.assertIn("Authoritative", index)
        self.assertNotIn("<div class=\"score-label\">Win rate</div>", index)

    def test_dashboard_requests_btc_authoritative_score_explicitly(self):
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn("/api/oracle/score?symbol=BTCUSDT", app_js)


if __name__ == "__main__":
    unittest.main()
