from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import polymarket.h011_v3_raw_transaction as rt
from polymarket.h011_v3_committed_snapshot import validate_committed_chain
import polymarket.h011_v3_runtime as runtime
from polymarket.h011_v3_runtime import startup_recovery

RUNTIME = Path(__file__).parents[2] / "polymarket" / "h011_v3_runtime.py"
FAULT_POINTS = [
    rt.FAULT_PUBLISH_AFTER_STAGED_MARKER,
    rt.FAULT_PUBLISH_AFTER_ARTIFACT_MARKER_UPDATE,
    rt.FAULT_PUBLISH_AFTER_SIDECAR_MARKER_UPDATE,
    rt.FAULT_PUBLISH_AFTER_MANIFEST_MARKER_UPDATE,
    rt.FAULT_PUBLISH_AFTER_COMMITTED_MARKER,
    rt.FAULT_PUBLISH_AFTER_STAGING_UNLINK,
    rt.FAULT_PUBLISH_AFTER_MARKER_UNLINK,
]


def _rename_exchange_supported(parent: Path) -> bool:
    directory = parent / "exchange-check"
    directory.mkdir()
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        (directory / "a").write_text("a")
        (directory / "b").write_text("b")
        try:
            rt._renameat2_exchange(fd, "a", "b")
            return True
        except (OSError, rt.AtomicMarkerUpdateUnsupportedError):
            return False
    finally:
        os.close(fd)


def test_startup_clean_chain_enables_scanner(tmp_path):
    state = startup_recovery(results_root=tmp_path / "results")
    assert state["runtime_state"] == "RUNNING"
    assert state["chain_verified"] is True
    assert state["scanner_enabled"] is True
    assert state["publication_enabled"] is True
    assert state["paper_only"] is True
    assert state["orders_enabled"] is False
    assert state["live_capital_locked"] is True


def test_malformed_marker_blocks_without_deletion(tmp_path):
    results = tmp_path / "results"
    chain = results / "h011_v3" / "raw_chain_v1"
    chain.mkdir(parents=True)
    marker = chain / f"manifest_txn_000000_{uuid.uuid4()}.marker"
    marker.write_text("not-json")
    state = startup_recovery(results_root=results)
    assert state["runtime_state"] == "BLOCKED_RAW_INTEGRITY"
    assert state["scanner_enabled"] is False
    assert state["publication_enabled"] is False
    assert marker.exists()


@pytest.mark.parametrize("fault_point", FAULT_POINTS)
def test_same_volume_process_crash_then_startup_recovery(tmp_path, fault_point):
    if not _rename_exchange_supported(tmp_path):
        pytest.skip("filesystem does not support renameat2(RENAME_EXCHANGE)")
    results = tmp_path / fault_point.lower()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(RUNTIME.parent)
    run_id = f"run-{fault_point.lower()}"
    scan_id = f"scan-{fault_point.lower()}"
    crashed = subprocess.run([
        sys.executable, str(RUNTIME), "--results-root", str(results),
        "--synthetic-publish", "--run-id", run_id, "--scan-id", scan_id,
        "--fault-point", fault_point,
    ], env=env, check=False, capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 99, crashed.stderr

    restarted = subprocess.run([
        sys.executable, str(RUNTIME), "--results-root", str(results), "--startup-check",
    ], env=env, check=False, capture_output=True, text=True, timeout=30)
    assert restarted.returncode == 0, restarted.stderr
    state = json.loads(restarted.stdout.strip().splitlines()[-1])
    assert state["chain_verified"] is True
    assert state["scanner_enabled"] is True
    chain = validate_committed_chain(results / "h011_v3" / "raw_chain_v1")
    assert chain.latest is not None
    assert chain.latest["run_id"] == run_id
    raw = results / "h011_v3" / "raw_chain_v1"
    assert not list(raw.glob("*.marker"))
    assert not list(raw.glob("*.marker.tmp.*"))
    pending = raw / ".pending"
    assert not pending.exists() or not list(pending.iterdir())


class _TimeoutThenKillProcess:
    def __init__(self):
        self.calls = []
        self.returncode = None
        self._wait_count = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        self._wait_count += 1
        if self._wait_count <= 2:
            raise subprocess.TimeoutExpired("scanner", timeout)
        self.returncode = -9
        return self.returncode

    def terminate(self):
        self.calls.append(("terminate", None))

    def kill(self):
        self.calls.append(("kill", None))


def test_scanner_timeout_terminates_kills_reaps_and_disables_readiness(tmp_path, monkeypatch):
    monkeypatch.setenv("H011_SCANNER_TIMEOUT_S", "1")
    supervisor = runtime.RuntimeSupervisor(tmp_path)
    process = _TimeoutThenKillProcess()
    supervisor.scanner = process  # type: ignore[assignment]

    return_code, timed_out = supervisor._wait_for_scanner()

    assert (return_code, timed_out) == (runtime.SCANNER_TIMEOUT_EXIT_CODE, True)
    assert process.calls == [
        ("wait", 1.0),
        ("terminate", None),
        ("wait", runtime.CHILD_TERMINATE_TIMEOUT_S),
        ("kill", None),
        ("wait", runtime.CHILD_KILL_TIMEOUT_S),
    ]
    assert supervisor.scanner is None
    state = json.loads(runtime.runtime_state_path(tmp_path).read_text(encoding="utf-8"))
    assert state["runtime_state"] == "SCANNER_FAILED"
    assert state["readiness"] is False
    assert state["scanner_enabled"] is False
    assert state["publication_enabled"] is False
    assert state["blocking_reason"] == "scanner_timeout"
    assert state["scanner_last_failure"]


@pytest.mark.parametrize("value", ["bad", "0", "-1", "nan", "inf"])
def test_malformed_scanner_timeout_configuration_fails_closed(tmp_path, monkeypatch, value):
    monkeypatch.setenv("H011_SCANNER_TIMEOUT_S", value)
    supervisor = runtime.RuntimeSupervisor(tmp_path)
    assert supervisor.scanner_timeout_s is None
