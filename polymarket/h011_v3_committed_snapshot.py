"""Committed manifest-chain reader for SENEX / SENECIO H-011 V3.

The manifest chain is the authority. ``latest.json`` is only a derived cache and
is returned only when its raw-chain binding matches the latest fully verified
manifest entry.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat as statmod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # runtime imports modules from /app/polymarket
    import h011_v3_raw_transaction as rt
except ModuleNotFoundError:  # package imports used by tests
    from polymarket import h011_v3_raw_transaction as rt  # type: ignore


class CommittedChainError(RuntimeError):
    """The committed raw chain or its derived cache cannot be trusted."""


class NoCommittedScan(CommittedChainError):
    """The chain is valid but contains no committed scans."""


@dataclass(frozen=True)
class CommittedChainState:
    raw_directory: Path
    entries: tuple[dict[str, Any], ...]
    latest: dict[str, Any] | None
    chain_verified: bool

    def to_dict(self) -> dict[str, Any]:
        latest = self.latest or {}
        return {
            "chain_verified": self.chain_verified,
            "entry_count": len(self.entries),
            "current_sequence": latest.get("sequence"),
            "manifest_hash": latest.get("manifest_hash"),
            "previous_manifest_hash": latest.get("previous_manifest_hash"),
            "artifact_name": latest.get("filename"),
            "artifact_sha256": latest.get("file_sha256"),
            "canonical_events_sha256": latest.get("canonical_events_sha256"),
            "event_count": latest.get("event_count", 0),
            "scan_id": latest.get("scan_id"),
            "run_id": latest.get("run_id"),
        }


def _read_regular(root_fd: int, name: str) -> tuple[bytes, os.stat_result]:
    rt.validate_bare_filename(name)
    try:
        path_stat = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise CommittedChainError(f"required committed file missing: {name}") from exc
    if statmod.S_ISLNK(path_stat.st_mode) or not statmod.S_ISREG(path_stat.st_mode):
        raise CommittedChainError(f"committed entry is not a regular non-symlink file: {name}")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError as exc:
        raise CommittedChainError(f"cannot open committed entry {name}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            raise CommittedChainError(f"committed entry changed while opening: {name}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), opened
    finally:
        os.close(fd)


def _require_mode_0444(name: str, entry_stat: os.stat_result) -> None:
    mode = statmod.S_IMODE(entry_stat.st_mode)
    if mode != 0o444:
        raise CommittedChainError(f"{name} mode must be 0444, got {oct(mode)}")


def _validate_directory_contents(
    *, guard: rt.RawChainLockGuard, entries: list[dict[str, Any]], policy: rt.MarkerValidationPolicy
) -> None:
    root_fd = guard.trusted.fd
    referenced: set[str] = {f"{policy.manifest_prefix}.lock"}
    for entry in entries:
        referenced.add(entry["filename"])
        referenced.add(entry["filename"] + ".sha256")
        referenced.add(f"{policy.manifest_prefix}_{entry['sequence']:06d}.json")
    allowed_metadata = {".pending", ".quarantine", ".eligibility_state.json"}

    for name in os.listdir(root_fd):
        if name in referenced or name in allowed_metadata:
            continue
        if (
            name.startswith("raw_scan_")
            or name.startswith(f"{policy.manifest_prefix}_")
            or name.endswith(".sha256")
            or ".tmp." in name
            or name.endswith(".marker")
        ):
            raise CommittedChainError(f"unowned transaction residue in committed root: {name}")

    for child in (".pending", ".quarantine"):
        try:
            child_stat = os.stat(child, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if statmod.S_ISLNK(child_stat.st_mode) or not statmod.S_ISDIR(child_stat.st_mode):
            raise CommittedChainError(f"{child} must be a real directory")
        child_fd = os.open(child, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            names = sorted(os.listdir(child_fd))
        finally:
            os.close(child_fd)
        if names:
            raise CommittedChainError(f"unresolved evidence in {child}: {names}")


def validate_committed_chain_under_lock(
    *,
    guard: rt.RawChainLockGuard,
    raw_directory: Path,
    policy: rt.MarkerValidationPolicy = rt.DEFAULT_MARKER_POLICY,
) -> CommittedChainState:
    """Validate logical linkage, physical files, permissions, and residues."""
    rt.assert_guard_valid(guard, raw_directory, policy.manifest_prefix)
    try:
        entries = rt._read_validated_manifest_chain_under_lock(  # noqa: SLF001
            guard=guard,
            raw_directory=raw_directory,
            policy=policy,
        )
    except Exception as exc:
        raise CommittedChainError(f"manifest-chain validation failed: {exc}") from exc

    root_fd = guard.trusted.fd
    committed_device: int | None = None
    for entry in entries:
        manifest_name = f"{policy.manifest_prefix}_{entry['sequence']:06d}.json"
        manifest_bytes, manifest_stat = _read_regular(root_fd, manifest_name)
        _require_mode_0444(manifest_name, manifest_stat)
        expected_manifest_bytes = rt.canonical_manifest_file_bytes(entry)
        if manifest_bytes != expected_manifest_bytes:
            raise CommittedChainError(f"manifest bytes mismatch: {manifest_name}")

        artifact_name = entry["filename"]
        artifact_bytes, artifact_stat = _read_regular(root_fd, artifact_name)
        _require_mode_0444(artifact_name, artifact_stat)
        if committed_device is None:
            committed_device = artifact_stat.st_dev
        elif artifact_stat.st_dev != committed_device:
            raise CommittedChainError(f"artifact device differs within committed chain: {artifact_name}")
        if manifest_stat.st_dev != artifact_stat.st_dev:
            raise CommittedChainError(f"manifest/artifact filesystem mismatch: {manifest_name}")
        actual_sha = hashlib.sha256(artifact_bytes).hexdigest()
        if actual_sha != entry["file_sha256"]:
            raise CommittedChainError(
                f"artifact SHA-256 mismatch: {artifact_name}: {actual_sha} != {entry['file_sha256']}"
            )

        sidecar_name = artifact_name + ".sha256"
        sidecar_bytes, sidecar_stat = _read_regular(root_fd, sidecar_name)
        _require_mode_0444(sidecar_name, sidecar_stat)
        if sidecar_stat.st_dev != artifact_stat.st_dev:
            raise CommittedChainError(f"sidecar/artifact filesystem mismatch: {sidecar_name}")
        expected_sidecar = f"{entry['file_sha256']}  {artifact_name}\n".encode("ascii")
        if sidecar_bytes != expected_sidecar:
            raise CommittedChainError(f"sidecar mismatch: {sidecar_name}")

        artifact_fd = os.open(artifact_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            events = rt.load_raw_events_strict_fd(artifact_fd)
        except Exception as exc:
            raise CommittedChainError(f"strict raw artifact validation failed: {artifact_name}: {exc}") from exc
        finally:
            os.close(artifact_fd)
        if len(events) != entry["event_count"]:
            raise CommittedChainError(f"event count mismatch: {artifact_name}")
        condition_ids = sorted(
            {
                str(event.get("requested_condition_id") or "")
                for event in events
                if str(event.get("requested_condition_id") or "")
            }
        )
        if condition_ids != entry["condition_ids"]:
            raise CommittedChainError(f"condition IDs mismatch: {artifact_name}")
        canonical_events_sha = rt.canonical_events_sha256(events)
        if canonical_events_sha != entry["canonical_events_sha256"]:
            raise CommittedChainError(f"canonical event hash mismatch: {artifact_name}")

    _validate_directory_contents(guard=guard, entries=entries, policy=policy)
    return CommittedChainState(
        raw_directory=raw_directory,
        entries=tuple(dict(entry) for entry in entries),
        latest=dict(entries[-1]) if entries else None,
        chain_verified=True,
    )


def validate_committed_chain(
    raw_directory: Path,
    policy: rt.MarkerValidationPolicy = rt.DEFAULT_MARKER_POLICY,
) -> CommittedChainState:
    lock = rt.RawChainLock(raw_directory, policy.manifest_prefix)
    with lock.acquire() as guard:
        return validate_committed_chain_under_lock(
            guard=guard, raw_directory=raw_directory, policy=policy
        )


def _snapshot_file(state_dir: Path) -> tuple[dict[str, Any], bytes]:
    latest = state_dir / "latest.json"
    sidecar = state_dir / "latest.json.sha256"
    try:
        latest_stat = os.lstat(latest)
        sidecar_stat = os.lstat(sidecar)
    except FileNotFoundError as exc:
        raise NoCommittedScan("committed snapshot cache is not available") from exc
    if statmod.S_ISLNK(latest_stat.st_mode) or statmod.S_ISLNK(sidecar_stat.st_mode):
        raise CommittedChainError("snapshot cache or sidecar is a symlink")
    raw = latest.read_bytes()
    expected = sidecar.read_text(encoding="ascii").strip()
    actual = hashlib.sha256(raw).hexdigest()
    if expected != actual:
        raise CommittedChainError(f"snapshot cache SHA mismatch: {actual} != {expected}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommittedChainError(f"snapshot cache is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CommittedChainError("snapshot cache root must be an object")
    return payload, raw


def load_committed_snapshot(
    *,
    results_root: Path,
    raw_directory: Path | None = None,
    state_dir: Path | None = None,
    policy: rt.MarkerValidationPolicy = rt.DEFAULT_MARKER_POLICY,
) -> tuple[dict[str, Any], CommittedChainState]:
    raw_directory = raw_directory or results_root / "h011_v3" / "raw_chain_v1"
    state_dir = state_dir or results_root / "v3" / "state"
    chain = validate_committed_chain(raw_directory, policy)
    if chain.latest is None:
        raise NoCommittedScan("no committed raw scan is available")
    snapshot, _ = _snapshot_file(state_dir)
    binding = ((snapshot.get("aggregate_metrics") or {}).get("raw_chain") or {})
    expected = chain.to_dict()
    fields = (
        "current_sequence",
        "manifest_hash",
        "artifact_name",
        "artifact_sha256",
        "canonical_events_sha256",
        "event_count",
        "scan_id",
        "run_id",
    )
    mismatches = {
        field: {"snapshot": binding.get(field), "chain": expected.get(field)}
        for field in fields
        if binding.get(field) != expected.get(field)
    }
    if mismatches:
        raise CommittedChainError(f"snapshot cache is not bound to latest committed manifest: {mismatches}")
    return snapshot, chain


def snapshot_age_sec(snapshot: dict[str, Any], *, now: datetime | None = None) -> float | None:
    generated = snapshot.get("generated_at")
    if not isinstance(generated, str) or not generated:
        return None
    try:
        generated_at = datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except ValueError:
        return None
    if generated_at.tzinfo is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - generated_at.astimezone(timezone.utc)).total_seconds())


def replay_latest_committed(
    *,
    results_root: Path,
    raw_directory: Path | None = None,
    policy: rt.MarkerValidationPolicy = rt.DEFAULT_MARKER_POLICY,
) -> dict[str, Any]:
    raw_directory = raw_directory or results_root / "h011_v3" / "raw_chain_v1"
    chain = validate_committed_chain(raw_directory, policy)
    if chain.latest is None:
        raise NoCommittedScan("no committed raw scan is available")
    entry = chain.latest
    artifact = raw_directory / entry["filename"]
    events = rt.load_raw_events_strict(artifact)
    source_counts: dict[str, int] = {}
    for event in events:
        source = str(event.get("source") or "UNKNOWN")
        source_counts[source] = source_counts.get(source, 0) + 1
    return {
        "scan_id": entry["scan_id"],
        "run_id": entry["run_id"],
        "sequence": entry["sequence"],
        "manifest_hash": entry["manifest_hash"],
        "previous_manifest_hash": entry["previous_manifest_hash"],
        "artifact": entry["filename"],
        "file_sha256": entry["file_sha256"],
        "file_sha256_matches": True,
        "canonical_events_sha256": entry["canonical_events_sha256"],
        "event_count": entry["event_count"],
        "source_counts": source_counts,
        "raw_complete": True,
        "chain_verified": True,
        "transform_reexecuted": False,
        "replay_verified": True,
        "replay_contract": "strict_committed_raw_chain_v1",
        "error": None,
    }
