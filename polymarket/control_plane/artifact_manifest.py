"""
SENECIO H-011 V3 — Artifact Manifest System (FASE A.4 revised).

Provides append-only verification through chained manifests for raw events
and snapshots. Each manifest entry links to the previous via
`previous_manifest_hash`, creating a tamper-evident chain.

Addresses A.4.1–A.4.8:
  - ManifestPolicy dataclass for unified configuration
  - allowed_unregistered for pending artifact during write
  - EMPTY_CHAIN / BOOTSTRAP_REQUIRED / VALID_CHAIN / INVALID_CHAIN
  - Reserved field protection in extra_fields
  - Full durability (fdopen, fsync, dirfsync)
  - fcntl.flock for concurrency
  - Atomic verify → reserve → publish → reverify
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ═══════════════════════════════════════════════════════════════════════
# ManifestPolicy — single configuration object (A.4.2)
# ═══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ManifestPolicy:
    """Policy for manifest chain verification and writing."""
    manifest_prefix: str
    artifact_glob: str
    exclude_names: frozenset[str] = field(default_factory=frozenset)
    identity_fields: tuple[str, ...] = ()

    def lock_filename(self) -> str:
        return f"{self.manifest_prefix}.lock"


RAW_MANIFEST_POLICY = ManifestPolicy(
    manifest_prefix="manifest",
    artifact_glob="*.events.jsonl.gz",
    exclude_names=frozenset(),
    identity_fields=("run_id", "scan_id"),
)

SNAPSHOT_MANIFEST_POLICY = ManifestPolicy(
    manifest_prefix="smanifest",
    artifact_glob="snapshot_*.json",
    exclude_names=frozenset({"latest.json", "latest.json.sha256"}),
    identity_fields=("run_id", "scan_id"),
)


# ═══════════════════════════════════════════════════════════════════════
# Chain status (A.4.4)
# ═══════════════════════════════════════════════════════════════════════

CHAIN_EMPTY = "EMPTY_CHAIN"
CHAIN_VALID = "VALID_CHAIN"
CHAIN_BOOTSTRAP_REQUIRED = "BOOTSTRAP_REQUIRED"
CHAIN_INVALID = "INVALID_CHAIN"

RESERVED_FIELDS = frozenset({
    "sequence", "filename", "file_sha256",
    "previous_manifest_hash", "created_at", "manifest_hash",
})


# ═══════════════════════════════════════════════════════════════════════
# Hash helpers
# ═══════════════════════════════════════════════════════════════════════

def _compute_manifest_hash(entry: dict[str, Any]) -> str:
    copy = {k: v for k, v in entry.items() if k != "manifest_hash"}
    raw = json.dumps(copy, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _validate_sha256_hex(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
        return True
    except ValueError:
        return False


# ═══════════════════════════════════════════════════════════════════════
# Extra fields validation (A.4.5)
# ═══════════════════════════════════════════════════════════════════════

def _validate_extra_fields(extra_fields: dict[str, Any] | None, policy: ManifestPolicy) -> None:
    if extra_fields is None:
        return
    for key, value in extra_fields.items():
        if key in RESERVED_FIELDS:
            raise ValueError(f"Reserved field '{key}' cannot be set via extra_fields")
        # Validate identity fields
        if key in policy.identity_fields:
            if not isinstance(value, str) or not value:
                raise ValueError(f"Identity field '{key}' must be non-empty string")
            if key in ("run_id", "scan_id") and not value.strip():
                raise ValueError(f"Identity field '{key}' must be non-empty")


# ═══════════════════════════════════════════════════════════════════════
# Artifact glob matching (A.4.5)
# ═══════════════════════════════════════════════════════════════════════

def _matches_glob(filename: str, pattern: str) -> bool:
    """Check if filename matches a glob pattern using Path.match."""
    return Path(filename).match(pattern)


# ═══════════════════════════════════════════════════════════════════════
# Verify manifest chain (A.4.2, A.4.3, A.4.4)
# ═══════════════════════════════════════════════════════════════════════

def verify_manifest_chain(
    directory: Path,
    policy: ManifestPolicy,
    allowed_unregistered: set[str] | None = None,
) -> dict[str, Any]:
    """Verify a manifest chain in a directory.

    Args:
        directory: Directory containing artifacts and manifests
        policy: ManifestPolicy with prefix, glob, excludes, identity fields
        allowed_unregistered: Files temporarily allowed to be unregistered
                              (used during atomic write of a new artifact)

    Returns dict with:
      - chain_status: EMPTY_CHAIN | VALID_CHAIN | BOOTSTRAP_REQUIRED | INVALID_CHAIN
      - valid: bool (True only for EMPTY_CHAIN or VALID_CHAIN)
      - entries: list of manifest entries in sequence order
      - errors: list of error strings
      - unregistered_files: list of artifact files not in any manifest
      - sequence_count: int
    """
    if allowed_unregistered is None:
        allowed_unregistered = set()

    manifest_files = sorted(directory.glob(f"{policy.manifest_prefix}_*.json"))
    # Exclude lock file from manifest list
    manifest_files = [f for f in manifest_files if not f.name.endswith(".lock")]
    entries: list[dict[str, Any]] = []
    errors: list[str] = []

    # Load all manifests
    for mf in manifest_files:
        try:
            entry = json.loads(mf.read_text())
            entries.append(entry)
        except (json.JSONDecodeError, OSError) as e:
            errors.append(f"Cannot read manifest {mf.name}: {e}")
            return {"chain_status": CHAIN_INVALID, "valid": False, "entries": [],
                    "errors": errors, "unregistered_files": [], "sequence_count": 0}

    # Get all artifact files matching the glob
    all_artifacts = set()
    for f in directory.glob(policy.artifact_glob):
        if f.is_file() and f.name not in policy.exclude_names:
            all_artifacts.add(f.name)

    if not entries:
        if not all_artifacts:
            return {"chain_status": CHAIN_EMPTY, "valid": True, "entries": [],
                    "errors": [], "unregistered_files": [], "sequence_count": 0}
        else:
            # Artifacts exist but no manifests
            unregistered = all_artifacts - allowed_unregistered
            if unregistered:
                return {"chain_status": CHAIN_BOOTSTRAP_REQUIRED, "valid": False,
                        "entries": [], "errors": [f"Bootstrap required: {len(unregistered)} unregistered artifacts"],
                        "unregistered_files": sorted(unregistered), "sequence_count": 0}
            # All artifacts are allowed (pending write)
            return {"chain_status": CHAIN_EMPTY, "valid": True, "entries": [],
                    "errors": [], "unregistered_files": [], "sequence_count": 0}

    # Sort by sequence
    entries.sort(key=lambda e: e.get("sequence", -1))

    # Check sequence continuity (0, 1, 2, ...)
    for i, entry in enumerate(entries):
        seq = entry.get("sequence")
        if seq != i:
            errors.append(f"Sequence gap or duplicate: expected {i}, got {seq}")
            return {"chain_status": CHAIN_INVALID, "valid": False, "entries": entries,
                    "errors": errors, "unregistered_files": [], "sequence_count": len(entries)}

    # Verify chain links
    prev_hash = None
    seen_filenames: set[str] = set()
    seen_identities: dict[str, set[str]] = {f: set() for f in policy.identity_fields}

    for entry in entries:
        seq = entry["sequence"]

        # Check previous_manifest_hash
        entry_prev = entry.get("previous_manifest_hash")
        if seq == 0:
            if entry_prev is not None:
                errors.append(f"First entry (seq=0) has non-null previous_manifest_hash")
                return {"chain_status": CHAIN_INVALID, "valid": False, "entries": entries,
                        "errors": errors, "unregistered_files": [], "sequence_count": len(entries)}
        else:
            if entry_prev != prev_hash:
                errors.append(f"Chain broken at seq={seq}")
                return {"chain_status": CHAIN_INVALID, "valid": False, "entries": entries,
                        "errors": errors, "unregistered_files": [], "sequence_count": len(entries)}

        # Verify manifest_hash
        recomputed = _compute_manifest_hash(entry)
        if entry.get("manifest_hash") != recomputed:
            errors.append(f"Manifest hash mismatch at seq={seq}")
            return {"chain_status": CHAIN_INVALID, "valid": False, "entries": entries,
                    "errors": errors, "unregistered_files": [], "sequence_count": len(entries)}

        # Check filename uniqueness
        fname = entry.get("filename", "")
        if fname in seen_filenames:
            errors.append(f"Duplicate filename in manifest: {fname}")
            return {"chain_status": CHAIN_INVALID, "valid": False, "entries": entries,
                    "errors": errors, "unregistered_files": [], "sequence_count": len(entries)}
        seen_filenames.add(fname)

        # Check identity field uniqueness
        for field in policy.identity_fields:
            val = entry.get(field)
            if val is None:
                errors.append(f"Manifest seq={seq} missing required field '{field}'")
                return {"chain_status": CHAIN_INVALID, "valid": False, "entries": entries,
                        "errors": errors, "unregistered_files": [], "sequence_count": len(entries)}
            if val in seen_identities[field]:
                errors.append(f"Duplicate {field} in manifest: {val}")
                return {"chain_status": CHAIN_INVALID, "valid": False, "entries": entries,
                        "errors": errors, "unregistered_files": [], "sequence_count": len(entries)}
            seen_identities[field].add(val)

        # Verify artifact file exists and matches hash
        artifact_path = directory / fname
        if not artifact_path.exists():
            errors.append(f"Artifact file missing: {fname} (seq={seq})")
            return {"chain_status": CHAIN_INVALID, "valid": False, "entries": entries,
                    "errors": errors, "unregistered_files": [], "sequence_count": len(entries)}

        actual_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if actual_sha != entry.get("file_sha256"):
            errors.append(f"File hash mismatch for {fname}")
            return {"chain_status": CHAIN_INVALID, "valid": False, "entries": entries,
                    "errors": errors, "unregistered_files": [], "sequence_count": len(entries)}

        prev_hash = entry.get("manifest_hash")

    # Check for unregistered artifacts
    registered = {e["filename"] for e in entries}
    unregistered = all_artifacts - registered - allowed_unregistered
    if unregistered:
        errors.append(f"Unregistered artifact files: {sorted(unregistered)}")

    chain_status = CHAIN_VALID if not errors else CHAIN_INVALID
    return {
        "chain_status": chain_status,
        "valid": chain_status in (CHAIN_EMPTY, CHAIN_VALID),
        "entries": entries,
        "errors": errors,
        "unregistered_files": sorted(unregistered),
        "sequence_count": len(entries),
    }


# ═══════════════════════════════════════════════════════════════════════
# Atomic manifest writing (A.4.1, A.4.3, A.4.5, A.4.6, A.4.7)
# ═══════════════════════════════════════════════════════════════════════

def write_manifest_atomic(
    directory: Path,
    artifact_path: Path,
    policy: ManifestPolicy,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a manifest entry atomically with exclusive creation and locking.

    A.4.1: Uses allowed_unregistered to permit the pending artifact during prevalidation.
    A.4.3: The artifact being registered is temporarily allowed as unregistered.
    A.4.5: Validates extra_fields don't contain reserved keys.
    A.4.6: Uses fdopen + flush + fsync + dirfsync for full durability.
    A.4.7: Uses fcntl.flock for concurrency control.

    Returns the manifest entry dict.
    Raises:
      ValueError: if extra_fields contains reserved keys or invalid values.
      RuntimeError: if existing chain is corrupt or bootstrap required.
      FileExistsError: if manifest file already exists.
    """
    # A.4.5: Validate extra fields
    _validate_extra_fields(extra_fields, policy)

    # A.4.5: Validate artifact_path is in directory and matches glob
    if artifact_path.parent != directory:
        raise ValueError(f"Artifact {artifact_path} is not in directory {directory}")
    if not _matches_glob(artifact_path.name, policy.artifact_glob):
        raise ValueError(f"Artifact {artifact_path.name} does not match glob {policy.artifact_glob}")

    # A.4.7: Acquire lock
    lock_path = directory / policy.lock_filename()
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # A.4.1/A.4.3: Prevalidate with pending artifact allowed
        precheck = verify_manifest_chain(
            directory, policy,
            allowed_unregistered={artifact_path.name},
        )

        if precheck["chain_status"] == CHAIN_INVALID:
            raise RuntimeError(f"Cannot write manifest: chain is corrupt: {'; '.join(precheck['errors'])}")
        if precheck["chain_status"] == CHAIN_BOOTSTRAP_REQUIRED:
            raise RuntimeError(
                f"Cannot write manifest: bootstrap required — "
                f"legacy artifacts exist without manifests: {precheck['unregistered_files']}"
            )

        # Get next sequence from verified chain
        sequence = len(precheck["entries"])
        previous_hash = precheck["entries"][-1]["manifest_hash"] if precheck["entries"] else None

        # Compute file SHA256 from the artifact
        file_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()

        # Build manifest entry
        entry: dict[str, Any] = {
            "sequence": sequence,
            "filename": artifact_path.name,
            "file_sha256": file_sha256,
            "previous_manifest_hash": previous_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if extra_fields:
            entry.update(extra_fields)
        entry["manifest_hash"] = _compute_manifest_hash(entry)

        manifest_path = directory / f"{policy.manifest_prefix}_{sequence:06d}.json"
        manifest_content = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()

        # A.4.6: Atomic exclusive creation with full durability
        fd = os.open(str(manifest_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(manifest_content)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            # If write fails, try to clean up the partially written file
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                manifest_path.unlink()
            except OSError:
                pass
            raise

        # A.4.6: Sync the directory
        dir_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

        # Post-write verification: full chain check with NO allowed unregistered
        recheck = verify_manifest_chain(directory, policy, allowed_unregistered=set())
        if not recheck["valid"]:
            # The manifest was published but chain is invalid — this is a blocking error
            raise RuntimeError(
                f"Manifest chain corrupt after write: {'; '.join(recheck['errors'])}. "
                f"Published manifest: {manifest_path.name}"
            )

        return entry

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def publish_artifact_bytes_with_manifest(
    directory: Path,
    artifact_name: str,
    artifact_bytes: bytes,
    policy: ManifestPolicy,
    identity_fields: dict[str, str],
    extra_manifest_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically create an artifact and append its manifest under one lock.

    This is the concurrency-safe API for callers that own the artifact bytes.
    It removes the race inherent in publishing a final-path artifact that was
    created before the chain lock was acquired. No sleeps or retries are used.
    """
    if not isinstance(artifact_bytes, (bytes, bytearray)):
        raise TypeError("artifact_bytes must be bytes")
    artifact_path = directory / artifact_name
    combined_fields = {**identity_fields, **(extra_manifest_fields or {})}
    _validate_extra_fields(extra_manifest_fields, policy)
    for field in policy.identity_fields:
        value = combined_fields.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Required identity field '{field}' missing or empty")
    if not _matches_glob(artifact_name, policy.artifact_glob):
        raise ValueError(f"Artifact {artifact_name} does not match glob {policy.artifact_glob}")
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / policy.lock_filename()
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    artifact_created = False
    sidecar_path = artifact_path.with_suffix(artifact_path.suffix + ".sha256")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        precheck = verify_manifest_chain(directory, policy)
        if not precheck["valid"]:
            raise RuntimeError(f"Cannot publish: chain is not valid: {precheck['errors']}")
        for field in policy.identity_fields:
            value = combined_fields[field]
            if value in {entry.get(field) for entry in precheck["entries"]}:
                raise ValueError(f"Duplicate {field}: {value}")
        sequence = len(precheck["entries"])
        previous_hash = precheck["entries"][-1]["manifest_hash"] if precheck["entries"] else None
        file_sha256 = hashlib.sha256(bytes(artifact_bytes)).hexdigest()
        fd = os.open(str(artifact_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o444)
        artifact_created = True
        with os.fdopen(fd, "wb") as handle:
            handle.write(bytes(artifact_bytes))
            handle.flush()
            os.fsync(handle.fileno())
        sidecar_tmp = sidecar_path.with_name(sidecar_path.name + ".tmp")
        sidecar_tmp.write_text(file_sha256 + "\n", encoding="ascii")
        with sidecar_tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(sidecar_tmp, sidecar_path)
        entry: dict[str, Any] = {
            "sequence": sequence,
            "filename": artifact_name,
            "file_sha256": file_sha256,
            "previous_manifest_hash": previous_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        entry.update(combined_fields)
        entry["manifest_hash"] = _compute_manifest_hash(entry)
        manifest_path = directory / f"{policy.manifest_prefix}_{sequence:06d}.json"
        manifest_content = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
        manifest_fd = os.open(str(manifest_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o444)
        with os.fdopen(manifest_fd, "wb") as handle:
            handle.write(manifest_content)
            handle.flush()
            os.fsync(handle.fileno())
        dir_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        recheck = verify_manifest_chain(directory, policy)
        if not recheck["valid"]:
            raise RuntimeError(f"Chain corrupt after publish: {recheck['errors']}")
        return entry
    except Exception:
        if artifact_created:
            # Fail closed. Cleanup is safe only while the chain lock is held and
            # only if no manifest references the candidate filename.
            manifests = verify_manifest_chain(directory, policy, allowed_unregistered={artifact_name})
            referenced = any(entry.get("filename") == artifact_name for entry in manifests.get("entries", []))
            if not referenced:
                for candidate in (sidecar_path, artifact_path):
                    try:
                        candidate.unlink()
                    except FileNotFoundError:
                        pass
        raise
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


# ═══════════════════════════════════════════════════════════════════════
# Helpers (fail-closed)
# ═══════════════════════════════════════════════════════════════════════

def get_last_manifest_hash(directory: Path, policy: ManifestPolicy) -> str | None:
    """Get the manifest_hash of the last entry, or None if empty/corrupt."""
    result = verify_manifest_chain(directory, policy)
    if not result["valid"]:
        return None
    entries = result["entries"]
    if not entries:
        return None
    return entries[-1].get("manifest_hash")


def get_next_sequence(directory: Path, policy: ManifestPolicy) -> int | None:
    """Get the next sequence number, or None if chain is corrupt."""
    result = verify_manifest_chain(directory, policy)
    if not result["valid"]:
        return None
    return len(result["entries"])


# ═══════════════════════════════════════════════════════════════════════
# publish_artifact_with_manifest — Full transaction (A.4 runtime)
# ═══════════════════════════════════════════════════════════════════════

def publish_artifact_with_manifest(
    directory: Path,
    artifact_path: Path,
    policy: ManifestPolicy,
    identity_fields: dict[str, str],
    extra_manifest_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full transaction: validate candidate → publish artifact → sidecar → manifest → reverify.

    The artifact must already exist at artifact_path (staged by the caller).
    This function:
      1. Acquires lock
      2. Verifies existing chain
      3. Validates candidate (identity fields present, not duplicate, glob match)
      4. Computes file SHA256
      5. Writes sidecar SHA256
      6. Writes manifest (atomic, exclusive)
      7. Re-verifies full chain
      8. Releases lock

    Returns the manifest entry dict.
    Raises ValueError for invalid candidate.
    Raises RuntimeError for chain corruption or write failure.
    """
    # Validate identity fields are present and non-empty
    combined_fields = {**identity_fields, **(extra_manifest_fields or {})}
    _validate_extra_fields(extra_manifest_fields, policy)
    for field in policy.identity_fields:
        val = combined_fields.get(field)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"Required identity field '{field}' missing or empty")

    # Acquire lock
    lock_path = directory / policy.lock_filename()
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # Now that we hold the lock, verify the artifact exists and is valid
        if artifact_path.parent != directory:
            raise ValueError(f"Artifact {artifact_path} is not in directory {directory}")
        if not _matches_glob(artifact_path.name, policy.artifact_glob):
            raise ValueError(f"Artifact {artifact_path.name} does not match glob {policy.artifact_glob}")
        if not artifact_path.exists():
            raise ValueError(f"Artifact file does not exist: {artifact_path}")

        # Verify existing chain (with pending artifact allowed)
        precheck = verify_manifest_chain(
            directory, policy,
            allowed_unregistered={artifact_path.name},
        )

        # If BOOTSTRAP_REQUIRED but all unregistered files are just our pending artifact,
        # treat as EMPTY_CHAIN (first write)
        if precheck["chain_status"] == CHAIN_BOOTSTRAP_REQUIRED:
            unreg = set(precheck.get("unregistered_files", []))
            if unreg == {artifact_path.name}:
                precheck["chain_status"] = CHAIN_EMPTY
                precheck["valid"] = True
                precheck["entries"] = []
                precheck["errors"] = []
                precheck["unregistered_files"] = []

        if precheck["chain_status"] == CHAIN_INVALID:
            raise RuntimeError(f"Chain corrupt: {'; '.join(precheck['errors'])}")
        if precheck["chain_status"] == CHAIN_BOOTSTRAP_REQUIRED:
            raise RuntimeError(
                f"Bootstrap required — legacy artifacts exist: {precheck['unregistered_files']}"
            )

        # Validate candidate against existing entries
        existing_entries = precheck["entries"]
        for field in policy.identity_fields:
            val = combined_fields[field]
            existing_values = {e.get(field) for e in existing_entries}
            if val in existing_values:
                raise ValueError(f"Duplicate {field}: {val}")

        # Reserve sequence
        sequence = len(existing_entries)
        previous_hash = existing_entries[-1]["manifest_hash"] if existing_entries else None

        # Compute file SHA256
        file_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()

        # Write sidecar SHA256 (mandatory)
        sidecar_path = artifact_path.with_suffix(artifact_path.suffix + ".sha256")
        sidecar_content = file_sha256 + "\n"
        sidecar_tmp = sidecar_path.with_suffix(".tmp")
        sidecar_tmp.write_text(sidecar_content)
        os.rename(str(sidecar_tmp), str(sidecar_path))

        # Build manifest entry
        entry: dict[str, Any] = {
            "sequence": sequence,
            "filename": artifact_path.name,
            "file_sha256": file_sha256,
            "previous_manifest_hash": previous_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        entry.update(combined_fields)
        entry["manifest_hash"] = _compute_manifest_hash(entry)

        # Write manifest (atomic, exclusive)
        manifest_path = directory / f"{policy.manifest_prefix}_{sequence:06d}.json"
        manifest_content = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
        fd = os.open(str(manifest_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(manifest_content)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                manifest_path.unlink()
            except OSError:
                pass
            raise

        # Sync directory
        dir_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

        # Post-write verification (strict — no allowed unregistered)
        recheck = verify_manifest_chain(directory, policy, allowed_unregistered=set())
        if not recheck["valid"]:
            raise RuntimeError(
                f"Chain corrupt after publish: {'; '.join(recheck['errors'])}. "
                f"Published: {manifest_path.name}"
            )

        return entry

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


# ═══════════════════════════════════════════════════════════════════════
# publish_staged_artifact_with_manifest — Full transaction from staging (B4-B6, B8)
# ═══════════════════════════════════════════════════════════════════════

def publish_staged_artifact_with_manifest(
    directory: Path,
    sealed,  # SealedRawArtifact or similar
    policy: ManifestPolicy,
    identity_fields: dict[str, str],
    extra_manifest_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """B4: Full transaction from a sealed staging descriptor.

    Everything happens under the manifest lock:
      1. acquire lock
      2. verify current chain
      3. validate candidate (identity, duplicate, glob)
      4. reserve sequence
      5. create transaction marker
      6. publish final artifact (no overwrite — B5, os.link)
      7. fsync
      8. publish sidecar
      9. fsync
      10. publish manifest
      11. fsync
      12. strict reverify
      13. mark committed, remove marker + staging
      14. unlock
    """
    staging_path = sealed.staging_path
    final_name = sealed.final_name
    final_path = directory / final_name

    # B8: Validate identity fields and extra fields
    combined_fields = {**identity_fields, **(extra_manifest_fields or {})}
    _validate_extra_fields(extra_manifest_fields, policy)
    for field in policy.identity_fields:
        val = combined_fields.get(field)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"Required identity field '{field}' missing or empty")

    # B5: Pre-checks before lock
    if final_path.exists():
        raise FileExistsError(f"Final artifact already exists: {final_name}")

    # Acquire lock
    lock_path = directory / policy.lock_filename()
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # B5: Check again under lock
        if final_path.exists():
            raise FileExistsError(f"Final artifact already exists (under lock): {final_name}")

        # Verify existing chain
        precheck = verify_manifest_chain(directory, policy, allowed_unregistered=set())
        if precheck["chain_status"] == CHAIN_INVALID:
            raise RuntimeError(f"Chain corrupt: {'; '.join(precheck['errors'])}")
        if precheck["chain_status"] == CHAIN_BOOTSTRAP_REQUIRED:
            raise RuntimeError(f"Bootstrap required: {precheck['unregistered_files']}")

        # Validate candidate against existing entries
        existing_entries = precheck["entries"]
        for field in policy.identity_fields:
            val = combined_fields[field]
            existing_values = {e.get(field) for e in existing_entries}
            if val in existing_values:
                raise ValueError(f"Duplicate {field}: {val}")

        # Reserve sequence
        sequence = len(existing_entries)
        previous_hash = existing_entries[-1]["manifest_hash"] if existing_entries else None

        # B6: Create transaction marker
        marker_name = f"{policy.manifest_prefix}_txn_{sequence:06d}.marker"
        marker_path = directory / marker_name
        marker = {
            "transaction_id": f"txn_{sequence:06d}_{datetime.now(timezone.utc).isoformat()}",
            "status": "STAGED",
            "staging_path": str(staging_path),
            "final_path": str(final_path),
            "sequence": sequence,
            "run_id": identity_fields.get("run_id", ""),
            "scan_id": identity_fields.get("scan_id", ""),
        }
        marker_fd = os.open(str(marker_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(marker_fd, "wb") as f:
            f.write(json.dumps(marker, sort_keys=True).encode())
            f.flush()
            os.fsync(f.fileno())

        try:
            # B5: Publish final artifact (os.link fails if target exists)
            os.link(str(staging_path), str(final_path))
            _dir_fsync(directory)

            marker["status"] = "ARTIFACT_PUBLISHED"

            # Publish sidecar
            sidecar_path = final_path.with_suffix(final_path.suffix + ".sha256")
            if sidecar_path.exists():
                raise FileExistsError(f"Sidecar already exists: {sidecar_path.name}")
            sidecar_tmp = sidecar_path.with_suffix(".tmp")
            sidecar_tmp.write_text(sealed.file_sha256 + "\n")
            os.rename(str(sidecar_tmp), str(sidecar_path))
            _dir_fsync(directory)
            marker["status"] = "SIDECAR_PUBLISHED"

            # Build manifest entry
            entry: dict[str, Any] = {
                "sequence": sequence,
                "filename": final_name,
                "file_sha256": sealed.file_sha256,
                "previous_manifest_hash": previous_hash,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            entry.update(combined_fields)
            entry["manifest_hash"] = _compute_manifest_hash(entry)

            # Publish manifest
            manifest_path = directory / f"{policy.manifest_prefix}_{sequence:06d}.json"
            manifest_content = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
            mfd = os.open(str(manifest_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(mfd, "wb") as f:
                f.write(manifest_content)
                f.flush()
                os.fsync(f.fileno())
            _dir_fsync(directory)
            marker["status"] = "MANIFEST_PUBLISHED"

            # Strict reverify
            recheck = verify_manifest_chain(directory, policy, allowed_unregistered=set())
            if not recheck["valid"]:
                raise RuntimeError(
                    f"Chain corrupt after publish: {'; '.join(recheck['errors'])}"
                )

            # B6: Mark committed, clean up
            marker["status"] = "COMMITTED"
            try:
                staging_path.unlink()
            except OSError:
                pass
            marker_path.unlink()

            return entry

        except Exception as publish_error:
            # B6: Recovery — quarantine partial artifacts
            marker["status"] = "QUARANTINED"
            try:
                quarantine_dir = directory / ".quarantine"
                quarantine_dir.mkdir(exist_ok=True)
                if final_path.exists():
                    qpath = quarantine_dir / f"{final_name}.quarantined"
                    os.rename(str(final_path), str(qpath))
                sidecar = final_path.with_suffix(final_path.suffix + ".sha256")
                if sidecar.exists():
                    os.rename(str(sidecar), str(quarantine_dir / f"{sidecar.name}.quarantined"))
            except OSError:
                pass
            try:
                marker_path.write_text(json.dumps(marker, sort_keys=True))
            except OSError:
                pass
            raise RuntimeError(
                f"Publish failed, quarantined: {publish_error}"
            ) from publish_error

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _dir_fsync(directory: Path) -> None:
    """Sync directory metadata to disk."""
    dir_fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
