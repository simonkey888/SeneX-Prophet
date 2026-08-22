from __future__ import annotations

import unittest

from fastapi.routing import APIRoute

from backend import main as legacy
from backend import main_real


class Order070R6PublicBoundaryTests(unittest.TestCase):
    @staticmethod
    def _paths(app):
        return {
            route.path
            for route in app.router.routes
            if isinstance(route, APIRoute)
        }

    def test_optional_heavy_get_routes_are_not_mounted_publicly(self):
        public_paths = self._paths(main_real.app)
        leaked = sorted(
            path
            for path in public_paths
            if path in main_real.OPTIONAL_HEAVY_PUBLIC_DENY_PATHS
            or any(
                path.startswith(prefix)
                for prefix in main_real.OPTIONAL_HEAVY_PUBLIC_DENY_PREFIXES
            )
        )
        self.assertEqual(leaked, [])

    def test_legacy_heavy_routes_remain_outside_public_app(self):
        legacy_routes = {
            route.path: route
            for route in legacy.app.router.routes
            if isinstance(route, APIRoute)
        }
        public_paths = self._paths(main_real.app)
        discovered = {
            path
            for path in legacy_routes
            if path in main_real.OPTIONAL_HEAVY_PUBLIC_DENY_PATHS
            or any(
                path.startswith(prefix)
                for prefix in main_real.OPTIONAL_HEAVY_PUBLIC_DENY_PREFIXES
            )
        }
        self.assertTrue(discovered, "expected optional-heavy legacy routes")
        self.assertTrue(discovered.isdisjoint(public_paths))

    def test_public_openapi_contains_no_optional_heavy_routes(self):
        paths = set(main_real.app.openapi().get("paths", {}))
        self.assertFalse(paths & main_real.OPTIONAL_HEAVY_PUBLIC_DENY_PATHS)
        self.assertFalse(
            any(
                path.startswith(prefix)
                for path in paths
                for prefix in main_real.OPTIONAL_HEAVY_PUBLIC_DENY_PREFIXES
            )
        )

    def test_authority_refresh_delay_compensates_capture_latency(self):
        self.assertEqual(main_real._authority_refresh_delay(5.0, 0.0), 5.0)
        self.assertEqual(main_real._authority_refresh_delay(5.0, 2.0), 3.0)
        self.assertEqual(main_real._authority_refresh_delay(5.0, 7.0), 0.1)

    def test_canonical_order070_surfaces_remain_public(self):
        paths = self._paths(main_real.app)
        required = {
            "/healthz",
            "/readyz",
            "/api/health",
            "/api/oracle/state",
            "/api/oracle/score",
            "/api/portfolio/live_gate",
            "/api/authority/snapshot",
            "/api/runtime/provenance",
            "/api/market-context",
        }
        self.assertTrue(required <= paths)


if __name__ == "__main__":
    unittest.main()
