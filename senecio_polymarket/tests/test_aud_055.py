import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.routing import APIRoute

from backend.authoritative_score import build_authoritative_score


BASE_TS = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)


def _proof_row(idx: int, *, direction: str, outcome: str) -> dict:
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
        "symbol": "BTCUSDT",
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


def _rows(long_n: int, short_n: int, *, long_wins: int, short_wins: int) -> list[dict]:
    rows = []
    idx = 1
    for i in range(long_n):
        rows.append(_proof_row(idx, direction="LONG", outcome="WIN" if i < long_wins else "LOSS"))
        idx += 1
    for i in range(short_n):
        rows.append(_proof_row(idx, direction="SHORT", outcome="WIN" if i < short_wins else "LOSS"))
        idx += 1
    return rows


class AuthoritativeScoreTruthTests(unittest.TestCase):
    def test_n0_authoritative_score_is_null(self):
        score = build_authoritative_score([])
        self.assertEqual(score["score_status"], "UNKNOWN")
        self.assertIsNone(score["authoritative_score_pct"])
        self.assertIsNone(score["observed_win_rate_pct"])

    def test_n1_win_authoritative_score_is_null(self):
        score = build_authoritative_score(_rows(1, 0, long_wins=1, short_wins=0))
        self.assertEqual(score["score_status"], "INSUFFICIENT_EVIDENCE")
        self.assertIsNone(score["authoritative_score_pct"])
        self.assertEqual(score["observed_win_rate_pct"], 100.0)
        self.assertTrue(score["observed_win_rate_diagnostic_only"])

    def test_n99_authoritative_score_is_null(self):
        score = build_authoritative_score(_rows(50, 49, long_wins=50, short_wins=49))
        self.assertEqual(score["verified"], 99)
        self.assertEqual(score["score_status"], "INSUFFICIENT_EVIDENCE")
        self.assertIsNone(score["authoritative_score_pct"])
        self.assertEqual(score["minimum_evidence"]["global_n"], 100)
        self.assertEqual(score["minimum_evidence"]["direction_n"], 30)

    def test_calibrated_gate_only_exposes_numeric_authoritative_score(self):
        score = build_authoritative_score(_rows(50, 50, long_wins=30, short_wins=30))
        self.assertEqual(score["verified"], 100)
        self.assertTrue(score["gates"]["long_1h"]["pass"])
        self.assertTrue(score["gates"]["short_1h"]["pass"])
        self.assertTrue(score["gates"]["global_1h"]["pass"])
        self.assertEqual(score["score_status"], "CALIBRATED")
        self.assertEqual(score["authoritative_score_pct"], 60.0)
        self.assertEqual(score["observed_win_rate_pct"], 60.0)

    def test_sufficient_sample_but_failed_quality_gate_is_rejected(self):
        score = build_authoritative_score(_rows(50, 50, long_wins=25, short_wins=20))
        self.assertEqual(score["score_status"], "REJECTED")
        self.assertIsNone(score["authoritative_score_pct"])


class ProductionScoreRouteTests(unittest.TestCase):
    def test_main_real_installs_exactly_one_truth_safe_score_route(self):
        from backend import main_real

        routes = [
            route for route in main_real.app.router.routes
            if isinstance(route, APIRoute)
            and route.path == "/api/oracle/score"
            and "GET" in route.methods
        ]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].name, "oracle_score_authoritative_aud055")

    def test_frontend_never_uses_observed_rate_as_authoritative_value(self):
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "frontend" / "app.js").read_text(encoding="utf-8")
        index = (root / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn("s.authoritative_score_pct", app_js)
        self.assertIn("observed_win_rate_pct", app_js)
        self.assertNotIn("s.verified ? `${Number(s.win_rate_pct", app_js)
        self.assertIn("Authoritative", index)
        self.assertNotIn("<div class=\"score-label\">Win rate</div>", index)


if __name__ == "__main__":
    unittest.main()
