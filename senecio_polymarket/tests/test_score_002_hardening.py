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


def test_startup_script_fails_closed_without_supabase_config():
    source = (ROOT / "senecio_polymarket/start_single_authority.sh").read_text()
    assert "SUPABASE_URL" in source
    assert "SUPABASE_KEY" in source
    assert "exit 78" in source


def test_root_dockerfile_starts_single_authority_launcher():
    source = (ROOT / "Dockerfile").read_text()
    assert "COPY senecio_polymarket/start_single_authority.sh ./start_single_authority.sh" in source
    assert 'CMD ["/app/start_single_authority.sh"]' in source
