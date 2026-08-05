"""Phase II-A tests for the transactional H-011 raw scan publisher."""
from __future__ import annotations

import errno
import hashlib
import json
import multiprocessing
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "polymarket"))

import h011_v3_raw_transaction as rt
from h011_v3_raw_transaction import (
    MarkerValidationError,
    MarkerValidationPolicy,
    PublishCleanupPending,
    PublishPostCommitNotificationError,
    PublishTransactionFailure,
    RawChainLock,
    RawScanStager,
    RecoveryRequiredError,
    canonical_json_bytes,
    canonical_payload_sha256,
    compute_manifest_hash,
    parse_marker,
    publish_raw_scan,
    validate_marker,
)


CREATED_AT = "2026-07-14T12:00:00Z"


@pytest.fixture(autouse=True)
def _publisher_isolation():
    yield
    rt.set_fault_injection_hook(None)
    with rt._ACTIVE_GUARDS_LOCK:
        guards = [record.guard for record in rt._ACTIVE_GUARDS.values()]
    errors = []
    for guard in guards:
        try:
            guard.close()
        except Exception as exc:  # pragma: no cover - fixture reports it
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
    return MarkerValidationPolicy(
        manifest_prefix="manifest",
        artifact_filename_pattern=re.compile(
            r"^raw_scan_[A-Za-z0-9_.-]+_[0-9a-f]{12}\.events\.jsonl\.gz$"
        ),
    )


def _event(cid: str = "0xabc", value: int = 1) -> dict[str, Any]:
    payload = [{"price": 0.5, "size": value}]
    return {
        "received_at_utc": "2026-07-14T11:59:00Z",
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
    with RawScanStager("run-ii-a", scan_id, raw_dir) as stager:
        stager.append_event(_event(value=value))
        stager.seal()
        return stager.transfer()


def _publish(raw_dir: Path, policy: MarkerValidationPolicy, transfer):
    with RawChainLock(raw_dir, policy.manifest_prefix).acquire() as guard:
        return publish_raw_scan(
            transfer=transfer,
            guard=guard,
            raw_directory=raw_dir,
            policy=policy,
            manifest_created_at=CREATED_AT,
        )


def _manifest(raw_dir: Path, sequence: int) -> dict[str, Any]:
    return json.loads((raw_dir / f"manifest_{sequence:06d}.json").read_bytes())


def _markers(raw_dir: Path) -> list[Path]:
    return list(raw_dir.glob("manifest_txn_*.marker"))


def _temps(raw_dir: Path) -> list[Path]:
    return [path for path in raw_dir.iterdir() if ".tmp." in path.name]


def test_happy_path_sequence_zero_exact_bytes_and_cleanup(
    raw_dir: Path, policy: MarkerValidationPolicy,
):
    transfer = _transfer(raw_dir, "scan-zero")
    sealed = transfer.sealed
    expected_artifact = (raw_dir / ".pending" / sealed.staging_filename).read_bytes()

    result = _publish(raw_dir, policy, transfer)

    assert result.status == "PUBLISHED"
    assert result.manifest_entry is not None
    assert result.manifest_entry["sequence"] == 0
    assert result.manifest_entry["previous_manifest_hash"] is None
    assert (raw_dir / sealed.final_name).read_bytes() == expected_artifact
    assert hashlib.sha256(expected_artifact).hexdigest() == sealed.file_sha256
    assert (raw_dir / f"{sealed.final_name}.sha256").read_bytes() == (
        f"{sealed.file_sha256}  {sealed.final_name}\n".encode("ascii")
    )
    manifest_bytes = (raw_dir / "manifest_000000.json").read_bytes()
    assert manifest_bytes == canonical_json_bytes(result.manifest_entry)
    assert compute_manifest_hash(result.manifest_entry) == result.manifest_entry["manifest_hash"]
    assert transfer._closed is True
    assert transfer.staging_fd == -1
    assert not (raw_dir / ".pending" / sealed.staging_filename).exists()
    assert not _markers(raw_dir)
    assert not _temps(raw_dir)


def test_happy_path_sequence_n_has_contiguous_previous_hash(
    raw_dir: Path, policy: MarkerValidationPolicy,
):
    first = _publish(raw_dir, policy, _transfer(raw_dir, "scan-one", value=1))
    second = _publish(raw_dir, policy, _transfer(raw_dir, "scan-two", value=2))
    assert second.manifest_entry["sequence"] == 1
    assert second.manifest_entry["previous_manifest_hash"] == first.manifest_entry["manifest_hash"]
    assert [_manifest(raw_dir, i)["sequence"] for i in range(2)] == [0, 1]


def test_marker_is_removed_after_staging_and_transfer_close(
    raw_dir: Path, policy: MarkerValidationPolicy, monkeypatch,
):
    transfer = _transfer(raw_dir, "marker-last")
    staging = transfer.sealed.staging_filename
    order: list[tuple[str, str, bool]] = []
    real_unlink = os.unlink

    def tracing_unlink(name, *args, **kwargs):
        if name == staging:
            marker_paths = _markers(raw_dir)
            assert len(marker_paths) == 1
            assert parse_marker(marker_paths[0].read_bytes())["status"] == "COMMITTED"
        if isinstance(name, str) and name.endswith(".marker"):
            assert not (raw_dir / ".pending" / staging).exists()
            assert (raw_dir / transfer.sealed.final_name).is_file()
            assert (raw_dir / f"{transfer.sealed.final_name}.sha256").is_file()
            assert (raw_dir / "manifest_000000.json").is_file()
        result = real_unlink(name, *args, **kwargs)
        if name == staging or (isinstance(name, str) and name.endswith(".marker")):
            order.append(("unlink", name, transfer._closed))
        return result

    monkeypatch.setattr(os, "unlink", tracing_unlink)
    _publish(raw_dir, policy, transfer)
    assert order[0] == ("unlink", staging, False)
    assert order[-1][1].endswith(".marker")
    assert order[-1][2] is True


FAULT_EXPECTATIONS = {
    rt.FAULT_PUBLISH_BEFORE_STAGED_MARKER: (None, False, True, False, False, False),
    rt.FAULT_PUBLISH_AFTER_STAGED_MARKER: ("STAGED", True, True, False, False, False),
    rt.FAULT_PUBLISH_AFTER_ARTIFACT_LINK: ("STAGED", True, True, True, False, False),
    rt.FAULT_PUBLISH_AFTER_ARTIFACT_DIR_FSYNC: ("STAGED", True, True, True, False, False),
    rt.FAULT_PUBLISH_AFTER_ARTIFACT_MARKER_UPDATE: ("ARTIFACT_PUBLISHED", True, True, True, False, False),
    rt.FAULT_PUBLISH_AFTER_SIDECAR_LINK: ("ARTIFACT_PUBLISHED", True, True, True, True, False),
    rt.FAULT_PUBLISH_AFTER_SIDECAR_DIR_FSYNC: ("ARTIFACT_PUBLISHED", True, True, True, True, False),
    rt.FAULT_PUBLISH_AFTER_SIDECAR_MARKER_UPDATE: ("SIDECAR_PUBLISHED", True, True, True, True, False),
    rt.FAULT_PUBLISH_AFTER_MANIFEST_LINK: ("SIDECAR_PUBLISHED", True, True, True, True, True),
    rt.FAULT_PUBLISH_AFTER_MANIFEST_DIR_FSYNC: ("SIDECAR_PUBLISHED", True, True, True, True, True),
    rt.FAULT_PUBLISH_AFTER_MANIFEST_MARKER_UPDATE: ("MANIFEST_PUBLISHED", True, True, True, True, True),
    rt.FAULT_PUBLISH_AFTER_COMMITTED_MARKER: ("COMMITTED", True, True, True, True, True),
    rt.FAULT_PUBLISH_AFTER_STAGING_UNLINK: ("COMMITTED", True, False, True, True, True),
    rt.FAULT_PUBLISH_AFTER_PENDING_DIR_FSYNC: ("COMMITTED", True, False, True, True, True),
    rt.FAULT_PUBLISH_AFTER_TRANSFER_CLOSE: ("COMMITTED", True, False, True, True, True),
    rt.FAULT_PUBLISH_AFTER_MARKER_UNLINK: ("COMMITTED", True, False, True, True, True),
    rt.FAULT_PUBLISH_AFTER_FINAL_ROOT_FSYNC: ("COMMITTED", True, False, True, True, True),
}


@pytest.mark.parametrize("point", list(FAULT_EXPECTATIONS))
def test_each_durable_fault_point_has_truthful_filesystem_snapshot(
    raw_dir: Path, policy: MarkerValidationPolicy, point: str,
):
    transfer = _transfer(raw_dir, f"fault-{point.lower()}")
    sealed = transfer.sealed
    rt.set_fault_injection_hook(
        lambda current: (_ for _ in ()).throw(RuntimeError(point))
        if current == point else None
    )
    expected = FAULT_EXPECTATIONS[point]
    error_type = (
        PublishPostCommitNotificationError
        if point == rt.FAULT_PUBLISH_AFTER_FINAL_ROOT_FSYNC
        else PublishTransactionFailure
    )
    with RawChainLock(raw_dir, policy.manifest_prefix).acquire() as guard:
        with pytest.raises(error_type) as raised:
            publish_raw_scan(
                transfer=transfer,
                guard=guard,
                raw_directory=raw_dir,
                policy=policy,
                manifest_created_at=CREATED_AT,
            )
    rt.set_fault_injection_hook(None)
    error = raised.value
    status, consumed, staging, artifact, sidecar, manifest = expected
    assert error.durable_marker_status == status
    assert error.transfer_consumed is consumed
    assert (raw_dir / ".pending" / sealed.staging_filename).exists() is staging
    assert (raw_dir / sealed.final_name).exists() is artifact
    assert (raw_dir / f"{sealed.final_name}.sha256").exists() is sidecar
    assert (raw_dir / "manifest_000000.json").exists() is manifest
    marker_paths = _markers(raw_dir)
    if point in {
        rt.FAULT_PUBLISH_BEFORE_STAGED_MARKER,
        rt.FAULT_PUBLISH_AFTER_MARKER_UNLINK,
        rt.FAULT_PUBLISH_AFTER_FINAL_ROOT_FSYNC,
    }:
        assert marker_paths == []
    else:
        assert len(marker_paths) == 1
        marker = parse_marker(marker_paths[0].read_bytes())
        validate_marker(marker, policy)
        assert marker["status"] == status
    if consumed:
        assert transfer._closed is True
    else:
        assert transfer._closed is False
        transfer.close()


def test_active_marker_blocks_without_modifying_disk(
    raw_dir: Path, policy: MarkerValidationPolicy,
):
    first = _transfer(raw_dir, "active-first")
    rt.set_fault_injection_hook(
        lambda point: (_ for _ in ()).throw(RuntimeError(point))
        if point == rt.FAULT_PUBLISH_AFTER_STAGED_MARKER else None
    )
    with pytest.raises(PublishTransactionFailure):
        _publish(raw_dir, policy, first)
    rt.set_fault_injection_hook(None)
    before = {path.name: path.read_bytes() for path in raw_dir.iterdir() if path.is_file()}
    second = _transfer(raw_dir, "active-second")
    with RawChainLock(raw_dir, policy.manifest_prefix).acquire() as guard:
        with pytest.raises(RecoveryRequiredError):
            publish_raw_scan(
                transfer=second,
                guard=guard,
                raw_directory=raw_dir,
                policy=policy,
                manifest_created_at=CREATED_AT,
            )
    after = {path.name: path.read_bytes() for path in raw_dir.iterdir() if path.is_file()}
    assert after == before
    assert second._closed is False
    second.close()


def test_corrupt_marker_blocks_without_replacement(
    raw_dir: Path, policy: MarkerValidationPolicy,
):
    name = "manifest_txn_000000_11111111-1111-4111-8111-111111111111.marker"
    corrupt = b'{"corrupt":true}'
    (raw_dir / name).write_bytes(corrupt)
    transfer = _transfer(raw_dir, "corrupt-marker")
    with RawChainLock(raw_dir, policy.manifest_prefix).acquire() as guard:
        with pytest.raises(MarkerValidationError):
            publish_raw_scan(
                transfer=transfer, guard=guard, raw_directory=raw_dir,
                policy=policy, manifest_created_at=CREATED_AT,
            )
    assert (raw_dir / name).read_bytes() == corrupt
    assert transfer._closed is False
    transfer.close()


@pytest.mark.parametrize("mutation", ["hash", "gap", "previous"])
def test_corrupt_gap_or_wrong_previous_manifest_blocks(
    raw_dir: Path, policy: MarkerValidationPolicy, mutation: str,
):
    first = _publish(raw_dir, policy, _transfer(raw_dir, "chain-base"))
    if mutation == "hash":
        entry = _manifest(raw_dir, 0)
        entry["manifest_hash"] = "0" * 64
        manifest_path = raw_dir / "manifest_000000.json"
        manifest_path.chmod(0o644)
        try:
            manifest_path.write_bytes(canonical_json_bytes(entry))
        finally:
            manifest_path.chmod(0o444)
    elif mutation == "gap":
        (raw_dir / "manifest_000000.json").rename(raw_dir / "manifest_000001.json")
    else:
        entry = dict(first.manifest_entry)
        entry["sequence"] = 1
        entry["scan_id"] = "wrong-previous"
        entry["previous_manifest_hash"] = "1" * 64
        entry["manifest_hash"] = compute_manifest_hash(entry)
        (raw_dir / "manifest_000001.json").write_bytes(canonical_json_bytes(entry))
    transfer = _transfer(raw_dir, f"blocked-{mutation}")
    with RawChainLock(raw_dir, policy.manifest_prefix).acquire() as guard:
        with pytest.raises(MarkerValidationError):
            publish_raw_scan(
                transfer=transfer, guard=guard, raw_directory=raw_dir,
                policy=policy, manifest_created_at=CREATED_AT,
            )
    assert transfer._closed is False
    transfer.close()


def test_existing_artifact_is_never_overwritten(
    raw_dir: Path, policy: MarkerValidationPolicy,
):
    transfer = _transfer(raw_dir, "artifact-collision")
    destination = raw_dir / transfer.sealed.final_name
    original = b"preexisting-artifact"
    destination.write_bytes(original)
    with pytest.raises(PublishTransactionFailure) as raised:
        _publish(raw_dir, policy, transfer)
    assert destination.read_bytes() == original
    assert raised.value.durable_marker_status == "STAGED"
    assert len(_markers(raw_dir)) == 1


@pytest.mark.parametrize("target", ["sidecar", "manifest"])
def test_racing_sidecar_or_manifest_destination_is_never_overwritten(
    raw_dir: Path, policy: MarkerValidationPolicy, monkeypatch, target: str,
):
    transfer = _transfer(raw_dir, f"{target}-collision")
    sealed = transfer.sealed
    expected_name = (
        sealed.final_name + ".sha256" if target == "sidecar" else "manifest_000000.json"
    )
    original = b"racing-winner"
    real_link = os.link

    def racing_link(src, dst, *args, **kwargs):
        if dst == expected_name:
            root_fd = kwargs["dst_dir_fd"]
            winner_fd = os.open(
                expected_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o444, dir_fd=root_fd,
            )
            os.write(winner_fd, original)
            os.close(winner_fd)
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(PublishTransactionFailure):
        _publish(raw_dir, policy, transfer)
    assert (raw_dir / expected_name).read_bytes() == original


@pytest.mark.parametrize("target", ["artifact", "sidecar", "manifest"])
def test_symlink_destination_is_rejected_and_not_followed(
    raw_dir: Path, policy: MarkerValidationPolicy, monkeypatch, target: str,
):
    transfer = _transfer(raw_dir, f"symlink-{target}")
    sealed = transfer.sealed
    victim = raw_dir / "victim"
    victim.write_bytes(b"victim")
    expected_name = {
        "artifact": sealed.final_name,
        "sidecar": sealed.final_name + ".sha256",
        "manifest": "manifest_000000.json",
    }[target]
    if target == "artifact":
        (raw_dir / expected_name).symlink_to(victim)
    else:
        real_link = os.link

        def racing_link(src, dst, *args, **kwargs):
            if dst == expected_name:
                os.symlink("victim", expected_name, dir_fd=kwargs["dst_dir_fd"])
            return real_link(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(PublishTransactionFailure):
        _publish(raw_dir, policy, transfer)
    assert victim.read_bytes() == b"victim"
    assert (raw_dir / expected_name).is_symlink()


def test_short_write_preserves_marker_and_staging(
    raw_dir: Path, policy: MarkerValidationPolicy, monkeypatch,
):
    transfer = _transfer(raw_dir, "short-write")
    sealed = transfer.sealed
    real_write = os.write
    armed = False
    injected = False

    def hook(point: str):
        nonlocal armed
        if point == rt.FAULT_PUBLISH_AFTER_ARTIFACT_MARKER_UPDATE:
            armed = True

    def short_write(fd, payload):
        nonlocal armed, injected
        if armed and not injected:
            injected = True
            armed = False
            return 0
        return real_write(fd, payload)

    rt.set_fault_injection_hook(hook)
    monkeypatch.setattr(os, "write", short_write)
    with pytest.raises(PublishTransactionFailure) as raised:
        _publish(raw_dir, policy, transfer)

    assert injected is True
    assert raised.value.durable_marker_status == "ARTIFACT_PUBLISHED"
    assert raised.value.transfer_consumed is True
    assert transfer._closed is True
    assert len(_markers(raw_dir)) == 1
    marker = parse_marker(_markers(raw_dir)[0].read_bytes())
    assert marker["status"] == "ARTIFACT_PUBLISHED"
    assert (raw_dir / ".pending" / sealed.staging_filename).exists()
    assert (raw_dir / sealed.final_name).is_file()
    assert not (raw_dir / f"{sealed.final_name}.sha256").exists()
    assert not (raw_dir / "manifest_000000.json").exists()
    assert not _temps(raw_dir)


def test_partial_writes_are_completed_without_truncation(
    raw_dir: Path, policy: MarkerValidationPolicy, monkeypatch,
):
    transfer = _transfer(raw_dir, "partial-write")
    sealed = transfer.sealed
    real_write = os.write

    def partial_write(fd, payload):
        return real_write(fd, payload[:7])

    monkeypatch.setattr(os, "write", partial_write)
    result = _publish(raw_dir, policy, transfer)
    assert (raw_dir / f"{sealed.final_name}.sha256").read_bytes() == (
        f"{sealed.file_sha256}  {sealed.final_name}\n".encode("ascii")
    )
    assert (raw_dir / "manifest_000000.json").read_bytes() == canonical_json_bytes(
        result.manifest_entry
    )


def test_sidecar_file_fsync_failure_preserves_recoverable_state(
    raw_dir: Path, policy: MarkerValidationPolicy, monkeypatch,
):
    transfer = _transfer(raw_dir, "file-fsync")
    armed = False
    real_fsync = os.fsync

    def hook(point: str):
        nonlocal armed
        if point == rt.FAULT_PUBLISH_AFTER_ARTIFACT_MARKER_UPDATE:
            armed = True

    def failing_fsync(fd: int):
        nonlocal armed
        mode = os.fstat(fd).st_mode
        if armed and stat.S_ISREG(mode):
            armed = False
            raise OSError(errno.EIO, "sidecar temp fsync fault")
        return real_fsync(fd)

    rt.set_fault_injection_hook(hook)
    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(PublishTransactionFailure) as raised:
        _publish(raw_dir, policy, transfer)
    assert raised.value.durable_marker_status == "ARTIFACT_PUBLISHED"
    assert len(_markers(raw_dir)) == 1
    assert (raw_dir / transfer.sealed.final_name).is_file()
    assert not (raw_dir / f"{transfer.sealed.final_name}.sha256").exists()


def test_artifact_root_directory_fsync_failure_keeps_staged_marker(
    raw_dir: Path, policy: MarkerValidationPolicy, monkeypatch,
):
    transfer = _transfer(raw_dir, "root-fsync")
    root_identity = (raw_dir.stat().st_dev, raw_dir.stat().st_ino)
    armed = False
    real_fsync = os.fsync

    def hook(point: str):
        nonlocal armed
        if point == rt.FAULT_PUBLISH_AFTER_ARTIFACT_LINK:
            armed = True

    def failing_fsync(fd: int):
        nonlocal armed
        current = os.fstat(fd)
        if armed and (current.st_dev, current.st_ino) == root_identity:
            armed = False
            raise OSError(errno.EIO, "raw root fsync fault")
        return real_fsync(fd)

    rt.set_fault_injection_hook(hook)
    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(PublishTransactionFailure) as raised:
        _publish(raw_dir, policy, transfer)
    assert raised.value.durable_marker_status == "STAGED"
    assert (raw_dir / transfer.sealed.final_name).is_file()
    assert parse_marker(_markers(raw_dir)[0].read_bytes())["status"] == "STAGED"


def test_pending_directory_fsync_failure_is_committed_cleanup_pending(
    raw_dir: Path, policy: MarkerValidationPolicy, monkeypatch,
):
    transfer = _transfer(raw_dir, "pending-fsync")
    pending_identity = (
        (raw_dir / ".pending").stat().st_dev,
        (raw_dir / ".pending").stat().st_ino,
    )
    armed = False
    real_fsync = os.fsync

    def hook(point: str):
        nonlocal armed
        if point == rt.FAULT_PUBLISH_AFTER_STAGING_UNLINK:
            armed = True

    def failing_fsync(fd: int):
        nonlocal armed
        current = os.fstat(fd)
        if armed and (current.st_dev, current.st_ino) == pending_identity:
            armed = False
            raise OSError(errno.EIO, "pending fsync fault")
        return real_fsync(fd)

    rt.set_fault_injection_hook(hook)
    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(PublishCleanupPending) as raised:
        _publish(raw_dir, policy, transfer)
    assert raised.value.committed is True
    assert raised.value.durable_marker_status == "COMMITTED"
    assert not (raw_dir / ".pending" / transfer.sealed.staging_filename).exists()
    assert parse_marker(_markers(raw_dir)[0].read_bytes())["status"] == "COMMITTED"


def test_transfer_close_failure_leaves_committed_marker(
    raw_dir: Path, policy: MarkerValidationPolicy, monkeypatch,
):
    transfer = _transfer(raw_dir, "close-failure")
    real_close = transfer.close
    monkeypatch.setattr(
        transfer, "close",
        lambda: (_ for _ in ()).throw(OSError(errno.EIO, "transfer close fault")),
    )
    with pytest.raises(PublishCleanupPending) as raised:
        _publish(raw_dir, policy, transfer)
    assert raised.value.committed is True
    assert raised.value.cleanup_pending is True
    marker = parse_marker(_markers(raw_dir)[0].read_bytes())
    assert marker["status"] == "COMMITTED"
    monkeypatch.setattr(transfer, "close", real_close)
    transfer.close()


def test_marker_cleanup_failure_is_committed_cleanup_pending(
    raw_dir: Path, policy: MarkerValidationPolicy, monkeypatch,
):
    transfer = _transfer(raw_dir, "marker-cleanup-failure")
    real_unlink = os.unlink

    def fail_marker(name, *args, **kwargs):
        if isinstance(name, str) and name.endswith(".marker"):
            raise OSError(errno.EIO, "marker unlink fault")
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", fail_marker)
    with pytest.raises(PublishCleanupPending) as raised:
        _publish(raw_dir, policy, transfer)
    assert raised.value.committed is True
    assert len(_markers(raw_dir)) == 1
    assert parse_marker(_markers(raw_dir)[0].read_bytes())["status"] == "COMMITTED"


def test_marker_cleanup_root_fsync_failure_reports_marker_absent_but_unconfirmed(
    raw_dir: Path, policy: MarkerValidationPolicy, monkeypatch,
):
    transfer = _transfer(raw_dir, "marker-cleanup-fsync")
    root_identity = (raw_dir.stat().st_dev, raw_dir.stat().st_ino)
    armed = False
    real_fsync = os.fsync

    def hook(point: str):
        nonlocal armed
        if point == rt.FAULT_PUBLISH_AFTER_TRANSFER_CLOSE:
            armed = True

    def failing_fsync(fd: int):
        nonlocal armed
        current = os.fstat(fd)
        if armed and (current.st_dev, current.st_ino) == root_identity:
            armed = False
            raise OSError(errno.EIO, "final root fsync fault")
        return real_fsync(fd)

    rt.set_fault_injection_hook(hook)
    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(PublishCleanupPending) as raised:
        _publish(raw_dir, policy, transfer)
    assert raised.value.committed is True
    assert raised.value.cleanup_pending is True
    assert raised.value.filesystem_snapshot["marker"]["exists"] is False
    assert not _markers(raw_dir)


def test_one_hundred_publications_have_no_fd_leak(
    raw_dir: Path, policy: MarkerValidationPolicy,
):
    baseline = len(os.listdir("/proc/self/fd"))
    for index in range(100):
        _publish(raw_dir, policy, _transfer(raw_dir, f"fd-{index}", value=index))
    assert len(os.listdir("/proc/self/fd")) == baseline
    assert len(list(raw_dir.glob("manifest_*.json"))) == 100
    assert not _markers(raw_dir)
    assert not _temps(raw_dir)


def _process_publish_worker(raw_path: str, scan_id: str, start, output) -> None:
    raw_dir = Path(raw_path)
    policy = MarkerValidationPolicy(
        manifest_prefix="manifest",
        artifact_filename_pattern=re.compile(
            r"^raw_scan_[A-Za-z0-9_.-]+_[0-9a-f]{12}\.events\.jsonl\.gz$"
        ),
    )
    try:
        transfer = _transfer(raw_dir, scan_id)
        start.wait(5)
        result = _publish(raw_dir, policy, transfer)
        output.put(("ok", result.manifest_entry["sequence"]))
    except BaseException as exc:
        output.put(("error", type(exc).__name__, str(exc)))


def test_two_processes_compete_and_leave_valid_contiguous_chain(
    raw_dir: Path, policy: MarkerValidationPolicy,
):
    context = multiprocessing.get_context("fork")
    start = context.Event()
    output = context.Queue()
    workers = [
        context.Process(
            target=_process_publish_worker,
            args=(str(raw_dir), f"process-{index}", start, output),
        )
        for index in range(2)
    ]
    for worker in workers:
        worker.start()
    start.set()
    results = [output.get(timeout=10) for _ in workers]
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0
    assert sorted(results) == [("ok", 0), ("ok", 1)]
    entries = [_manifest(raw_dir, index) for index in range(2)]
    assert entries[1]["previous_manifest_hash"] == entries[0]["manifest_hash"]
    assert not _markers(raw_dir)
    assert not _temps(raw_dir)
