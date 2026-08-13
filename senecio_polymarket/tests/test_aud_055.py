"""Historical AUD-055 regression entrypoint.

AUD-059 supersedes the former assumptions that overlapping 1h observations were
independent and that persisted confidence was a calibrated probability. The
current statistical-truth regressions are imported here so repository-wide test
discovery cannot silently reintroduce the retired semantics.
"""
import inspect
import unittest
from pathlib import Path

from fastapi.routing import APIRoute
from test_aud_059 import IndependentAuthorityTests, LearningProvenanceTests


class ProductionScoreRouteCompatibilityTests(unittest.TestCase):
    def test_main_real_keeps_one_symbol_scoped_score_route(self):
        from backend import main_real
        routes = [
            route for route in main_real.app.router.routes
            if isinstance(route, APIRoute)
            and route.path == "/api/oracle/score"
            and "GET" in route.methods
        ]
        self.assertEqual(len(routes), 1)
        self.assertIn("symbol", inspect.signature(routes[0].endpoint).parameters)

    def test_dashboard_requests_btc_authority_and_keeps_observed_diagnostic(self):
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn("/api/oracle/score?symbol=BTCUSDT", app_js)
        self.assertIn("authoritative_score_pct", app_js)
        self.assertIn("observed_win_rate_pct", app_js)


if __name__ == "__main__":
    unittest.main()
