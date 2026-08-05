"""Hostile process-crash audit for the H-011 Phase II-A publisher."""
from __future__ import annotations

import hashlib
import multiprocessing
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "polymarket"))

import h011_v3_raw_transaction as rt
from h011_v3_raw_transaction import (
    MarkerValidationPolicy,
    PublishTransactionFailure,
    RawChainLock,
    RawScanStager,
    RecoveryRequiredError,
    canonical_payload_sha256,
    parse_marker,
    publish_raw_scan,
    validate_marker,
)


CREATED_AT = "2026-07-17T19:00:00Z"
CRASH_EXIT_CODE = 91
FORK_AVAILABLE = "fork" in multiprocessing.get_all_start_methods()


@pytest.fixture(autouse=True)
def _isolation():
    yield
    rt.set_fault_injection_hook(None)
    with rt._ACTIVE_GUARDS_LOCK:
        guards = [record.guard for record in rt._ACTIVE_GUARDS.values()]
    errors: list[BaseException] = []
    for guard in guards:
        try:
            guard.close()
        except BaseException as exc:  # pragma: no cover - fixture reports it
            errors.append(exc)
    with rt._ACTIVE_GUARDS_LOCK:
        assert not rt._ACTIVE_GUARDS
        assert not rt._CHAIN_RESERVATIONS
    assert not errors


@pytest.fixture
def raw_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "raw"
    directory.mkdir()
    return directory


@pytest.fixture
def policy() -> MarkerValidationPolicy:
    return _policy()


def _policy() -> MarkerValidationPolicy:
    return MarkerValidationPolicy(
        manifest_prefix="manifest",
        artifact_filename_pattern=re.compile(
            r"^raw_scan_[A-Za-z0-9_.-]+_[0-9a-f]{12}\.events\.jsonl\.gz$"
        ),
    )


def _event(cid: str = "0xabc", value: int = 1) -> dict[str, Any]:
    payload = [{"price": 0.5, "size": value}]
    return {
        "received_at_utc": "2026-07-17T18:59:00Z",
        "source": "polymarket_data_api",
        "endpoint": "/trades",
        "request_params": {"market": cid},
        "requested_condition_id": cid,
        "payload": payload,
        "payload_sha256": canonical_payload_sha256(payload),
        "cohort_id": "300s",
        "schema_version": "raw_trade_event_v1",
    }


def _transfer(raw_dir: Path, scan_id: str, *, value: int = 1):
    stager = RawScanStager("run-crash-audit", scan_id, raw_dir)
    stager.__enter__()
    stager.append_event(_event(value=value))
    stager.seal()
    return stager.transfer()


def _publish(raw_dir: Path, policy: MarkerValidationPolicy, transfer):
    guard = RawChainLock(raw_dir, policy.manifest_prefix).acquire()
    try:
        return publish_raw_scan(
            transfer=transfer,
            guard=guard,
            raw_directory=raw_dir,
            policy=policy,
            manifest_created_at=CREATED_AT,
        )
    finally:
        guard.close()


def _safe_scan_id(scan_id: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_." else "_"
        for character in scan_id
    )[:100]


def _final_name(scan_id: str) -> str:
    safe_id = _safe_scan_id(scan_id)
    scan_hash = hashlib.sha256(scan_id.encode("utf-8")).hexdigest()[:12]
    return f"raw_scan_{safe_id}_{scan_hash}.events.jsonl.gz"


def _marker_paths(raw_dir: Path) -> list[Path]:
    return sorted(raw_dir.glob("manifest_txn_*.marker"))


def _root_temps(raw_dir: Path) -> list[Path]:
    return sorted(
        path for path in raw_dir.iterdir()
        if path.is_file() and ".tmp." in path.name
    )


def _child_publish_crash(raw_path: str, scan_id: str, point: str) -> None:
    raw_dir = Path(raw_path)
    policy = _policy()
    transfer = _transfer(raw_dir, scan_id)
    guard = RawChainLock(raw_dir, policy.manifest_prefix).acquire()

    def crash_hook(current: str) -> None:
        if current == point:
            os._exit(CRASH_EXIT_CODE)

    rt.set_fault_injection_hook(crash_hook)
    publish_raw_scan(
        transfer=transfer,
        guard=guard,
        raw_directory=raw_dir,
        policy=policy,
        manifest_created_at=CREATED_AT,
    )
    os._exit(0)


def _run_process_crash(raw_dir: Path, scan_id: str, point: str) -> None:
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_child_publish_crash,
        args=(str(raw_dir), scan_id, point),
    )
    process.start()
    process.join(20)
    assert process.exitcode == CRASH_EXIT_CODE
    process.close()


PUBLISH_CRASH_EXPECTATIONS = {
    rt.FAULT_PUBLISH_BEFORE_STAGED_MARKER: (
        None, True, False, False, False, None,
    ),
    rt.FAULT_PUBLISH_AFTER_STAGED_MARKER: (
        "STAGED", True, False, False, False, None,
    ),
    rt.FAULT_PUBLISH_AFTER_ARTIFACT_LINK: (
        "STAGED", True, True, False, False, None,
    ),
    rt.FAULT_PUBLISH_AFTER_ARTIFACT_DIR_FSYNC: (
        "STAGED", True, True, False, False, None,
    ),
    rt.FAULT_PUBLISH_AFTER_ARTIFACT_MARKER_UPDATE: (
        "ARTIFACT_PUBLISHED", True, True, False, False, None,
    ),
    rt.FAULT_PUBLISH_AFTER_SIDECAR_LINK: (
        "ARTIFACT_PUBLISHED", True, True, True, False, "sidecar",
    ),
    rt.FAULT_PUBLISH_AFTER_SIDECAR_DIR_FSYNC: (
        "ARTIFACT_PUBLISHED", True, True, True, False, "sidecar",
    ),
    rt.FAULT_PUBLISH_AFTER_SIDECAR_MARKER_UPDATE: (
        "SIDECAR_PUBLISHED", True, True, True, False, None,
    ),
    rt.FAULT_PUBLISH_AFTER_MANIFEST_LINK: (
        "SIDECAR_PUBLISHED", True, True, True, True, "manifest",
    ),
    rt.FAULT_PUBLISH_AFTER_MANIFEST_DIR_FSYNC: (
        "SIDECAR_PUBLISHED", True, True, True, True, "manifest",
    ),
    rt.FAULT_PUBLISH_AFTER_MANIFEST_MARKER_UPDATE: (
        "MANIFEST_PUBLISHED", True, True, True, True, None,
    ),
    rt.FAULT_PUBLISH_AFTER_COMMITTED_MARKER: (
        "COMMITTED", True, True, True, True, None,
    ),
    rt.FAULT_PUBLISH_AFTER_STAGING_UNLINK: (
        "COMMITTED", False, True, True, True, None,
    ),
    rt.FAULT_PUBLISH_AFTER_PENDING_DIR_FSYNC: (
        "COMMITTED", False, True, True, True, None,
    ),
    rt.FAULT_PUBLISH_AFTER_TRANSFER_CLOSE: (
        "COMMITTED", False, True, True, True, None,
    ),
    rt.FAULT_PUBLISH_AFTER_MARKER_UNLINK: (
        None, False, True, True, True, None,
    ),
    rt.FAULT_PUBLISH_AFTER_FINAL_ROOT_FSYNC: (
        None, False, True, True, True, None,
    ),
}


@pytest.mark.skipif(not FORK_AVAILABLE, reason="requires Linux fork semantics")
@pytest.mark.parametrize("point", list(PUBLISH_CRASH_EXPECTATIONS))
def test_real_process_death_at_all_publisher_boundaries(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
    point: str,
):
    scan_id = f"crash-{point.lower()}"
    final_name = _final_name(scan_id)
    _run_process_crash(raw_dir, scan_id, point)

    expected = PUBLISH_CRASH_EXPECTATIONS[point]
    status, staging, artifact, sidecar, manifest, temp_kind = expected
    pending = raw_dir / ".pending"
    staging_paths = list(pending.glob(f"raw_scan_{_safe_scan_id(scan_id)}_*.tmp"))

    assert bool(staging_paths) is staging
    assert (raw_dir / final_name).exists() is artifact
    assert (raw_dir / f"{final_name}.sha256").exists() is sidecar
    assert (raw_dir / "manifest_000000.json").exists() is manifest

    marker_paths = _marker_paths(raw_dir)
    if status is None:
        assert marker_paths == []
    else:
        assert len(marker_paths) == 1
        marker = parse_marker(marker_paths[0].read_bytes())
        validate_marker(marker, policy)
        assert marker["status"] == status
        assert marker["final_name"] == final_name
        if staging:
            staging_stat = staging_paths[0].stat()
            assert (
                marker["device_id"], marker["inode"], marker["size_bytes"]
            ) == (
                staging_stat.st_dev, staging_stat.st_ino, staging_stat.st_size
            )

    root_temps = _root_temps(raw_dir)
    if temp_kind is None:
        assert root_temps == []
    elif temp_kind == "sidecar":
        assert len(root_temps) == 1
        assert root_temps[0].name.startswith(f".{final_name}.sha256.tmp.")
    else:
        assert len(root_temps) == 1
        assert root_temps[0].name.startswith(".manifest_000000.json.tmp.")

    # Kernel process teardown must release flock even though no finally block ran.
    guard = RawChainLock(raw_dir, policy.manifest_prefix).acquire()
    guard.close()


MARKER_CREATE_CRASH_EXPECTATIONS = {
    rt.FAULT_CREATE_AFTER_FINAL_LINK: True,
    rt.FAULT_CREATE_AFTER_TEMP_UNLINK: False,
    rt.FAULT_CREATE_AFTER_DIR_FSYNC: False,
}


@pytest.mark.skipif(not FORK_AVAILABLE, reason="requires Linux fork semantics")
@pytest.mark.parametrize("point", list(MARKER_CREATE_CRASH_EXPECTATIONS))
def test_real_process_death_inside_marker_creation_preserves_evidence(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
    point: str,
):
    scan_id = f"marker-{point.lower()}"
    _run_process_crash(raw_dir, scan_id, point)

    marker_paths = _marker_paths(raw_dir)
    assert len(marker_paths) == 1
    marker = parse_marker(marker_paths[0].read_bytes())
    validate_marker(marker, policy)
    assert marker["status"] == "STAGED"
    assert (raw_dir / ".pending" / marker["staging_filename"]).is_file()
    assert not (raw_dir / marker["final_name"]).exists()

    marker_temps = [
        path for path in _root_temps(raw_dir)
        if path.name.startswith(marker_paths[0].name + ".tmp.")
    ]
    assert bool(marker_temps) is MARKER_CREATE_CRASH_EXPECTATIONS[point]


def test_orphan_canonical_marker_temp_blocks_new_publication(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
):
    temp_name = (
        "manifest_txn_000000_11111111-1111-4111-8111-111111111111"
        ".marker.tmp." + "a" * 32
    )
    residue = raw_dir / temp_name
    residue.write_bytes(b"durable-marker-temp-residue")
    residue.chmod(0o444)
    transfer = _transfer(raw_dir, "blocked-by-marker-temp")

    with RawChainLock(raw_dir, policy.manifest_prefix).acquire() as guard:
        with pytest.raises(RecoveryRequiredError):
            publish_raw_scan(
                transfer=transfer,
                guard=guard,
                raw_directory=raw_dir,
                policy=policy,
                manifest_created_at=CREATED_AT,
            )

    assert residue.read_bytes() == b"durable-marker-temp-residue"
    assert transfer._closed is False
    assert _marker_paths(raw_dir) == []
    assert not (raw_dir / transfer.sealed.final_name).exists()
    transfer.close()


def test_marker_temp_source_symlink_race_is_not_silently_committed(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
    monkeypatch,
):
    transfer = _transfer(raw_dir, "marker-source-symlink-race")
    victim = raw_dir / "attacker-marker-source"
    victim_bytes = b"attacker-controlled-marker-source"
    victim.write_bytes(victim_bytes)
    real_link = os.link
    raced = False
    reached_after_staged = False

    def racing_link(src, dst, *args, **kwargs):
        nonlocal raced
        if not raced and isinstance(dst, str) and dst.endswith(".marker"):
            raced = True
            os.unlink(src, dir_fd=kwargs["src_dir_fd"])
            os.symlink(
                victim.name,
                src,
                dir_fd=kwargs["src_dir_fd"],
            )
        return real_link(src, dst, *args, **kwargs)

    def observe_hook(point: str) -> None:
        nonlocal reached_after_staged
        if point == rt.FAULT_PUBLISH_AFTER_STAGED_MARKER:
            reached_after_staged = True

    monkeypatch.setattr(os, "link", racing_link)
    rt.set_fault_injection_hook(observe_hook)

    with pytest.raises(PublishTransactionFailure):
        _publish(raw_dir, policy, transfer)

    assert raced is True
    assert reached_after_staged is False
    assert victim.read_bytes() == victim_bytes
    marker_paths = _marker_paths(raw_dir)
    assert len(marker_paths) == 1
    assert marker_paths[0].is_symlink()


def _child_hold_lock_then_crash(raw_path: str, ready_path: str) -> None:
    raw_dir = Path(raw_path)
    policy = _policy()
    transfer = _transfer(raw_dir, "crash-lock-holder")
    guard = RawChainLock(raw_dir, policy.manifest_prefix).acquire()

    def crash_hook(point: str) -> None:
        if point == rt.FAULT_PUBLISH_AFTER_STAGED_MARKER:
            ready = Path(ready_path)
            ready.write_text("ready", encoding="utf-8")
            with ready.open("rb") as handle:
                os.fsync(handle.fileno())
            time.sleep(1.0)
            os._exit(CRASH_EXIT_CODE)

    rt.set_fault_injection_hook(crash_hook)
    publish_raw_scan(
        transfer=transfer,
        guard=guard,
        raw_directory=raw_dir,
        policy=policy,
        manifest_created_at=CREATED_AT,
    )
    os._exit(0)


def _child_waiting_publisher(raw_path: str) -> None:
    raw_dir = Path(raw_path)
    policy = _policy()
    transfer = _transfer(raw_dir, "waiting-publisher")
    try:
        guard = RawChainLock(raw_dir, policy.manifest_prefix).acquire()
        try:
            publish_raw_scan(
                transfer=transfer,
                guard=guard,
                raw_directory=raw_dir,
                policy=policy,
                manifest_created_at=CREATED_AT,
            )
        finally:
            guard.close()
    except RecoveryRequiredError:
        os._exit(0)
    except BaseException:
        os._exit(3)
    os._exit(4)


@pytest.mark.skipif(not FORK_AVAILABLE, reason="requires Linux fork semantics")
def test_waiting_process_acquires_released_lock_and_blocks_on_crash_marker(
    raw_dir: Path,
    tmp_path: Path,
):
    context = multiprocessing.get_context("fork")
    ready = tmp_path / "crash-holder-ready"
    holder = context.Process(
        target=_child_hold_lock_then_crash,
        args=(str(raw_dir), str(ready)),
    )
    holder.start()
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists()

    waiter = context.Process(
        target=_child_waiting_publisher,
        args=(str(raw_dir),),
    )
    waiter.start()
    time.sleep(0.2)
    assert waiter.is_alive(), "waiter should still be blocked on the held flock"

    holder.join(10)
    waiter.join(10)
    assert holder.exitcode == CRASH_EXIT_CODE
    assert waiter.exitcode == 0
    holder.close()
    waiter.close()

    marker_paths = _marker_paths(raw_dir)
    assert len(marker_paths) == 1
    assert parse_marker(marker_paths[0].read_bytes())["status"] == "STAGED"
    assert not (raw_dir / "manifest_000000.json").exists()


def test_final_artifacts_are_regular_read_only_files(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
):
    transfer = _transfer(raw_dir, "read-only-finals")
    final_name = transfer.sealed.final_name
    result = _publish(raw_dir, policy, transfer)
    assert result.status == "PUBLISHED"

    for path in (
        raw_dir / final_name,
        raw_dir / f"{final_name}.sha256",
        raw_dir / "manifest_000000.json",
    ):
        mode = path.stat().st_mode
        assert stat.S_ISREG(mode)
        assert stat.S_IMODE(mode) == 0o444
