"""
SENECIO H-011 V3 — Raw Artifact Transaction Core Primitives (Phase I, G1-G7 hardened).

Implements the foundational layer of the E1-E7 design with F1-F9 + G1-G7 hardening:

  G1: TrustedDirectory + validate_safe_prefix; lstat/fstat inode check
  G2: No registry mutex during flock; chain reservations + LockReleaseError + BROKEN
  G3: Transactional update with rollback + 5 fault injection points
  G4: Stager completely dir_fd-based (raw_dir_fd, pending_dir_fd, dup fd for gzip)
  G5: __exit__ propagates failures (gzip close, cleanup, diagnostic)
  G6: Evidence location state machine (PENDING_ONLY/LINKED/QUARANTINE_ONLY)
  G7: Evidence always read-only via fchmod before publishing

NOT implemented (later Phase II work):
  - recovery state machine
  - integration with run_scan_v3
"""
from __future__ import annotations

import base64
import binascii
import ctypes
import ctypes.util
import errno
import fcntl
import gzip
import hashlib
import json
import os
import re
import stat as statmod
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Literal, NoReturn


# ═══════════════════════════════════════════════════════════════════════
# Section 1 — Canonicalization
# ═══════════════════════════════════════════════════════════════════════

_HEX64_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_ISO_8601_RE: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)
_UUID4_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def canonical_json_bytes(obj: Any) -> bytes:
    """Canonical JSON encoding (deterministic, UTF-8, no NaN/Infinity)."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_payload_sha256(payload: Any) -> str:
    """SHA-256 of canonical JSON encoding of a single event payload."""
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def canonical_events_sha256(events: list[dict[str, Any]]) -> str:
    """SHA-256 of canonical JSON encoding of a list of events (order preserved)."""
    return hashlib.sha256(canonical_json_bytes(events)).hexdigest()


def manifest_hash_input_bytes(entry: dict[str, Any]) -> bytes:
    """Canonical JSON bytes of a manifest entry EXCLUDING manifest_hash."""
    body = {k: v for k, v in entry.items() if k != "manifest_hash"}
    return canonical_json_bytes(body)


def compute_manifest_hash(entry: dict[str, Any]) -> str:
    """SHA-256 of canonical JSON of entry EXCLUDING manifest_hash."""
    return hashlib.sha256(manifest_hash_input_bytes(entry)).hexdigest()


def canonical_manifest_file_bytes(entry: dict[str, Any]) -> bytes:
    """Canonical JSON bytes of entry INCLUDING manifest_hash."""
    return canonical_json_bytes(entry)


def compute_marker_integrity_sha256(marker: dict[str, Any]) -> str:
    """SHA-256 of canonical JSON of marker EXCLUDING marker_integrity_sha256."""
    body = {k: v for k, v in marker.items() if k != "marker_integrity_sha256"}
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def compute_eligibility_integrity_sha256(state: dict[str, Any]) -> str:
    """SHA-256 of canonical JSON of eligibility state EXCLUDING state_sha256."""
    body = {k: v for k, v in state.items() if k != "state_sha256"}
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def compute_diagnostic_integrity_sha256(diag: dict[str, Any]) -> str:
    """SHA-256 of canonical JSON of diagnostic EXCLUDING diagnostic_integrity_sha256."""
    body = {k: v for k, v in diag.items() if k != "diagnostic_integrity_sha256"}
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()



# ═══════════════════════════════════════════════════════════════════════
# Section 2 — Enums (as Literal types) and error hierarchy
# ═══════════════════════════════════════════════════════════════════════

MarkerStatus = Literal[
    "STAGED",
    "ARTIFACT_PUBLISHED",
    "SIDECAR_PUBLISHED",
    "MANIFEST_PUBLISHED",
    "COMMITTED",
]

MarkerResolution = Literal["ACTIVE", "BLOCKED", "QUARANTINED", "COMMITTED"]

StagerState = Literal[
    "OPEN",
    "SEALED",
    "TRANSFERRED",
    "ABORTED_BEFORE_TRANSFER",
    "ABORTED_WITH_DIAGNOSTIC_EVIDENCE",
    "BLOCKED_DIAGNOSTIC_PERSISTENCE",
]

PublishResultStatus = Literal["PUBLISHED", "RECOVERABLE_ERROR", "BLOCKED"]

EvidenceLocation = Literal["PENDING", "QUARANTINE"]

GuardHealth = Literal["ACTIVE", "BROKEN", "CLOSED"]

MarkerFilesystemState = Literal[
    "PRE_COMMIT_FAILED_CLEAN",
    "PRE_COMMIT_CLEANUP_UNCONFIRMED",
    "COMMITTED_OLD_TEMP_PRESENT",
    "COMMITTED_OLD_TEMP_REMOVED_FSYNC_UNCONFIRMED",
    "COMMITTED_CLEAN",
]

MARKER_STATUSES: Final[frozenset[str]] = frozenset({
    "STAGED", "ARTIFACT_PUBLISHED", "SIDECAR_PUBLISHED",
    "MANIFEST_PUBLISHED", "COMMITTED",
})
MARKER_RESOLUTIONS: Final[frozenset[str]] = frozenset({
    "ACTIVE", "BLOCKED", "QUARANTINED", "COMMITTED",
})
STAGER_STATES: Final[frozenset[str]] = frozenset({
    "OPEN", "SEALED", "TRANSFERRED",
    "ABORTED_BEFORE_TRANSFER", "ABORTED_WITH_DIAGNOSTIC_EVIDENCE",
    "BLOCKED_DIAGNOSTIC_PERSISTENCE",
})
PUBLISH_RESULT_STATUSES: Final[frozenset[str]] = frozenset({
    "PUBLISHED", "RECOVERABLE_ERROR", "BLOCKED",
})


class RawTransactionError(Exception):
    """Base class for the raw transaction subsystem."""


class RawEventPersistenceError(RawTransactionError):
    """Raised when appending or fsyncing a raw event to staging fails."""


class RawArtifactTransactionError(RawTransactionError):
    """Raised inside the publish pipeline (Phase II)."""


class RecoveryRequiredError(RawArtifactTransactionError):
    """A durable transaction marker requires recovery before publishing."""


class PublishTransactionFailure(RawArtifactTransactionError):
    """A publisher failure with explicit ownership and filesystem semantics."""

    def __init__(
        self,
        message: str,
        *,
        failure_stage: str,
        durable_marker_status: str | None,
        marker_filename: str | None,
        transfer_consumed: bool,
        committed: bool,
        cleanup_pending: bool,
        filesystem_snapshot: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.failure_stage = failure_stage
        self.durable_marker_status = durable_marker_status
        self.marker_filename = marker_filename
        self.transfer_consumed = transfer_consumed
        self.committed = committed
        self.cleanup_pending = cleanup_pending
        self.filesystem_snapshot = filesystem_snapshot


class PublishCleanupPending(PublishTransactionFailure):
    """The manifest committed, but durable transaction cleanup is incomplete."""


class PublishPostCommitNotificationError(PublishTransactionFailure):
    """A test/observer hook failed after the transaction became committed-clean."""


class IdentityCollisionError(RawTransactionError):
    """Raised when run_id/scan_id collide with an existing manifest entry."""


class MarkerValidationError(RawTransactionError):
    """Raised when a marker fails schema, type, or integrity validation."""


class MarkerIntegrityError(MarkerValidationError):
    """Raised when marker_integrity_sha256 does not match."""


class CandidateManifestMismatchError(MarkerValidationError):
    """Raised when the E7 five-check candidate manifest validation fails."""


class MarkerCandidateBindingError(MarkerValidationError):
    """Raised when marker fields do not exactly match candidate_manifest fields (F2)."""


class EligibilityCorruptionError(RawTransactionError):
    """Raised when .eligibility_state.json is present but corrupt."""


class EligibilityMonotonicityError(RawTransactionError):
    """Raised when an attempt is made to revert first_eligible_scan_seen."""


class LockAcquisitionError(RawTransactionError):
    """Raised when fcntl.flock fails."""


class LockReleaseError(RawTransactionError):
    """Raised when flock unlock or fd close fails (G2)."""


class NestedLockingError(RawTransactionError):
    """Raised when a second lock is attempted for the same (directory, prefix)."""


class GuardValidationError(RawTransactionError):
    """Raised when a guard fails validation."""


class StagerStateError(RawTransactionError):
    """Raised when a RawScanStager method is called from the wrong state."""


class PathSafetyError(RawTransactionError):
    """Raised when a path contains forbidden components."""


class AtomicMarkerUpdateUnsupportedError(RawTransactionError):
    """Raised when renameat2(RENAME_EXCHANGE) is not available (F3)."""


class AtomicMarkerUpdateError(RawTransactionError):
    """Raised when a transactional marker update fails and rollback was attempted (G3)."""


class AtomicMarkerTargetChangedError(AtomicMarkerUpdateError):
    """The marker target disappeared between validation and RENAME_EXCHANGE."""


class AtomicMarkerRollbackFailed(RawTransactionError):
    """Rollback could not prove restoration of the pre-update marker."""

    def __init__(
        self,
        message: str,
        *,
        failed_operation: str,
        marker_snapshot: dict[str, Any],
        temp_snapshot: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.failed_operation = failed_operation
        self.marker_snapshot = marker_snapshot
        self.temp_snapshot = temp_snapshot


class MarkerCreateCleanupPending(RawTransactionError):
    """Marker creation cleanup is incomplete or not durably confirmed."""

    def __init__(
        self,
        message: str,
        *,
        final_created: bool,
        temp_unlinked: bool,
        cleanup_durability_confirmed: bool,
        filesystem_state: MarkerFilesystemState,
    ) -> None:
        super().__init__(message)
        self.final_created = final_created
        self.temp_unlinked = temp_unlinked
        self.cleanup_durability_confirmed = cleanup_durability_confirmed
        self.filesystem_state = filesystem_state


class MarkerUpdateCleanupPending(RawTransactionError):
    """Marker update committed but old marker cleanup/durability is incomplete."""

    def __init__(
        self,
        message: str,
        *,
        committed: bool,
        temp_unlinked: bool,
        cleanup_durability_confirmed: bool,
        filesystem_state: MarkerFilesystemState,
    ) -> None:
        super().__init__(message)
        self.committed = committed
        self.temp_unlinked = temp_unlinked
        self.cleanup_durability_confirmed = cleanup_durability_confirmed
        self.filesystem_state = filesystem_state


class MarkerPostCommitNotificationError(RawTransactionError):
    """A test/observer hook failed after the operation became committed-clean."""

    def __init__(self, message: str, *, operation: str) -> None:
        super().__init__(message)
        self.operation = operation
        self.filesystem_state: MarkerFilesystemState = "COMMITTED_CLEAN"


class DirectoryCreationDurabilityError(RawTransactionError):
    """A child directory was created but its parent fsync was not confirmed."""

    def __init__(self, message: str, *, directory_name: str, created: bool) -> None:
        super().__init__(message)
        self.directory_name = directory_name
        self.created = created
        self.parent_fsync_confirmed = False


class DiagnosticPersistenceError(RawTransactionError):
    """Raised when diagnostic evidence cannot be persisted (F7)."""



# ═══════════════════════════════════════════════════════════════════════
# Section 3 — G1: TrustedDirectory + path safety
# ═══════════════════════════════════════════════════════════════════════

_FORBIDDEN_NAME_PATTERNS: Final[tuple[str, ...]] = ("/", "\\", "..")
_MAX_PREFIX_LEN: Final[int] = 64
_SAFE_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_]+$")


def validate_bare_filename(name: str) -> None:
    """Validate that `name` is a bare filename (no path components)."""
    if not isinstance(name, str):
        raise PathSafetyError(f"filename must be a string, got {type(name).__name__}")
    if not name:
        raise PathSafetyError("filename is empty")
    for pat in _FORBIDDEN_NAME_PATTERNS:
        if pat in name:
            raise PathSafetyError(
                f"filename contains forbidden component {pat!r}: {name!r}"
            )
    if os.path.isabs(name):
        raise PathSafetyError(f"absolute filename forbidden: {name!r}")
    parts = Path(name).parts
    if len(parts) != 1:
        raise PathSafetyError(
            f"filename has multiple path components: {name!r} -> {parts}"
        )


def validate_safe_prefix(prefix: str) -> None:
    """G1 — Validate manifest_prefix before constructing any filename.

    Rules:
      - non-empty string
      - matches ^[A-Za-z0-9_]+$
      - no "/", "\\", ".", ".."
      - length <= _MAX_PREFIX_LEN
    """
    if not isinstance(prefix, str):
        raise PathSafetyError(f"prefix must be str, got {type(prefix).__name__}")
    if not prefix:
        raise PathSafetyError("prefix is empty")
    if len(prefix) > _MAX_PREFIX_LEN:
        raise PathSafetyError(f"prefix too long: {len(prefix)} > {_MAX_PREFIX_LEN}")
    for pat in _FORBIDDEN_NAME_PATTERNS:
        if pat in prefix:
            raise PathSafetyError(f"prefix contains forbidden component {pat!r}: {prefix!r}")
    if not _SAFE_PREFIX_RE.match(prefix):
        raise PathSafetyError(f"prefix does not match ^[A-Za-z0-9_]+$: {prefix!r}")


@dataclass
class TrustedDirectory:
    """G1 — A directory opened and verified as trusted.

    The fd remains open for the lifetime of this object. st_dev/st_ino
    are captured at open time and verified against lstat to detect
    symlink replacement.
    """
    path: Path
    fd: int
    st_dev: int
    st_ino: int
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        os.close(self.fd)
        self._closed = True

    def __enter__(self) -> "TrustedDirectory":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_trusted_directory(path: Path) -> TrustedDirectory:
    """G1 — Open a directory as trusted.

    Sequence:
      1. lstat(path) — reject symlink
      2. open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
      3. fstat(fd)
      4. Verify lstat and fstat represent the same inode/dev

    Raises PathSafetyError if the path is a symlink or lstat/fstat mismatch.
    """
    if not isinstance(path, Path):
        raise PathSafetyError(f"path must be Path, got {type(path).__name__}")
    # 1. lstat to reject symlink
    try:
        lst = os.lstat(str(path))
    except FileNotFoundError as exc:
        raise PathSafetyError(f"directory does not exist: {path}") from exc
    except OSError as exc:
        raise PathSafetyError(f"cannot lstat {path}: {exc}") from exc
    if statmod.S_ISLNK(lst.st_mode):
        raise PathSafetyError(f"directory is a symlink: {path}")
    if not statmod.S_ISDIR(lst.st_mode):
        raise PathSafetyError(f"not a directory: {path}")
    # 2. open with O_NOFOLLOW
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PathSafetyError(f"directory is a symlink: {path}") from exc
        raise PathSafetyError(f"cannot open directory {path}: {exc}") from exc
    # 3. fstat
    try:
        fst = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise PathSafetyError(f"fstat failed for {path}: {exc}") from exc
    # 4. Verify lstat and fstat agree
    if lst.st_dev != fst.st_dev or lst.st_ino != fst.st_ino:
        os.close(fd)
        raise PathSafetyError(
            f"lstat/fstat mismatch for {path}: "
            f"lstat dev={lst.st_dev} ino={lst.st_ino} vs "
            f"fstat dev={fst.st_dev} ino={fst.st_ino}"
        )
    return TrustedDirectory(path=path, fd=fd, st_dev=fst.st_dev, st_ino=fst.st_ino)


def validate_real_directory(path: Path) -> None:
    """Validate that `path` is a real directory (not a symlink)."""
    try:
        st = os.lstat(str(path))
    except FileNotFoundError as exc:
        raise PathSafetyError(f"directory does not exist: {path}") from exc
    except OSError as exc:
        raise PathSafetyError(f"cannot lstat {path}: {exc}") from exc
    if statmod.S_ISLNK(st.st_mode):
        raise PathSafetyError(f"symlink forbidden for directory: {path}")
    if not statmod.S_ISDIR(st.st_mode):
        raise PathSafetyError(f"not a directory: {path}")


def reject_symlink_path(path: Path) -> None:
    """Raise PathSafetyError if `path` is a symlink (by name)."""
    try:
        st = os.lstat(str(path))
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PathSafetyError(f"cannot lstat {path}: {exc}") from exc
    if statmod.S_ISLNK(st.st_mode):
        raise PathSafetyError(f"symlink forbidden: {path}")



# ═══════════════════════════════════════════════════════════════════════
# Section 4 — Marker schema v2 with F2 binding and F9 strict validators
# ═══════════════════════════════════════════════════════════════════════

MARKER_VERSION: Final[str] = "h011-artifact-txn-v2"
ELIGIBILITY_SCHEMA_VERSION: Final[str] = "h011-eligibility-v1"
DIAGNOSTIC_SCHEMA_VERSION: Final[str] = "h011-diagnostic-v1"

REQUIRED_MARKER_FIELDS: Final[tuple[str, ...]] = (
    "transaction_version",
    "transaction_uuid",
    "ownership_token",
    "status",
    "resolution",
    "sequence",
    "run_id",
    "scan_id",
    "staging_filename",
    "final_name",
    "sidecar_name",
    "manifest_name",
    "device_id",
    "inode",
    "size_bytes",
    "file_sha256",
    "canonical_events_sha256",
    "event_count",
    "condition_ids",
    "previous_manifest_hash",
    "candidate_manifest",
    "candidate_manifest_bytes_base64",
    "candidate_manifest_bytes_sha256",
    "manifest_created_at",
    "recoverable",
    "marker_integrity_sha256",
)

OPTIONAL_MARKER_FIELDS: Final[tuple[str, ...]] = (
    "failure_stage",
    "failure_type",
    "failure_message",
)

REQUIRED_CANDIDATE_MANIFEST_FIELDS: Final[tuple[str, ...]] = (
    "sequence",
    "run_id",
    "scan_id",
    "filename",
    "file_sha256",
    "canonical_events_sha256",
    "event_count",
    "condition_ids",
    "previous_manifest_hash",
    "created_at",
    "manifest_hash",
)


@dataclass(frozen=True)
class MarkerValidationPolicy:
    """F2 — Policy for marker validation."""
    manifest_prefix: str
    artifact_filename_pattern: re.Pattern[str]

    def __post_init__(self):
        validate_safe_prefix(self.manifest_prefix)


DEFAULT_ARTIFACT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^raw_scan_[A-Za-z0-9_.-]+_[0-9a-f]{12}\.events\.jsonl\.gz$"
)

DEFAULT_MARKER_POLICY: Final[MarkerValidationPolicy] = MarkerValidationPolicy(
    manifest_prefix="manifest",
    artifact_filename_pattern=DEFAULT_ARTIFACT_PATTERN,
)


# ─── F9: Strict validators ───

def _validate_hex64(value: Any, field_name: str) -> None:
    if not isinstance(value, str):
        raise MarkerValidationError(
            f"{field_name} must be str, got {type(value).__name__}"
        )
    if not _HEX64_RE.match(value):
        raise MarkerValidationError(
            f"{field_name} must be 64-char lowercase hex, got {value!r}"
        )


def _validate_uuid4_strict(value: Any, field_name: str) -> None:
    """F9 — Use uuid.UUID() and check version == 4."""
    if not isinstance(value, str):
        raise MarkerValidationError(
            f"{field_name} must be str, got {type(value).__name__}"
        )
    try:
        u = uuid.UUID(value)
    except ValueError as exc:
        raise MarkerValidationError(
            f"{field_name} invalid UUID: {value!r}: {exc}"
        ) from exc
    if u.version != 4:
        raise MarkerValidationError(
            f"{field_name} must be UUID version 4, got version {u.version}: {value!r}"
        )


def _validate_iso8601_utc_strict(value: Any, field_name: str) -> None:
    """F9 — Strict ISO 8601 UTC validation."""
    if not isinstance(value, str):
        raise MarkerValidationError(
            f"{field_name} must be str, got {type(value).__name__}"
        )
    if not _ISO_8601_RE.match(value):
        raise MarkerValidationError(
            f"{field_name} must match ISO 8601 pattern, got {value!r}"
        )
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise MarkerValidationError(
            f"{field_name} invalid ISO 8601 date: {value!r}: {exc}"
        ) from exc
    offset = parsed.utcoffset()
    if offset is None:
        raise MarkerValidationError(f"{field_name} missing timezone: {value!r}")
    if offset != timedelta(0):
        raise MarkerValidationError(
            f"{field_name} non-UTC offset {offset}: {value!r}"
        )


def _validate_device_id(value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise MarkerValidationError(f"device_id must be int, got {type(value).__name__}")
    if value < 0:
        raise MarkerValidationError(f"device_id must be non-negative, got {value}")
    if value > 2**64 - 1:
        raise MarkerValidationError(f"device_id exceeds 64-bit range: {value}")


def _validate_inode(value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise MarkerValidationError(f"inode must be int, got {type(value).__name__}")
    if value < 0:
        raise MarkerValidationError(f"inode must be non-negative, got {value}")
    if value > 2**64 - 1:
        raise MarkerValidationError(f"inode exceeds 64-bit range: {value}")


def _validate_non_empty_str(value: Any, field_name: str) -> None:
    if not isinstance(value, str):
        raise MarkerValidationError(
            f"{field_name} must be str, got {type(value).__name__}"
        )
    if not value:
        raise MarkerValidationError(f"{field_name} must be non-empty")


def _validate_bare_filename_field(value: Any, field_name: str) -> None:
    if not isinstance(value, str):
        raise MarkerValidationError(
            f"{field_name} must be str, got {type(value).__name__}"
        )
    try:
        validate_bare_filename(value)
    except PathSafetyError as exc:
        raise MarkerValidationError(
            f"{field_name} is not a safe bare filename: {exc}"
        ) from exc


def parse_marker(raw_bytes: bytes) -> dict[str, Any]:
    """Parse marker bytes into a dict. Strict JSON."""
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise MarkerValidationError(
            f"raw_bytes must be bytes, got {type(raw_bytes).__name__}"
        )
    try:
        obj = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarkerValidationError(f"marker is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise MarkerValidationError(
            f"marker root must be a JSON object, got {type(obj).__name__}"
        )
    return obj


def _validate_candidate_manifest_fields(cm: dict[str, Any]) -> None:
    """F9 — Validate exact types and fields of candidate_manifest."""
    if not isinstance(cm, dict):
        raise MarkerValidationError(
            f"candidate_manifest must be dict, got {type(cm).__name__}"
        )
    missing = [f for f in REQUIRED_CANDIDATE_MANIFEST_FIELDS if f not in cm]
    if missing:
        raise MarkerValidationError(f"candidate_manifest missing required fields: {missing}")
    allowed = set(REQUIRED_CANDIDATE_MANIFEST_FIELDS)
    unknown = [k for k in cm.keys() if k not in allowed]
    if unknown:
        raise MarkerValidationError(f"candidate_manifest has unknown fields: {unknown}")
    if not isinstance(cm["sequence"], int) or isinstance(cm["sequence"], bool):
        raise MarkerValidationError("candidate_manifest.sequence must be int")
    if cm["sequence"] < 0:
        raise MarkerValidationError("candidate_manifest.sequence must be non-negative")
    _validate_non_empty_str(cm["run_id"], "candidate_manifest.run_id")
    _validate_non_empty_str(cm["scan_id"], "candidate_manifest.scan_id")
    _validate_bare_filename_field(cm["filename"], "candidate_manifest.filename")
    _validate_hex64(cm["file_sha256"], "candidate_manifest.file_sha256")
    _validate_hex64(cm["canonical_events_sha256"], "candidate_manifest.canonical_events_sha256")
    if not isinstance(cm["event_count"], int) or isinstance(cm["event_count"], bool):
        raise MarkerValidationError("candidate_manifest.event_count must be int")
    if cm["event_count"] < 0:
        raise MarkerValidationError("candidate_manifest.event_count must be non-negative")
    if not isinstance(cm["condition_ids"], list):
        raise MarkerValidationError("candidate_manifest.condition_ids must be list")
    for i, c in enumerate(cm["condition_ids"]):
        if not isinstance(c, str):
            raise MarkerValidationError(f"candidate_manifest.condition_ids[{i}] must be str")
    pmh = cm["previous_manifest_hash"]
    if pmh is not None:
        if not isinstance(pmh, str):
            raise MarkerValidationError(
                "candidate_manifest.previous_manifest_hash must be str or null"
            )
        if not _HEX64_RE.match(pmh):
            raise MarkerValidationError(
                f"candidate_manifest.previous_manifest_hash must be 64-char hex or null, got {pmh!r}"
            )
    _validate_iso8601_utc_strict(cm["created_at"], "candidate_manifest.created_at")
    _validate_hex64(cm["manifest_hash"], "candidate_manifest.manifest_hash")


def _validate_marker_candidate_binding(marker: dict[str, Any], policy: MarkerValidationPolicy) -> None:
    """F2 — Exact binding between marker fields and candidate_manifest fields."""
    cm = marker["candidate_manifest"]
    _validate_candidate_manifest_fields(cm)
    bindings: list[tuple[str, Any, Any, str]] = [
        ("sequence", cm["sequence"], marker["sequence"], "candidate_manifest.sequence"),
        ("run_id", cm["run_id"], marker["run_id"], "candidate_manifest.run_id"),
        ("scan_id", cm["scan_id"], marker["scan_id"], "candidate_manifest.scan_id"),
        ("filename", cm["filename"], marker["final_name"], "candidate_manifest.filename vs marker.final_name"),
        ("file_sha256", cm["file_sha256"], marker["file_sha256"], "file_sha256"),
        ("canonical_events_sha256", cm["canonical_events_sha256"], marker["canonical_events_sha256"], "canonical_events_sha256"),
        ("event_count", cm["event_count"], marker["event_count"], "event_count"),
        ("condition_ids", cm["condition_ids"], marker["condition_ids"], "condition_ids"),
        ("previous_manifest_hash", cm["previous_manifest_hash"], marker["previous_manifest_hash"], "previous_manifest_hash"),
        ("created_at", cm["created_at"], marker["manifest_created_at"], "created_at vs manifest_created_at"),
    ]
    for field_name, cm_val, marker_val, label in bindings:
        if cm_val != marker_val:
            raise MarkerCandidateBindingError(
                f"{label} mismatch: candidate={cm_val!r} marker={marker_val!r}"
            )
    expected_sidecar = marker["final_name"] + ".sha256"
    if marker["sidecar_name"] != expected_sidecar:
        raise MarkerCandidateBindingError(
            f"sidecar_name must be final_name + '.sha256': "
            f"expected {expected_sidecar!r}, got {marker['sidecar_name']!r}"
        )
    expected_manifest_name = f"{policy.manifest_prefix}_{marker['sequence']:06d}.json"
    if marker["manifest_name"] != expected_manifest_name:
        raise MarkerCandidateBindingError(
            f"manifest_name mismatch: expected {expected_manifest_name!r}, got {marker['manifest_name']!r}"
        )
    if not policy.artifact_filename_pattern.match(marker["final_name"]):
        raise MarkerCandidateBindingError(
            f"final_name does not match artifact_filename_pattern: {marker['final_name']!r}"
        )
    cid = marker["condition_ids"]
    if cid != sorted(set(cid)):
        raise MarkerCandidateBindingError(
            f"condition_ids must be sorted and deduplicated, got {cid}"
        )
    if marker["sequence"] == 0:
        if marker["previous_manifest_hash"] is not None:
            raise MarkerCandidateBindingError(
                f"sequence=0 requires previous_manifest_hash=null, got {marker['previous_manifest_hash']!r}"
            )
    else:
        pmh = marker["previous_manifest_hash"]
        if not isinstance(pmh, str) or not _HEX64_RE.match(pmh):
            raise MarkerCandidateBindingError(
                f"sequence>0 requires previous_manifest_hash as 64-char hex, got {pmh!r}"
            )


def validate_marker(marker: dict[str, Any], policy: MarkerValidationPolicy) -> None:
    """Validate marker schema, types, integrity hash, E7 candidate equivalence,
    and F2 exact marker<->candidate binding."""
    if not isinstance(marker, dict):
        raise MarkerValidationError(f"marker must be dict, got {type(marker).__name__}")
    if not isinstance(policy, MarkerValidationPolicy):
        raise MarkerValidationError(
            f"policy must be MarkerValidationPolicy, got {type(policy).__name__}"
        )
    missing = [f for f in REQUIRED_MARKER_FIELDS if f not in marker]
    if missing:
        raise MarkerValidationError(f"marker missing required fields: {missing}")
    allowed = set(REQUIRED_MARKER_FIELDS) | set(OPTIONAL_MARKER_FIELDS)
    unknown = [k for k in marker.keys() if k not in allowed]
    if unknown:
        raise MarkerValidationError(f"marker has unknown fields: {unknown}")
    if marker["transaction_version"] != MARKER_VERSION:
        raise MarkerValidationError(
            f"transaction_version must be {MARKER_VERSION!r}, got {marker['transaction_version']!r}"
        )
    _validate_uuid4_strict(marker["transaction_uuid"], "transaction_uuid")
    _validate_uuid4_strict(marker["ownership_token"], "ownership_token")
    if marker["status"] not in MARKER_STATUSES:
        raise MarkerValidationError(
            f"status must be one of {sorted(MARKER_STATUSES)}, got {marker['status']!r}"
        )
    if marker["resolution"] not in MARKER_RESOLUTIONS:
        raise MarkerValidationError(
            f"resolution must be one of {sorted(MARKER_RESOLUTIONS)}, got {marker['resolution']!r}"
        )
    if not isinstance(marker["sequence"], int) or isinstance(marker["sequence"], bool):
        raise MarkerValidationError(f"sequence must be int, got {type(marker['sequence']).__name__}")
    if marker["sequence"] < 0:
        raise MarkerValidationError(f"sequence must be non-negative, got {marker['sequence']}")
    _validate_non_empty_str(marker["run_id"], "run_id")
    _validate_non_empty_str(marker["scan_id"], "scan_id")
    _validate_bare_filename_field(marker["staging_filename"], "staging_filename")
    _validate_bare_filename_field(marker["final_name"], "final_name")
    _validate_bare_filename_field(marker["sidecar_name"], "sidecar_name")
    _validate_bare_filename_field(marker["manifest_name"], "manifest_name")
    if not marker["staging_filename"].endswith(".tmp"):
        raise MarkerValidationError(
            f"staging_filename must end with .tmp, got {marker['staging_filename']!r}"
        )
    if not marker["sidecar_name"].endswith(".sha256"):
        raise MarkerValidationError(
            f"sidecar_name must end with .sha256, got {marker['sidecar_name']!r}"
        )
    _validate_device_id(marker["device_id"])
    _validate_inode(marker["inode"])
    if not isinstance(marker["size_bytes"], int) or isinstance(marker["size_bytes"], bool):
        raise MarkerValidationError(f"size_bytes must be int, got {type(marker['size_bytes']).__name__}")
    if marker["size_bytes"] < 0:
        raise MarkerValidationError(f"size_bytes must be >= 0, got {marker['size_bytes']}")
    _validate_hex64(marker["file_sha256"], "file_sha256")
    _validate_hex64(marker["canonical_events_sha256"], "canonical_events_sha256")
    _validate_hex64(marker["candidate_manifest_bytes_sha256"], "candidate_manifest_bytes_sha256")
    _validate_hex64(marker["marker_integrity_sha256"], "marker_integrity_sha256")
    if not isinstance(marker["event_count"], int) or isinstance(marker["event_count"], bool):
        raise MarkerValidationError(f"event_count must be int, got {type(marker['event_count']).__name__}")
    if marker["event_count"] < 0:
        raise MarkerValidationError(f"event_count must be >= 0, got {marker['event_count']}")
    cid = marker["condition_ids"]
    if not isinstance(cid, list):
        raise MarkerValidationError(f"condition_ids must be list, got {type(cid).__name__}")
    for i, c in enumerate(cid):
        if not isinstance(c, str):
            raise MarkerValidationError(f"condition_ids[{i}] must be str, got {type(c).__name__}")
    pmh = marker["previous_manifest_hash"]
    if pmh is not None:
        if not isinstance(pmh, str):
            raise MarkerValidationError(
                f"previous_manifest_hash must be str or null, got {type(pmh).__name__}"
            )
        if not _HEX64_RE.match(pmh):
            raise MarkerValidationError(
                f"previous_manifest_hash must be 64-char hex or null, got {pmh!r}"
            )
    cm = marker["candidate_manifest"]
    if not isinstance(cm, dict):
        raise MarkerValidationError(
            f"candidate_manifest must be dict, got {type(cm).__name__}"
        )
    if not isinstance(marker["candidate_manifest_bytes_base64"], str):
        raise MarkerValidationError("candidate_manifest_bytes_base64 must be str")
    _validate_iso8601_utc_strict(marker["manifest_created_at"], "manifest_created_at")
    rec = marker["recoverable"]
    if not isinstance(rec, bool):
        raise MarkerValidationError(
            f"recoverable must be bool (REQUIRED, E3), got {type(rec).__name__}: {rec!r}"
        )
    for f in OPTIONAL_MARKER_FIELDS:
        v = marker.get(f)
        if v is not None and not isinstance(v, str):
            raise MarkerValidationError(f"{f} must be str or null, got {type(v).__name__}")
    recomputed = compute_marker_integrity_sha256(marker)
    if recomputed != marker["marker_integrity_sha256"]:
        raise MarkerIntegrityError(
            f"marker_integrity_sha256 mismatch: computed={recomputed} "
            f"stored={marker['marker_integrity_sha256']}"
        )
    errors = validate_candidate_manifest_exact(marker)
    if errors:
        raise CandidateManifestMismatchError(
            "candidate_manifest E7 validation failed: " + "; ".join(errors)
        )
    _validate_marker_candidate_binding(marker, policy)


def validate_candidate_manifest_exact(marker: dict[str, Any]) -> list[str]:
    """E7 — five-check candidate manifest validation. Returns list of errors."""
    errors: list[str] = []
    b64 = marker.get("candidate_manifest_bytes_base64", "")
    if not isinstance(b64, str):
        errors.append(f"candidate_manifest_bytes_base64 must be str, got {type(b64).__name__}")
        return errors
    try:
        decoded = base64.b64decode(b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        errors.append(f"base64 decode failed: {exc}")
        return errors
    computed_sha = hashlib.sha256(decoded).hexdigest()
    if computed_sha != marker.get("candidate_manifest_bytes_sha256"):
        errors.append(f"candidate_manifest_bytes_sha256 mismatch")
    try:
        decoded_dict = json.loads(decoded)
    except json.JSONDecodeError as exc:
        errors.append(f"json decode of base64 bytes failed: {exc}")
        return errors
    if decoded_dict != marker.get("candidate_manifest"):
        errors.append("candidate_manifest dict != decoded base64 bytes")
    cm = marker.get("candidate_manifest")
    if not isinstance(cm, dict):
        errors.append("candidate_manifest must be dict for check 4")
    else:
        try:
            canonical = canonical_manifest_file_bytes(cm)
        except (TypeError, ValueError) as exc:
            errors.append(f"canonical_manifest_file_bytes failed: {exc}")
        else:
            if decoded != canonical:
                errors.append("decoded base64 bytes != canonical_manifest_file_bytes")
    if isinstance(cm, dict) and "manifest_hash" in cm:
        try:
            recomputed = compute_manifest_hash(cm)
        except (TypeError, ValueError) as exc:
            errors.append(f"compute_manifest_hash failed: {exc}")
        else:
            if recomputed != cm["manifest_hash"]:
                errors.append(f"manifest_hash mismatch: computed={recomputed} stored={cm['manifest_hash']}")
    elif isinstance(cm, dict):
        errors.append("candidate_manifest missing manifest_hash key for check 5")
    return errors



# ═══════════════════════════════════════════════════════════════════════
# Section 5 — Marker persistence (G3: transactional update with rollback)
# ═══════════════════════════════════════════════════════════════════════

_RENAME_EXCHANGE: Final[int] = 2

# G3 fault injection points
FAULT_AFTER_EXCHANGE: Final[str] = "AFTER_EXCHANGE"
FAULT_AFTER_NEW_MARKER_VERIFY: Final[str] = "AFTER_NEW_MARKER_VERIFY"
FAULT_BEFORE_FIRST_DIR_FSYNC: Final[str] = "BEFORE_FIRST_DIR_FSYNC"
FAULT_AFTER_FIRST_DIR_FSYNC: Final[str] = "AFTER_FIRST_DIR_FSYNC"
FAULT_AFTER_OLD_MARKER_UNLINK: Final[str] = "AFTER_OLD_MARKER_UNLINK"
FAULT_AFTER_SECOND_DIR_FSYNC: Final[str] = "AFTER_SECOND_DIR_FSYNC"
FAULT_CREATE_AFTER_FINAL_LINK: Final[str] = "CREATE_AFTER_FINAL_LINK"
FAULT_CREATE_AFTER_TEMP_UNLINK: Final[str] = "CREATE_AFTER_TEMP_UNLINK"
FAULT_CREATE_AFTER_DIR_FSYNC: Final[str] = "CREATE_AFTER_DIR_FSYNC"
FAULT_ROLLBACK_EXCHANGE_FAILURE: Final[str] = "ROLLBACK_EXCHANGE_FAILURE"
FAULT_ROLLBACK_FSYNC_FAILURE: Final[str] = "ROLLBACK_FSYNC_FAILURE"
FAULT_ROLLBACK_TEMP_UNLINK_FAILURE: Final[str] = "ROLLBACK_TEMP_UNLINK_FAILURE"
FAULT_ENTER_AFTER_RAW_DIR_OPEN: Final[str] = "ENTER_AFTER_RAW_DIR_OPEN"
FAULT_ENTER_AFTER_PENDING_OPEN: Final[str] = "ENTER_AFTER_PENDING_OPEN"
FAULT_ENTER_AFTER_STAGING_CREATE: Final[str] = "ENTER_AFTER_STAGING_CREATE"
FAULT_ENTER_AFTER_DUP: Final[str] = "ENTER_AFTER_DUP"
FAULT_ENTER_AFTER_GZIP_CREATE: Final[str] = "ENTER_AFTER_GZIP_CREATE"
FAULT_PENDING_AFTER_MKDIR_BEFORE_PARENT_FSYNC: Final[str] = (
    "PENDING_AFTER_MKDIR_BEFORE_PARENT_FSYNC"
)
FAULT_QUARANTINE_AFTER_MKDIR_BEFORE_PARENT_FSYNC: Final[str] = (
    "QUARANTINE_AFTER_MKDIR_BEFORE_PARENT_FSYNC"
)
FAULT_BEFORE_EXCHANGE: Final[str] = "BEFORE_EXCHANGE"

# Phase II-A publisher durable-boundary fault points.
FAULT_PUBLISH_BEFORE_STAGED_MARKER: Final[str] = "PUBLISH_BEFORE_STAGED_MARKER"
FAULT_PUBLISH_AFTER_STAGED_MARKER: Final[str] = "PUBLISH_AFTER_STAGED_MARKER"
FAULT_PUBLISH_AFTER_ARTIFACT_LINK: Final[str] = "PUBLISH_AFTER_ARTIFACT_LINK"
FAULT_PUBLISH_AFTER_ARTIFACT_DIR_FSYNC: Final[str] = "PUBLISH_AFTER_ARTIFACT_DIR_FSYNC"
FAULT_PUBLISH_AFTER_ARTIFACT_MARKER_UPDATE: Final[str] = "PUBLISH_AFTER_ARTIFACT_MARKER_UPDATE"
FAULT_PUBLISH_AFTER_SIDECAR_LINK: Final[str] = "PUBLISH_AFTER_SIDECAR_LINK"
FAULT_PUBLISH_AFTER_SIDECAR_DIR_FSYNC: Final[str] = "PUBLISH_AFTER_SIDECAR_DIR_FSYNC"
FAULT_PUBLISH_AFTER_SIDECAR_MARKER_UPDATE: Final[str] = "PUBLISH_AFTER_SIDECAR_MARKER_UPDATE"
FAULT_PUBLISH_AFTER_MANIFEST_LINK: Final[str] = "PUBLISH_AFTER_MANIFEST_LINK"
FAULT_PUBLISH_AFTER_MANIFEST_DIR_FSYNC: Final[str] = "PUBLISH_AFTER_MANIFEST_DIR_FSYNC"
FAULT_PUBLISH_AFTER_MANIFEST_MARKER_UPDATE: Final[str] = "PUBLISH_AFTER_MANIFEST_MARKER_UPDATE"
FAULT_PUBLISH_AFTER_COMMITTED_MARKER: Final[str] = "PUBLISH_AFTER_COMMITTED_MARKER"
FAULT_PUBLISH_AFTER_STAGING_UNLINK: Final[str] = "PUBLISH_AFTER_STAGING_UNLINK"
FAULT_PUBLISH_AFTER_PENDING_DIR_FSYNC: Final[str] = "PUBLISH_AFTER_PENDING_DIR_FSYNC"
FAULT_PUBLISH_AFTER_TRANSFER_CLOSE: Final[str] = "PUBLISH_AFTER_TRANSFER_CLOSE"
FAULT_PUBLISH_AFTER_MARKER_UNLINK: Final[str] = "PUBLISH_AFTER_MARKER_UNLINK"
FAULT_PUBLISH_AFTER_FINAL_ROOT_FSYNC: Final[str] = "PUBLISH_AFTER_FINAL_ROOT_FSYNC"

# Global fault injection hook (for testing). Set to a callable that takes
# the fault point name and raises if it matches the desired fault.
_fault_injection_hook: Any = None


def set_fault_injection_hook(hook: Any) -> None:
    """Set a fault injection hook for G3 testing. Pass None to disable."""
    global _fault_injection_hook
    _fault_injection_hook = hook


def _inject_fault(point: str) -> None:
    """Call the fault injection hook if set."""
    if _fault_injection_hook is not None:
        _fault_injection_hook(point)


def _load_renameat2():
    """Try to load renameat2 from libc. Returns function or None."""
    try:
        libc_path = ctypes.util.find_library("c")
        if libc_path is None:
            return None
        libc = ctypes.CDLL(libc_path, use_errno=True)
        if not hasattr(libc, "renameat2"):
            return None
        func = libc.renameat2
        func.restype = ctypes.c_int
        func.argtypes = [
            ctypes.c_int,    # olddirfd
            ctypes.c_char_p, # oldpath
            ctypes.c_int,    # newdirfd
            ctypes.c_char_p, # newpath
            ctypes.c_uint,   # flags
        ]
        return func
    except (OSError, AttributeError):
        return None


_renameat2_func = _load_renameat2()


def _renameat2_exchange(dir_fd: int, old_name: str, new_name: str) -> None:
    """Call renameat2 with RENAME_EXCHANGE flag."""
    if _renameat2_func is None:
        raise AtomicMarkerUpdateUnsupportedError(
            "renameat2 is not available on this platform"
        )
    ret = _renameat2_func(
        dir_fd,
        old_name.encode("utf-8"),
        dir_fd,
        new_name.encode("utf-8"),
        _RENAME_EXCHANGE,
    )
    if ret != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), old_name)


def _dir_fsync_via_fd(dir_fd: int) -> None:
    """fsync a directory via its open fd."""
    os.fsync(dir_fd)


def _open_or_create_trusted_child_directory(
    parent: TrustedDirectory,
    child_name: str,
    *,
    creation_fault_point: str,
) -> TrustedDirectory:
    """Open a trusted child directory and durably record any creation.

    The child name is always resolved relative to ``parent.fd``.  If the
    initial open observes ENOENT, mkdir races are accepted, but the parent
    directory is fsynced before the child is opened and returned.
    """
    validate_bare_filename(child_name)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        child_fd = os.open(child_name, flags, dir_fd=parent.fd)
    except FileNotFoundError:
        created = False
        try:
            os.mkdir(child_name, dir_fd=parent.fd, mode=0o755)
            created = True
        except FileExistsError:
            # A concurrent creator won.  Its directory is still validated
            # below and the parent is fsynced before we trust it.
            pass
        if created:
            try:
                _inject_fault(creation_fault_point)
            except BaseException as exc:
                raise DirectoryCreationDurabilityError(
                    f"{child_name} created but parent-directory durability "
                    f"was not confirmed before {creation_fault_point}: {exc}",
                    directory_name=child_name,
                    created=True,
                ) from exc
        try:
            os.fsync(parent.fd)
        except OSError as exc:
            raise DirectoryCreationDurabilityError(
                f"{child_name} creation observed but parent-directory fsync failed: {exc}",
                directory_name=child_name,
                created=created,
            ) from exc
        try:
            child_fd = os.open(child_name, flags, dir_fd=parent.fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise PathSafetyError(
                    f"{child_name} is a symlink or not a directory"
                ) from exc
            raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise PathSafetyError(
                f"{child_name} is a symlink or not a directory"
            ) from exc
        raise
    try:
        child_stat = os.fstat(child_fd)
        if not statmod.S_ISDIR(child_stat.st_mode):
            raise PathSafetyError(f"{child_name} is not a directory")
    except BaseException:
        os.close(child_fd)
        raise
    return TrustedDirectory(
        path=parent.path / child_name,
        fd=child_fd,
        st_dev=child_stat.st_dev,
        st_ino=child_stat.st_ino,
    )


def prepare_validated_marker_bytes(
    marker_body: dict[str, Any],
    policy: MarkerValidationPolicy,
) -> bytes:
    """F1 — Prepare canonical marker bytes with validation BEFORE any FS op."""
    body = dict(marker_body)
    body.pop("marker_integrity_sha256", None)
    body["marker_integrity_sha256"] = compute_marker_integrity_sha256(body)
    validate_marker(body, policy=policy)
    return canonical_json_bytes(body)


def _cleanup_temp_durably(
    dir_fd: int,
    temp_name: str,
) -> tuple[bool, bool, str | None]:
    """Unlink a pre-commit temp and durably record its absence."""
    errors: list[str] = []
    temp_unlinked = False
    try:
        os.unlink(temp_name, dir_fd=dir_fd)
        temp_unlinked = True
    except FileNotFoundError:
        temp_unlinked = True
    except OSError as exc:
        errors.append(f"temp unlink failed: {exc}")
    parent_fsynced = False
    try:
        _dir_fsync_via_fd(dir_fd)
        parent_fsynced = True
    except OSError as exc:
        errors.append(f"directory fsync after temp cleanup failed: {exc}")
    confirmed = temp_unlinked and parent_fsynced
    return temp_unlinked, confirmed, "; ".join(errors) or None


def _mark_precommit_failed_clean(exc: BaseException) -> BaseException:
    """Attach the proved filesystem state without erasing error classification."""
    setattr(exc, "filesystem_state", "PRE_COMMIT_FAILED_CLEAN")
    setattr(exc, "committed", False)
    setattr(exc, "temp_unlinked", True)
    setattr(exc, "cleanup_durability_confirmed", True)
    return exc


def create_marker_no_replace_under_lock(
    guard: "RawChainLockGuard",
    directory: Path,
    marker_name: str,
    marker_body: dict[str, Any],
    policy: MarkerValidationPolicy,
) -> Path:
    """Create and durably verify a marker without replacing any destination.

    The temporary marker name is made durable before linking the canonical
    name. The canonical hardlink is verified by bytes and inode identity both
    before and after directory commit, and the source link is never followed
    if it is raced into a symlink.
    """
    assert_guard_valid(guard, directory, policy.manifest_prefix)
    validate_bare_filename(marker_name)
    canonical_bytes = prepare_validated_marker_bytes(marker_body, policy)
    canonical_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
    dir_fd = guard.trusted.fd

    try:
        existing_fd = os.open(
            marker_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd
        )
        os.close(existing_fd)
        raise FileExistsError(
            f"marker already exists: {marker_name} — "
            "use update_existing_marker_atomic_under_lock"
        )
    except FileNotFoundError:
        pass
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PathSafetyError(
                f"existing marker path is a symlink: {marker_name}"
            ) from exc
        raise

    temp_name = f"{marker_name}.tmp.{uuid.uuid4().hex}"
    temp_fd = -1
    try:
        temp_fd = os.open(
            temp_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o644,
            dir_fd=dir_fd,
        )
        _write_all_fd(temp_fd, canonical_bytes)
        os.fsync(temp_fd)
        temp_stat = os.fstat(temp_fd)
        if not statmod.S_ISREG(temp_stat.st_mode):
            raise PathSafetyError(
                f"marker temp is not a regular file: {temp_name}"
            )
    except BaseException as exc:
        close_error: OSError | None = None
        if temp_fd >= 0:
            try:
                os.close(temp_fd)
            except OSError as inner_exc:
                close_error = inner_exc
            temp_fd = -1
        temp_unlinked, confirmed, cleanup_error = _cleanup_temp_durably(
            dir_fd, temp_name
        )
        if close_error is not None or not confirmed:
            raise MarkerCreateCleanupPending(
                f"marker temp preparation failed ({exc}); "
                f"close_error={close_error}; cleanup={cleanup_error}",
                final_created=False,
                temp_unlinked=temp_unlinked,
                cleanup_durability_confirmed=confirmed,
                filesystem_state="PRE_COMMIT_CLEANUP_UNCONFIRMED",
            ) from exc
        _mark_precommit_failed_clean(exc)
        raise
    try:
        os.close(temp_fd)
    except OSError as exc:
        temp_unlinked, confirmed, cleanup_error = _cleanup_temp_durably(
            dir_fd, temp_name
        )
        raise MarkerCreateCleanupPending(
            f"marker temp close failed ({exc}); cleanup={cleanup_error}",
            final_created=False,
            temp_unlinked=temp_unlinked,
            cleanup_durability_confirmed=confirmed,
            filesystem_state=(
                "PRE_COMMIT_FAILED_CLEAN"
                if confirmed
                else "PRE_COMMIT_CLEANUP_UNCONFIRMED"
            ),
        ) from exc
    temp_fd = -1

    # Make the temp directory entry durable. A crash before the canonical link
    # can therefore be detected as an explicit marker-temp residue.
    try:
        _dir_fsync_via_fd(dir_fd)
    except OSError as exc:
        temp_unlinked, confirmed, cleanup_error = _cleanup_temp_durably(
            dir_fd, temp_name
        )
        if not confirmed:
            raise MarkerCreateCleanupPending(
                f"marker temp durability failed ({exc}); "
                f"cleanup={cleanup_error}",
                final_created=False,
                temp_unlinked=temp_unlinked,
                cleanup_durability_confirmed=False,
                filesystem_state="PRE_COMMIT_CLEANUP_UNCONFIRMED",
            ) from exc
        _mark_precommit_failed_clean(exc)
        raise

    try:
        temp_stat = _verify_published_file_fd(
            dir_fd=dir_fd,
            name=temp_name,
            expected_bytes=canonical_bytes,
            expected_sha256=canonical_sha256,
        )
    except BaseException as exc:
        temp_unlinked, confirmed, cleanup_error = _cleanup_temp_durably(
            dir_fd, temp_name
        )
        if not confirmed:
            raise MarkerCreateCleanupPending(
                f"marker temp verification failed ({exc}); "
                f"cleanup={cleanup_error}",
                final_created=False,
                temp_unlinked=temp_unlinked,
                cleanup_durability_confirmed=False,
                filesystem_state="PRE_COMMIT_CLEANUP_UNCONFIRMED",
            ) from exc
        _mark_precommit_failed_clean(exc)
        raise

    expected_identity = (
        temp_stat.st_dev,
        temp_stat.st_ino,
        temp_stat.st_size,
    )
    try:
        os.link(
            temp_name,
            marker_name,
            src_dir_fd=dir_fd,
            dst_dir_fd=dir_fd,
            follow_symlinks=False,
        )
    except BaseException as exc:
        temp_unlinked, confirmed, cleanup_error = _cleanup_temp_durably(
            dir_fd, temp_name
        )
        if not confirmed:
            raise MarkerCreateCleanupPending(
                f"marker final link failed ({exc}); cleanup={cleanup_error}",
                final_created=False,
                temp_unlinked=temp_unlinked,
                cleanup_durability_confirmed=False,
                filesystem_state="PRE_COMMIT_CLEANUP_UNCONFIRMED",
            ) from exc
        _mark_precommit_failed_clean(exc)
        raise

    try:
        _inject_fault(FAULT_CREATE_AFTER_FINAL_LINK)
    except BaseException as exc:
        raise MarkerCreateCleanupPending(
            f"final marker link exists but temp link remains and directory "
            f"durability is not confirmed: {exc}",
            final_created=True,
            temp_unlinked=False,
            cleanup_durability_confirmed=False,
            filesystem_state="COMMITTED_OLD_TEMP_PRESENT",
        ) from exc

    try:
        _verify_published_file_fd(
            dir_fd=dir_fd,
            name=marker_name,
            expected_bytes=canonical_bytes,
            expected_sha256=canonical_sha256,
            expected_identity=expected_identity,
        )
    except BaseException as exc:
        raise MarkerCreateCleanupPending(
            f"canonical marker verification failed before directory commit: {exc}",
            final_created=True,
            temp_unlinked=False,
            cleanup_durability_confirmed=False,
            filesystem_state="COMMITTED_OLD_TEMP_PRESENT",
        ) from exc

    # First directory fsync is the canonical-marker commit point. Keep the
    # temp hardlink until this succeeds so at least one verified name remains.
    try:
        _dir_fsync_via_fd(dir_fd)
    except OSError as exc:
        raise MarkerCreateCleanupPending(
            f"canonical marker link exists but first directory fsync failed: {exc}",
            final_created=True,
            temp_unlinked=False,
            cleanup_durability_confirmed=False,
            filesystem_state="COMMITTED_OLD_TEMP_PRESENT",
        ) from exc

    try:
        _verify_published_file_fd(
            dir_fd=dir_fd,
            name=marker_name,
            expected_bytes=canonical_bytes,
            expected_sha256=canonical_sha256,
            expected_identity=expected_identity,
        )
    except BaseException as exc:
        raise MarkerCreateCleanupPending(
            f"canonical marker changed after directory commit: {exc}",
            final_created=True,
            temp_unlinked=False,
            cleanup_durability_confirmed=False,
            filesystem_state="COMMITTED_OLD_TEMP_PRESENT",
        ) from exc

    try:
        os.unlink(temp_name, dir_fd=dir_fd)
    except OSError as exc:
        raise MarkerCreateCleanupPending(
            f"canonical marker is committed but temp unlink failed: {exc}",
            final_created=True,
            temp_unlinked=False,
            cleanup_durability_confirmed=False,
            filesystem_state="COMMITTED_OLD_TEMP_PRESENT",
        ) from exc

    try:
        _inject_fault(FAULT_CREATE_AFTER_TEMP_UNLINK)
    except BaseException as exc:
        raise MarkerCreateCleanupPending(
            f"canonical marker is committed and temp is absent, but cleanup "
            f"fsync is pending: {exc}",
            final_created=True,
            temp_unlinked=True,
            cleanup_durability_confirmed=False,
            filesystem_state="COMMITTED_OLD_TEMP_REMOVED_FSYNC_UNCONFIRMED",
        ) from exc

    try:
        _dir_fsync_via_fd(dir_fd)
    except OSError as exc:
        raise MarkerCreateCleanupPending(
            f"canonical marker is committed and temp was unlinked; second "
            f"directory fsync failed: {exc}",
            final_created=True,
            temp_unlinked=True,
            cleanup_durability_confirmed=False,
            filesystem_state="COMMITTED_OLD_TEMP_REMOVED_FSYNC_UNCONFIRMED",
        ) from exc

    try:
        _verify_published_file_fd(
            dir_fd=dir_fd,
            name=marker_name,
            expected_bytes=canonical_bytes,
            expected_sha256=canonical_sha256,
            expected_identity=expected_identity,
        )
    except BaseException as exc:
        raise MarkerCreateCleanupPending(
            f"canonical marker changed after committed-clean publication: {exc}",
            final_created=True,
            temp_unlinked=True,
            cleanup_durability_confirmed=True,
            filesystem_state="COMMITTED_CLEAN",
        ) from exc

    try:
        _inject_fault(FAULT_CREATE_AFTER_DIR_FSYNC)
    except BaseException as exc:
        raise MarkerPostCommitNotificationError(
            f"marker creation is committed-clean; post-commit hook failed: {exc}",
            operation="create",
        ) from exc
    return directory / marker_name


def _snapshot_dir_entry(dir_fd: int, name: str) -> dict[str, Any]:
    """Capture existence, identity and bytes for rollback ambiguity evidence."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        return {"exists": False, "name": name}
    except OSError as exc:
        return {"exists": None, "name": name, "error": str(exc)}
    try:
        entry_stat = os.fstat(fd)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        entry_bytes = b"".join(chunks)
        return {
            "exists": True,
            "name": name,
            "dev": entry_stat.st_dev,
            "ino": entry_stat.st_ino,
            "size": entry_stat.st_size,
            "bytes": entry_bytes,
            "sha256": hashlib.sha256(entry_bytes).hexdigest(),
        }
    finally:
        os.close(fd)


def _rollback_marker_update(
    dir_fd: int,
    marker_name: str,
    temp_name: str,
    original_stat: os.stat_result,
    original_bytes: bytes,
    cause: BaseException,
) -> NoReturn:
    """Restore and prove the original marker, or report an explicit ambiguity."""
    phase = "exchange"
    try:
        _inject_fault(FAULT_ROLLBACK_EXCHANGE_FAILURE)
        _renameat2_exchange(dir_fd, temp_name, marker_name)
        phase = "directory fsync"
        _inject_fault(FAULT_ROLLBACK_FSYNC_FAILURE)
        _dir_fsync_via_fd(dir_fd)
        phase = "original verification"
        fd = os.open(marker_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        try:
            restored_stat = os.fstat(fd)
            restored_bytes = b""
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                restored_bytes += chunk
        finally:
            os.close(fd)
        if ((restored_stat.st_dev, restored_stat.st_ino) !=
                (original_stat.st_dev, original_stat.st_ino) or
                restored_bytes != original_bytes):
            raise OSError("restored marker inode/dev or bytes mismatch")
        phase = "new temp unlink"
        _inject_fault(FAULT_ROLLBACK_TEMP_UNLINK_FAILURE)
        os.unlink(temp_name, dir_fd=dir_fd)
        _dir_fsync_via_fd(dir_fd)
        try:
            os.stat(temp_name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise OSError("rollback temp still exists")
    except Exception as rollback_exc:
        marker_snapshot = _snapshot_dir_entry(dir_fd, marker_name)
        temp_snapshot = _snapshot_dir_entry(dir_fd, temp_name)
        raise AtomicMarkerRollbackFailed(
            f"marker rollback failed at {phase}; marker={marker_name} temp={temp_name}; "
            f"original_dev={original_stat.st_dev} original_ino={original_stat.st_ino}; "
            f"cause={cause}; rollback_error={rollback_exc}; "
            f"marker_snapshot={marker_snapshot}; temp_snapshot={temp_snapshot}",
            failed_operation=phase,
            marker_snapshot=marker_snapshot,
            temp_snapshot=temp_snapshot,
        ) from rollback_exc
    restored_error = AtomicMarkerUpdateError(
        f"marker update failed before commit and original marker was restored: {cause}"
    )
    _mark_precommit_failed_clean(restored_error)
    raise restored_error from cause


def update_existing_marker_atomic_under_lock(
    guard: "RawChainLockGuard",
    directory: Path,
    marker_name: str,
    marker_body: dict[str, Any],
    policy: MarkerValidationPolicy,
) -> Path:
    """G3 — Transactional marker update via renameat2(RENAME_EXCHANGE) with rollback.

    Sequence:
      1. assert_guard_valid
      2. prepare_validated_marker_bytes — validate BEFORE any FS op
      3. Open existing marker (O_NOFOLLOW), capture inode/dev
      4. Write temp (O_CREAT | O_EXCL | O_WRONLY), fsync
      5. RENAME_EXCHANGE temp <-> marker
         — FAULT_AFTER_EXCHANGE
      6. Verify temp now has old marker inode/dev
         — FAULT_AFTER_NEW_MARKER_VERIFY
      7. Verify marker now has new bytes
      8. fsync directory — COMMIT POINT
         — FAULT_AFTER_FIRST_DIR_FSYNC
      9. unlink temp (old marker)
         — FAULT_AFTER_OLD_MARKER_UNLINK
     10. fsync directory
         — FAULT_AFTER_SECOND_DIR_FSYNC

    Rollback: if fault occurs before COMMIT POINT (step 8),
    RENAME_EXCHANGE again to restore, fsync, clean temp, raise.

    If fault occurs after COMMIT POINT but before cleanup (step 9 or 10),
    the new marker is authoritative; old marker is pending cleanup.
    Raise MarkerUpdateCleanupPending.
    """
    assert_guard_valid(guard, directory, policy.manifest_prefix)
    validate_bare_filename(marker_name)
    canonical_bytes = prepare_validated_marker_bytes(marker_body, policy)
    dir_fd = guard.trusted.fd

    # 3. Open existing marker, capture inode/dev
    try:
        existing_fd = os.open(marker_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"marker does not exist: {marker_name} — use create_marker_no_replace_under_lock"
        ) from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PathSafetyError(f"existing marker is a symlink: {marker_name}") from exc
        raise
    try:
        existing_stat = os.fstat(existing_fd)
        original_bytes = b""
        while True:
            chunk = os.read(existing_fd, 65536)
            if not chunk:
                break
            original_bytes += chunk
    finally:
        os.close(existing_fd)

    # 4. Write temp
    temp_name = f"{marker_name}.tmp.{uuid.uuid4().hex}"
    temp_fd = os.open(
        temp_name,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
        0o644,
        dir_fd=dir_fd,
    )
    file_obj = None
    try:
        file_obj = os.fdopen(temp_fd, "wb")
        temp_fd = -1
        with file_obj:
            file_obj.write(canonical_bytes)
            file_obj.flush()
            os.fsync(file_obj.fileno())
    except BaseException as exc:
        close_error: OSError | None = None
        if temp_fd >= 0:
            try:
                os.close(temp_fd)
            except OSError as inner_exc:
                close_error = inner_exc
        temp_unlinked, confirmed, cleanup_error = _cleanup_temp_durably(
            dir_fd, temp_name)
        if close_error is not None or not confirmed:
            raise MarkerUpdateCleanupPending(
                f"marker update temp preparation failed ({exc}); close_error={close_error}; "
                f"pre-commit cleanup not confirmed: {cleanup_error}",
                committed=False,
                temp_unlinked=temp_unlinked,
                cleanup_durability_confirmed=False,
                filesystem_state="PRE_COMMIT_CLEANUP_UNCONFIRMED",
            ) from exc
        _mark_precommit_failed_clean(exc)
        raise

    # 5. RENAME_EXCHANGE temp <-> marker
    try:
        _inject_fault(FAULT_BEFORE_EXCHANGE)
    except BaseException as exc:
        temp_unlinked, confirmed, cleanup_error = _cleanup_temp_durably(
            dir_fd, temp_name)
        if not confirmed:
            raise MarkerUpdateCleanupPending(
                f"pre-exchange hook failed ({exc}); cleanup not confirmed: {cleanup_error}",
                committed=False,
                temp_unlinked=temp_unlinked,
                cleanup_durability_confirmed=False,
                filesystem_state="PRE_COMMIT_CLEANUP_UNCONFIRMED",
            ) from exc
        clean_error = AtomicMarkerUpdateError(
            f"marker update failed before exchange and temp cleanup was durable: {exc}"
        )
        _mark_precommit_failed_clean(clean_error)
        raise clean_error from exc
    try:
        _renameat2_exchange(dir_fd, temp_name, marker_name)
    except AtomicMarkerUpdateUnsupportedError as exc:
        classified: BaseException = exc
    except OSError as exc:
        if exc.errno in (errno.ENOSYS, errno.EINVAL):
            classified = AtomicMarkerUpdateUnsupportedError(
                f"RENAME_EXCHANGE is unsupported: {exc}")
        elif exc.errno == errno.ENOENT:
            classified = AtomicMarkerTargetChangedError(
                f"marker target changed or disappeared before RENAME_EXCHANGE: {exc}")
        elif exc.errno == errno.ELOOP:
            classified = PathSafetyError(
                f"symlink encountered during RENAME_EXCHANGE: {exc}")
        else:
            classified = AtomicMarkerUpdateError(
                f"RENAME_EXCHANGE operational failure: {exc}")
    else:
        classified = None
    if classified is not None:
        temp_unlinked, confirmed, cleanup_error = _cleanup_temp_durably(
            dir_fd, temp_name)
        if not confirmed:
            raise MarkerUpdateCleanupPending(
                f"{classified}; pre-commit temp cleanup not confirmed: {cleanup_error}",
                committed=False,
                temp_unlinked=temp_unlinked,
                cleanup_durability_confirmed=False,
                filesystem_state="PRE_COMMIT_CLEANUP_UNCONFIRMED",
            ) from classified
        _mark_precommit_failed_clean(classified)
        raise classified

    # FAULT_AFTER_EXCHANGE
    try:
        _inject_fault(FAULT_AFTER_EXCHANGE)
    except Exception as exc:
        _rollback_marker_update(
            dir_fd, marker_name, temp_name, existing_stat, original_bytes, exc)

    # 6. Verify temp now has old marker inode/dev
    try:
        verify_fd = os.open(temp_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        _rollback_marker_update(
            dir_fd, marker_name, temp_name, existing_stat, original_bytes, exc)
    try:
        verify_stat = os.fstat(verify_fd)
    finally:
        os.close(verify_fd)
    if verify_stat.st_dev != existing_stat.st_dev or verify_stat.st_ino != existing_stat.st_ino:
        mismatch = AtomicMarkerUpdateError(
            f"RENAME_EXCHANGE verification failed: temp inode/dev mismatch. "
            f"Expected dev={existing_stat.st_dev} ino={existing_stat.st_ino}, "
            f"got dev={verify_stat.st_dev} ino={verify_stat.st_ino}."
        )
        _rollback_marker_update(
            dir_fd, marker_name, temp_name, existing_stat, original_bytes, mismatch)

    # FAULT_AFTER_NEW_MARKER_VERIFY
    try:
        _inject_fault(FAULT_AFTER_NEW_MARKER_VERIFY)
    except Exception as exc:
        _rollback_marker_update(
            dir_fd, marker_name, temp_name, existing_stat, original_bytes, exc)

    # 7. Verify marker now has new bytes (read and compare)
    new_marker_fd = -1
    try:
        new_marker_fd = os.open(marker_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        new_bytes = b""
        while True:
            chunk = os.read(new_marker_fd, 65536)
            if not chunk:
                break
            new_bytes += chunk
    except OSError as exc:
        _rollback_marker_update(
            dir_fd, marker_name, temp_name, existing_stat, original_bytes, exc)
    finally:
        if new_marker_fd >= 0:
            os.close(new_marker_fd)
    if new_bytes != canonical_bytes:
        _rollback_marker_update(
            dir_fd, marker_name, temp_name, existing_stat, original_bytes,
            AtomicMarkerUpdateError("new marker bytes do not match canonical_bytes"))

    # 8. fsync directory — COMMIT POINT
    try:
        _inject_fault(FAULT_BEFORE_FIRST_DIR_FSYNC)
    except Exception as exc:
        _rollback_marker_update(
            dir_fd, marker_name, temp_name, existing_stat, original_bytes, exc)
    try:
        _dir_fsync_via_fd(dir_fd)
    except OSError as exc:
        _rollback_marker_update(
            dir_fd, marker_name, temp_name, existing_stat, original_bytes, exc)

    # FAULT_AFTER_FIRST_DIR_FSYNC — after commit point
    try:
        _inject_fault(FAULT_AFTER_FIRST_DIR_FSYNC)
    except Exception as exc:
        # After commit point — new marker is authoritative.
        # Old marker (in temp) is pending cleanup.
        raise MarkerUpdateCleanupPending(
            f"fault after first dir fsync (commit point reached). "
            f"New marker is authoritative. Old marker in {temp_name} pending cleanup. "
            f"Original fault: {exc}",
            committed=True,
            temp_unlinked=False,
            cleanup_durability_confirmed=False,
            filesystem_state="COMMITTED_OLD_TEMP_PRESENT",
        ) from exc

    # 9. unlink temp (old marker)
    try:
        os.unlink(temp_name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass  # Already cleaned up
    except OSError as exc:
        raise MarkerUpdateCleanupPending(
            f"cannot unlink old marker temp {temp_name}: {exc}. "
            f"New marker is authoritative.",
            committed=True,
            temp_unlinked=False,
            cleanup_durability_confirmed=False,
            filesystem_state="COMMITTED_OLD_TEMP_PRESENT",
        ) from exc

    # FAULT_AFTER_OLD_MARKER_UNLINK
    try:
        _inject_fault(FAULT_AFTER_OLD_MARKER_UNLINK)
    except Exception as exc:
        raise MarkerUpdateCleanupPending(
            f"fault after old marker unlink. "
            f"New marker is authoritative. "
            f"Original fault: {exc}",
            committed=True,
            temp_unlinked=True,
            cleanup_durability_confirmed=False,
            filesystem_state="COMMITTED_OLD_TEMP_REMOVED_FSYNC_UNCONFIRMED",
        ) from exc

    # 10. fsync directory
    try:
        _dir_fsync_via_fd(dir_fd)
    except OSError as exc:
        raise MarkerUpdateCleanupPending(
            f"new marker committed and old marker unlinked; second directory fsync failed: {exc}",
            committed=True,
            temp_unlinked=True,
            cleanup_durability_confirmed=False,
            filesystem_state="COMMITTED_OLD_TEMP_REMOVED_FSYNC_UNCONFIRMED",
        ) from exc

    # FAULT_AFTER_SECOND_DIR_FSYNC
    try:
        _inject_fault(FAULT_AFTER_SECOND_DIR_FSYNC)
    except Exception as exc:
        raise MarkerPostCommitNotificationError(
            f"marker update is committed-clean; post-commit hook failed: {exc}",
            operation="update",
        ) from exc

    return directory / marker_name


# ═══════════════════════════════════════════════════════════════════════
# Section 6 — G2: Locking with reservations + LockReleaseError + BROKEN
# ═══════════════════════════════════════════════════════════════════════

_ACTIVE_GUARDS_LOCK = threading.Lock()
_ACTIVE_GUARDS: dict[str, "GuardRecord"] = {}
_CHAIN_RESERVATIONS: set[tuple[int, int, str]] = set()


@dataclass(frozen=True)
class GuardRecord:
    """F5/G2 — Registry record for an active guard."""
    guard: "RawChainLockGuard"
    pid: int
    directory: Path
    prefix: str
    lock_fd: int
    st_dev: int        # lock file dev
    st_ino: int        # lock file ino
    trusted_dev: int   # trusted directory dev (G2: chain key)
    trusted_ino: int   # trusted directory ino (G2: chain key)
    lock_name: str
    token: str
    health: GuardHealth


@dataclass
class GuardReleaseState:
    """Monotonic release progress for a retryable BROKEN guard."""

    flock_released: bool = False
    lock_fd_closed: bool = False
    trusted_fd_closed: bool = False


@dataclass(frozen=True)
class RawChainLockGuard:
    """G2 — Proof that the caller holds the raw chain flock.

    Carries the TrustedDirectory (G1) and the lock_fd. The guard is the
    sole proof of holding the lock; the registry is authoritative.
    """
    directory: Path
    prefix: str
    lock_fd: int
    pid: int
    token: str
    trusted: TrustedDirectory
    _closed: bool = False
    _health: GuardHealth = "ACTIVE"
    _release_state: GuardReleaseState = field(default_factory=GuardReleaseState)

    def __enter__(self) -> "RawChainLockGuard":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """G2 — Close the guard. Does NOT silence errors.

        If flock unlock or fd close fails, raises LockReleaseError and
        marks the guard as BROKEN in the registry.
        """
        if getattr(self, "_closed", False):
            return
        release_errors: list[str] = []
        state = self._release_state
        if not state.flock_released:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            except OSError as exc:
                release_errors.append(f"flock unlock failed: {exc}")
            else:
                state.flock_released = True
        # If unlock failed, retain the lock fd so a retry can unlock it.
        if state.flock_released and not state.lock_fd_closed:
            try:
                os.close(self.lock_fd)
            except OSError as exc:
                release_errors.append(f"close lock_fd failed: {exc}")
            else:
                state.lock_fd_closed = True
        if not state.trusted_fd_closed:
            try:
                self.trusted.close()
            except OSError as exc:
                release_errors.append(f"close trusted fd failed: {exc}")
            else:
                state.trusted_fd_closed = True

        all_released = (
            state.flock_released
            and state.lock_fd_closed
            and state.trusted_fd_closed
        )
        object.__setattr__(self, "_closed", all_released)
        object.__setattr__(self, "_health", "CLOSED" if all_released else "BROKEN")
        with _ACTIVE_GUARDS_LOCK:
            record = _ACTIVE_GUARDS.get(self.token)
            if record is not None:
                if all_released:
                    _ACTIVE_GUARDS.pop(self.token, None)
                else:
                    object.__setattr__(record, "health", "BROKEN")
        if release_errors:
            raise LockReleaseError("; ".join(release_errors))
        if not all_released:
            raise LockReleaseError(
                "guard release incomplete without an operating-system error"
            )


def _release_unregistered_lock_strict(lock_fd: int, trusted: TrustedDirectory) -> None:
    errors: list[str] = []
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError as exc:
        errors.append(f"flock unlock failed: {exc}")
    try:
        os.close(lock_fd)
    except OSError as exc:
        errors.append(f"lock fd close failed: {exc}")
    try:
        trusted.close()
    except OSError as exc:
        errors.append(f"trusted directory close failed: {exc}")
    if errors:
        raise LockReleaseError("; ".join(errors))


@dataclass(frozen=True)
class RawChainLock:
    """G2 — Factory for RawChainLockGuard.

    Sequence:
      1. validate_safe_prefix
      2. Open trusted root directory (G1)
      3. Open lock file with O_NOFOLLOW
      4. Reserve chain key (under mutex)
      5. Release mutex
      6. Acquire flock (may block)
      7. Reacquire mutex
      8. Check for active guard on same chain
      9. Convert reservation to active guard
     10. Release mutex
    """
    directory: Path
    prefix: str

    def __post_init__(self):
        validate_safe_prefix(self.prefix)

    def acquire(self) -> RawChainLockGuard:
        # 1. validate_safe_prefix (done in __post_init__)
        # 2. Open trusted root
        trusted = open_trusted_directory(self.directory)
        # 3. Open lock file with O_NOFOLLOW
        lock_name = f"{self.prefix}.lock"
        try:
            lock_fd = os.open(
                lock_name,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o644,
                dir_fd=trusted.fd,
            )
        except OSError as exc:
            trusted.close()
            if exc.errno == errno.ELOOP:
                raise PathSafetyError(f"lock file is a symlink: {lock_name}") from exc
            raise LockAcquisitionError(f"cannot open lock file: {exc}") from exc
        # 4. Reserve chain key (under mutex)
        chain_key = (trusted.st_dev, trusted.st_ino, self.prefix)
        with _ACTIVE_GUARDS_LOCK:
            # Check for existing active guard on same chain
            for record in _ACTIVE_GUARDS.values():
                if (record.trusted_dev == trusted.st_dev and
                    record.trusted_ino == trusted.st_ino and
                    record.prefix == self.prefix and
                    record.health in ("ACTIVE", "BROKEN")):
                    os.close(lock_fd)
                    trusted.close()
                    raise NestedLockingError(
                        f"a guard is already active for "
                        f"directory={trusted.path} prefix={self.prefix}"
                    )
            if chain_key in _CHAIN_RESERVATIONS:
                os.close(lock_fd)
                trusted.close()
                raise NestedLockingError(
                    f"a reservation is already pending for chain {chain_key}"
                )
            _CHAIN_RESERVATIONS.add(chain_key)
        # 5. Release mutex (implicit — exited with block)
        # 6. Acquire flock (may block — no mutex held)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except OSError as exc:
            with _ACTIVE_GUARDS_LOCK:
                _CHAIN_RESERVATIONS.discard(chain_key)
            os.close(lock_fd)
            trusted.close()
            raise LockAcquisitionError(
                f"fcntl.flock(LOCK_EX) failed: {exc}. "
                f"Filesystem may not support flock."
            ) from exc
        # 7. Reacquire mutex
        with _ACTIVE_GUARDS_LOCK:
            _CHAIN_RESERVATIONS.discard(chain_key)
            # 8. Check for active guard on same chain (again, under mutex)
            for record in _ACTIVE_GUARDS.values():
                if (record.trusted_dev == trusted.st_dev and
                    record.trusted_ino == trusted.st_ino and
                    record.prefix == self.prefix and
                    record.health in ("ACTIVE", "BROKEN")):
                    # Another thread acquired between our check and flock
                    _release_unregistered_lock_strict(lock_fd, trusted)
                    raise NestedLockingError(
                        f"another guard became active while waiting for flock"
                    )
            # Capture st_dev/st_ino of the lock file
            try:
                st = os.fstat(lock_fd)
            except OSError as exc:
                _release_unregistered_lock_strict(lock_fd, trusted)
                raise LockAcquisitionError(f"fstat of lock file failed: {exc}") from exc
            token = str(uuid.uuid4())
            guard = RawChainLockGuard(
                directory=trusted.path,
                prefix=self.prefix,
                lock_fd=lock_fd,
                pid=os.getpid(),
                token=token,
                trusted=trusted,
            )
            record = GuardRecord(
                guard=guard,
                pid=os.getpid(),
                directory=trusted.path,
                prefix=self.prefix,
                lock_fd=lock_fd,
                st_dev=st.st_dev,
                st_ino=st.st_ino,
                trusted_dev=trusted.st_dev,
                trusted_ino=trusted.st_ino,
                lock_name=lock_name,
                token=token,
                health="ACTIVE",
            )
            _ACTIVE_GUARDS[token] = record
        # 10. Release mutex (implicit)
        return guard


def assert_guard_valid(
    guard: RawChainLockGuard,
    directory: Path,
    prefix: str,
) -> None:
    """G2 — Authoritative guard validation."""
    if not isinstance(guard, RawChainLockGuard):
        raise GuardValidationError(
            f"guard must be RawChainLockGuard, got {type(guard).__name__}"
        )
    if getattr(guard, "_closed", False):
        raise GuardValidationError("guard is already closed")
    if getattr(guard, "_health", "ACTIVE") != "ACTIVE":
        raise GuardValidationError(f"guard health is {guard._health}")
    if guard.pid != os.getpid():
        raise GuardValidationError(
            f"guard PID mismatch: guard.pid={guard.pid} os.getpid()={os.getpid()}"
        )
    if guard.directory != directory:
        raise GuardValidationError(
            f"guard directory mismatch: guard.directory={guard.directory} "
            f"expected={directory}"
        )
    if guard.prefix != prefix:
        raise GuardValidationError(
            f"guard prefix mismatch: guard.prefix={guard.prefix!r} "
            f"expected={prefix!r}"
        )
    with _ACTIVE_GUARDS_LOCK:
        record = _ACTIVE_GUARDS.get(guard.token)
        if record is None:
            raise GuardValidationError(
                f"guard token {guard.token} is not in the active registry"
            )
        if record.guard is not guard:
            raise GuardValidationError(
                f"guard object mismatch: registry record.guard is not this guard"
            )
        if record.lock_fd != guard.lock_fd:
            raise GuardValidationError(
                f"guard fd mismatch: registry fd={record.lock_fd} guard fd={guard.lock_fd}"
            )
        try:
            current_st = os.fstat(guard.lock_fd)
        except OSError as exc:
            raise GuardValidationError(
                f"guard lock_fd {guard.lock_fd} is not a valid open fd: {exc}"
            ) from exc
        if current_st.st_dev != record.st_dev or current_st.st_ino != record.st_ino:
            raise GuardValidationError(
                f"guard fd no longer points to the lock file: "
                f"fd dev={current_st.st_dev} ino={current_st.st_ino} vs "
                f"registry dev={record.st_dev} ino={record.st_ino}"
            )
        try:
            path_st = os.stat(
                record.lock_name,
                dir_fd=guard.trusted.fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise GuardValidationError(f"lock path lstat failed: {exc}") from exc
        if statmod.S_ISLNK(path_st.st_mode):
            raise GuardValidationError(
                f"lock name was replaced with a symlink: {record.lock_name}"
            )
        if path_st.st_dev != record.st_dev or path_st.st_ino != record.st_ino:
            raise GuardValidationError(
                f"lock path was replaced: "
                f"path dev={path_st.st_dev} ino={path_st.st_ino} vs "
                f"registry dev={record.st_dev} ino={record.st_ino}"
            )
        # Verify trusted directory fd is still valid
        try:
            td_st = os.fstat(guard.trusted.fd)
        except OSError as exc:
            raise GuardValidationError(
                f"trusted directory fd is not valid: {exc}"
            ) from exc
        if td_st.st_dev != guard.trusted.st_dev or td_st.st_ino != guard.trusted.st_ino:
            raise GuardValidationError(
                f"trusted directory fd no longer points to original: "
                f"fd dev={td_st.st_dev} ino={td_st.st_ino} vs "
                f"original dev={guard.trusted.st_dev} ino={guard.trusted.st_ino}"
            )



# ═══════════════════════════════════════════════════════════════════════
# Section 7 — Types / models
# ═══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SealedRawArtifact:
    """A2 — Sealed descriptor returned by RawScanStager.seal()."""
    version: int
    staging_filename: str
    final_name: str
    run_id: str
    scan_id: str
    event_count: int
    condition_ids: tuple[str, ...]
    file_sha256: str
    canonical_events_sha256: str
    size_bytes: int
    sealed_at: str
    device_id: int
    inode: int


@dataclass
class RawArtifactTransfer:
    """Owns the sealed read-only inode and its trusted pending directory."""
    sealed: SealedRawArtifact
    ownership_token: str
    staging_filename: str
    staging_fd: int
    pending_directory: TrustedDirectory
    _closed: bool = False

    @property
    def staging_path(self) -> Path:
        """Informational only; fd + dir_fd are the security authority."""
        return self.pending_directory.path / self.staging_filename

    def close(self) -> None:
        if self._closed:
            return
        errors: list[str] = []
        if self.staging_fd >= 0:
            try:
                os.close(self.staging_fd)
                self.staging_fd = -1
            except OSError as exc:
                errors.append(f"close transfer staging fd failed: {exc}")
        try:
            self.pending_directory.close()
        except OSError as exc:
            errors.append(f"close transfer pending directory failed: {exc}")
        if errors:
            raise RawEventPersistenceError("; ".join(errors))
        self._closed = True

    def __enter__(self) -> "RawArtifactTransfer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass(frozen=True)
class PublishResult:
    """A8 — Result of publish_raw_scan() (Phase II)."""
    status: PublishResultStatus
    manifest_entry: dict[str, Any] | None = None
    failure_stage: str | None = None
    failure_message: str | None = None


@dataclass(frozen=True)
class DiagnosticEvidence:
    """E4, F7, G6 — Durable diagnostic evidence."""
    diagnostic_version: str
    transaction_uuid: str
    ownership_token: str
    diagnostic_created_at: str
    triggering_state: str
    failure_stage: str
    failure_type: str
    failure_message: str
    staging_filename: str
    staging_sha256: str | None
    staging_size_bytes: int | None
    marker_filename: str | None
    marker_integrity_sha256: str | None
    events_appended_before_failure: int | None
    events_appended_total_expected: int | None
    recoverable: bool
    diagnostic_integrity_sha256: str
    evidence_location: EvidenceLocation
    evidence_filename: str
    secondary_evidence_location: EvidenceLocation | None
    secondary_evidence_filename: str | None


# ═══════════════════════════════════════════════════════════════════════
# Section 8 — RawScanStager (G4/G5/G6/G7)
# ═══════════════════════════════════════════════════════════════════════

_RAW_EVENT_REQUIRED_FIELDS: Final[frozenset[str]] = frozenset({
    "received_at_utc", "source", "endpoint", "payload",
    "payload_sha256", "schema_version",
})


def _safe_scan_id(scan_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in scan_id)
    return safe[:100]


def load_raw_events_strict(path: Path) -> list[dict[str, Any]]:
    """Load raw events from a gzipped JSONL file with strict validation.

    F6 rule 7: Recomputes and verifies payload_sha256 for every event.
    """
    events: list[dict[str, Any]] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line_num, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                raise ValueError(f"Empty line {line_num} in {path.name}")
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON at line {line_num} in {path.name}: {exc}"
                ) from exc
            if not isinstance(event, dict):
                raise ValueError(f"Non-dict payload at line {line_num} in {path.name}")
            missing = _RAW_EVENT_REQUIRED_FIELDS - set(event.keys())
            if missing:
                raise ValueError(f"Missing fields {missing} at line {line_num} in {path.name}")
            payload = event["payload"]
            recomputed = canonical_payload_sha256(payload)
            if recomputed != event["payload_sha256"]:
                raise ValueError(
                    f"payload_sha256 mismatch at line {line_num} in {path.name}: "
                    f"computed={recomputed} stored={event['payload_sha256']}"
                )
            events.append(event)
    return events


def load_raw_events_strict_fd(fd: int) -> list[dict[str, Any]]:
    """G4 — Load raw events from an open fd (gzipped JSONL)."""
    events: list[dict[str, Any]] = []
    # Seek to beginning before reading
    os.lseek(fd, 0, os.SEEK_SET)
    # Read all bytes from fd into a bytes buffer, then gzip-decode
    raw = b""
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        raw += chunk
    import io
    with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as handle:
        text = handle.read().decode("utf-8")
    for line_num, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            raise ValueError(f"Empty line {line_num}")
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON at line {line_num}: {exc}") from exc
        if not isinstance(event, dict):
            raise ValueError(f"Non-dict payload at line {line_num}")
        missing = _RAW_EVENT_REQUIRED_FIELDS - set(event.keys())
        if missing:
            raise ValueError(f"Missing fields {missing} at line {line_num}")
        payload = event["payload"]
        recomputed = canonical_payload_sha256(payload)
        if recomputed != event["payload_sha256"]:
            raise ValueError(
                f"payload_sha256 mismatch at line {line_num}: "
                f"computed={recomputed} stored={event['payload_sha256']}"
            )
        events.append(event)
    return events


def _compute_sha256_from_fd(fd: int) -> str:
    """G4 — Compute SHA-256 from an open fd by reading all bytes."""
    h = hashlib.sha256()
    # Seek to beginning
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


# ═══════════════════════════════════════════════════════════════════════
# Section 8 — Phase II-A transactional raw publisher
# ═══════════════════════════════════════════════════════════════════════

_MANIFEST_FILE_FIELDS: Final[frozenset[str]] = frozenset(
    REQUIRED_CANDIDATE_MANIFEST_FIELDS
)


def _read_all_fd(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _open_regular_readonly_under_directory(
    dir_fd: int,
    name: str,
) -> tuple[int, os.stat_result]:
    validate_bare_filename(name)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PathSafetyError(f"symlink forbidden: {name}") from exc
        raise
    try:
        entry_stat = os.fstat(fd)
        if not statmod.S_ISREG(entry_stat.st_mode):
            raise PathSafetyError(f"published entry is not a regular file: {name}")
        return fd, entry_stat
    except BaseException:
        os.close(fd)
        raise


def _read_validated_manifest_chain_under_lock(
    *,
    guard: RawChainLockGuard,
    raw_directory: Path,
    policy: MarkerValidationPolicy,
) -> list[dict[str, Any]]:
    """Read and validate the complete canonical manifest chain via root dir_fd."""
    assert_guard_valid(guard, raw_directory, policy.manifest_prefix)
    prefix = policy.manifest_prefix
    manifest_re = re.compile(rf"^{re.escape(prefix)}_(\d{{6}})\.json$")
    marker_re = re.compile(
        rf"^{re.escape(prefix)}_txn_(\d{{6}})_"
        rf"([0-9a-f]{{8}}-[0-9a-f]{{4}}-4[0-9a-f]{{3}}-"
        rf"[89ab][0-9a-f]{{3}}-[0-9a-f]{{12}})\.marker$"
    )
    marker_temp_re = re.compile(
        rf"^{re.escape(prefix)}_txn_(\d{{6}})_"
        rf"([0-9a-f]{{8}}-[0-9a-f]{{4}}-4[0-9a-f]{{3}}-"
        rf"[89ab][0-9a-f]{{3}}-[0-9a-f]{{12}})"
        rf"\.marker\.tmp\.[0-9a-f]{{32}}$"
    )
    manifest_names: list[tuple[int, str]] = []
    marker_names: list[str] = []
    marker_temp_names: list[str] = []
    for name in os.listdir(guard.trusted.fd):
        manifest_match = manifest_re.fullmatch(name)
        if manifest_match is not None:
            manifest_names.append((int(manifest_match.group(1)), name))
            continue
        if name.startswith(f"{prefix}_") and name.endswith(".json"):
            raise MarkerValidationError(f"non-canonical manifest filename: {name}")
        marker_match = marker_re.fullmatch(name)
        if marker_match is not None:
            marker_names.append(name)
            continue
        marker_temp_match = marker_temp_re.fullmatch(name)
        if marker_temp_match is not None:
            marker_temp_names.append(name)
            continue
        if name.startswith(f"{prefix}_txn_") and name.endswith(".marker"):
            raise MarkerValidationError(f"non-canonical transaction marker filename: {name}")
        if name.startswith(f"{prefix}_txn_") and ".marker.tmp." in name:
            raise MarkerValidationError(
                f"non-canonical transaction marker temp filename: {name}"
            )

    active_markers: list[tuple[str, str]] = []
    for name in sorted(marker_names):
        fd, _ = _open_regular_readonly_under_directory(guard.trusted.fd, name)
        try:
            raw = _read_all_fd(fd)
        finally:
            os.close(fd)
        marker = parse_marker(raw)
        validate_marker(marker, policy)
        expected_name = marker_filename(
            prefix, marker["sequence"], marker["transaction_uuid"]
        )
        if name != expected_name:
            raise MarkerValidationError(
                f"transaction marker filename/body mismatch: {name} != {expected_name}"
            )
        active_markers.append((name, marker["status"]))
    if active_markers or marker_temp_names:
        raise RecoveryRequiredError(
            "transaction marker evidence requires recovery: "
            f"markers={active_markers} temps={sorted(marker_temp_names)}"
        )

    if not manifest_names:
        return []
    manifest_names.sort()
    observed_sequences = [sequence for sequence, _ in manifest_names]
    expected_sequences = list(range(len(manifest_names)))
    if observed_sequences != expected_sequences:
        raise MarkerValidationError(
            f"manifest chain is not contiguous: observed={observed_sequences} "
            f"expected={expected_sequences}"
        )

    entries: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    previous_hash: str | None = None
    for sequence, name in manifest_names:
        fd, _ = _open_regular_readonly_under_directory(guard.trusted.fd, name)
        try:
            raw = _read_all_fd(fd)
        finally:
            os.close(fd)
        try:
            entry = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarkerValidationError(f"manifest {name} is not valid JSON: {exc}") from exc
        if not isinstance(entry, dict):
            raise MarkerValidationError(f"manifest {name} root must be an object")
        if set(entry) != _MANIFEST_FILE_FIELDS:
            raise MarkerValidationError(
                f"manifest {name} fields differ from schema: {sorted(entry)}"
            )
        _validate_candidate_manifest_fields(entry)
        if canonical_manifest_file_bytes(entry) != raw:
            raise MarkerValidationError(f"manifest {name} bytes are not canonical")
        if entry["sequence"] != sequence:
            raise MarkerValidationError(
                f"manifest filename/sequence mismatch: {name} vs {entry['sequence']}"
            )
        expected_hash = compute_manifest_hash(entry)
        if entry["manifest_hash"] != expected_hash:
            raise MarkerValidationError(
                f"manifest {name} hash mismatch: expected={expected_hash} "
                f"stored={entry['manifest_hash']}"
            )
        if entry["previous_manifest_hash"] != previous_hash:
            raise MarkerValidationError(
                f"manifest {name} previous hash mismatch: "
                f"expected={previous_hash!r} stored={entry['previous_manifest_hash']!r}"
            )
        if entry["condition_ids"] != sorted(set(entry["condition_ids"])):
            raise MarkerValidationError(f"manifest {name} condition_ids are not canonical")
        identity = (entry["run_id"], entry["scan_id"])
        if identity in identities:
            raise IdentityCollisionError(f"duplicate run_id/scan_id in manifest chain: {identity}")
        identities.add(identity)
        entries.append(entry)
        previous_hash = entry["manifest_hash"]
    return entries


def _validate_transfer_for_publish(
    *,
    transfer: RawArtifactTransfer,
    guard: RawChainLockGuard,
    policy: MarkerValidationPolicy,
) -> list[dict[str, Any]]:
    if not isinstance(transfer, RawArtifactTransfer):
        raise RawArtifactTransactionError(
            f"transfer must be RawArtifactTransfer, got {type(transfer).__name__}"
        )
    if transfer._closed:
        raise RawArtifactTransactionError("transfer is already closed")
    if transfer.staging_fd < 0:
        raise RawArtifactTransactionError("transfer staging fd is invalid")
    if transfer.pending_directory._closed:
        raise RawArtifactTransactionError("transfer pending directory is closed")
    sealed = transfer.sealed
    if not isinstance(sealed, SealedRawArtifact):
        raise RawArtifactTransactionError("transfer sealed descriptor is invalid")
    if transfer.staging_filename != sealed.staging_filename:
        raise RawArtifactTransactionError("transfer staging filename differs from sealed descriptor")
    validate_bare_filename(transfer.staging_filename)
    validate_bare_filename(sealed.final_name)
    if policy.artifact_filename_pattern.fullmatch(sealed.final_name) is None:
        raise RawArtifactTransactionError(
            f"sealed final name does not match artifact policy: {sealed.final_name}"
        )
    try:
        access_mode = fcntl.fcntl(transfer.staging_fd, fcntl.F_GETFL) & os.O_ACCMODE
    except OSError as exc:
        raise RawArtifactTransactionError(f"cannot inspect staging fd flags: {exc}") from exc
    if access_mode != os.O_RDONLY:
        raise RawArtifactTransactionError("transfer staging fd is not O_RDONLY")

    pending_root_stat = os.stat(".pending", dir_fd=guard.trusted.fd, follow_symlinks=False)
    if not statmod.S_ISDIR(pending_root_stat.st_mode):
        raise PathSafetyError(".pending under raw root is not a directory")
    pending_fd_stat = os.fstat(transfer.pending_directory.fd)
    if ((pending_root_stat.st_dev, pending_root_stat.st_ino) !=
            (pending_fd_stat.st_dev, pending_fd_stat.st_ino)):
        raise PathSafetyError("transfer pending directory does not belong to guarded raw root")

    fd_stat = os.fstat(transfer.staging_fd)
    if not statmod.S_ISREG(fd_stat.st_mode):
        raise RawArtifactTransactionError("transfer staging fd is not a regular file")
    name_stat = os.stat(
        transfer.staging_filename,
        dir_fd=transfer.pending_directory.fd,
        follow_symlinks=False,
    )
    expected_identity = (sealed.device_id, sealed.inode, sealed.size_bytes)
    if (fd_stat.st_dev, fd_stat.st_ino, fd_stat.st_size) != expected_identity:
        raise RawArtifactTransactionError("staging fd identity/size differs from sealed descriptor")
    if (name_stat.st_dev, name_stat.st_ino, name_stat.st_size) != expected_identity:
        raise RawArtifactTransactionError("staging name identity/size differs from sealed descriptor")
    if statmod.S_IMODE(fd_stat.st_mode) != 0o444:
        raise RawArtifactTransactionError(
            f"sealed staging mode must be 0444, got {oct(statmod.S_IMODE(fd_stat.st_mode))}"
        )
    if _compute_sha256_from_fd(transfer.staging_fd) != sealed.file_sha256:
        raise RawArtifactTransactionError("sealed file_sha256 does not match staging bytes")
    reread_fd = os.dup(transfer.staging_fd)
    try:
        events = load_raw_events_strict_fd(reread_fd)
    finally:
        os.close(reread_fd)
    if canonical_events_sha256(events) != sealed.canonical_events_sha256:
        raise RawArtifactTransactionError("canonical_events_sha256 mismatch")
    if len(events) != sealed.event_count:
        raise RawArtifactTransactionError("event_count mismatch")
    condition_id_set: set[str] = set()
    for event in events:
        condition_id = event.get("requested_condition_id", "")
        if condition_id and not isinstance(condition_id, str):
            raise RawArtifactTransactionError(
                "requested_condition_id must be a string when present"
            )
        if condition_id:
            condition_id_set.add(condition_id)
    condition_ids = sorted(condition_id_set)
    if tuple(condition_ids) != sealed.condition_ids:
        raise RawArtifactTransactionError("condition_ids mismatch")
    return events


def _write_all_fd(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "short write while publishing")
        offset += written


def _verify_published_file_fd(
    *,
    dir_fd: int,
    name: str,
    expected_bytes: bytes | None = None,
    expected_sha256: str | None = None,
    expected_identity: tuple[int, int, int] | None = None,
) -> os.stat_result:
    fd, entry_stat = _open_regular_readonly_under_directory(dir_fd, name)
    try:
        actual = _read_all_fd(fd)
    finally:
        os.close(fd)
    if expected_bytes is not None and actual != expected_bytes:
        raise RawArtifactTransactionError(f"published bytes mismatch for {name}")
    if expected_sha256 is not None:
        actual_hash = hashlib.sha256(actual).hexdigest()
        if actual_hash != expected_sha256:
            raise RawArtifactTransactionError(
                f"published sha256 mismatch for {name}: {actual_hash} != {expected_sha256}"
            )
    if expected_identity is not None:
        identity = (entry_stat.st_dev, entry_stat.st_ino, entry_stat.st_size)
        if identity != expected_identity:
            raise RawArtifactTransactionError(
                f"published inode/dev/size mismatch for {name}: {identity} != {expected_identity}"
            )
    return entry_stat


def _publish_bytes_no_replace_under_lock(
    *,
    dir_fd: int,
    final_name: str,
    payload: bytes,
    after_link_fault: str,
    after_dir_fsync_fault: str,
) -> None:
    validate_bare_filename(final_name)
    temp_name = f".{final_name}.tmp.{uuid.uuid4().hex}"
    temp_fd = -1
    final_created = False
    try:
        temp_fd = os.open(
            temp_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=dir_fd,
        )
        _write_all_fd(temp_fd, payload)
        os.fchmod(temp_fd, 0o444)
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = -1
        os.link(
            temp_name,
            final_name,
            src_dir_fd=dir_fd,
            dst_dir_fd=dir_fd,
            follow_symlinks=False,
        )
        final_created = True
        _inject_fault(after_link_fault)
        os.fsync(dir_fd)
        _inject_fault(after_dir_fsync_fault)
        os.unlink(temp_name, dir_fd=dir_fd)
        os.fsync(dir_fd)
        _verify_published_file_fd(
            dir_fd=dir_fd,
            name=final_name,
            expected_bytes=payload,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )
    except BaseException:
        if temp_fd >= 0:
            os.close(temp_fd)
        # Before a final link exists, cleanup is safe only when its absence is
        # durably recorded.  Once linked, retain the temp as recovery evidence.
        if not final_created:
            temp_unlinked, confirmed, cleanup_error = _cleanup_temp_durably(
                dir_fd, temp_name)
            if not confirmed:
                raise RawArtifactTransactionError(
                    f"pre-link temp cleanup not durable for {final_name}: "
                    f"temp_unlinked={temp_unlinked} error={cleanup_error}"
                )
        raise


def _publisher_filesystem_snapshot(
    *,
    root_fd: int,
    pending_fd: int,
    marker_name: str | None,
    staging_name: str,
    final_name: str,
    sidecar_name: str,
    manifest_name: str,
) -> dict[str, Any]:
    def snapshot(fd: int, name: str) -> dict[str, Any]:
        if fd < 0:
            return {"name": name, "exists": None, "error": "directory fd closed"}
        return _snapshot_dir_entry(fd, name)

    root_names = os.listdir(root_fd) if root_fd >= 0 else []
    return {
        "marker": snapshot(root_fd, marker_name) if marker_name else None,
        "staging": snapshot(pending_fd, staging_name),
        "artifact": snapshot(root_fd, final_name),
        "sidecar": snapshot(root_fd, sidecar_name),
        "manifest": snapshot(root_fd, manifest_name),
        "temporary_names": sorted(
            name for name in root_names if ".tmp." in name
        ),
    }


def _marker_body_for_candidate(
    *,
    transfer: RawArtifactTransfer,
    candidate: dict[str, Any],
    manifest_name: str,
    transaction_uuid: str,
    status: MarkerStatus,
    resolution: MarkerResolution,
    recoverable: bool,
) -> dict[str, Any]:
    sealed = transfer.sealed
    manifest_bytes = canonical_manifest_file_bytes(candidate)
    body = {
        "transaction_version": MARKER_VERSION,
        "transaction_uuid": transaction_uuid,
        "ownership_token": transfer.ownership_token,
        "status": status,
        "resolution": resolution,
        "sequence": candidate["sequence"],
        "run_id": sealed.run_id,
        "scan_id": sealed.scan_id,
        "staging_filename": sealed.staging_filename,
        "final_name": sealed.final_name,
        "sidecar_name": sealed.final_name + ".sha256",
        "manifest_name": manifest_name,
        "device_id": sealed.device_id,
        "inode": sealed.inode,
        "size_bytes": sealed.size_bytes,
        "file_sha256": sealed.file_sha256,
        "canonical_events_sha256": sealed.canonical_events_sha256,
        "event_count": sealed.event_count,
        "condition_ids": list(sealed.condition_ids),
        "previous_manifest_hash": candidate["previous_manifest_hash"],
        "candidate_manifest": candidate,
        "candidate_manifest_bytes_base64": base64.b64encode(manifest_bytes).decode("ascii"),
        "candidate_manifest_bytes_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "manifest_created_at": candidate["created_at"],
        "recoverable": recoverable,
    }
    return body


def _update_transaction_marker_status(
    *,
    guard: RawChainLockGuard,
    raw_directory: Path,
    policy: MarkerValidationPolicy,
    marker_name: str,
    marker_body: dict[str, Any],
    status: MarkerStatus,
) -> dict[str, Any]:
    updated = dict(marker_body)
    updated["status"] = status
    updated["resolution"] = "COMMITTED" if status == "COMMITTED" else "ACTIVE"
    updated["recoverable"] = status != "COMMITTED"
    updated.pop("marker_integrity_sha256", None)
    try:
        update_existing_marker_atomic_under_lock(
            guard, raw_directory, marker_name, updated, policy)
    except BaseException as exc:
        if (isinstance(exc, MarkerPostCommitNotificationError) or
                isinstance(exc, MarkerUpdateCleanupPending) and exc.committed):
            setattr(exc, "durable_marker_status", status)
        raise
    marker_bytes = prepare_validated_marker_bytes(updated, policy)
    return parse_marker(marker_bytes)


def publish_raw_scan(
    *,
    transfer: RawArtifactTransfer,
    guard: RawChainLockGuard,
    raw_directory: Path,
    policy: MarkerValidationPolicy,
    manifest_created_at: str,
) -> PublishResult:
    """Publish one sealed raw scan as an ordered, durable transaction.

    Ownership remains with the caller until the STAGED marker exists.  Once
    that marker is durable (or potentially durable), the publisher consumes
    and closes the transfer on every path.  Recovery is intentionally out of
    scope: any pre-existing marker blocks publication.
    """
    assert_guard_valid(guard, raw_directory, policy.manifest_prefix)
    _validate_iso8601_utc_strict(manifest_created_at, "manifest_created_at")
    _validate_transfer_for_publish(transfer=transfer, guard=guard, policy=policy)
    chain = _read_validated_manifest_chain_under_lock(
        guard=guard, raw_directory=raw_directory, policy=policy)
    sealed = transfer.sealed
    if any((entry["run_id"], entry["scan_id"]) == (sealed.run_id, sealed.scan_id)
           for entry in chain):
        raise IdentityCollisionError(
            f"run_id/scan_id already published: {(sealed.run_id, sealed.scan_id)}"
        )
    sequence = len(chain)
    previous_hash = chain[-1]["manifest_hash"] if chain else None
    candidate: dict[str, Any] = {
        "sequence": sequence,
        "run_id": sealed.run_id,
        "scan_id": sealed.scan_id,
        "filename": sealed.final_name,
        "file_sha256": sealed.file_sha256,
        "canonical_events_sha256": sealed.canonical_events_sha256,
        "event_count": sealed.event_count,
        "condition_ids": list(sealed.condition_ids),
        "previous_manifest_hash": previous_hash,
        "created_at": manifest_created_at,
    }
    candidate["manifest_hash"] = compute_manifest_hash(candidate)
    manifest_bytes = canonical_manifest_file_bytes(candidate)
    sidecar_name = sealed.final_name + ".sha256"
    sidecar_bytes = f"{sealed.file_sha256}  {sealed.final_name}\n".encode("ascii")
    manifest_name = f"{policy.manifest_prefix}_{sequence:06d}.json"
    transaction_uuid = str(uuid.uuid4())
    marker_name = marker_filename(policy.manifest_prefix, sequence, transaction_uuid)
    marker_body = _marker_body_for_candidate(
        transfer=transfer,
        candidate=candidate,
        manifest_name=manifest_name,
        transaction_uuid=transaction_uuid,
        status="STAGED",
        resolution="ACTIVE",
        recoverable=True,
    )
    # Validate the entire marker/candidate binding before any publication.
    marker_body = parse_marker(prepare_validated_marker_bytes(marker_body, policy))

    try:
        _inject_fault(FAULT_PUBLISH_BEFORE_STAGED_MARKER)
    except BaseException as exc:
        snapshot = _publisher_filesystem_snapshot(
            root_fd=guard.trusted.fd,
            pending_fd=transfer.pending_directory.fd,
            marker_name=None,
            staging_name=sealed.staging_filename,
            final_name=sealed.final_name,
            sidecar_name=sidecar_name,
            manifest_name=manifest_name,
        )
        raise PublishTransactionFailure(
            f"publish failed before STAGED marker: {exc}",
            failure_stage="P0_CANDIDATE",
            durable_marker_status=None,
            marker_filename=None,
            transfer_consumed=False,
            committed=False,
            cleanup_pending=False,
            filesystem_snapshot=snapshot,
        ) from exc
    consumed = False
    durable_status: str | None = None
    committed = False
    stage = "P1_STAGED_MARKER"
    try:
        try:
            create_marker_no_replace_under_lock(
                guard, raw_directory, marker_name, marker_body, policy)
            consumed = True
            durable_status = "STAGED"
        except MarkerCreateCleanupPending as exc:
            consumed = exc.final_created
            durable_status = "STAGED" if exc.final_created else None
            raise
        except MarkerPostCommitNotificationError:
            consumed = True
            durable_status = "STAGED"
            raise
        _inject_fault(FAULT_PUBLISH_AFTER_STAGED_MARKER)

        stage = "P2_ARTIFACT"
        os.link(
            sealed.staging_filename,
            sealed.final_name,
            src_dir_fd=transfer.pending_directory.fd,
            dst_dir_fd=guard.trusted.fd,
            follow_symlinks=False,
        )
        _inject_fault(FAULT_PUBLISH_AFTER_ARTIFACT_LINK)
        _verify_published_file_fd(
            dir_fd=guard.trusted.fd,
            name=sealed.final_name,
            expected_sha256=sealed.file_sha256,
            expected_identity=(sealed.device_id, sealed.inode, sealed.size_bytes),
        )
        os.fsync(guard.trusted.fd)
        _inject_fault(FAULT_PUBLISH_AFTER_ARTIFACT_DIR_FSYNC)
        marker_body = _update_transaction_marker_status(
            guard=guard,
            raw_directory=raw_directory,
            policy=policy,
            marker_name=marker_name,
            marker_body=marker_body,
            status="ARTIFACT_PUBLISHED",
        )
        durable_status = "ARTIFACT_PUBLISHED"
        _inject_fault(FAULT_PUBLISH_AFTER_ARTIFACT_MARKER_UPDATE)

        stage = "P3_SIDECAR"
        _publish_bytes_no_replace_under_lock(
            dir_fd=guard.trusted.fd,
            final_name=sidecar_name,
            payload=sidecar_bytes,
            after_link_fault=FAULT_PUBLISH_AFTER_SIDECAR_LINK,
            after_dir_fsync_fault=FAULT_PUBLISH_AFTER_SIDECAR_DIR_FSYNC,
        )
        marker_body = _update_transaction_marker_status(
            guard=guard,
            raw_directory=raw_directory,
            policy=policy,
            marker_name=marker_name,
            marker_body=marker_body,
            status="SIDECAR_PUBLISHED",
        )
        durable_status = "SIDECAR_PUBLISHED"
        _inject_fault(FAULT_PUBLISH_AFTER_SIDECAR_MARKER_UPDATE)

        stage = "P4_MANIFEST"
        _publish_bytes_no_replace_under_lock(
            dir_fd=guard.trusted.fd,
            final_name=manifest_name,
            payload=manifest_bytes,
            after_link_fault=FAULT_PUBLISH_AFTER_MANIFEST_LINK,
            after_dir_fsync_fault=FAULT_PUBLISH_AFTER_MANIFEST_DIR_FSYNC,
        )
        published_manifest = json.loads(manifest_bytes)
        if published_manifest != candidate or compute_manifest_hash(published_manifest) != candidate["manifest_hash"]:
            raise RawArtifactTransactionError("published manifest validation failed")
        marker_body = _update_transaction_marker_status(
            guard=guard,
            raw_directory=raw_directory,
            policy=policy,
            marker_name=marker_name,
            marker_body=marker_body,
            status="MANIFEST_PUBLISHED",
        )
        durable_status = "MANIFEST_PUBLISHED"
        _inject_fault(FAULT_PUBLISH_AFTER_MANIFEST_MARKER_UPDATE)

        stage = "P5_COMMITTED_MARKER"
        marker_body = _update_transaction_marker_status(
            guard=guard,
            raw_directory=raw_directory,
            policy=policy,
            marker_name=marker_name,
            marker_body=marker_body,
            status="COMMITTED",
        )
        durable_status = "COMMITTED"
        committed = True
        _inject_fault(FAULT_PUBLISH_AFTER_COMMITTED_MARKER)

        stage = "P6_PENDING_CLEANUP"
        os.unlink(sealed.staging_filename, dir_fd=transfer.pending_directory.fd)
        _inject_fault(FAULT_PUBLISH_AFTER_STAGING_UNLINK)
        os.fsync(transfer.pending_directory.fd)
        _inject_fault(FAULT_PUBLISH_AFTER_PENDING_DIR_FSYNC)
        transfer.close()
        _inject_fault(FAULT_PUBLISH_AFTER_TRANSFER_CLOSE)

        stage = "P7_MARKER_CLEANUP"
        os.unlink(marker_name, dir_fd=guard.trusted.fd)
        _inject_fault(FAULT_PUBLISH_AFTER_MARKER_UNLINK)
        os.fsync(guard.trusted.fd)
        try:
            os.stat(marker_name, dir_fd=guard.trusted.fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RawArtifactTransactionError("transaction marker still exists after cleanup")
        try:
            _inject_fault(FAULT_PUBLISH_AFTER_FINAL_ROOT_FSYNC)
        except BaseException as exc:
            snapshot = _publisher_filesystem_snapshot(
                root_fd=guard.trusted.fd,
                pending_fd=-1,
                marker_name=marker_name,
                staging_name=sealed.staging_filename,
                final_name=sealed.final_name,
                sidecar_name=sidecar_name,
                manifest_name=manifest_name,
            )
            raise PublishPostCommitNotificationError(
                f"publisher committed-clean; post-commit hook failed: {exc}",
                failure_stage=stage,
                durable_marker_status="COMMITTED",
                marker_filename=marker_name,
                transfer_consumed=True,
                committed=True,
                cleanup_pending=False,
                filesystem_snapshot=snapshot,
            ) from exc
        return PublishResult(status="PUBLISHED", manifest_entry=candidate)
    except PublishPostCommitNotificationError:
        raise
    except BaseException as exc:
        if getattr(exc, "durable_marker_status", None) is not None:
            durable_status = exc.durable_marker_status
            committed = durable_status == "COMMITTED"
        close_error: BaseException | None = None
        if consumed and not transfer._closed:
            try:
                transfer.close()
            except BaseException as inner_exc:
                close_error = inner_exc
        pending_fd = (
            transfer.pending_directory.fd
            if not transfer.pending_directory._closed else -1
        )
        snapshot = _publisher_filesystem_snapshot(
            root_fd=guard.trusted.fd,
            pending_fd=pending_fd,
            marker_name=marker_name if consumed else None,
            staging_name=sealed.staging_filename,
            final_name=sealed.final_name,
            sidecar_name=sidecar_name,
            manifest_name=manifest_name,
        )
        cleanup_pending = committed or close_error is not None
        error_type = PublishCleanupPending if committed else PublishTransactionFailure
        detail = f"{exc}"
        if close_error is not None:
            detail += f"; transfer close failed: {close_error}"
        raise error_type(
            f"publish failed at {stage}: {detail}",
            failure_stage=stage,
            durable_marker_status=durable_status,
            marker_filename=marker_name if consumed else None,
            transfer_consumed=consumed,
            committed=committed,
            cleanup_pending=cleanup_pending,
            filesystem_snapshot=snapshot,
        ) from exc


@dataclass
class RawScanStager:
    """G4/G5/G6/G7 — Isolated raw scan stager, completely dir_fd-based.

    G4: All operations use dir_fd (raw_dir_fd, pending_dir_fd).
        No Path.exists(), Path.read_bytes(), Path.unlink(), gzip.open(path),
        os.chmod(path), or resolve() as authority.
    G5: __exit__ propagates failures (gzip close, cleanup, diagnostic).
    G6: Evidence location state machine.
    G7: Evidence always read-only via fchmod before publishing.
    """
    run_id: str
    scan_id: str
    raw_dir: Path
    _state: StagerState = "OPEN"
    _events: list[dict[str, Any]] = field(default_factory=list)
    _condition_ids: set[str] = field(default_factory=set)
    _sealed_descriptor: SealedRawArtifact | None = None
    _transferred: bool = False
    _ownership_token: str | None = None
    # G4: dir fds
    _raw_dir_fd: int = -1
    _pending_dir_fd: int = -1
    _staging_fd: int = -1
    _raw_trusted: TrustedDirectory | None = None
    _pending_trusted: TrustedDirectory | None = None
    _quarantine_trusted: TrustedDirectory | None = None
    _staging_name: str = ""
    _raw_file: Any = None
    _gzip_handle: Any = None
    # G6 flags
    _entered: bool = False
    _write_attempted: bool = False
    _persistence_failure: bool = False
    _seal_started: bool = False
    _diagnostic_evidence: tuple[str, str] | None = None

    def __enter__(self) -> "RawScanStager":
        if self._entered:
            raise StagerStateError("stager already entered — cannot enter twice")
        if self._state != "OPEN":
            raise StagerStateError(f"cannot enter from state {self._state}")
        trusted_raw: TrustedDirectory | None = None
        pending_trusted: TrustedDirectory | None = None
        staging_fd = -1
        dup_fd = -1
        raw_file = None
        gzip_handle = None
        staging_name = ""
        try:
            trusted_raw = open_trusted_directory(self.raw_dir)
            _inject_fault(FAULT_ENTER_AFTER_RAW_DIR_OPEN)
            pending_trusted = _open_or_create_trusted_child_directory(
                trusted_raw,
                ".pending",
                creation_fault_point=FAULT_PENDING_AFTER_MKDIR_BEFORE_PARENT_FSYNC,
            )
            _inject_fault(FAULT_ENTER_AFTER_PENDING_OPEN)
            safe_id = _safe_scan_id(self.scan_id)
            staging_name = f"raw_scan_{safe_id}_{uuid.uuid4().hex[:12]}.jsonl.gz.tmp"
            staging_fd = os.open(
                staging_name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                0o644, dir_fd=pending_trusted.fd,
            )
            _inject_fault(FAULT_ENTER_AFTER_STAGING_CREATE)
            dup_fd = os.dup(staging_fd)
            _inject_fault(FAULT_ENTER_AFTER_DUP)
            raw_file = os.fdopen(dup_fd, "ab", closefd=True)
            dup_fd = -1
            gzip_handle = gzip.GzipFile(fileobj=raw_file, mode="ab")
            _inject_fault(FAULT_ENTER_AFTER_GZIP_CREATE)
        except BaseException:
            cleanup_errors: list[str] = []
            for label, resource in (("gzip", gzip_handle), ("raw file", raw_file)):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception as close_exc:
                        cleanup_errors.append(f"{label} close failed: {close_exc}")
            if dup_fd >= 0:
                try:
                    os.close(dup_fd)
                except OSError as close_exc:
                    cleanup_errors.append(f"dup close failed: {close_exc}")
            if staging_fd >= 0:
                try:
                    os.close(staging_fd)
                except OSError as close_exc:
                    cleanup_errors.append(f"staging close failed: {close_exc}")
            if staging_name and pending_trusted is not None:
                try:
                    os.unlink(staging_name, dir_fd=pending_trusted.fd)
                    os.fsync(pending_trusted.fd)
                except FileNotFoundError:
                    pass
                except OSError as cleanup_exc:
                    cleanup_errors.append(f"staging rollback failed: {cleanup_exc}")
            for label, trusted in (("pending", pending_trusted), ("raw", trusted_raw)):
                if trusted is not None:
                    try:
                        trusted.close()
                    except OSError as close_exc:
                        cleanup_errors.append(f"{label} directory close failed: {close_exc}")
            if cleanup_errors:
                raise RawEventPersistenceError("enter rollback: " + "; ".join(cleanup_errors))
            raise
        self._raw_trusted = trusted_raw
        self._pending_trusted = pending_trusted
        self._raw_dir_fd = trusted_raw.fd
        self._pending_dir_fd = pending_trusted.fd
        self._staging_fd = staging_fd
        self._staging_name = staging_name
        self._raw_file = raw_file
        self._gzip_handle = gzip_handle
        self._entered = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        """G5 — Propagate failures. Do NOT silence errors."""
        original_exception = exc_val if isinstance(exc_val, BaseException) else None
        try:
            if self._state == "TRANSFERRED":
                self._close_resources_strict(close_staging=False, close_pending=False)
                return False
            if self._state in ("ABORTED_WITH_DIAGNOSTIC_EVIDENCE",
                               "BLOCKED_DIAGNOSTIC_PERSISTENCE"):
                self._close_resources_strict()
                return False
            if self._state == "SEALED":
                self._delete_staging_safely()
                self._state = "ABORTED_BEFORE_TRANSFER"
            elif exc_type is not None and (self._write_attempted or self._seal_started):
                self._preserve_diagnostic_evidence(
                    f"OPEN_EXCEPTION ({exc_type.__name__})",
                    original_exception or RuntimeError(str(exc_val)),
                )
                self._state = "ABORTED_WITH_DIAGNOSTIC_EVIDENCE"
            else:
                self._delete_staging_safely()
                self._state = "ABORTED_BEFORE_TRANSFER"
            self._close_resources_strict(close_staging=False)
        except RawEventPersistenceError as cleanup_exc:
            self._state = "BLOCKED_DIAGNOSTIC_PERSISTENCE"
            raise DiagnosticPersistenceError(
                f"strict exit cleanup failed: {cleanup_exc}"
            ) from (original_exception or cleanup_exc)
        return False

    def _close_resources_strict(self, *, close_staging: bool = True,
                                close_pending: bool = True,
                                close_raw: bool = True) -> None:
        """Close every resource owned by this stager and aggregate failures."""
        errors: list[str] = []
        for attribute, label in (("_gzip_handle", "gzip"), ("_raw_file", "raw file")):
            resource = getattr(self, attribute)
            if resource is not None:
                try:
                    resource.close()
                    setattr(self, attribute, None)
                except Exception as exc:
                    errors.append(f"{label} close failed: {exc}")
        if close_staging and self._staging_fd >= 0:
            try:
                os.close(self._staging_fd)
                self._staging_fd = -1
            except OSError as exc:
                errors.append(f"staging fd close failed: {exc}")
        if close_pending and self._pending_trusted is not None:
            try:
                self._pending_trusted.close()
                self._pending_trusted = None
                self._pending_dir_fd = -1
            except OSError as exc:
                errors.append(f"pending directory close failed: {exc}")
        if self._quarantine_trusted is not None:
            try:
                self._quarantine_trusted.close()
                self._quarantine_trusted = None
            except OSError as exc:
                errors.append(f"quarantine directory close failed: {exc}")
        if close_raw and self._raw_trusted is not None:
            try:
                self._raw_trusted.close()
                self._raw_trusted = None
                self._raw_dir_fd = -1
            except OSError as exc:
                errors.append(f"raw directory close failed: {exc}")
        if errors:
            raise RawEventPersistenceError("; ".join(errors))

    def append_event(self, event: dict[str, Any]) -> None:
        """G4/G6 — Append a raw event with fail-closed lifecycle."""
        if self._state != "OPEN":
            raise StagerStateError(f"cannot append_event from state {self._state}")
        if self._gzip_handle is None or self._staging_fd < 0:
            raise StagerStateError("stager not initialized — use 'with' statement")
        if not isinstance(event, dict):
            raise RawEventPersistenceError(
                f"event must be dict, got {type(event).__name__}"
            )
        missing = _RAW_EVENT_REQUIRED_FIELDS - set(event.keys())
        if missing:
            raise RawEventPersistenceError(f"event missing required fields {missing}")
        self._write_attempted = True
        try:
            line = json.dumps(
                event, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ) + "\n"
        except (ValueError, TypeError) as exc:
            self._fail_with_diagnostic("APPEND_EVENT_SERIALIZE", exc)
        try:
            self._gzip_handle.write(line.encode("utf-8"))
            self._gzip_handle.flush()
            os.fsync(self._gzip_handle.fileno())
        except (OSError, ValueError, RuntimeError, EOFError, gzip.BadGzipFile) as exc:
            self._fail_with_diagnostic("APPEND_EVENT_FSYNC", exc)
        self._events.append(event)
        cid = event.get("requested_condition_id", "")
        if cid:
            self._condition_ids.add(cid)

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def condition_ids(self) -> list[str]:
        return sorted(self._condition_ids)

    @property
    def state(self) -> StagerState:
        return self._state

    @property
    def staging_path(self) -> Path:
        """For compatibility — returns the absolute path of staging."""
        return self.raw_dir / ".pending" / self._staging_name

    def seal(self) -> SealedRawArtifact:
        """G4/G7 — Seal with fail-closed lifecycle. D2 definitive order."""
        if self._state != "OPEN":
            raise StagerStateError(f"cannot seal from state {self._state}")
        if self._gzip_handle is None or self._staging_fd < 0:
            raise StagerStateError("stager not initialized")
        self._seal_started = True
        # 1. flush
        try:
            self._gzip_handle.flush()
        except (OSError, ValueError, RuntimeError, EOFError, gzip.BadGzipFile) as exc:
            self._fail_with_diagnostic("SEAL_FLUSH", exc)
        # 2. close
        try:
            self._gzip_handle.close()
        except (OSError, ValueError, RuntimeError, EOFError, gzip.BadGzipFile) as exc:
            self._gzip_handle = None
            self._fail_with_diagnostic("SEAL_GZIP_CLOSE", exc)
        self._gzip_handle = None
        if self._raw_file is not None:
            try:
                self._raw_file.close()
            except (OSError, ValueError, RuntimeError) as exc:
                self._fail_with_diagnostic("SEAL_RAW_FILE_CLOSE", exc)
            self._raw_file = None
        # 3. fsync staging fd
        try:
            os.fsync(self._staging_fd)
        except OSError as exc:
            self._fail_with_diagnostic("SEAL_FSYNC", exc)
        # 4. fstat staging fd
        try:
            st = os.fstat(self._staging_fd)
        except OSError as exc:
            self._fail_with_diagnostic("SEAL_FSTAT", exc)
        # 5. chmod 0o444 via fchmod (G4: no os.chmod(path))
        try:
            os.fchmod(self._staging_fd, 0o444)
        except OSError as exc:
            self._fail_with_diagnostic("SEAL_CHMOD", exc)
        # 6. fsync staging fd again after chmod
        try:
            os.fsync(self._staging_fd)
        except OSError as exc:
            self._fail_with_diagnostic("SEAL_FSYNC_AFTER_CHMOD", exc)
        # 7. fsync .pending directory
        try:
            os.fsync(self._pending_dir_fd)
        except OSError as exc:
            self._fail_with_diagnostic("SEAL_DIR_FSYNC", exc)
        # 8. strict reread via dup fd
        reread_fd = -1
        try:
            reread_fd = os.dup(self._staging_fd)
            disk_events = load_raw_events_strict_fd(reread_fd)
            os.close(reread_fd)
            reread_fd = -1
        except (ValueError, OSError, RuntimeError, EOFError, gzip.BadGzipFile) as exc:
            if reread_fd >= 0:
                try:
                    os.close(reread_fd)
                except OSError as close_exc:
                    exc = RuntimeError(f"{exc}; reread fd close failed: {close_exc}")
            self._fail_with_diagnostic("SEAL_STRICT_REREAD", exc)
        # 9. verify event count
        if len(disk_events) != len(self._events):
            self._fail_with_diagnostic(
                "SEAL_EVENT_COUNT_MISMATCH",
                RuntimeError(f"disk={len(disk_events)} != memory={len(self._events)}"),
            )
        disk_condition_ids: set[str] = set()
        for ev in disk_events:
            cid = ev.get("requested_condition_id", "")
            if cid:
                disk_condition_ids.add(cid)
        disk_canonical_sha = canonical_events_sha256(disk_events)
        # 10. file_sha256 from fd
        try:
            file_sha = _compute_sha256_from_fd(self._staging_fd)
        except (OSError, ValueError, RuntimeError) as exc:
            self._fail_with_diagnostic("SEAL_FILE_HASH", exc)
        # 10b. Drop the O_RDWR description permanently and reopen by trusted
        # dir_fd as O_RDONLY. chmod cannot revoke write access from an open fd.
        writable_fd = self._staging_fd
        try:
            os.close(writable_fd)
            self._staging_fd = -1
            readonly_fd = os.open(
                self._staging_name, os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=self._pending_dir_fd,
            )
            self._staging_fd = readonly_fd
            readonly_stat = os.fstat(readonly_fd)
            if ((readonly_stat.st_dev, readonly_stat.st_ino, readonly_stat.st_size) !=
                    (st.st_dev, st.st_ino, st.st_size)):
                raise OSError("sealed inode/dev/size changed during read-only reopen")
            if statmod.S_IMODE(readonly_stat.st_mode) != 0o444:
                raise OSError(
                    f"sealed mode is {oct(statmod.S_IMODE(readonly_stat.st_mode))}, expected 0o444")
            if _compute_sha256_from_fd(readonly_fd) != file_sha:
                raise OSError("sealed hash changed during read-only reopen")
        except (OSError, ValueError, RuntimeError) as exc:
            self._fail_with_diagnostic("SEAL_READONLY_REOPEN", exc)
        # 11. build SealedRawArtifact
        safe_id = _safe_scan_id(self.scan_id)
        scan_id_hash = hashlib.sha256(self.scan_id.encode("utf-8")).hexdigest()[:12]
        final_name = f"raw_scan_{safe_id}_{scan_id_hash}.events.jsonl.gz"
        sealed_at = datetime.now(timezone.utc).isoformat()
        descriptor = SealedRawArtifact(
            version=1,
            staging_filename=self._staging_name,
            final_name=final_name,
            run_id=self.run_id,
            scan_id=self.scan_id,
            event_count=len(disk_events),
            condition_ids=tuple(sorted(disk_condition_ids)),
            file_sha256=file_sha,
            canonical_events_sha256=disk_canonical_sha,
            size_bytes=st.st_size,
            sealed_at=sealed_at,
            device_id=st.st_dev,
            inode=st.st_ino,
        )
        self._sealed_descriptor = descriptor
        self._state = "SEALED"
        return descriptor

    def transfer(self) -> RawArtifactTransfer:
        if self._state != "SEALED":
            raise StagerStateError(
                f"cannot transfer from state {self._state} — must be SEALED"
            )
        if self._transferred:
            raise StagerStateError("already transferred")
        if self._sealed_descriptor is None:
            raise StagerStateError("internal: sealed descriptor missing")
        self._transferred = True
        self._ownership_token = str(uuid.uuid4())
        if self._staging_fd < 0 or self._pending_trusted is None:
            raise StagerStateError("sealed resources are unavailable for transfer")
        transfer_fd = self._staging_fd
        transfer_pending = self._pending_trusted
        self._staging_fd = -1
        self._pending_trusted = None
        self._pending_dir_fd = -1
        self._state = "TRANSFERRED"
        return RawArtifactTransfer(
            sealed=self._sealed_descriptor,
            ownership_token=self._ownership_token,
            staging_filename=self._staging_name,
            staging_fd=transfer_fd,
            pending_directory=transfer_pending,
        )

    # ─── G6/G7: fail-closed internal methods ───

    def _fail_with_diagnostic(self, stage: str, exc: BaseException) -> NoReturn:
        """G6 — Single route for diagnostic preservation."""
        secondary_errors: list[str] = []
        if self._gzip_handle is not None:
            try:
                self._gzip_handle.close()
                self._gzip_handle = None
            except Exception as close_exc:
                secondary_errors.append(f"gzip close failed: {close_exc}")
        if self._raw_file is not None:
            try:
                self._raw_file.close()
                self._raw_file = None
            except Exception as close_exc:
                secondary_errors.append(f"raw file close failed: {close_exc}")
        diagnostic_exc: BaseException = exc
        if secondary_errors:
            diagnostic_exc = RuntimeError(f"{exc}; secondary close failures: {'; '.join(secondary_errors)}")
        self._persistence_failure = True
        if self._write_attempted or self._seal_started:
            try:
                self._preserve_diagnostic_evidence(stage, diagnostic_exc)
                self._close_resources_strict()
                self._state = "ABORTED_WITH_DIAGNOSTIC_EVIDENCE"
            except Exception as diag_exc:
                self._state = "BLOCKED_DIAGNOSTIC_PERSISTENCE"
                raise DiagnosticPersistenceError(
                    f"failed to persist diagnostic evidence for {stage}: {diag_exc}"
                ) from diag_exc
        else:
            try:
                self._delete_staging_safely()
            except RawEventPersistenceError as del_exc:
                self._state = "BLOCKED_DIAGNOSTIC_PERSISTENCE"
                raise DiagnosticPersistenceError(
                    f"failed to clean up staging after {stage}: {del_exc}"
                ) from exc
            self._state = "ABORTED_BEFORE_TRANSFER"
        raise RawEventPersistenceError(f"{stage}: {exc}") from exc

    def _delete_staging_safely(self) -> None:
        """G4 — Delete staging via dir_fd. Does NOT silence errors."""
        if not self._staging_name:
            return
        if self._staging_fd >= 0:
            try:
                os.close(self._staging_fd)
            except OSError as exc:
                raise RawEventPersistenceError(
                    f"failed to close staging {self._staging_name}: {exc}") from exc
            self._staging_fd = -1
        try:
            os.unlink(self._staging_name, dir_fd=self._pending_dir_fd)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RawEventPersistenceError(
                f"failed to unlink staging {self._staging_name}: {exc}"
            ) from exc
        try:
            os.fsync(self._pending_dir_fd)
        except OSError as exc:
            raise RawEventPersistenceError(
                f"staging file {self._staging_name} was deleted; "
                f"durability of deletion not confirmed: {exc}"
            ) from exc

    def _preserve_diagnostic_evidence(self, stage: str, exc: BaseException) -> None:
        """G6/G7 — Preserve staging and write diagnostic JSON.

        G6: Evidence location state machine:
          PENDING_ONLY → QUARANTINE_LINKED_AND_PENDING_PRESENT → QUARANTINE_ONLY

        G7: Evidence is set to 0o444 via fchmod before publishing.
        """
        if self._staging_fd < 0 or not self._staging_name:
            raise OSError("no staging fd for diagnostic preservation")

        # G7: Set staging to read-only BEFORE publishing
        try:
            os.fchmod(self._staging_fd, 0o444)
            os.fsync(self._staging_fd)
        except OSError as inner_exc:
            raise DiagnosticPersistenceError(
                f"cannot make diagnostic evidence immutable and durable: {inner_exc}"
            ) from inner_exc
        # fsync .pending directory
        try:
            os.fsync(self._pending_dir_fd)
        except OSError as inner_exc:
            raise OSError(f"cannot fsync .pending for staging durability: {inner_exc}") from inner_exc

        # Compute staging hash + size from fd
        staging_sha = _compute_sha256_from_fd(self._staging_fd)
        try:
            st = os.fstat(self._staging_fd)
            staging_size = st.st_size
            staging_dev = st.st_dev
            staging_ino = st.st_ino
        except OSError as inner_exc:
            raise OSError(f"cannot fstat staging: {inner_exc}") from inner_exc

        staging_name = self._staging_name
        base_name = staging_name[:-4] if staging_name.endswith(".tmp") else staging_name

        # G6: Evidence location state machine
        evidence_location: EvidenceLocation = "PENDING"
        evidence_filename: str = staging_name
        secondary_evidence_location: EvidenceLocation | None = None
        secondary_evidence_filename: str | None = None

        # Try to create .quarantine and hardlink staging there
        quarantine_dir_fd = -1
        quarantine_name = ""
        link_created = False
        quarantine_fsynced = False
        pending_unlinked = False
        pending_fsynced = True  # already fsynced above

        try:
            # Create .quarantine relative to raw_dir_fd
            if self._raw_trusted is None:
                raise DiagnosticPersistenceError("trusted raw directory unavailable")
            self._quarantine_trusted = _open_or_create_trusted_child_directory(
                self._raw_trusted,
                ".quarantine",
                creation_fault_point=(
                    FAULT_QUARANTINE_AFTER_MKDIR_BEFORE_PARENT_FSYNC
                ),
            )
            quarantine_dir_fd = self._quarantine_trusted.fd

            # G6: hardlink staging → quarantine (no-replace)
            quarantine_name = f"{base_name}.{uuid.uuid4().hex[:8]}.quarantined"
            try:
                os.link(
                    staging_name, quarantine_name,
                    src_dir_fd=self._pending_dir_fd,
                    dst_dir_fd=quarantine_dir_fd,
                )
                link_created = True
            except FileExistsError:
                # Retry with new name
                quarantine_name = f"{base_name}.{uuid.uuid4().hex[:8]}.quarantined"
                os.link(
                    staging_name, quarantine_name,
                    src_dir_fd=self._pending_dir_fd,
                    dst_dir_fd=quarantine_dir_fd,
                )
                link_created = True

            # G6: fsync quarantine dir
            os.fsync(quarantine_dir_fd)
            quarantine_fsynced = True

            # G6: verify quarantine hardlink exists and matches
            q_verify_fd = -1
            try:
                q_verify_fd = os.open(
                    quarantine_name,
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=quarantine_dir_fd,
                )
                q_st = os.fstat(q_verify_fd)
                if q_st.st_dev != staging_dev or q_st.st_ino != staging_ino:
                    raise OSError("quarantine hardlink inode/dev mismatch")
            except OSError as inner_exc:
                raise OSError(f"quarantine verify failed: {inner_exc}") from inner_exc
            finally:
                if q_verify_fd >= 0:
                    os.close(q_verify_fd)

            # G6: unlink staging from pending
            try:
                os.unlink(staging_name, dir_fd=self._pending_dir_fd)
                pending_unlinked = True
            except OSError as inner_exc:
                raise OSError(f"cannot unlink staging from pending: {inner_exc}") from inner_exc

            # G6: fsync pending dir
            os.fsync(self._pending_dir_fd)
            pending_fsynced = True

            # Update evidence location
            evidence_location = "QUARANTINE"
            evidence_filename = quarantine_name
            secondary_evidence_location = None
            secondary_evidence_filename = None

        except (OSError, ValueError, RuntimeError, EOFError, gzip.BadGzipFile) as inner_exc:
            # Resolve location from completed transitions, never from intent.
            if pending_unlinked:
                if not link_created or not quarantine_fsynced:
                    raise DiagnosticPersistenceError(
                        "pending link removed but quarantine durability is unconfirmed"
                    ) from inner_exc
                evidence_location = "QUARANTINE"
                evidence_filename = quarantine_name
                secondary_evidence_location = None
                secondary_evidence_filename = None
            elif not pending_fsynced:
                try:
                    os.fsync(self._pending_dir_fd)
                except OSError:
                    raise OSError(
                        f"cannot ensure staging durability in pending: {inner_exc}"
                    ) from inner_exc
                evidence_location = "PENDING"
                evidence_filename = staging_name
                if link_created and quarantine_name:
                    secondary_evidence_location = "QUARANTINE"
                    secondary_evidence_filename = quarantine_name
            else:
                evidence_location = "PENDING"
                evidence_filename = staging_name
                if link_created and quarantine_name:
                    secondary_evidence_location = "QUARANTINE"
                    secondary_evidence_filename = quarantine_name

        # Verify the selected primary evidence by fd before naming it in a
        # diagnostic record.
        primary_dir_fd = (
            quarantine_dir_fd if evidence_location == "QUARANTINE"
            else self._pending_dir_fd
        )
        if primary_dir_fd < 0:
            raise DiagnosticPersistenceError("primary evidence directory fd unavailable")
        evidence_fd = os.open(
            evidence_filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=primary_dir_fd)
        try:
            evidence_stat = os.fstat(evidence_fd)
            if not statmod.S_ISREG(evidence_stat.st_mode):
                raise DiagnosticPersistenceError("primary evidence is not a regular file")
            if ((evidence_stat.st_dev, evidence_stat.st_ino, evidence_stat.st_size) !=
                    (staging_dev, staging_ino, staging_size)):
                raise DiagnosticPersistenceError("primary evidence inode/dev/size mismatch")
            if statmod.S_IMODE(evidence_stat.st_mode) != 0o444:
                raise DiagnosticPersistenceError("primary evidence mode is not 0o444")
            if _compute_sha256_from_fd(evidence_fd) != staging_sha:
                raise DiagnosticPersistenceError("primary evidence SHA-256 mismatch")
        finally:
            os.close(evidence_fd)

        # Build diagnostic dict
        txn_uuid = str(uuid.uuid4())
        ownership_token = self._ownership_token or str(uuid.uuid4())
        diag_dict: dict[str, Any] = {
            "diagnostic_version": DIAGNOSTIC_SCHEMA_VERSION,
            "transaction_uuid": txn_uuid,
            "ownership_token": ownership_token,
            "diagnostic_created_at": datetime.now(timezone.utc).isoformat(),
            "triggering_state": self._state,
            "failure_stage": stage,
            "failure_type": type(exc).__name__,
            "failure_message": str(exc),
            "staging_filename": staging_name,
            "staging_sha256": staging_sha,
            "staging_size_bytes": staging_size,
            "marker_filename": None,
            "marker_integrity_sha256": None,
            "events_appended_before_failure": len(self._events),
            "events_appended_total_expected": None,
            "recoverable": False,
            "evidence_location": evidence_location,
            "evidence_filename": evidence_filename,
            "secondary_evidence_location": secondary_evidence_location,
            "secondary_evidence_filename": secondary_evidence_filename,
        }
        integrity = compute_diagnostic_integrity_sha256(diag_dict)
        diag_dict["diagnostic_integrity_sha256"] = integrity
        diag_bytes = canonical_json_bytes(diag_dict)

        # Write diagnostic JSON via O_EXCL temp + hardlink (no-replace)
        diag_dir_fd = primary_dir_fd
        try:
            diag_name = f"diagnostic_{txn_uuid}.json"
            temp_name = f"{diag_name}.tmp.{uuid.uuid4().hex}"
            temp_fd = os.open(
                temp_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o644,
                dir_fd=diag_dir_fd,
            )
            temp_file = None
            try:
                temp_file = os.fdopen(temp_fd, "wb")
                temp_fd = -1
                with temp_file:
                    written = temp_file.write(diag_bytes)
                    if written != len(diag_bytes):
                        raise OSError(
                            f"short diagnostic write: {written}/{len(diag_bytes)}")
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
            except Exception as write_exc:
                cleanup_errors: list[str] = []
                if temp_fd >= 0:
                    try:
                        os.close(temp_fd)
                    except OSError as close_exc:
                        cleanup_errors.append(f"temp close failed: {close_exc}")
                try:
                    os.unlink(temp_name, dir_fd=diag_dir_fd)
                except FileNotFoundError:
                    pass
                except OSError as unlink_exc:
                    cleanup_errors.append(f"temp unlink failed: {unlink_exc}")
                try:
                    os.fsync(diag_dir_fd)
                except OSError as fsync_exc:
                    cleanup_errors.append(f"directory fsync failed: {fsync_exc}")
                if cleanup_errors:
                    raise DiagnosticPersistenceError(
                        f"diagnostic temp write failed ({write_exc}); cleanup: "
                        f"{'; '.join(cleanup_errors)}"
                    ) from write_exc
                raise
            final_diag_name = diag_name
            try:
                os.link(temp_name, final_diag_name,
                        src_dir_fd=diag_dir_fd, dst_dir_fd=diag_dir_fd)
            except FileExistsError:
                final_diag_name = f"diagnostic_{txn_uuid}.{uuid.uuid4().hex[:8]}.json"
                os.link(temp_name, final_diag_name,
                        src_dir_fd=diag_dir_fd, dst_dir_fd=diag_dir_fd)
            os.fsync(diag_dir_fd)
            os.unlink(temp_name, dir_fd=diag_dir_fd)
            os.fsync(diag_dir_fd)
            final_fd = os.open(
                final_diag_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=diag_dir_fd)
            try:
                final_stat = os.fstat(final_fd)
                if not statmod.S_ISREG(final_stat.st_mode):
                    raise DiagnosticPersistenceError(
                        "persisted diagnostic is not a regular file")
                final_bytes = b""
                while True:
                    chunk = os.read(final_fd, 65536)
                    if not chunk:
                        break
                    final_bytes += chunk
            finally:
                os.close(final_fd)
            if final_bytes != diag_bytes:
                raise DiagnosticPersistenceError(
                    "persisted diagnostic bytes differ from canonical bytes")
            parsed_diag = json.loads(final_bytes.decode("utf-8"))
            if compute_diagnostic_integrity_sha256(parsed_diag) != parsed_diag.get(
                    "diagnostic_integrity_sha256"):
                raise DiagnosticPersistenceError("diagnostic integrity reread failed")
            if (parsed_diag.get("evidence_location") != evidence_location or
                    parsed_diag.get("evidence_filename") != evidence_filename):
                raise DiagnosticPersistenceError("diagnostic evidence location reread failed")
            os.fsync(diag_dir_fd)
        finally:
            if quarantine_dir_fd >= 0:
                if self._quarantine_trusted is not None:
                    self._quarantine_trusted.close()
                    self._quarantine_trusted = None
                else:
                    os.close(quarantine_dir_fd)

        self._diagnostic_evidence = (evidence_location, evidence_filename)


# ═══════════════════════════════════════════════════════════════════════
# Section 9 — Eligibility state (F8)
# ═══════════════════════════════════════════════════════════════════════

ELIGIBILITY_FILENAME: Final[str] = ".eligibility_state.json"
ELIGIBILITY_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "schema_version",
    "first_eligible_scan_seen",
    "first_eligible_scan_id",
    "first_persistible_data_api_request_at",
    "state_sha256",
)


@dataclass(frozen=True)
class EligibilityState:
    """D3 — Persisted eligibility state for INV-005."""
    schema_version: str
    first_eligible_scan_seen: bool
    first_eligible_scan_id: str | None
    first_persistible_data_api_request_at: str | None
    state_sha256: str


def _validate_utc_timestamp(value: Any, field_name: str,
                            error_type: type[Exception] = ValueError) -> datetime:
    if not isinstance(value, str) or not value:
        raise error_type(f"{field_name} must be a non-empty string")
    if not _ISO_8601_RE.fullmatch(value):
        raise error_type(f"{field_name} must include an explicit timezone: {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise error_type(f"{field_name} invalid ISO 8601: {value!r}: {exc}") from exc
    offset = parsed.utcoffset()
    if offset is None or offset != timedelta(0):
        raise error_type(f"{field_name} must be UTC, got offset {offset}: {value!r}")
    return parsed


def _read_eligibility_via_fd(dir_fd: int) -> EligibilityState | None:
    """F8 — Read eligibility state via fd with O_NOFOLLOW (no TOCTOU)."""
    try:
        file_fd = os.open(
            ELIGIBILITY_FILENAME,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=dir_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as inner_exc:
        if inner_exc.errno == errno.ELOOP:
            raise EligibilityCorruptionError(
                f"eligibility file is a symlink: {ELIGIBILITY_FILENAME}"
            ) from inner_exc
        raise EligibilityCorruptionError(f"cannot open eligibility file: {inner_exc}") from inner_exc
    try:
        raw = b""
        while True:
            chunk = os.read(file_fd, 65536)
            if not chunk:
                break
            raw += chunk
    except OSError as inner_exc:
        raise EligibilityCorruptionError(f"cannot read eligibility file: {inner_exc}") from inner_exc
    finally:
        os.close(file_fd)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as inner_exc:
        raise EligibilityCorruptionError(f"eligibility file is not valid JSON: {inner_exc}") from inner_exc
    if not isinstance(obj, dict):
        raise EligibilityCorruptionError(
            f"eligibility file root must be object, got {type(obj).__name__}"
        )
    allowed = set(ELIGIBILITY_REQUIRED_FIELDS)
    unknown = [k for k in obj.keys() if k not in allowed]
    if unknown:
        raise EligibilityCorruptionError(f"eligibility file has unknown fields: {unknown}")
    missing = [f for f in ELIGIBILITY_REQUIRED_FIELDS if f not in obj]
    if missing:
        raise EligibilityCorruptionError(f"eligibility file missing keys {missing}")
    if obj["schema_version"] != ELIGIBILITY_SCHEMA_VERSION:
        raise EligibilityCorruptionError(
            f"eligibility schema_version mismatch: got {obj['schema_version']!r}"
        )
    if obj["first_eligible_scan_seen"] is not True:
        raise EligibilityCorruptionError(
            f"eligibility file has first_eligible_scan_seen={obj['first_eligible_scan_seen']!r}"
        )
    sid = obj["first_eligible_scan_id"]
    if not isinstance(sid, str) or not sid:
        raise EligibilityCorruptionError(
            f"first_eligible_scan_id must be non-empty string, got {sid!r}"
        )
    rat = obj["first_persistible_data_api_request_at"]
    if not isinstance(rat, str) or not rat:
        raise EligibilityCorruptionError(
            f"first_persistible_data_api_request_at must be non-empty string, got {rat!r}"
        )
    _validate_utc_timestamp(
        rat, "first_persistible_data_api_request_at", EligibilityCorruptionError)
    if not isinstance(obj["state_sha256"], str) or not _HEX64_RE.match(obj["state_sha256"]):
        raise EligibilityCorruptionError(
            f"state_sha256 must be 64-char hex, got {obj['state_sha256']!r}"
        )
    recomputed = compute_eligibility_integrity_sha256(obj)
    if recomputed != obj["state_sha256"]:
        raise EligibilityCorruptionError(
            f"eligibility state_sha256 mismatch: computed={recomputed} "
            f"stored={obj['state_sha256']}"
        )
    return EligibilityState(
        schema_version=obj["schema_version"],
        first_eligible_scan_seen=obj["first_eligible_scan_seen"],
        first_eligible_scan_id=obj["first_eligible_scan_id"],
        first_persistible_data_api_request_at=obj["first_persistible_data_api_request_at"],
        state_sha256=obj["state_sha256"],
    )


def read_eligibility_state(directory: Path) -> EligibilityState | None:
    """F8 — Read eligibility state. Opens trusted dir fd."""
    trusted = open_trusted_directory(directory)
    try:
        return _read_eligibility_via_fd(trusted.fd)
    finally:
        trusted.close()


def mark_first_eligible_scan_seen_under_lock(
    guard: RawChainLockGuard,
    directory: Path,
    prefix: str,
    first_eligible_scan_id: str,
    first_persistible_data_api_request_at: str,
) -> EligibilityState:
    """F8 — Mark the first eligible scan as seen. Requires RawChainLockGuard."""
    assert_guard_valid(guard, directory, prefix)
    if not isinstance(first_eligible_scan_id, str) or not first_eligible_scan_id:
        raise ValueError("first_eligible_scan_id must be non-empty string")
    _validate_utc_timestamp(
        first_persistible_data_api_request_at,
        "first_persistible_data_api_request_at",
    )
    dir_fd = guard.trusted.fd
    existing = _read_eligibility_via_fd(dir_fd)
    if existing is not None:
        if existing.first_eligible_scan_seen is True:
            return existing
        else:
            raise EligibilityCorruptionError(
                "eligibility file has first_eligible_scan_seen=false"
            )
    body: dict[str, Any] = {
        "schema_version": ELIGIBILITY_SCHEMA_VERSION,
        "first_eligible_scan_seen": True,
        "first_eligible_scan_id": first_eligible_scan_id,
        "first_persistible_data_api_request_at": first_persistible_data_api_request_at,
    }
    integrity = compute_eligibility_integrity_sha256(body)
    body["state_sha256"] = integrity
    canonical = canonical_json_bytes(body)
    temp_name = f"{ELIGIBILITY_FILENAME}.tmp.{uuid.uuid4().hex}"
    temp_fd = os.open(
        temp_name,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
        0o644,
        dir_fd=dir_fd,
    )
    temp_file = None
    try:
        temp_file = os.fdopen(temp_fd, "wb")
        temp_fd = -1
        with temp_file as f:
            f.write(canonical)
            f.flush()
            os.fsync(f.fileno())
    except Exception as exc:
        if temp_fd >= 0:
            os.close(temp_fd)
        try:
            os.unlink(temp_name, dir_fd=dir_fd)
            os.fsync(dir_fd)
        except FileNotFoundError:
            pass
        except OSError as cleanup_exc:
            raise MarkerCreateCleanupPending(
                f"eligibility temp write failed and cleanup is pending: {cleanup_exc}",
                final_created=False,
                temp_unlinked=False,
                cleanup_durability_confirmed=False,
                filesystem_state="PRE_COMMIT_CLEANUP_UNCONFIRMED",
            ) from cleanup_exc
        raise exc
    final_created = False
    try:
        os.link(temp_name, ELIGIBILITY_FILENAME,
                src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        final_created = True
    except FileExistsError:
        try:
            os.unlink(temp_name, dir_fd=dir_fd)
            os.fsync(dir_fd)
        except FileNotFoundError:
            pass
        existing = _read_eligibility_via_fd(dir_fd)
        if existing is not None and existing.first_eligible_scan_seen is True:
            return existing
        raise EligibilityCorruptionError("concurrent eligibility write resulted in unexpected state")
    except Exception as exc:
        try:
            os.unlink(temp_name, dir_fd=dir_fd)
            os.fsync(dir_fd)
        except FileNotFoundError:
            pass
        except OSError as cleanup_exc:
            raise MarkerCreateCleanupPending(
                f"eligibility hardlink failed and temp cleanup is pending: {cleanup_exc}",
                final_created=False,
                temp_unlinked=False,
                cleanup_durability_confirmed=False,
                filesystem_state="PRE_COMMIT_CLEANUP_UNCONFIRMED",
            ) from cleanup_exc
        raise exc
    if final_created:
        try:
            os.unlink(temp_name, dir_fd=dir_fd)
            os.fsync(dir_fd)
        except OSError as exc:
            temp_unlinked = _snapshot_dir_entry(
                dir_fd, temp_name).get("exists") is False
            raise MarkerCreateCleanupPending(
                f"eligibility committed; cleanup/durability pending: {exc}",
                final_created=True,
                temp_unlinked=temp_unlinked,
                cleanup_durability_confirmed=False,
                filesystem_state=(
                    "COMMITTED_OLD_TEMP_REMOVED_FSYNC_UNCONFIRMED"
                    if temp_unlinked else "COMMITTED_OLD_TEMP_PRESENT"
                ),
            ) from exc
    return EligibilityState(
        schema_version=body["schema_version"],
        first_eligible_scan_seen=body["first_eligible_scan_seen"],
        first_eligible_scan_id=body["first_eligible_scan_id"],
        first_persistible_data_api_request_at=body["first_persistible_data_api_request_at"],
        state_sha256=integrity,
    )


# ═══════════════════════════════════════════════════════════════════════
# Section 10 — Marker filename helper (F9)
# ═══════════════════════════════════════════════════════════════════════

def marker_filename(prefix: str, sequence: int, transaction_uuid: str) -> str:
    """F9 — Compute canonical marker filename with strict validation."""
    validate_safe_prefix(prefix)
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise ValueError(f"sequence must be non-negative int, got {sequence!r}")
    try:
        u = uuid.UUID(transaction_uuid)
    except ValueError as exc:
        raise ValueError(f"transaction_uuid invalid UUID: {exc}") from exc
    if u.version != 4:
        raise ValueError(f"transaction_uuid must be UUID version 4, got version {u.version}")
    return f"{prefix}_txn_{sequence:06d}_{transaction_uuid}.marker"


def _canonical_diagnostic_bytes(diag_body: dict[str, Any]) -> bytes:
    body_without = {k: v for k, v in diag_body.items() if k != "diagnostic_integrity_sha256"}
    integrity = hashlib.sha256(canonical_json_bytes(body_without)).hexdigest()
    body_with = dict(diag_body)
    body_with["diagnostic_integrity_sha256"] = integrity
    return canonical_json_bytes(body_with)


def _dir_fsync(directory: Path) -> None:
    """fsync a directory by opening it O_RDONLY | O_DIRECTORY | O_NOFOLLOW."""
    fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
