from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import polymarket.dashboard_v3 as dashboard
import polymarket.h011_v3_raw_transaction as rt
from polymarket.h011_v3_runtime import _publish_synthetic_snapshot
from polymarket.h011_v3_committed_snapshot import validate_committed_chain


def _exchange(dir_fd: int, old_name: str, new_name: str) -> None:
    temp = f".swap.{uuid.uuid4().hex}"
    os.rename(old_name, temp, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    os.rename(new_name, old_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    os.rename(temp, new_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def _response_json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def _runtime_payload(now: datetime, **changes):
    payload = {
        "runtime_state": "RUNNING",
        "readiness": True,
        "chain_verified": True,
        "scanner_enabled": True,
        "updated_at": now.isoformat(),
        "started_at": (now - timedelta(seconds=60)).isoformat(),
        "scanner_last_success": (now - timedelta(seconds=30)).isoformat(),
    }
    payload.update(changes)
    return payload


def _ready_response(tmp_path, monkeypatch, payload):
    state_file = tmp_path / "runtime_state.json"
    state_file.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(dashboard, "RUNTIME_STATE_FILE", state_file)
    return dashboard.readyz()


def _publish(raw: Path) -> dict:
    payload = {"test": True}
    event = {
        "received_at_utc": "2026-07-21T00:00:00+00:00",
        "source": "dashboard_test",
        "endpoint": "/test",
        "request_params": {},
        "requested_condition_id": "condition",
        "payload": payload,
        "payload_sha256": rt.canonical_payload_sha256(payload),
        "cohort_id": "h011-v3-w300-vwap-structure-v2",
        "schema_version": "h011-raw-envelope-v1",
    }
    with rt.RawScanStager("run", "scan", raw) as stager:
        stager.append_event(event)
        stager.seal()
        transfer = stager.transfer()
        with rt.RawChainLock(raw, "manifest").acquire() as guard:
            rt.publish_raw_scan(
                transfer=transfer, guard=guard, raw_directory=raw,
                policy=rt.DEFAULT_MARKER_POLICY,
                manifest_created_at="2026-07-21T00:00:00+00:00",
            )
    return validate_committed_chain(raw).to_dict()


def test_no_committed_scan_exposes_visible_diagnostic(tmp_path, monkeypatch):
    results = tmp_path / "results"
    raw = results / "h011_v3" / "raw_chain_v1"
    raw.mkdir(parents=True)
    monkeypatch.setattr(dashboard, "RESULTS_DIR", results)
    monkeypatch.setattr(dashboard, "RAW_CHAIN_DIR", raw)
    monkeypatch.setattr(dashboard, "RUNTIME_STATE_FILE", results / "h011_v3" / "runtime_state.json")
    state = dashboard.api_state()
    assert state.status_code == 503
    payload = _response_json(state)
    assert payload["paper_only"] is True
    assert payload["orders_enabled"] is False
    assert payload["live_capital_locked"] is True
    health = _response_json(dashboard.healthz())
    assert health["status"] == "NO_COMMITTED_SCAN"
    assert health["liveness"] is True


def test_api_uses_committed_reader_and_real_snapshot_age(tmp_path, monkeypatch):
    results = tmp_path / "results"
    raw = results / "h011_v3" / "raw_chain_v1"
    raw.mkdir(parents=True)
    monkeypatch.setattr(rt, "_renameat2_exchange", _exchange)
    chain = _publish(raw)
    _publish_synthetic_snapshot(results, chain)
    runtime_file = results / "h011_v3" / "runtime_state.json"
    runtime_file.write_text(json.dumps({
        "runtime_state": "RUNNING", "readiness": True, "liveness": True,
        "scanner_enabled": True, "publication_enabled": True,
        "recovery_status": "NO_RECOVERY_NEEDED", "storage_status": "READY_CODE_VALIDATED",
        "chain_verified": True, "paper_only": True, "orders_enabled": False,
        "live_capital_locked": True, "legacy_mode": False,
    }))
    monkeypatch.setattr(dashboard, "RESULTS_DIR", results)
    monkeypatch.setattr(dashboard, "RAW_CHAIN_DIR", raw)
    monkeypatch.setattr(dashboard, "RUNTIME_STATE_FILE", runtime_file)

    state_response = dashboard.api_state()
    assert state_response.status_code == 200
    state = _response_json(state_response)
    assert state["raw_chain"]["current_sequence"] == 0
    assert state["snapshot_age_sec"] is not None
    assert state["runtime"]["runtime_state"] == "RUNNING"
    integrity = _response_json(dashboard.api_integrity())
    assert integrity["chain_verified"] is True
    assert integrity["replay_verified"] is True
    assert integrity["legacy_mode"] is False
    replay = _response_json(dashboard.api_replay())
    assert replay["artifact"] == chain["artifact_name"]
    assert replay["replay_contract"] == "strict_committed_raw_chain_v1"
    health = _response_json(dashboard.healthz())
    assert health["ok"] is True
    assert health["snapshot_age_sec"] is not None


def test_default_launch_path_binds_application_to_loopback(monkeypatch):
    observed = {}
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setattr(dashboard.uvicorn, "run", lambda app, **kwargs: observed.update(kwargs))

    dashboard.main()

    assert dashboard.UVICORN_HOST == "127.0.0.1"
    assert observed["host"] == "127.0.0.1"
    assert observed["host"] not in {"0.0.0.0", "::"}


def test_fresh_running_state_is_ready(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    response = _ready_response(tmp_path, monkeypatch, _runtime_payload(now))
    assert response.status_code == 200


def test_explicit_diagnostic_harness_preserves_isolated_readiness(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setenv("H011_RUNTIME_DIAGNOSTIC_ONLY", "true")
    response = _ready_response(
        tmp_path,
        monkeypatch,
        _runtime_payload(
            now,
            runtime_state="DEGRADED",
            blocking_reason="diagnostic_only_mode",
            scanner_enabled=False,
            publication_enabled=False,
            scanner_last_success=None,
        ),
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"updated_at": "2000-01-01T00:00:00+00:00"}, "runtime_state_stale_or_invalid"),
        ({"scanner_last_success": "2000-01-01T00:00:00+00:00"}, "scanner_success_stale_or_invalid"),
        ({"chain_verified": False}, "chain_not_verified"),
        ({"runtime_state": "DEGRADED"}, "runtime_state_not_running"),
    ],
)
def test_readiness_fails_closed_for_stale_or_invalid_runtime_state(
    tmp_path, monkeypatch, changes, reason
):
    now = datetime.now(timezone.utc)
    response = _ready_response(tmp_path, monkeypatch, _runtime_payload(now, **changes))
    assert response.status_code == 503
    assert _response_json(response)["reason"] == reason


def test_startup_grace_requires_enabled_scanner_and_fresh_start(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    fresh = _runtime_payload(now, scanner_last_success=None)
    assert _ready_response(tmp_path, monkeypatch, fresh).status_code == 200

    disabled = _runtime_payload(now, scanner_last_success=None, scanner_enabled=False)
    response = _ready_response(tmp_path, monkeypatch, disabled)
    assert response.status_code == 503
    assert _response_json(response)["reason"] == "scanner_not_enabled_during_startup"

    expired = _runtime_payload(
        now,
        scanner_last_success=None,
        started_at=(now - timedelta(seconds=1501)).isoformat(),
    )
    response = _ready_response(tmp_path, monkeypatch, expired)
    assert response.status_code == 503
    assert _response_json(response)["reason"] == "startup_grace_expired_or_invalid"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("H011_READYZ_STATE_MAX_AGE_S", "not-a-number"),
        ("H011_READYZ_SCANNER_SUCCESS_MAX_AGE_S", "0"),
        ("H011_READYZ_STARTUP_GRACE_S", "nan"),
    ],
)
def test_malformed_freshness_configuration_fails_closed(tmp_path, monkeypatch, name, value):
    now = datetime.now(timezone.utc)
    monkeypatch.setenv(name, value)
    response = _ready_response(tmp_path, monkeypatch, _runtime_payload(now))
    assert response.status_code == 503
    assert _response_json(response)["reason"] == "readiness_config_invalid"
