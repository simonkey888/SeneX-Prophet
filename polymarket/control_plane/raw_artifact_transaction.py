"""
SENECIO H-011 V3 — Raw Artifact Transaction System.

Implements the full transaction lifecycle for publishing immutable raw
artifacts with manifests, sidecars, and crash recovery.

Fases T1-T8:
  - Strict validation of sealed artifacts
  - Reserved field and identity validation
  - Durable non-replace sidecar publication
  - Persistent transaction marker state machine
  - Crash recovery on restart
  - Chain integrity verification (sidecar + raw content)
  - Staging lifecycle management
"""
from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control_plane.artifact_manifest import (
    ManifestPolicy,
    RAW_MANIFEST_POLICY,
    SNAPSHOT_MANIFEST_POLICY,
    RESERVED_FIELDS,
    _compute_manifest_hash,
    _matches_glob,
    verify_manifest_chain,
    CHAIN_EMPTY,
    CHAIN_VALID,
    CHAIN_BOOTSTRAP_REQUIRED,
    CHAIN_INVALID,
)


# ═══════════════════════════════════════════════════════════════════════
# T1: Strict raw event loading
# ═══════════════════════════════════════════════════════════════════════

def load_raw_events_strict(path: Path) -> list[dict[str, Any]]:
    """Load raw events from a gzipped JSONL file with strict validation.

    Fails on:
      - gzip truncation or CRC error
      - invalid JSON lines
      - empty lines (unexpected)
      - non-dict payloads
      - missing required schema fields
    """
    events: list[dict[str, Any]] = []
    required_fields = {"received_at_utc", "source", "endpoint", "payload",
                       "payload_sha256", "schema_version"}

    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line_num, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                raise ValueError(f"Empty line {line_num} in {path.name}")
            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON at line {line_num} in {path.name}: {e}") from e
            if not isinstance(event, dict):
                raise ValueError(f"Non-dict payload at line {line_num} in {path.name}")
            missing = required_fields - set(event.keys())
            if missing:
                raise ValueError(f"Missing fields {missing} at line {line_num} in {path.name}")
            events.append(event)

    return events


# ═══════════════════════════════════════════════════════════════════════
# T1: Sealed artifact validation
# ═══════════════════════════════════════════════════════════════════════

def validate_sealed_artifact(
    sealed,
    directory: Path,
    policy: ManifestPolicy,
    identity_fields: dict[str, str],
) -> None:
    """T1: Validate a SealedRawArtifact before any publication.

    Raises ValueError on any validation failure.
    """
    # 1. Validate staging_path
    staging = Path(sealed.staging_path)
    if not staging.exists():
        raise ValueError(f"Staging file does not exist: {staging}")
    if not staging.is_file():
        raise ValueError(f"Staging path is not a regular file: {staging}")
    if staging.is_symlink():
        raise ValueError(f"Staging path is a symlink: {staging}")
    pending_dir = directory / ".pending"
    try:
        staging.relative_to(pending_dir)
    except ValueError:
        raise ValueError(f"Staging file not in .pending/: {staging}")
    if not staging.name.endswith(".tmp"):
        raise ValueError(f"Staging file does not have .tmp extension: {staging.name}")

    # 2. Validate final_name
    final_name = sealed.final_name
    if final_name != Path(final_name).name:
        raise ValueError(f"final_name contains path components: {final_name}")
    if "/" in final_name or "\\" in final_name or ".." in final_name:
        raise ValueError(f"final_name contains forbidden characters: {final_name}")
    if not _matches_glob(final_name, policy.artifact_glob):
        raise ValueError(f"final_name does not match artifact glob: {final_name}")

    # Check targets don't exist yet
    final_path = directory / final_name
    if final_path.exists():
        raise FileExistsError(f"Final artifact already exists: {final_name}")
    sidecar_path = final_path.with_suffix(final_path.suffix + ".sha256")
    if sidecar_path.exists():
        raise FileExistsError(f"Sidecar already exists: {sidecar_path.name}")

    # 3. Re-read staging in strict mode
    try:
        disk_events = load_raw_events_strict(staging)
    except (gzip.BadGzipFile, OSError, ValueError) as e:
        raise ValueError(f"Staging file content invalid: {e}") from e

    # 4. Recalculate and compare
    disk_event_count = len(disk_events)
    if disk_event_count != sealed.event_count:
        raise ValueError(
            f"event_count mismatch: sealed={sealed.event_count}, disk={disk_event_count}"
        )

    disk_condition_ids = set()
    for ev in disk_events:
        cid = ev.get("requested_condition_id", "")
        if cid:
            disk_condition_ids.add(cid)
    disk_cids_tuple = tuple(sorted(disk_condition_ids))
    if disk_cids_tuple != sealed.condition_ids:
        raise ValueError(
            f"condition_ids mismatch: sealed={sealed.condition_ids}, disk={disk_cids_tuple}"
        )

    canonical = json.dumps(
        disk_events, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    disk_canonical_sha = hashlib.sha256(canonical).hexdigest()
    if disk_canonical_sha != sealed.canonical_events_sha256:
        raise ValueError(
            f"canonical_events_sha256 mismatch: sealed={sealed.canonical_events_sha256[:16]}, "
            f"disk={disk_canonical_sha[:16]}"
        )

    disk_file_sha = hashlib.sha256(staging.read_bytes()).hexdigest()
    if disk_file_sha != sealed.file_sha256:
        raise ValueError(
            f"file_sha256 mismatch: sealed={sealed.file_sha256[:16]}, disk={disk_file_sha[:16]}"
        )

    # 5. Verify identity consistency
    if sealed.run_id != identity_fields.get("run_id"):
        raise ValueError(
            f"run_id mismatch: sealed={sealed.run_id}, identity={identity_fields.get('run_id')}"
        )
    if sealed.scan_id != identity_fields.get("scan_id"):
        raise ValueError(
            f"scan_id mismatch: sealed={sealed.scan_id}, identity={identity_fields.get('scan_id')}"
        )


# ═══════════════════════════════════════════════════════════════════════
# T2: Field validation
# ═══════════════════════════════════════════════════════════════════════

def validate_identity_and_extra_fields(
    identity_fields: dict[str, str],
    extra_manifest_fields: dict[str, Any] | None,
    policy: ManifestPolicy,
) -> None:
    """T2: Validate identity_fields and extra_manifest_fields separately.

    - Neither can contain reserved fields
    - identity_fields must have exactly the keys declared by policy.identity_fields
    - extra_manifest_fields cannot contain reserved fields
    """
    # Validate identity_fields
    identity_keys = set(identity_fields.keys())
    expected_keys = set(policy.identity_fields)
    if identity_keys != expected_keys:
        raise ValueError(
            f"identity_fields keys {identity_keys} != policy.identity_fields {expected_keys}"
        )
    for key, val in identity_fields.items():
        if key in RESERVED_FIELDS:
            raise ValueError(f"Reserved field '{key}' in identity_fields")
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"Identity field '{key}' must be non-empty string")

    # Validate extra_manifest_fields
    if extra_manifest_fields:
        for key in extra_manifest_fields:
            if key in RESERVED_FIELDS:
                raise ValueError(f"Reserved field '{key}' in extra_manifest_fields")
            if key in policy.identity_fields:
                raise ValueError(f"Identity field '{key}' cannot also be in extra_manifest_fields")


# ═══════════════════════════════════════════════════════════════════════
# T4: Transaction marker state machine
# ═══════════════════════════════════════════════════════════════════════

MARKER_VERSION = "h011-artifact-txn-v1"

MARKER_STAGED = "STAGED"
MARKER_ARTIFACT_PUBLISHED = "ARTIFACT_PUBLISHED"
MARKER_SIDECAR_PUBLISHED = "SIDECAR_PUBLISHED"
MARKER_MANIFEST_PUBLISHED = "MANIFEST_PUBLISHED"
MARKER_COMMITTED = "COMMITTED"
MARKER_QUARANTINED = "QUARANTINED"

ALL_MARKER_STATES = (
    MARKER_STAGED, MARKER_ARTIFACT_PUBLISHED, MARKER_SIDECAR_PUBLISHED,
    MARKER_MANIFEST_PUBLISHED, MARKER_COMMITTED, MARKER_QUARANTINED,
)


def create_marker(
    *,
    sequence: int,
    run_id: str,
    scan_id: str,
    staging_path: str,
    final_name: str,
    sidecar_name: str,
    manifest_name: str,
    file_sha256: str,
    canonical_events_sha256: str,
    event_count: int,
    condition_ids: list[str],
    previous_manifest_hash: str | None,
    candidate_manifest_hash: str,
    policy_name: str = "raw",
) -> dict[str, Any]:
    """Create a transaction marker dict."""
    return {
        "transaction_version": MARKER_VERSION,
        "transaction_id": f"txn_{sequence:06d}_{datetime.now(timezone.utc).isoformat()}",
        "policy": policy_name,
        "status": MARKER_STAGED,
        "sequence": sequence,
        "run_id": run_id,
        "scan_id": scan_id,
        "staging_path": staging_path,
        "final_name": final_name,
        "sidecar_name": sidecar_name,
        "manifest_name": manifest_name,
        "file_sha256": file_sha256,
        "canonical_events_sha256": canonical_events_sha256,
        "event_count": event_count,
        "condition_ids": condition_ids,
        "previous_manifest_hash": previous_manifest_hash,
        "candidate_manifest_hash": candidate_manifest_hash,
    }


def persist_transaction_marker(marker_path: Path, marker: dict[str, Any]) -> None:
    """T4: Persist a transaction marker atomically.

    Uses temp file + rename for atomic update.
    """
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = marker_path.with_suffix(".tmp")
    content = json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()
    fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp_path.unlink(missing_ok=True)
        raise
    os.rename(str(tmp_path), str(marker_path))
    # fsync directory
    dir_fd = os.open(str(marker_path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# ═══════════════════════════════════════════════════════════════════════
# T3: Durable non-replace sidecar
# ═══════════════════════════════════════════════════════════════════════

def publish_sidecar_durable(
    sidecar_path: Path,
    file_sha256: str,
) -> None:
    """T3: Publish a sidecar file durably with no-overwrite.

    Uses O_CREAT | O_EXCL in .pending, then hardlink to final.
    """
    if sidecar_path.exists():
        raise FileExistsError(f"Sidecar already exists: {sidecar_path.name}")

    # Create in .pending first
    pending_dir = sidecar_path.parent / ".pending"
    pending_dir.mkdir(exist_ok=True)
    tmp_name = f"sidecar_{sidecar_path.name}.tmp"
    tmp_path = pending_dir / tmp_name

    content = file_sha256 + "\n"
    fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content.encode("ascii"))
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp_path.unlink(missing_ok=True)
        raise

    # Hardlink to final (no-overwrite)
    try:
        os.link(str(tmp_path), str(sidecar_path))
    except FileExistsError:
        tmp_path.unlink(missing_ok=True)
        raise

    # fsync directory
    dir_fd = os.open(str(sidecar_path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

    # Remove temp
    tmp_path.unlink(missing_ok=True)

    # Verify: re-read sidecar
    sidecar_content = sidecar_path.read_text().strip()
    if len(sidecar_content) != 64:
        raise ValueError(f"Sidecar content is not 64 hex chars: {len(sidecar_content)}")
    try:
        int(sidecar_content, 16)
    except ValueError:
        raise ValueError(f"Sidecar content is not valid hex: {sidecar_content[:20]}")
    if sidecar_content != file_sha256:
        raise ValueError(
            f"Sidecar content mismatch: sidecar={sidecar_content[:16]}, expected={file_sha256[:16]}"
        )


# ═══════════════════════════════════════════════════════════════════════
# T5: Recovery
# ═══════════════════════════════════════════════════════════════════════

def recover_incomplete_transactions(directory: Path, policy: ManifestPolicy) -> list[dict[str, Any]]:
    """T5: Recover incomplete transactions.

    Must be called under the manifest lock.
    Returns a list of recovery results.
    """
    results: list[dict[str, Any]] = []
    marker_files = sorted(directory.glob(f"{policy.manifest_prefix}_txn_*.marker"))

    for mf in marker_files:
        try:
            marker = json.loads(mf.read_text())
        except (json.JSONDecodeError, OSError) as e:
            # Corrupt marker — quarantine
            quarantine = directory / ".quarantine"
            quarantine.mkdir(exist_ok=True)
            os.rename(str(mf), str(quarantine / f"{mf.name}.corrupt"))
            results.append({"marker": mf.name, "action": "QUARANTINED", "reason": f"corrupt marker: {e}"})
            continue

        status = marker.get("status", "UNKNOWN")
        seq = marker.get("sequence", -1)
        final_name = marker.get("final_name", "")
        sidecar_name = marker.get("sidecar_name", "")
        manifest_name = marker.get("manifest_name", "")
        staging_path = Path(marker.get("staging_path", ""))

        final_path = directory / final_name
        sidecar_path = directory / sidecar_name
        manifest_path = directory / manifest_name

        if status == MARKER_COMMITTED:
            # Clean up residual marker and staging
            mf.unlink(missing_ok=True)
            staging_path.unlink(missing_ok=True)
            results.append({"marker": mf.name, "action": "CLEANED", "reason": "already committed"})
            continue

        if status == MARKER_QUARANTINED:
            # Already quarantined — leave as-is
            results.append({"marker": mf.name, "action": "SKIP", "reason": "already quarantined"})
            continue

        # For all other states, try to complete or quarantine
        # Check what files exist
        artifact_exists = final_path.exists()
        sidecar_exists = sidecar_path.exists()
        manifest_exists = manifest_path.exists()

        if manifest_exists:
            # Manifest was published — verify chain
            recheck = verify_manifest_chain(directory, policy)
            if recheck["valid"]:
                # Chain is valid — complete the commit
                marker["status"] = MARKER_COMMITTED
                persist_transaction_marker(mf, marker)
                mf.unlink(missing_ok=True)
                staging_path.unlink(missing_ok=True)
                results.append({"marker": mf.name, "action": "COMMITTED", "reason": "recovery verified chain"})
            else:
                # Chain invalid — block, keep all evidence
                marker["status"] = MARKER_QUARANTINED
                persist_transaction_marker(mf, marker)
                results.append({"marker": mf.name, "action": "BLOCKED", "reason": f"chain invalid: {recheck['errors']}"})
            continue

        if artifact_exists and sidecar_exists:
            # Artifact + sidecar published but not manifest — try to publish manifest
            # Build candidate manifest from marker
            entry = {
                "sequence": seq,
                "filename": final_name,
                "file_sha256": marker.get("file_sha256", ""),
                "previous_manifest_hash": marker.get("previous_manifest_hash"),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "run_id": marker.get("run_id", ""),
                "scan_id": marker.get("scan_id", ""),
                "event_count": marker.get("event_count", 0),
                "condition_ids": marker.get("condition_ids", []),
                "canonical_events_sha256": marker.get("canonical_events_sha256", ""),
            }
            entry["manifest_hash"] = _compute_manifest_hash(entry)

            # Verify candidate matches marker
            if entry["manifest_hash"] != marker.get("candidate_manifest_hash"):
                # Candidate hash doesn't match — quarantine
                marker["status"] = MARKER_QUARANTINED
                persist_transaction_marker(mf, marker)
                results.append({"marker": mf.name, "action": "QUARANTINED", "reason": "manifest hash mismatch"})
                continue

            # Try to write manifest
            try:
                mfd = os.open(str(manifest_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                with os.fdopen(mfd, "wb") as f:
                    f.write(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode())
                    f.flush()
                    os.fsync(f.fileno())
                # Verify chain
                recheck = verify_manifest_chain(directory, policy)
                if recheck["valid"]:
                    marker["status"] = MARKER_COMMITTED
                    persist_transaction_marker(mf, marker)
                    mf.unlink(missing_ok=True)
                    staging_path.unlink(missing_ok=True)
                    results.append({"marker": mf.name, "action": "COMMITTED", "reason": "recovery published manifest"})
                else:
                    marker["status"] = MARKER_QUARANTINED
                    persist_transaction_marker(mf, marker)
                    results.append({"marker": mf.name, "action": "BLOCKED", "reason": f"chain invalid after manifest: {recheck['errors']}"})
            except FileExistsError:
                # Manifest already exists (race?) — verify
                recheck = verify_manifest_chain(directory, policy)
                if recheck["valid"]:
                    marker["status"] = MARKER_COMMITTED
                    persist_transaction_marker(mf, marker)
                    mf.unlink(missing_ok=True)
                    staging_path.unlink(missing_ok=True)
                    results.append({"marker": mf.name, "action": "COMMITTED", "reason": "manifest existed, chain valid"})
                else:
                    marker["status"] = MARKER_QUARANTINED
                    persist_transaction_marker(mf, marker)
                    results.append({"marker": mf.name, "action": "BLOCKED", "reason": "manifest existed, chain invalid"})
            continue

        # Artifact exists but no sidecar, or neither exists
        # Quarantine any partial files
        quarantine = directory / ".quarantine"
        quarantine.mkdir(exist_ok=True)
        if artifact_exists:
            os.rename(str(final_path), str(quarantine / f"{final_name}.quarantined"))
        if sidecar_exists:
            os.rename(str(sidecar_path), str(quarantine / f"{sidecar_name}.quarantined"))
        marker["status"] = MARKER_QUARANTINED
        persist_transaction_marker(mf, marker)
        results.append({"marker": mf.name, "action": "QUARANTINED", "reason": f"incomplete at status={status}"})

    return results


# ═══════════════════════════════════════════════════════════════════════
# Full transaction: publish_staged_artifact_with_manifest (revised T1-T8)
# ═══════════════════════════════════════════════════════════════════════

def publish_staged_artifact_with_manifest_v2(
    directory: Path,
    sealed,
    policy: ManifestPolicy,
    identity_fields: dict[str, str],
    extra_manifest_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full transaction from sealed staging descriptor.

    T1-T8 compliant:
      1. T2: Validate identity + extra fields
      2. T1: Validate sealed artifact (strict)
      3. Acquire lock
      4. T5: Recover incomplete transactions
      5. Verify chain
      6. Validate candidate against existing
      7. T4: Create marker (STAGED)
      8. T5: Publish artifact (os.link, no-overwrite)
      9. T3: Publish sidecar (durable, no-overwrite)
      10. T6: Build + verify manifest candidate in memory
      11. Publish manifest (O_EXCL)
      12. T7: Strict reverify chain
      13. T4: Mark COMMITTED, cleanup
      14. Release lock
    """
    # T2: Validate fields before lock
    validate_identity_and_extra_fields(identity_fields, extra_manifest_fields, policy)

    # T1: Validate sealed artifact before lock
    validate_sealed_artifact(sealed, directory, policy, identity_fields)

    final_name = sealed.final_name
    final_path = directory / final_name
    sidecar_name = final_name + ".sha256"
    sidecar_path = directory / sidecar_name
    manifest_name = f"{policy.manifest_prefix}_{{:06d}}.json"  # filled after sequence reserved

    # Build combined fields for manifest
    combined_fields = {**identity_fields, **(extra_manifest_fields or {})}
    combined_fields.update(sealed.to_manifest_fields())

    # Acquire lock
    lock_path = directory / policy.lock_filename()
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # T5: Recover incomplete transactions
        recover_incomplete_transactions(directory, policy)

        # Verify existing chain
        precheck = verify_manifest_chain(directory, policy, allowed_unregistered=set())
        if precheck["chain_status"] == CHAIN_INVALID:
            raise RuntimeError(f"Chain corrupt: {'; '.join(precheck['errors'])}")
        if precheck["chain_status"] == CHAIN_BOOTSTRAP_REQUIRED:
            raise RuntimeError(f"Bootstrap required: {precheck['unregistered_files']}")

        # Validate candidate against existing entries
        existing_entries = precheck["entries"]
        for field_name in policy.identity_fields:
            val = combined_fields[field_name]
            existing_values = {e.get(field_name) for e in existing_entries}
            if val in existing_values:
                raise ValueError(f"Duplicate {field_name}: {val}")

        # Reserve sequence
        sequence = len(existing_entries)
        previous_hash = existing_entries[-1]["manifest_hash"] if existing_entries else None
        manifest_name = manifest_name.format(sequence)
        manifest_path = directory / manifest_name

        # T6: Build manifest candidate in memory and verify virtual chain
        entry: dict[str, Any] = {
            "sequence": sequence,
            "filename": final_name,
            "file_sha256": sealed.file_sha256,
            "previous_manifest_hash": previous_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        entry.update(combined_fields)
        entry["manifest_hash"] = _compute_manifest_hash(entry)

        # T4: Create marker (STAGED)
        marker = create_marker(
            sequence=sequence,
            run_id=identity_fields["run_id"],
            scan_id=identity_fields["scan_id"],
            staging_path=str(sealed.staging_path),
            final_name=final_name,
            sidecar_name=sidecar_name,
            manifest_name=manifest_name,
            file_sha256=sealed.file_sha256,
            canonical_events_sha256=sealed.canonical_events_sha256,
            event_count=sealed.event_count,
            condition_ids=list(sealed.condition_ids),
            previous_manifest_hash=previous_hash,
            candidate_manifest_hash=entry["manifest_hash"],
            policy_name=policy.manifest_prefix,
        )
        marker_path = directory / f"{policy.manifest_prefix}_txn_{sequence:06d}.marker"
        persist_transaction_marker(marker_path, marker)

        try:
            # T5: Publish artifact (os.link, no-overwrite)
            os.link(str(sealed.staging_path), str(final_path))
            _dir_fsync(directory)
            marker["status"] = MARKER_ARTIFACT_PUBLISHED
            persist_transaction_marker(marker_path, marker)

            # T3: Publish sidecar (durable, no-overwrite)
            publish_sidecar_durable(sidecar_path, sealed.file_sha256)
            marker["status"] = MARKER_SIDECAR_PUBLISHED
            persist_transaction_marker(marker_path, marker)

            # T6: Publish manifest (O_EXCL)
            manifest_content = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
            mfd = os.open(str(manifest_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(mfd, "wb") as f:
                f.write(manifest_content)
                f.flush()
                os.fsync(f.fileno())
            _dir_fsync(directory)
            marker["status"] = MARKER_MANIFEST_PUBLISHED
            persist_transaction_marker(marker_path, marker)

            # T7: Strict reverify
            recheck = verify_manifest_chain(directory, policy, allowed_unregistered=set())
            if not recheck["valid"]:
                # T6: Don't break chain — block, keep all evidence
                marker["status"] = MARKER_QUARANTINED
                persist_transaction_marker(marker_path, marker)
                raise RuntimeError(
                    f"Chain invalid after manifest publish: {'; '.join(recheck['errors'])}. "
                    f"All artifacts preserved for investigation."
                )

            # T4: Mark COMMITTED, cleanup
            marker["status"] = MARKER_COMMITTED
            persist_transaction_marker(marker_path, marker)
            # Remove staging
            try:
                sealed.staging_path.unlink()
            except OSError:
                pass
            # Remove marker
            marker_path.unlink()

            return entry

        except Exception as publish_error:
            # T6: Don't destroy evidence — quarantine
            marker["status"] = MARKER_QUARANTINED
            persist_transaction_marker(marker_path, marker)
            # Don't move files — keep for investigation
            raise RuntimeError(
                f"Publish failed at status={marker.get('status', 'UNKNOWN')}: {publish_error}. "
                f"Marker preserved at {marker_path.name}. "
                f"Recovery will evaluate on next run."
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
