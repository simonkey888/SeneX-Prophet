"""Phase II-B tests for H-011 V3 raw transaction recovery."""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "polymarket"))

import h011_v3_raw_recovery as recovery
import h011_v3_raw_transaction as rt
from h011_v3_raw_recovery import (
    RecoveryBlockedError,
    RecoveryInterruptedError,
    recover_raw_scan_transaction,
)
from h011_v3_raw_transaction import (
    MarkerValidationPolicy,
    PublishTransactionFailure,
    RawChainLock,
    RawScanStager,
    canonical_payload_sha256,
    publish_raw_scan,
)


CREATED_AT = "2026-07-17T18:00:00Z"


@pytest.fixture(autouse=True)
def _isolation():
    yield
    rt.set_fault_injection_hook(None)
    with rt._ACTIVE_GUARDS_LOCK:
        guards = [record.guard for record in rt._ACTIVE_GUARDS.values()]
    errors = []
    for guard in guards:
        try:
            guard.close()
        except Exception as exc:  # pragma: no cover
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
        "received_at_utc": "2026-07-17T17:59:00Z",
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
    with RawScanStager("run-ii-b", scan_id, raw_dir) as stager:
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


def _recover(raw_dir: Path, policy: MarkerValidationPolicy):
    with RawChainLock(raw_dir, policy.manifest_prefix).acquire() as guard:
        return recover_raw_scan_transaction(
            guard=guard,
            raw_directory=raw_dir,
            policy=policy,
        )


def _marker_paths(raw_dir: Path) -> list[Path]:
    return sorted(raw_dir.glob("manifest_txn_*.marker"))


def _temp_paths(raw_dir: Path) -> list[Path]:
    return sorted(path for path in raw_dir.iterdir() if ".tmp." in path.name)


def _crash(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
    point: str,
    *,
    scan_id: str = "crash",
    value: int = 1,
):
    transfer = _transfer(raw_dir, scan_id, value=value)
    sealed = transfer.sealed
    rt.set_fault_injection_hook(
        lambda current: (_ for _ in ()).throw(RuntimeError(point))
        if current == point
        else None
    )
    with pytest.raises(PublishTransactionFailure):
        _publish(raw_dir, policy, transfer)
    rt.set_fault_injection_hook(None)
    assert transfer._closed is True
    assert len(_marker_paths(raw_dir)) == 1
    return sealed


PUBLISH_CRASH_POINTS = [
    rt.FAULT_PUBLISH_AFTER_STAGED_MARKER,
    rt.FAULT_PUBLISH_AFTER_ARTIFACT_LINK,
    rt.FAULT_PUBLISH_AFTER_ARTIFACT_MARKER_UPDATE,
    rt.FAULT_PUBLISH_AFTER_SIDECAR_LINK,
    rt.FAULT_PUBLISH_AFTER_SIDECAR_MARKER_UPDATE,
    rt.FAULT_PUBLISH_AFTER_MANIFEST_LINK,
    rt.FAULT_PUBLISH_AFTER_MANIFEST_MARKER_UPDATE,
    rt.FAULT_PUBLISH_AFTER_COMMITTED_MARKER,
]


@pytest.mark.parametrize("point", PUBLISH_CRASH_POINTS)
def test_recovery_completes_every_publisher_state_and_is_idempotent(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
    point: str,
):
    sealed = _crash(raw_dir, policy, point, scan_id=point.lower())

    result = _recover(raw_dir, policy)

    assert result.status == "RECOVERED"
    assert result.final_status == "COMMITTED"
    assert result.manifest_entry is not None
    assert result.manifest_entry["sequence"] == 0
    assert (raw_dir / sealed.final_name).is_file()
    assert (raw_dir / f"{sealed.final_name}.sha256").is_file()
    assert (raw_dir / "manifest_000000.json").is_file()
    assert not (raw_dir / ".pending" / sealed.staging_filename).exists()
    assert _marker_paths(raw_dir) == []
    assert _temp_paths(raw_dir) == []

    second = _recover(raw_dir, policy)
    assert second.status == "NO_RECOVERY_NEEDED"


RECOVERY_FAULT_POINTS = [
    recovery.FAULT_RECOVERY_AFTER_ARTIFACT,
    recovery.FAULT_RECOVERY_AFTER_SIDECAR_LINK,
    recovery.FAULT_RECOVERY_AFTER_SIDECAR_DIR_FSYNC,
    recovery.FAULT_RECOVERY_AFTER_SIDECAR,
    recovery.FAULT_RECOVERY_AFTER_MANIFEST_LINK,
    recovery.FAULT_RECOVERY_AFTER_MANIFEST_DIR_FSYNC,
    recovery.FAULT_RECOVERY_AFTER_MANIFEST,
    recovery.FAULT_RECOVERY_AFTER_COMMITTED_MARKER,
    recovery.FAULT_RECOVERY_AFTER_STAGING_UNLINK,
    recovery.FAULT_RECOVERY_AFTER_TEMP_CLEANUP,
    recovery.FAULT_RECOVERY_AFTER_MARKER_UNLINK,
    recovery.FAULT_RECOVERY_AFTER_FINAL_ROOT_FSYNC,
]


@pytest.mark.parametrize("point", RECOVERY_FAULT_POINTS)
def test_recovery_can_resume_after_each_durable_boundary(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
    point: str,
):
    sealed = _crash(
        raw_dir,
        policy,
        rt.FAULT_PUBLISH_AFTER_STAGED_MARKER,
        scan_id=point.lower(),
    )
    rt.set_fault_injection_hook(
        lambda current: (_ for _ in ()).throw(RuntimeError(point))
        if current == point
        else None
    )
    with pytest.raises(RecoveryInterruptedError) as raised:
        _recover(raw_dir, policy)
    rt.set_fault_injection_hook(None)
    assert raised.value.recoverable is True
    assert raised.value.filesystem_snapshot

    resumed = _recover(raw_dir, policy)
    if point in {
        recovery.FAULT_RECOVERY_AFTER_MARKER_UNLINK,
        recovery.FAULT_RECOVERY_AFTER_FINAL_ROOT_FSYNC,
    }:
        assert resumed.status == "NO_RECOVERY_NEEDED"
    else:
        assert resumed.status == "RECOVERED"
    assert (raw_dir / sealed.final_name).is_file()
    assert (raw_dir / "manifest_000000.json").is_file()
    assert _marker_paths(raw_dir) == []
    assert _temp_paths(raw_dir) == []


def test_no_marker_is_a_durable_noop(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
):
    before = sorted(path.name for path in raw_dir.iterdir())
    result = _recover(raw_dir, policy)
    after = sorted(path.name for path in raw_dir.iterdir())
    assert result.status == "NO_RECOVERY_NEEDED"
    assert after == before + [f"{policy.manifest_prefix}.lock"]


def test_wrong_existing_sidecar_blocks_and_preserves_marker(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
):
    sealed = _crash(
        raw_dir,
        policy,
        rt.FAULT_PUBLISH_AFTER_ARTIFACT_MARKER_UPDATE,
        scan_id="wrong-sidecar",
    )
    sidecar = raw_dir / f"{sealed.final_name}.sha256"
    sidecar.write_bytes(b"wrong\n")
    sidecar.chmod(0o444)

    with pytest.raises(RecoveryBlockedError) as raised:
        _recover(raw_dir, policy)

    assert raised.value.failure_stage == "R3_SIDECAR"
    assert sidecar.read_bytes() == b"wrong\n"
    assert len(_marker_paths(raw_dir)) == 1


def test_missing_artifact_and_staging_blocks_without_rollback(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
):
    sealed = _crash(
        raw_dir,
        policy,
        rt.FAULT_PUBLISH_AFTER_STAGED_MARKER,
        scan_id="missing-evidence",
    )
    staging = raw_dir / ".pending" / sealed.staging_filename
    staging.unlink()

    with pytest.raises(RecoveryBlockedError) as raised:
        _recover(raw_dir, policy)

    assert raised.value.failure_stage == "R2_ARTIFACT"
    assert len(_marker_paths(raw_dir)) == 1
    assert not (raw_dir / sealed.final_name).exists()


def test_corrupt_marker_is_never_replaced_or_deleted(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
):
    _crash(
        raw_dir,
        policy,
        rt.FAULT_PUBLISH_AFTER_STAGED_MARKER,
        scan_id="corrupt-marker",
    )
    marker_path = _marker_paths(raw_dir)[0]
    corrupt = b'{"corrupt":true}'
    marker_path.chmod(0o644)
    marker_path.write_bytes(corrupt)

    with pytest.raises(RecoveryBlockedError):
        _recover(raw_dir, policy)

    assert marker_path.read_bytes() == corrupt


def test_multiple_markers_fail_closed_before_mutation(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
):
    _crash(
        raw_dir,
        policy,
        rt.FAULT_PUBLISH_AFTER_STAGED_MARKER,
        scan_id="multiple",
    )
    original = _marker_paths(raw_dir)[0]
    duplicate = raw_dir / (
        "manifest_txn_000000_22222222-2222-4222-8222-222222222222.marker"
    )
    duplicate.write_bytes(original.read_bytes())
    before = {
        path.name: path.read_bytes()
        for path in raw_dir.iterdir()
        if path.is_file()
    }

    with pytest.raises(RecoveryBlockedError):
        _recover(raw_dir, policy)

    after = {
        path.name: path.read_bytes()
        for path in raw_dir.iterdir()
        if path.is_file()
    }
    assert after == before


def test_sequence_one_recovery_preserves_chain_linkage(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
):
    first = _publish(raw_dir, policy, _transfer(raw_dir, "first", value=1))
    sealed = _crash(
        raw_dir,
        policy,
        rt.FAULT_PUBLISH_AFTER_SIDECAR_LINK,
        scan_id="second",
        value=2,
    )

    result = _recover(raw_dir, policy)

    assert result.manifest_entry["sequence"] == 1
    assert result.manifest_entry["previous_manifest_hash"] == (
        first.manifest_entry["manifest_hash"]
    )
    second = json.loads((raw_dir / "manifest_000001.json").read_bytes())
    assert second == result.manifest_entry
    assert (raw_dir / sealed.final_name).is_file()


def test_marker_is_removed_after_staging_and_transaction_temps(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
    monkeypatch,
):
    sealed = _crash(
        raw_dir,
        policy,
        rt.FAULT_PUBLISH_AFTER_SIDECAR_LINK,
        scan_id="marker-last",
    )
    real_unlink = os.unlink
    marker_unlinked = False

    def tracing_unlink(name, *args, **kwargs):
        nonlocal marker_unlinked
        if isinstance(name, str) and name.endswith(".marker"):
            assert not (raw_dir / ".pending" / sealed.staging_filename).exists()
            assert _temp_paths(raw_dir) == []
            assert (raw_dir / sealed.final_name).is_file()
            assert (raw_dir / f"{sealed.final_name}.sha256").is_file()
            assert (raw_dir / "manifest_000000.json").is_file()
            marker_unlinked = True
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", tracing_unlink)
    _recover(raw_dir, policy)
    assert marker_unlinked is True


def test_recovered_files_remain_read_only(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
):
    sealed = _crash(
        raw_dir,
        policy,
        rt.FAULT_PUBLISH_AFTER_STAGED_MARKER,
        scan_id="readonly",
    )
    _recover(raw_dir, policy)
    for path in (
        raw_dir / sealed.final_name,
        raw_dir / f"{sealed.final_name}.sha256",
        raw_dir / "manifest_000000.json",
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o444


def test_orphan_marker_temp_blocks_instead_of_reporting_noop(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
):
    orphan = raw_dir / (
        "manifest_txn_000000_11111111-1111-4111-8111-111111111111"
        ".marker.tmp.deadbeef"
    )
    orphan.write_bytes(b"orphan-marker-temp")
    orphan.chmod(0o444)

    with pytest.raises(RecoveryBlockedError) as raised:
        _recover(raw_dir, policy)

    assert raised.value.failure_stage == "R0_DISCOVERY"
    assert raised.value.recoverable is False
    assert orphan.read_bytes() == b"orphan-marker-temp"


def test_marker_replacement_during_cleanup_is_not_deleted(
    raw_dir: Path,
    policy: MarkerValidationPolicy,
    monkeypatch,
):
    _crash(
        raw_dir,
        policy,
        rt.FAULT_PUBLISH_AFTER_STAGED_MARKER,
        scan_id="marker-replacement",
    )
    marker_path = _marker_paths(raw_dir)[0]
    original = rt.parse_marker(marker_path.read_bytes())
    real_cleanup = recovery._cleanup_owned_temps

    def replacing_cleanup(*, root_fd, marker_name, marker):
        removed = real_cleanup(
            root_fd=root_fd,
            marker_name=marker_name,
            marker=marker,
        )
        replacement = rt.parse_marker(marker_path.read_bytes())
        replacement["ownership_token"] = str(uuid.uuid4())
        replacement_bytes = rt.prepare_validated_marker_bytes(
            replacement,
            policy,
        )
        marker_path.chmod(0o644)
        marker_path.write_bytes(replacement_bytes)
        marker_path.chmod(0o444)
        return removed

    monkeypatch.setattr(recovery, "_cleanup_owned_temps", replacing_cleanup)

    with pytest.raises(RecoveryBlockedError) as raised:
        _recover(raw_dir, policy)

    assert raised.value.failure_stage == "R8_MARKER_CLEANUP"
    assert marker_path.is_file()
    current = rt.parse_marker(marker_path.read_bytes())
    assert current["ownership_token"] != original["ownership_token"]
