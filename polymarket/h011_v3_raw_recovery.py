"""Crash-safe recovery for the H-011 V3 transactional raw publisher.

Phase II-B completes interrupted transactions created by ``publish_raw_scan``.
Recovery is forward-only after a durable STAGED marker: it verifies or recreates
artifact, sidecar, and manifest in order, commits the marker, then removes
staging and transaction-owned temporary evidence with the marker deleted last.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat as statmod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import h011_v3_raw_transaction as rt


RecoveryResultStatus = Literal["NO_RECOVERY_NEEDED", "RECOVERED"]


FAULT_RECOVERY_AFTER_ARTIFACT: Final[str] = "RECOVERY_AFTER_ARTIFACT"
FAULT_RECOVERY_AFTER_SIDECAR: Final[str] = "RECOVERY_AFTER_SIDECAR"
FAULT_RECOVERY_AFTER_MANIFEST: Final[str] = "RECOVERY_AFTER_MANIFEST"
FAULT_RECOVERY_AFTER_COMMITTED_MARKER: Final[str] = (
    "RECOVERY_AFTER_COMMITTED_MARKER"
)
FAULT_RECOVERY_AFTER_STAGING_UNLINK: Final[str] = (
    "RECOVERY_AFTER_STAGING_UNLINK"
)
FAULT_RECOVERY_AFTER_TEMP_CLEANUP: Final[str] = "RECOVERY_AFTER_TEMP_CLEANUP"
FAULT_RECOVERY_AFTER_MARKER_UNLINK: Final[str] = "RECOVERY_AFTER_MARKER_UNLINK"
FAULT_RECOVERY_AFTER_FINAL_ROOT_FSYNC: Final[str] = (
    "RECOVERY_AFTER_FINAL_ROOT_FSYNC"
)
FAULT_RECOVERY_AFTER_SIDECAR_LINK: Final[str] = (
    "RECOVERY_AFTER_SIDECAR_LINK"
)
FAULT_RECOVERY_AFTER_SIDECAR_DIR_FSYNC: Final[str] = (
    "RECOVERY_AFTER_SIDECAR_DIR_FSYNC"
)
FAULT_RECOVERY_AFTER_MANIFEST_LINK: Final[str] = (
    "RECOVERY_AFTER_MANIFEST_LINK"
)
FAULT_RECOVERY_AFTER_MANIFEST_DIR_FSYNC: Final[str] = (
    "RECOVERY_AFTER_MANIFEST_DIR_FSYNC"
)


_STATUS_RANK: Final[dict[str, int]] = {
    "STAGED": 0,
    "ARTIFACT_PUBLISHED": 1,
    "SIDECAR_PUBLISHED": 2,
    "MANIFEST_PUBLISHED": 3,
    "COMMITTED": 4,
}


@dataclass(frozen=True)
class RecoveryResult:
    status: RecoveryResultStatus
    marker_filename: str | None
    recovered_from_status: str | None
    final_status: str | None
    manifest_entry: dict[str, Any] | None
    actions: tuple[str, ...] = ()


class RawTransactionRecoveryError(rt.RawArtifactTransactionError):
    """Base error for Phase II-B recovery."""


class RecoveryBlockedError(RawTransactionRecoveryError):
    """Recovery cannot continue without risking corruption or data loss."""

    def __init__(
        self,
        message: str,
        *,
        failure_stage: str,
        marker_filename: str | None,
        filesystem_snapshot: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.failure_stage = failure_stage
        self.marker_filename = marker_filename
        self.filesystem_snapshot = filesystem_snapshot
        self.recoverable = False


class RecoveryInterruptedError(RawTransactionRecoveryError):
    """Recovery made only forward progress and can be retried safely."""

    def __init__(
        self,
        message: str,
        *,
        failure_stage: str,
        marker_filename: str | None,
        filesystem_snapshot: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.failure_stage = failure_stage
        self.marker_filename = marker_filename
        self.filesystem_snapshot = filesystem_snapshot
        self.recoverable = True


def _read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _entry_stat(dir_fd: int, name: str) -> os.stat_result | None:
    rt.validate_bare_filename(name)
    try:
        entry = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if statmod.S_ISLNK(entry.st_mode):
        raise rt.PathSafetyError(f"symlink forbidden during recovery: {name}")
    if not statmod.S_ISREG(entry.st_mode):
        raise rt.PathSafetyError(
            f"recovery entry must be a regular file: {name}"
        )
    return entry


def _read_regular(dir_fd: int, name: str) -> tuple[bytes, os.stat_result]:
    entry = _entry_stat(dir_fd, name)
    if entry is None:
        raise FileNotFoundError(name)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise rt.PathSafetyError(f"symlink forbidden: {name}") from exc
        raise
    try:
        opened = os.fstat(fd)
        if not statmod.S_ISREG(opened.st_mode):
            raise rt.PathSafetyError(f"not a regular file: {name}")
        if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
            raise rt.PathSafetyError(f"entry changed while opening: {name}")
        return _read_all(fd), opened
    finally:
        os.close(fd)


def _snapshot_entry(dir_fd: int | None, name: str) -> dict[str, Any]:
    if dir_fd is None:
        return {"name": name, "exists": False, "directory_missing": True}
    try:
        entry = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return {"name": name, "exists": False}
    except OSError as exc:
        return {"name": name, "exists": None, "error": str(exc)}
    return {
        "name": name,
        "exists": True,
        "mode": statmod.S_IFMT(entry.st_mode),
        "permissions": statmod.S_IMODE(entry.st_mode),
        "device_id": entry.st_dev,
        "inode": entry.st_ino,
        "size_bytes": entry.st_size,
        "is_symlink": statmod.S_ISLNK(entry.st_mode),
        "is_regular": statmod.S_ISREG(entry.st_mode),
    }


def _open_pending(root_fd: int) -> int | None:
    try:
        pending_fd = os.open(
            ".pending",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise rt.PathSafetyError(
                ".pending is a symlink or is not a directory"
            ) from exc
        raise
    try:
        pending_stat = os.fstat(pending_fd)
        path_stat = os.stat(
            ".pending", dir_fd=root_fd, follow_symlinks=False
        )
        if not statmod.S_ISDIR(path_stat.st_mode):
            raise rt.PathSafetyError(".pending is not a directory")
        if (pending_stat.st_dev, pending_stat.st_ino) != (
            path_stat.st_dev,
            path_stat.st_ino,
        ):
            raise rt.PathSafetyError(".pending changed while opening")
        return pending_fd
    except BaseException:
        os.close(pending_fd)
        raise


def _filesystem_snapshot(
    *,
    root_fd: int,
    pending_fd: int | None,
    marker: dict[str, Any] | None,
    marker_name: str | None,
) -> dict[str, Any]:
    if marker is None:
        return {
            "marker": (
                _snapshot_entry(root_fd, marker_name)
                if marker_name is not None
                else None
            ),
            "root_names": sorted(os.listdir(root_fd)),
        }
    return {
        "marker": _snapshot_entry(root_fd, marker_name or ""),
        "staging": _snapshot_entry(pending_fd, marker["staging_filename"]),
        "artifact": _snapshot_entry(root_fd, marker["final_name"]),
        "sidecar": _snapshot_entry(root_fd, marker["sidecar_name"]),
        "manifest": _snapshot_entry(root_fd, marker["manifest_name"]),
        "temporary_names": sorted(
            name for name in os.listdir(root_fd) if ".tmp." in name
        ),
    }


def _canonical_marker_names(
    *, root_fd: int, prefix: str
) -> tuple[list[str], list[str]]:
    marker_re = re.compile(
        rf"^{re.escape(prefix)}_txn_(\d{{6}})_"
        rf"([0-9a-f]{{8}}-[0-9a-f]{{4}}-4[0-9a-f]{{3}}-"
        rf"[89ab][0-9a-f]{{3}}-[0-9a-f]{{12}})\.marker$"
    )
    valid: list[str] = []
    malformed: list[str] = []
    for name in os.listdir(root_fd):
        if not (name.startswith(f"{prefix}_txn_") and name.endswith(".marker")):
            continue
        if marker_re.fullmatch(name) is None:
            malformed.append(name)
        else:
            valid.append(name)
    return sorted(valid), sorted(malformed)


def _marker_temp_owners(
    *, root_fd: int, prefix: str
) -> tuple[dict[str, list[str]], list[str]]:
    marker_pattern = (
        rf"{re.escape(prefix)}_txn_(\d{{6}})_"
        rf"[0-9a-f]{{8}}-[0-9a-f]{{4}}-4[0-9a-f]{{3}}-"
        rf"[89ab][0-9a-f]{{3}}-[0-9a-f]{{12}}\.marker"
    )
    temp_re = re.compile(
        rf"^(?P<owner>{marker_pattern})\.tmp\.[0-9a-f]+$"
    )
    owners: dict[str, list[str]] = {}
    malformed: list[str] = []
    for name in os.listdir(root_fd):
        if not (
            name.startswith(f"{prefix}_txn_")
            and ".marker.tmp." in name
        ):
            continue
        match = temp_re.fullmatch(name)
        if match is None:
            malformed.append(name)
            continue
        owners.setdefault(match.group("owner"), []).append(name)
    return (
        {owner: sorted(names) for owner, names in sorted(owners.items())},
        sorted(malformed),
    )


def _load_marker(
    *, root_fd: int, name: str, policy: rt.MarkerValidationPolicy
) -> dict[str, Any]:
    raw, _ = _read_regular(root_fd, name)
    marker = rt.parse_marker(raw)
    rt.validate_marker(marker, policy)
    if rt.canonical_json_bytes(marker) != raw:
        raise rt.MarkerValidationError(
            f"marker {name} is valid but not canonically encoded"
        )
    expected = rt.marker_filename(
        policy.manifest_prefix,
        marker["sequence"],
        marker["transaction_uuid"],
    )
    if expected != name:
        raise rt.MarkerValidationError(
            f"marker filename/body mismatch: {name} != {expected}"
        )
    return marker


def _validate_manifest_entry(
    *, raw: bytes, name: str, sequence: int
) -> dict[str, Any]:
    try:
        entry = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise rt.MarkerValidationError(
            f"manifest {name} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(entry, dict):
        raise rt.MarkerValidationError(f"manifest {name} root must be object")
    if set(entry) != set(rt.REQUIRED_CANDIDATE_MANIFEST_FIELDS):
        raise rt.MarkerValidationError(
            f"manifest {name} fields differ from schema: {sorted(entry)}"
        )
    rt._validate_candidate_manifest_fields(entry)
    if rt.canonical_manifest_file_bytes(entry) != raw:
        raise rt.MarkerValidationError(f"manifest {name} is not canonical")
    if entry["sequence"] != sequence:
        raise rt.MarkerValidationError(
            f"manifest filename/sequence mismatch: {name} vs {entry['sequence']}"
        )
    expected_hash = rt.compute_manifest_hash(entry)
    if entry["manifest_hash"] != expected_hash:
        raise rt.MarkerValidationError(
            f"manifest {name} hash mismatch: "
            f"{entry['manifest_hash']} != {expected_hash}"
        )
    if entry["condition_ids"] != sorted(set(entry["condition_ids"])):
        raise rt.MarkerValidationError(
            f"manifest {name} condition_ids are not canonical"
        )
    return entry


def _validate_chain_for_marker(
    *, root_fd: int, marker: dict[str, Any], policy: rt.MarkerValidationPolicy
) -> None:
    prefix = policy.manifest_prefix
    manifest_re = re.compile(rf"^{re.escape(prefix)}_(\d{{6}})\.json$")
    observed: dict[int, tuple[str, dict[str, Any], bytes]] = {}
    for name in os.listdir(root_fd):
        match = manifest_re.fullmatch(name)
        if match is None:
            if name.startswith(f"{prefix}_") and name.endswith(".json"):
                raise rt.MarkerValidationError(
                    f"non-canonical manifest filename: {name}"
                )
            continue
        sequence = int(match.group(1))
        raw, _ = _read_regular(root_fd, name)
        entry = _validate_manifest_entry(
            raw=raw, name=name, sequence=sequence
        )
        if sequence in observed:
            raise rt.MarkerValidationError(
                f"duplicate manifest sequence: {sequence}"
            )
        observed[sequence] = (name, entry, raw)

    marker_sequence = marker["sequence"]
    if any(sequence > marker_sequence for sequence in observed):
        raise rt.MarkerValidationError(
            "manifest exists beyond active transaction sequence"
        )
    required_prior = list(range(marker_sequence))
    prior_sequences = sorted(
        sequence for sequence in observed if sequence < marker_sequence
    )
    if prior_sequences != required_prior:
        raise rt.MarkerValidationError(
            f"prior manifest chain is not contiguous: "
            f"observed={prior_sequences} expected={required_prior}"
        )

    previous_hash: str | None = None
    identities: set[tuple[str, str]] = set()
    for sequence in required_prior:
        _, entry, _ = observed[sequence]
        if entry["previous_manifest_hash"] != previous_hash:
            raise rt.MarkerValidationError(
                f"manifest {sequence} previous hash mismatch"
            )
        identity = (entry["run_id"], entry["scan_id"])
        if identity in identities:
            raise rt.IdentityCollisionError(
                f"duplicate prior manifest identity: {identity}"
            )
        identities.add(identity)
        previous_hash = entry["manifest_hash"]

    if marker["previous_manifest_hash"] != previous_hash:
        raise rt.MarkerValidationError(
            "marker previous_manifest_hash does not match prior chain"
        )
    candidate_identity = (marker["run_id"], marker["scan_id"])
    if candidate_identity in identities:
        raise rt.IdentityCollisionError(
            f"candidate identity already exists in prior chain: {candidate_identity}"
        )

    current = observed.get(marker_sequence)
    if current is not None:
        current_name, current_entry, current_raw = current
        expected_raw = rt.canonical_manifest_file_bytes(
            marker["candidate_manifest"]
        )
        if current_name != marker["manifest_name"]:
            raise rt.MarkerValidationError(
                "current manifest name differs from marker binding"
            )
        if current_raw != expected_raw or current_entry != marker["candidate_manifest"]:
            raise rt.MarkerValidationError(
                "current manifest differs from marker candidate"
            )


def _verify_artifact(
    *, root_fd: int, marker: dict[str, Any]
) -> None:
    raw, entry = _read_regular(root_fd, marker["final_name"])
    identity = (entry.st_dev, entry.st_ino, entry.st_size)
    expected_identity = (
        marker["device_id"],
        marker["inode"],
        marker["size_bytes"],
    )
    if identity != expected_identity:
        raise RawTransactionRecoveryError(
            f"artifact identity mismatch: {identity} != {expected_identity}"
        )
    actual_hash = hashlib.sha256(raw).hexdigest()
    if actual_hash != marker["file_sha256"]:
        raise RawTransactionRecoveryError(
            f"artifact hash mismatch: {actual_hash} != {marker['file_sha256']}"
        )
    if statmod.S_IMODE(entry.st_mode) != 0o444:
        raise RawTransactionRecoveryError(
            f"artifact mode must be 0444, got {oct(statmod.S_IMODE(entry.st_mode))}"
        )


def _verify_staging(
    *, pending_fd: int, marker: dict[str, Any]
) -> None:
    raw, entry = _read_regular(pending_fd, marker["staging_filename"])
    identity = (entry.st_dev, entry.st_ino, entry.st_size)
    expected_identity = (
        marker["device_id"],
        marker["inode"],
        marker["size_bytes"],
    )
    if identity != expected_identity:
        raise RawTransactionRecoveryError(
            f"staging identity mismatch: {identity} != {expected_identity}"
        )
    actual_hash = hashlib.sha256(raw).hexdigest()
    if actual_hash != marker["file_sha256"]:
        raise RawTransactionRecoveryError(
            f"staging hash mismatch: {actual_hash} != {marker['file_sha256']}"
        )
    if statmod.S_IMODE(entry.st_mode) != 0o444:
        raise RawTransactionRecoveryError(
            f"staging mode must be 0444, got {oct(statmod.S_IMODE(entry.st_mode))}"
        )


def _ensure_artifact(
    *, root_fd: int, pending_fd: int | None, marker: dict[str, Any]
) -> str:
    if _entry_stat(root_fd, marker["final_name"]) is not None:
        _verify_artifact(root_fd=root_fd, marker=marker)
        return "artifact_verified"
    if pending_fd is None:
        raise RawTransactionRecoveryError(
            "artifact and .pending directory are both absent"
        )
    if _entry_stat(pending_fd, marker["staging_filename"]) is None:
        raise RawTransactionRecoveryError(
            "artifact and sealed staging evidence are both absent"
        )
    _verify_staging(pending_fd=pending_fd, marker=marker)
    try:
        os.link(
            marker["staging_filename"],
            marker["final_name"],
            src_dir_fd=pending_fd,
            dst_dir_fd=root_fd,
            follow_symlinks=False,
        )
    except FileExistsError:
        pass
    _verify_artifact(root_fd=root_fd, marker=marker)
    os.fsync(root_fd)
    return "artifact_published"


def _ensure_exact_bytes(
    *,
    root_fd: int,
    name: str,
    expected: bytes,
    after_link_fault: str,
    after_dir_fsync_fault: str,
) -> str:
    if _entry_stat(root_fd, name) is not None:
        actual, entry = _read_regular(root_fd, name)
        if actual != expected:
            raise RawTransactionRecoveryError(
                f"existing {name} differs from marker-derived canonical bytes"
            )
        if statmod.S_IMODE(entry.st_mode) != 0o444:
            raise RawTransactionRecoveryError(
                f"existing {name} mode must be 0444"
            )
        return f"{name}_verified"
    rt._publish_bytes_no_replace_under_lock(
        dir_fd=root_fd,
        final_name=name,
        payload=expected,
        after_link_fault=after_link_fault,
        after_dir_fsync_fault=after_dir_fsync_fault,
    )
    return f"{name}_published"


def _advance_marker(
    *,
    guard: rt.RawChainLockGuard,
    raw_directory: Path,
    policy: rt.MarkerValidationPolicy,
    marker_name: str,
    marker: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    if _STATUS_RANK[marker["status"]] >= _STATUS_RANK[status]:
        return marker
    return rt._update_transaction_marker_status(
        guard=guard,
        raw_directory=raw_directory,
        policy=policy,
        marker_name=marker_name,
        marker_body=marker,
        status=status,
    )


def _owned_temp_names(
    *, root_fd: int, marker_name: str, marker: dict[str, Any]
) -> list[str]:
    marker_temp_re = re.compile(
        rf"^{re.escape(marker_name)}\.tmp\.[0-9a-f]+$"
    )
    sidecar_temp_re = re.compile(
        rf"^\.{re.escape(marker['sidecar_name'])}\.tmp\.[0-9a-f]+$"
    )
    manifest_temp_re = re.compile(
        rf"^\.{re.escape(marker['manifest_name'])}\.tmp\.[0-9a-f]+$"
    )
    return sorted(
        name
        for name in os.listdir(root_fd)
        if marker_temp_re.fullmatch(name)
        or sidecar_temp_re.fullmatch(name)
        or manifest_temp_re.fullmatch(name)
    )


def _cleanup_owned_temps(
    *, root_fd: int, marker_name: str, marker: dict[str, Any]
) -> int:
    names = _owned_temp_names(
        root_fd=root_fd, marker_name=marker_name, marker=marker
    )
    for name in names:
        entry = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if statmod.S_ISLNK(entry.st_mode) or not statmod.S_ISREG(entry.st_mode):
            raise rt.PathSafetyError(
                f"transaction-owned temp is not a regular file: {name}"
            )
        os.unlink(name, dir_fd=root_fd)
    if names:
        os.fsync(root_fd)
    return len(names)


def _cleanup_staging(
    *, pending_fd: int | None, marker: dict[str, Any]
) -> bool:
    if pending_fd is None:
        return False
    if _entry_stat(pending_fd, marker["staging_filename"]) is None:
        return False
    _verify_staging(pending_fd=pending_fd, marker=marker)
    os.unlink(marker["staging_filename"], dir_fd=pending_fd)
    os.fsync(pending_fd)
    return True


def recover_raw_scan_transaction(
    *,
    guard: rt.RawChainLockGuard,
    raw_directory: Path,
    policy: rt.MarkerValidationPolicy,
) -> RecoveryResult:
    """Recover the single durable raw transaction under the chain lock.

    The function is idempotent and forward-only. A valid marker transfers
    ownership to the transaction subsystem, so recovery never deletes a
    partially published transaction as rollback. Any conflicting or missing
    evidence blocks recovery with a filesystem snapshot.
    """
    rt.assert_guard_valid(guard, raw_directory, policy.manifest_prefix)
    root_fd = guard.trusted.fd
    marker_name: str | None = None
    marker: dict[str, Any] | None = None
    pending_fd: int | None = None
    stage = "R0_DISCOVERY"
    actions: list[str] = []

    try:
        marker_names, malformed_markers = _canonical_marker_names(
            root_fd=root_fd, prefix=policy.manifest_prefix
        )
        marker_temp_owners, malformed_temps = _marker_temp_owners(
            root_fd=root_fd, prefix=policy.manifest_prefix
        )
        if malformed_markers or malformed_temps:
            raise RecoveryBlockedError(
                "non-canonical transaction marker evidence: "
                f"markers={malformed_markers} temps={malformed_temps}",
                failure_stage=stage,
                marker_filename=None,
                filesystem_snapshot={
                    "malformed_markers": malformed_markers,
                    "malformed_marker_temps": malformed_temps,
                    "root_names": sorted(os.listdir(root_fd)),
                },
            )
        if not marker_names:
            if marker_temp_owners:
                raise RecoveryBlockedError(
                    "orphan durable marker-temp evidence requires explicit "
                    f"recovery: {marker_temp_owners}",
                    failure_stage=stage,
                    marker_filename=None,
                    filesystem_snapshot={
                        "orphan_marker_temps": marker_temp_owners,
                        "root_names": sorted(os.listdir(root_fd)),
                    },
                )
            # A previous recovery attempt may have unlinked its marker before
            # confirming the directory fsync. Fsync makes no-marker durable.
            os.fsync(root_fd)
            return RecoveryResult(
                status="NO_RECOVERY_NEEDED",
                marker_filename=None,
                recovered_from_status=None,
                final_status=None,
                manifest_entry=None,
            )
        if len(marker_names) != 1:
            raise RecoveryBlockedError(
                f"expected exactly one transaction marker, found {marker_names}",
                failure_stage=stage,
                marker_filename=None,
                filesystem_snapshot={
                    "markers": marker_names,
                    "marker_temps": marker_temp_owners,
                    "root_names": sorted(os.listdir(root_fd)),
                },
            )

        marker_name = marker_names[0]
        foreign_temp_owners = sorted(
            owner for owner in marker_temp_owners if owner != marker_name
        )
        if foreign_temp_owners:
            raise RecoveryBlockedError(
                "marker-temp evidence belongs to another transaction: "
                f"{foreign_temp_owners}",
                failure_stage=stage,
                marker_filename=marker_name,
                filesystem_snapshot={
                    "marker": marker_name,
                    "marker_temps": marker_temp_owners,
                    "root_names": sorted(os.listdir(root_fd)),
                },
            )
        marker = _load_marker(root_fd=root_fd, name=marker_name, policy=policy)
        recovered_from = marker["status"]
        pending_fd = _open_pending(root_fd)

        stage = "R1_VALIDATE_CHAIN"
        _validate_chain_for_marker(
            root_fd=root_fd, marker=marker, policy=policy
        )

        stage = "R2_ARTIFACT"
        actions.append(
            _ensure_artifact(
                root_fd=root_fd, pending_fd=pending_fd, marker=marker
            )
        )
        marker = _advance_marker(
            guard=guard,
            raw_directory=raw_directory,
            policy=policy,
            marker_name=marker_name,
            marker=marker,
            status="ARTIFACT_PUBLISHED",
        )
        rt._inject_fault(FAULT_RECOVERY_AFTER_ARTIFACT)

        stage = "R3_SIDECAR"
        sidecar_bytes = (
            f"{marker['file_sha256']}  {marker['final_name']}\n".encode("ascii")
        )
        actions.append(
            _ensure_exact_bytes(
                root_fd=root_fd,
                name=marker["sidecar_name"],
                expected=sidecar_bytes,
                after_link_fault=FAULT_RECOVERY_AFTER_SIDECAR_LINK,
                after_dir_fsync_fault=FAULT_RECOVERY_AFTER_SIDECAR_DIR_FSYNC,
            )
        )
        marker = _advance_marker(
            guard=guard,
            raw_directory=raw_directory,
            policy=policy,
            marker_name=marker_name,
            marker=marker,
            status="SIDECAR_PUBLISHED",
        )
        rt._inject_fault(FAULT_RECOVERY_AFTER_SIDECAR)

        stage = "R4_MANIFEST"
        manifest_bytes = rt.canonical_manifest_file_bytes(
            marker["candidate_manifest"]
        )
        actions.append(
            _ensure_exact_bytes(
                root_fd=root_fd,
                name=marker["manifest_name"],
                expected=manifest_bytes,
                after_link_fault=FAULT_RECOVERY_AFTER_MANIFEST_LINK,
                after_dir_fsync_fault=FAULT_RECOVERY_AFTER_MANIFEST_DIR_FSYNC,
            )
        )
        marker = _advance_marker(
            guard=guard,
            raw_directory=raw_directory,
            policy=policy,
            marker_name=marker_name,
            marker=marker,
            status="MANIFEST_PUBLISHED",
        )
        rt._inject_fault(FAULT_RECOVERY_AFTER_MANIFEST)

        stage = "R5_COMMIT"
        marker = _advance_marker(
            guard=guard,
            raw_directory=raw_directory,
            policy=policy,
            marker_name=marker_name,
            marker=marker,
            status="COMMITTED",
        )
        actions.append("marker_committed")
        rt._inject_fault(FAULT_RECOVERY_AFTER_COMMITTED_MARKER)

        stage = "R6_STAGING_CLEANUP"
        if _cleanup_staging(pending_fd=pending_fd, marker=marker):
            actions.append("staging_removed")
        else:
            actions.append("staging_already_absent")
        rt._inject_fault(FAULT_RECOVERY_AFTER_STAGING_UNLINK)

        stage = "R7_TEMP_CLEANUP"
        removed_temps = _cleanup_owned_temps(
            root_fd=root_fd, marker_name=marker_name, marker=marker
        )
        actions.append(f"transaction_temps_removed:{removed_temps}")
        rt._inject_fault(FAULT_RECOVERY_AFTER_TEMP_CLEANUP)

        stage = "R8_MARKER_CLEANUP"
        # Marker is removed last. Re-read immediately before unlink so a
        # replacement cannot be silently deleted.
        current = _load_marker(
            root_fd=root_fd, name=marker_name, policy=policy
        )
        if marker["status"] != "COMMITTED" or marker["resolution"] != "COMMITTED":
            raise RawTransactionRecoveryError(
                "in-memory marker is not durably COMMITTED before final cleanup"
            )
        if current != marker:
            raise RawTransactionRecoveryError(
                "marker changed before final cleanup"
            )
        os.unlink(marker_name, dir_fd=root_fd)
        actions.append("marker_removed")
        rt._inject_fault(FAULT_RECOVERY_AFTER_MARKER_UNLINK)
        os.fsync(root_fd)
        rt._inject_fault(FAULT_RECOVERY_AFTER_FINAL_ROOT_FSYNC)

        return RecoveryResult(
            status="RECOVERED",
            marker_filename=marker_name,
            recovered_from_status=recovered_from,
            final_status="COMMITTED",
            manifest_entry=dict(marker["candidate_manifest"]),
            actions=tuple(actions),
        )
    except RecoveryBlockedError:
        raise
    except (
        rt.MarkerValidationError,
        rt.IdentityCollisionError,
        rt.PathSafetyError,
        RawTransactionRecoveryError,
    ) as exc:
        snapshot = _filesystem_snapshot(
            root_fd=root_fd,
            pending_fd=pending_fd,
            marker=marker,
            marker_name=marker_name,
        )
        raise RecoveryBlockedError(
            f"raw transaction recovery blocked at {stage}: {exc}",
            failure_stage=stage,
            marker_filename=marker_name,
            filesystem_snapshot=snapshot,
        ) from exc
    except BaseException as exc:
        snapshot = _filesystem_snapshot(
            root_fd=root_fd,
            pending_fd=pending_fd,
            marker=marker,
            marker_name=marker_name,
        )
        error_type = (
            RecoveryInterruptedError if marker is not None
            else RecoveryBlockedError
        )
        raise error_type(
            f"raw transaction recovery failed at {stage}: {exc}",
            failure_stage=stage,
            marker_filename=marker_name,
            filesystem_snapshot=snapshot,
        ) from exc
    finally:
        if pending_fd is not None:
            os.close(pending_fd)
