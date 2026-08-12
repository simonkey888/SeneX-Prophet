from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dual_writer_is_atomically_null_guarded():
    source = (ROOT / "senecio_polymarket/backend/supabase_client.py").read_text()
    assert '"outcome": "is.null"' in source
    assert '"audit->outcomes_dual": "is.null"' in source


def test_reconciler_fails_fast_on_missing_credentials():
    source = (ROOT / "senecio_polymarket/backend/settlement_reconciler.py").read_text()
    daemon = source[source.index("async def daemon"):source.index("if __name__ == \"__main__\"")]
    assert "raise RuntimeError" in daemon
    assert "SUPABASE_URL" in daemon and "SUPABASE_KEY" in daemon
    assert "except Exception:" not in daemon


def test_reconciler_patch_uses_absolute_supabase_url():
    source = (ROOT / "senecio_polymarket/backend/settlement_reconciler.py").read_text()
    assert 'client.patch(\n                        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"' in source


def test_reconciler_emits_heartbeat_only_after_cycle():
    source = (ROOT / "senecio_polymarket/backend/settlement_reconciler.py").read_text()
    daemon = source[source.index("async def daemon"):source.index("if __name__ == \"__main__\"")]
    assert "HEARTBEAT_FILE.touch()" in daemon
    assert "result = await reconcile_once()" in daemon
    assert daemon.index("result = await reconcile_once()") < daemon.index("HEARTBEAT_FILE.touch()")


def test_startup_script_fails_closed_without_supabase_config():
    source = (ROOT / "senecio_polymarket/start_single_authority.sh").read_text()
    assert "SUPABASE_URL" in source
    assert "SUPABASE_KEY" in source
    assert "exit 78" in source


def test_startup_script_monitors_reconciler_health():
    source = (ROOT / "senecio_polymarket/start_single_authority.sh").read_text()
    assert "RECONCILER_PID" in source
    assert "HEARTBEAT_FILE" in source
    assert "SENEX_RECONCILER_HEALTH_GRACE_SEC" in source
    assert "SENEX_RECONCILER_HEALTH_STALE_SEC" in source
    assert "reconciler heartbeat stale" in source
    assert "no heartbeat within" in source


def test_root_dockerfile_starts_single_authority_launcher():
    source = (ROOT / "Dockerfile").read_text()
    assert "COPY senecio_polymarket/start_single_authority.sh ./start_single_authority.sh" in source
    assert 'CMD ["/app/start_single_authority.sh"]' in source
