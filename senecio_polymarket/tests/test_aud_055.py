"""Historical AUD-055 compatibility regressions only.

AUD-059 statistical-truth tests live exclusively in ``test_aud_059.py`` so
``unittest discover`` collects every test exactly once.
"""
import inspect
import unittest
from pathlib import Path

from fastapi.routing import APIRoute


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
