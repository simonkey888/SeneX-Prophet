"""Controlled SENEX / SENECIO H-011 V3 runtime supervisor.

Startup recovery and committed-chain verification happen before the scanner is
allowed to run. There is no fallback to the legacy raw writer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

try:
    import h011_v3_raw_transaction as rt
    from h011_v3_raw_recovery import (
        RecoveryBlockedError,
        RecoveryInterruptedError,
        recover_raw_scan_transaction,
    )
    from h011_v3_committed_snapshot import (
        CommittedChainError,
        validate_committed_chain_under_lock,
    )
except ModuleNotFoundError:  # package imports used by tests
    from polymarket import h011_v3_raw_transaction as rt  # type: ignore
    from polymarket.h011_v3_raw_recovery import (  # type: ignore
        RecoveryBlockedError,
        RecoveryInterruptedError,
        recover_raw_scan_transaction,
    )
    from polymarket.h011_v3_committed_snapshot import (  # type: ignore
        CommittedChainError,
        validate_committed_chain_under_lock,
    )

RUNTIME_STATES: Final[frozenset[str]] = frozenset({
    "STARTING",
    "RECOVERING",
    "RUNNING",
    "DEGRADED",
    "BLOCKED_RAW_INTEGRITY",
    "BLOCKED_STORAGE_UNVERIFIED",
    "SCANNER_FAILED",
    "STOPPING",
})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_real_directory(path: Path) -> None:
    """Create a directory hierarchy without accepting symlink components."""
    path = path.absolute()
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for existing in (cursor, *reversed(missing)):
        if existing in missing:
            existing.mkdir(mode=0o755)
            _fsync_directory(existing.parent)
        st = os.lstat(existing)
        if not os.path.isdir(existing) or os.path.islink(existing):
            raise rt.PathSafetyError(f"storage directory is not a real directory: {existing}")
        if not (st.st_mode & 0o400):
            raise PermissionError(f"storage directory is not readable: {existing}")


def results_root_from_env() -> Path:
    return Path(os.environ.get("H011_RESULTS_DIR", "/app/polymarket/results"))


def raw_chain_dir(results_root: Path) -> Path:
    return results_root / "h011_v3" / "raw_chain_v1"


def runtime_state_path(results_root: Path) -> Path:
    return results_root / "h011_v3" / "runtime_state.json"


@dataclass
class RuntimeState:
    runtime_state: str = "STARTING"
    updated_at: str = field(default_factory=utc_now)
    started_at: str = field(default_factory=utc_now)
    readiness: bool = False
    liveness: bool = True
    scanner_enabled: bool = False
    publication_enabled: bool = False
    recovery_status: str = "NOT_STARTED"
    storage_status: str = "NOT_CHECKED"
    chain_verified: bool = False
    current_sequence: int | None = None
    manifest_hash: str | None = None
    scanner_last_start: str | None = None
    scanner_last_success: str | None = None
    scanner_last_failure: str | None = None
    blocking_reason: str | None = None
    last_error: str | None = None
    paper_only: bool = True
    orders_enabled: bool = False
    live_capital_locked: bool = True
    legacy_mode: bool = False

    def to_dict(self) -> dict[str, Any]:
        if self.runtime_state not in RUNTIME_STATES:
            raise ValueError(f"invalid runtime state: {self.runtime_state}")
        return {
            "schema_version": "h011-v3-runtime-state-v1",
            **self.__dict__,
        }


class RuntimeStateStore:
    def __init__(self, results_root: Path):
        self.results_root = results_root
        self.path = runtime_state_path(results_root)
        self.state = RuntimeState()

    def write(self, **changes: Any) -> dict[str, Any]:
        for key, value in changes.items():
            if not hasattr(self.state, key):
                raise KeyError(f"unknown runtime-state field: {key}")
            setattr(self.state, key, value)
        self.state.updated_at = utc_now()
        payload = self.state.to_dict()
        parent = self.path.parent
        _ensure_real_directory(parent)
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        temp = parent / f".{self.path.name}.tmp.{uuid.uuid4().hex}"
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
        try:
            view = memoryview(encoded)
            offset = 0
            while offset < len(view):
                written = os.write(fd, view[offset:])
                if written <= 0:
                    raise OSError("short write while publishing runtime state")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, self.path)
        _fsync_directory(parent)
        return payload


def prepare_storage(results_root: Path) -> Path:
    _ensure_real_directory(results_root)
    chain = raw_chain_dir(results_root)
    _ensure_real_directory(chain)
    # Opening as trusted proves O_NOFOLLOW, lstat/fstat identity, and directory access.
    trusted = rt.open_trusted_directory(chain)
    trusted.close()
    return chain


def startup_recovery(
    *,
    results_root: Path,
    store: RuntimeStateStore | None = None,
    policy: rt.MarkerValidationPolicy = rt.DEFAULT_MARKER_POLICY,
) -> dict[str, Any]:
    """Recover pending publication and prove steady state before scans."""
    store = store or RuntimeStateStore(results_root)
    store.write(
        runtime_state="STARTING",
        readiness=False,
        scanner_enabled=False,
        publication_enabled=False,
        recovery_status="NOT_STARTED",
        storage_status="CHECKING",
        chain_verified=False,
        blocking_reason=None,
        last_error=None,
    )
    try:
        chain = prepare_storage(results_root)
    except Exception as exc:
        return store.write(
            runtime_state="BLOCKED_STORAGE_UNVERIFIED",
            storage_status="FAILED",
            recovery_status="NOT_RUN",
            blocking_reason="storage_root_untrusted",
            last_error=f"{type(exc).__name__}: {exc}",
        )

    store.write(runtime_state="RECOVERING", storage_status="BASIC_PRIMITIVES_OK", recovery_status="RUNNING")
    try:
        with rt.RawChainLock(chain, policy.manifest_prefix).acquire() as guard:
            recovery = recover_raw_scan_transaction(
                guard=guard,
                raw_directory=chain,
                policy=policy,
            )
            committed = validate_committed_chain_under_lock(
                guard=guard,
                raw_directory=chain,
                policy=policy,
            )
    except RecoveryInterruptedError as exc:
        return store.write(
            runtime_state="DEGRADED",
            readiness=False,
            scanner_enabled=False,
            publication_enabled=False,
            recovery_status="INTERRUPTED_RETRY_REQUIRED",
            chain_verified=False,
            blocking_reason="recovery_interrupted",
            last_error=f"{type(exc).__name__}: {exc}",
        )
    except (RecoveryBlockedError, CommittedChainError, rt.RawTransactionError) as exc:
        return store.write(
            runtime_state="BLOCKED_RAW_INTEGRITY",
            readiness=False,
            scanner_enabled=False,
            publication_enabled=False,
            recovery_status="BLOCKED",
            chain_verified=False,
            blocking_reason="raw_chain_integrity",
            last_error=f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:
        return store.write(
            runtime_state="BLOCKED_STORAGE_UNVERIFIED",
            readiness=False,
            scanner_enabled=False,
            publication_enabled=False,
            recovery_status="FAILED",
            chain_verified=False,
            blocking_reason="storage_operation_failed",
            last_error=f"{type(exc).__name__}: {exc}",
        )

    chain_view = committed.to_dict()
    return store.write(
        runtime_state="RUNNING",
        readiness=True,
        scanner_enabled=True,
        publication_enabled=True,
        recovery_status=recovery.status,
        storage_status="READY_CODE_VALIDATED",
        chain_verified=True,
        current_sequence=chain_view.get("current_sequence"),
        manifest_hash=chain_view.get("manifest_hash"),
        blocking_reason=None,
        last_error=None,
    )


def synthetic_event(run_id: str, scan_id: str) -> dict[str, Any]:
    payload = {
        "synthetic": True,
        "purpose": "hostile_crash_restart_validation",
        "run_id": run_id,
        "scan_id": scan_id,
    }
    return {
        "received_at_utc": utc_now(),
        "source": "senex_synthetic_validation",
        "endpoint": "internal://synthetic-publish",
        "request_params": {},
        "requested_condition_id": "synthetic-condition",
        "payload": payload,
        "payload_sha256": rt.canonical_payload_sha256(payload),
        "cohort_id": "h011-v3-w300-vwap-structure-v2",
        "schema_version": "h011-raw-envelope-v1",
    }


def _publish_synthetic_snapshot(results_root: Path, chain_view: dict[str, Any]) -> None:
    """Publish a deterministic derived cache for isolated runtime validation."""
    try:
        from control_plane import coverage, state_snapshot
    except ModuleNotFoundError:
        from polymarket.control_plane import coverage, state_snapshot  # type: ignore
    state_snapshot.SNAPSHOT_DIR = results_root / "v3" / "state"
    results = [
        {
            "invariant_id": item["id"],
            "status": "PASS",
            "severity": item["severity"],
            "reason": "synthetic runtime validation evidence",
            "evidence": {"synthetic": True},
        }
        for item in coverage.get_catalog()
    ]
    invariants = {
        "summary": coverage.invariant_summary(results),
        "results": results,
        "catalog_version": coverage.CATALOG_VERSION,
        "catalog_hash": coverage.invariant_catalog_hash(),
    }
    snapshot = state_snapshot.build_snapshot(
        scan_id=str(chain_view.get("scan_id")),
        run_id=str(chain_view.get("run_id")),
        pipeline_version="h011-integrity-v3",
        cohort_id="h011-v3-w300-vwap-structure-v2",
        window_s=300,
        estimator="vwap",
        code_sha=os.environ.get("SENECIO_CODE_SHA", "synthetic-validation"),
        config_sha=hashlib.sha256(b"synthetic-paper-only-config").hexdigest(),
        scan_status="COMPLETE_VALIDATED",
        source_health={"synthetic_validation": {"status": "HEALTHY", "latency_ms": 0, "age_ms": 0}},
        funnel={"discovered": 0, "rejected": 0, "shadow_executable": 0},
        market_records=[],
        invariants=invariants,
        alerts=[],
        aggregate_metrics={"raw_chain": chain_view, "synthetic_validation": True},
    )
    state_snapshot.save_snapshot(snapshot)


def synthetic_publish(
    *,
    results_root: Path,
    run_id: str,
    scan_id: str,
    fault_point: str | None = None,
) -> dict[str, Any]:
    """Publish one synthetic scan; used only by isolated crash/restart tests."""
    state = startup_recovery(results_root=results_root)
    if not state.get("publication_enabled"):
        raise RuntimeError(f"publication is not enabled: {state}")
    chain = raw_chain_dir(results_root)

    if fault_point:
        def crash(point: str) -> None:
            if point == fault_point:
                os._exit(99)
        rt.set_fault_injection_hook(crash)
    try:
        with rt.RawScanStager(run_id=run_id, scan_id=scan_id, raw_dir=chain) as stager:
            stager.append_event(synthetic_event(run_id, scan_id))
            stager.seal()
            transfer = stager.transfer()
            with rt.RawChainLock(chain, rt.DEFAULT_MARKER_POLICY.manifest_prefix).acquire() as guard:
                recover_raw_scan_transaction(
                    guard=guard, raw_directory=chain, policy=rt.DEFAULT_MARKER_POLICY
                )
                result = rt.publish_raw_scan(
                    transfer=transfer,
                    guard=guard,
                    raw_directory=chain,
                    policy=rt.DEFAULT_MARKER_POLICY,
                    manifest_created_at=utc_now(),
                )
                committed = validate_committed_chain_under_lock(
                    guard=guard,
                    raw_directory=chain,
                    policy=rt.DEFAULT_MARKER_POLICY,
                )
        chain_view = committed.to_dict()
        _publish_synthetic_snapshot(results_root, chain_view)
        return {"publish_status": result.status, "chain": chain_view}
    finally:
        rt.set_fault_injection_hook(None)


class RuntimeSupervisor:
    def __init__(self, results_root: Path, interval_s: int = 300):
        self.results_root = results_root
        self.interval_s = interval_s
        self.store = RuntimeStateStore(results_root)
        self.stop_requested = False
        self.dashboard: subprocess.Popen[bytes] | None = None
        self.scanner: subprocess.Popen[bytes] | None = None

    def _signal(self, signum: int, _frame: Any) -> None:
        self.stop_requested = True
        self.store.write(runtime_state="STOPPING", readiness=False, scanner_enabled=False, publication_enabled=False)
        for child in (self.scanner, self.dashboard):
            if child is not None and child.poll() is None:
                child.send_signal(signum)

    def _launch_dashboard(self) -> None:
        env = os.environ.copy()
        env["H011_RESULTS_DIR"] = str(self.results_root)
        self.dashboard = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("dashboard_v3.py"))],
            env=env,
        )

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._signal)
        signal.signal(signal.SIGINT, self._signal)
        state = startup_recovery(results_root=self.results_root, store=self.store)
        self._launch_dashboard()
        if os.environ.get("H011_RUNTIME_DIAGNOSTIC_ONLY", "false").lower() == "true" and state.get("scanner_enabled"):
            self.store.write(
                runtime_state="DEGRADED", readiness=True, scanner_enabled=False,
                publication_enabled=False, blocking_reason="diagnostic_only_mode",
            )
            while not self.stop_requested and self.dashboard.poll() is None:
                time.sleep(1)
            return 0
        if not state.get("scanner_enabled"):
            # Keep diagnostics reachable but never scan or publish.
            while not self.stop_requested and self.dashboard.poll() is None:
                time.sleep(1)
            return 2

        scanner_command = [
            sys.executable,
            str(Path(__file__).with_name("vwap_detector_v2.py")),
            "--pipeline", "integrity-v3",
            "--mode", "scan",
            "--window", "300",
            "--max-markets", "10",
            "--gamma-limit", "3000",
        ]
        while not self.stop_requested:
            if self.dashboard.poll() is not None:
                self.store.write(
                    runtime_state="SCANNER_FAILED",
                    readiness=False,
                    scanner_enabled=False,
                    publication_enabled=False,
                    scanner_last_failure=utc_now(),
                    last_error=f"dashboard exited with {self.dashboard.returncode}",
                )
                return 3
            started = utc_now()
            self.store.write(runtime_state="RUNNING", scanner_last_start=started)
            env = os.environ.copy()
            env["H011_RESULTS_DIR"] = str(self.results_root)
            self.scanner = subprocess.Popen(scanner_command, env=env)
            return_code = self.scanner.wait()
            self.scanner = None
            if self.stop_requested:
                break
            if return_code != 0:
                self.store.write(
                    runtime_state="SCANNER_FAILED",
                    readiness=False,
                    scanner_enabled=False,
                    publication_enabled=False,
                    scanner_last_failure=utc_now(),
                    last_error=f"scanner exited with {return_code}",
                )
                return return_code
            try:
                chain = prepare_storage(self.results_root)
                with rt.RawChainLock(chain, rt.DEFAULT_MARKER_POLICY.manifest_prefix).acquire() as guard:
                    committed = validate_committed_chain_under_lock(
                        guard=guard, raw_directory=chain, policy=rt.DEFAULT_MARKER_POLICY
                    )
                chain_view = committed.to_dict()
            except Exception as exc:
                self.store.write(
                    runtime_state="BLOCKED_RAW_INTEGRITY", readiness=False,
                    scanner_enabled=False, publication_enabled=False,
                    scanner_last_failure=utc_now(), blocking_reason="post_scan_chain_verification",
                    last_error=f"{type(exc).__name__}: {exc}",
                )
                return 4
            self.store.write(
                runtime_state="RUNNING",
                readiness=True,
                scanner_enabled=True,
                publication_enabled=True,
                scanner_last_success=utc_now(),
                current_sequence=chain_view.get("current_sequence"),
                manifest_hash=chain_view.get("manifest_hash"),
                chain_verified=True,
                last_error=None,
            )
            deadline = time.monotonic() + self.interval_s
            while not self.stop_requested and time.monotonic() < deadline:
                if self.dashboard.poll() is not None:
                    break
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

        self.store.write(runtime_state="STOPPING", readiness=False, scanner_enabled=False, publication_enabled=False)
        if self.dashboard is not None and self.dashboard.poll() is None:
            self.dashboard.terminate()
            try:
                self.dashboard.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.dashboard.kill()
                self.dashboard.wait(timeout=5)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="SENEX H-011 V3 controlled runtime")
    parser.add_argument("--results-root", type=Path, default=results_root_from_env())
    parser.add_argument("--startup-check", action="store_true")
    parser.add_argument("--synthetic-publish", action="store_true")
    parser.add_argument("--run-id", default=f"synthetic-run-{uuid.uuid4()}")
    parser.add_argument("--scan-id", default=f"synthetic-scan-{uuid.uuid4()}")
    parser.add_argument("--fault-point")
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()

    # Permanent safety assertions.
    assert os.environ.get("H011_ORDERS_ENABLED", "false").lower() != "true"
    if args.startup_check:
        state = startup_recovery(results_root=args.results_root)
        print(json.dumps(state, sort_keys=True))
        return 0 if state.get("chain_verified") else 2
    if args.synthetic_publish:
        result = synthetic_publish(
            results_root=args.results_root,
            run_id=args.run_id,
            scan_id=args.scan_id,
            fault_point=args.fault_point,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    return RuntimeSupervisor(args.results_root, args.interval).run()


if __name__ == "__main__":
    raise SystemExit(main())
