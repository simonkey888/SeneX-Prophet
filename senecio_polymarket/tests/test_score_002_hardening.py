import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class Score002HardeningTests(unittest.TestCase):
    def test_dual_writer_is_atomically_null_guarded(self):
        source = (ROOT / "senecio_polymarket/backend/supabase_client.py").read_text()
        self.assertIn('"outcome": "is.null"', source)
        self.assertIn('"audit->outcomes_dual": "is.null"', source)

    def test_reconciler_fails_fast_on_missing_credentials(self):
        source = (ROOT / "senecio_polymarket/backend/settlement_reconciler.py").read_text()
        daemon = source[source.index("async def daemon"):source.index("if __name__ == \"__main__\"")]
        self.assertIn("raise RuntimeError", daemon)
        self.assertIn("SUPABASE_URL", daemon)
        self.assertIn("SUPABASE_KEY", daemon)
        self.assertNotIn("except Exception:", daemon)

    def test_reconciler_patch_uses_absolute_supabase_url(self):
        source = (ROOT / "senecio_polymarket/backend/settlement_reconciler.py").read_text()
        self.assertIn('client.patch(', source)
        self.assertIn('f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"', source)
        self.assertIn('"outcome": f"eq.{stored_outcome}"', source)
        self.assertIn('"audit->outcomes_dual": "is.null"', source)
        self.assertNotIn('"outcome": o1h', source)

    def test_reconciler_emits_heartbeat_only_after_cycle(self):
        source = (ROOT / "senecio_polymarket/backend/settlement_reconciler.py").read_text()
        daemon = source[source.index("async def daemon"):source.index("if __name__ == \"__main__\"")]
        self.assertIn("HEARTBEAT_FILE.touch()", daemon)
        self.assertIn("result = await reconcile_once()", daemon)
        self.assertLess(
            daemon.index("result = await reconcile_once()"),
            daemon.index("HEARTBEAT_FILE.touch()"),
        )

    def test_startup_script_fails_closed_without_supabase_config(self):
        source = (ROOT / "senecio_polymarket/start_single_authority.sh").read_text()
        self.assertIn("SUPABASE_URL", source)
        self.assertIn("SUPABASE_KEY", source)
        self.assertIn("exit 78", source)

    def test_startup_script_monitors_reconciler_health(self):
        source = (ROOT / "senecio_polymarket/start_single_authority.sh").read_text()
        self.assertIn("RECONCILER_PID", source)
        self.assertIn("HEARTBEAT_FILE", source)
        self.assertIn("SENEX_RECONCILER_HEALTH_GRACE_SEC", source)
        self.assertIn("SENEX_RECONCILER_HEALTH_STALE_SEC", source)
        self.assertIn("reconciler heartbeat stale", source)
        self.assertIn("no heartbeat within", source)

    def test_root_dockerfile_starts_single_authority_launcher(self):
        source = (ROOT / "Dockerfile").read_text()
        self.assertIn(
            "COPY senecio_polymarket/start_single_authority.sh ./start_single_authority.sh",
            source,
        )
        self.assertIn('CMD ["/app/start_single_authority.sh"]', source)


if __name__ == "__main__":
    unittest.main()
