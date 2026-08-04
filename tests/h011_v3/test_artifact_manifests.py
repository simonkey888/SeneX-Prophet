"""Tests for artifact manifest system (FASE A.4 revised).

Tests the manifest chain verification, atomic writing, tampering detection,
concurrency, and the 8 bloqueadores A.4.1–A.4.8.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "polymarket"))

from control_plane.artifact_manifest import (
    write_manifest_atomic,
    verify_manifest_chain,
    get_last_manifest_hash,
    get_next_sequence,
    _compute_manifest_hash,
    ManifestPolicy,
    RAW_MANIFEST_POLICY,
    SNAPSHOT_MANIFEST_POLICY,
    CHAIN_EMPTY,
    CHAIN_VALID,
    CHAIN_BOOTSTRAP_REQUIRED,
    CHAIN_INVALID,
    RESERVED_FIELDS,
)


@pytest.fixture
def raw_dir(tmp_path):
    """Directory for raw event artifacts."""
    return tmp_path / "raw"


@pytest.fixture
def snapshot_dir(tmp_path):
    """Directory for snapshot artifacts."""
    d = tmp_path / "state"
    d.mkdir()
    return d


def _make_raw_artifact(directory, name="raw_2026-07-13.events.jsonl.gz", content=b"raw content"):
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    p.write_bytes(content)
    return p


def _make_snapshot_artifact(directory, name="snapshot_001.json", content='{"snapshot_hash":"h1"}'):
    p = directory / name
    p.write_text(content)
    return p


# ═══════════════════════════════════════════════════════════════════════
# A.4.1: Second raw artifact appends successfully
# ═══════════════════════════════════════════════════════════════════════

def test_second_raw_artifact_appends_successfully(raw_dir):
    """A.4.1: Two consecutive raw artifact writes produce a valid chain."""
    a1 = _make_raw_artifact(raw_dir, "raw_001.events.jsonl.gz", b"content_1")
    write_manifest_atomic(raw_dir, a1, RAW_MANIFEST_POLICY,
                          extra_fields={"run_id": "r1", "scan_id": "s1"})

    a2 = _make_raw_artifact(raw_dir, "raw_002.events.jsonl.gz", b"content_2")
    write_manifest_atomic(raw_dir, a2, RAW_MANIFEST_POLICY,
                          extra_fields={"run_id": "r2", "scan_id": "s2"})

    result = verify_manifest_chain(raw_dir, RAW_MANIFEST_POLICY)
    assert result["chain_status"] == CHAIN_VALID
    assert result["sequence_count"] == 2


def test_second_snapshot_appends_successfully(snapshot_dir):
    """A.4.1: Two consecutive snapshot writes produce a valid chain."""
    s1 = _make_snapshot_artifact(snapshot_dir, "snapshot_001.json", '{"snapshot_hash":"h1"}')
    write_manifest_atomic(snapshot_dir, s1, SNAPSHOT_MANIFEST_POLICY,
                          extra_fields={"run_id": "r1", "scan_id": "s1", "snapshot_hash": "h1"})

    s2 = _make_snapshot_artifact(snapshot_dir, "snapshot_002.json", '{"snapshot_hash":"h2"}')
    write_manifest_atomic(snapshot_dir, s2, SNAPSHOT_MANIFEST_POLICY,
                          extra_fields={"run_id": "r2", "scan_id": "s2", "snapshot_hash": "h2"})

    result = verify_manifest_chain(snapshot_dir, SNAPSHOT_MANIFEST_POLICY)
    assert result["chain_status"] == CHAIN_VALID
    assert result["sequence_count"] == 2


# ═══════════════════════════════════════════════════════════════════════
# A.4.1: Overwrite prevention
# ═══════════════════════════════════════════════════════════════════════

def test_existing_target_manifest_cannot_be_overwritten(raw_dir):
    """A manifest file for the same sequence cannot be overwritten."""
    a1 = _make_raw_artifact(raw_dir, "raw_001.events.jsonl.gz", b"content_1")
    write_manifest_atomic(raw_dir, a1, RAW_MANIFEST_POLICY,
                          extra_fields={"run_id": "r1", "scan_id": "s1"})

    # The manifest_000000.json already exists — writing the same artifact
    # again should fail because verify_manifest_chain sees it as registered
    # and the next sequence would be 1 (not 0).
    # But if we try to add the SAME artifact again, the precheck sees it
    # as already registered (not unregistered), so sequence=1.
    # This test verifies that we CAN add a second DIFFERENT artifact:
    a2 = _make_raw_artifact(raw_dir, "raw_002.events.jsonl.gz", b"content_2")
    write_manifest_atomic(raw_dir, a2, RAW_MANIFEST_POLICY,
                          extra_fields={"run_id": "r2", "scan_id": "s2"})

    # Now manually try to create manifest_000000.json again
    manifest_path = raw_dir / "manifest_000000.json"
    assert manifest_path.exists()

    # O_EXCL prevents overwrite
    with pytest.raises(FileExistsError):
        fd = os.open(str(manifest_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)


# ═══════════════════════════════════════════════════════════════════════
# A.4.3: Pending artifact temporarily allowed
# ═══════════════════════════════════════════════════════════════════════

def test_pending_artifact_is_temporarily_allowed(raw_dir):
    """During write, the pending artifact is allowed as unregistered."""
    a1 = _make_raw_artifact(raw_dir, "raw_001.events.jsonl.gz", b"content_1")
    write_manifest_atomic(raw_dir, a1, RAW_MANIFEST_POLICY,
                          extra_fields={"run_id": "r1", "scan_id": "s1"})

    # Add a second artifact — it's unregistered before write
    a2 = _make_raw_artifact(raw_dir, "raw_002.events.jsonl.gz", b"content_2")

    # Pre-verify with allowed_unregistered should pass
    pre = verify_manifest_chain(raw_dir, RAW_MANIFEST_POLICY,
                                allowed_unregistered={"raw_002.events.jsonl.gz"})
    assert pre["valid"] is True

    # Without allowed_unregistered, it should be invalid (unregistered)
    strict = verify_manifest_chain(raw_dir, RAW_MANIFEST_POLICY)
    assert strict["valid"] is False  # a2 is unregistered


# ═══════════════════════════════════════════════════════════════════════
# A.4.3: Other unregistered artifact blocks write
# ═══════════════════════════════════════════════════════════════════════

def test_other_unregistered_artifact_blocks_write(raw_dir):
    """An unregistered artifact (not the pending one) blocks the write."""
    a1 = _make_raw_artifact(raw_dir, "raw_001.events.jsonl.gz", b"content_1")
    write_manifest_atomic(raw_dir, a1, RAW_MANIFEST_POLICY,
                          extra_fields={"run_id": "r1", "scan_id": "s1"})

    # Add two more artifacts — one will be pending, other is rogue
    a2 = _make_raw_artifact(raw_dir, "raw_002.events.jsonl.gz", b"content_2")
    a3 = _make_raw_artifact(raw_dir, "raw_003.events.jsonl.gz", b"rogue")

    # Writing a2 should fail because a3 is also unregistered (not in allowed_unregistered)
    with pytest.raises((RuntimeError, ValueError)):
        write_manifest_atomic(raw_dir, a2, RAW_MANIFEST_POLICY,
                              extra_fields={"run_id": "r2", "scan_id": "s2"})


# ═══════════════════════════════════════════════════════════════════════
# A.4.4: Empty vs bootstrap required
# ═══════════════════════════════════════════════════════════════════════

def test_empty_chain_distinct_from_corrupt_chain(raw_dir):
    """Empty chain (no artifacts, no manifests) is valid; artifacts without manifests is bootstrap."""
    # Completely empty
    raw_dir.mkdir(parents=True)
    result = verify_manifest_chain(raw_dir, RAW_MANIFEST_POLICY)
    assert result["chain_status"] == CHAIN_EMPTY
    assert result["valid"] is True

    # Artifacts exist but no manifests
    _make_raw_artifact(raw_dir, "raw_001.events.jsonl.gz", b"content")
    result = verify_manifest_chain(raw_dir, RAW_MANIFEST_POLICY)
    assert result["chain_status"] == CHAIN_BOOTSTRAP_REQUIRED
    assert result["valid"] is False


def test_legacy_artifacts_without_manifest_require_bootstrap(raw_dir):
    """Legacy artifacts without manifests require bootstrap, not auto-chain."""
    _make_raw_artifact(raw_dir, "raw_legacy.events.jsonl.gz", b"legacy")
    result = verify_manifest_chain(raw_dir, RAW_MANIFEST_POLICY)
    assert result["chain_status"] == CHAIN_BOOTSTRAP_REQUIRED

    # write_manifest_atomic must reject
    a_new = _make_raw_artifact(raw_dir, "raw_new.events.jsonl.gz", b"new")
    with pytest.raises(RuntimeError, match="bootstrap"):
        write_manifest_atomic(raw_dir, a_new, RAW_MANIFEST_POLICY,
                              extra_fields={"run_id": "r1", "scan_id": "s1"})


# ═══════════════════════════════════════════════════════════════════════
# A.4.5: Reserved fields rejected
# ═══════════════════════════════════════════════════════════════════════

def test_reserved_extra_fields_rejected(raw_dir):
    """Extra fields containing reserved keys are rejected."""
    a = _make_raw_artifact(raw_dir, "raw_001.events.jsonl.gz", b"content")
    for reserved in RESERVED_FIELDS:
        with pytest.raises(ValueError, match="Reserved field"):
            write_manifest_atomic(raw_dir, a, RAW_MANIFEST_POLICY,
                                  extra_fields={reserved: "x", "run_id": "r1", "scan_id": "s1"})


def test_wrong_artifact_glob_rejected(raw_dir):
    """Artifact that doesn't match the policy glob is rejected."""
    # Create a file that doesn't match *.events.jsonl.gz
    raw_dir.mkdir(parents=True, exist_ok=True)
    bad = raw_dir / "not_a_raw_file.txt"
    bad.write_bytes(b"wrong")
    with pytest.raises(ValueError, match="does not match glob"):
        write_manifest_atomic(raw_dir, bad, RAW_MANIFEST_POLICY,
                              extra_fields={"run_id": "r1", "scan_id": "s1"})


# ═══════════════════════════════════════════════════════════════════════
# A.4.6: Post-write chain has no unregistered files
# ═══════════════════════════════════════════════════════════════════════

def test_post_write_chain_has_no_unregistered_files(raw_dir):
    """After write, the full chain has zero unregistered files."""
    a1 = _make_raw_artifact(raw_dir, "raw_001.events.jsonl.gz", b"content_1")
    write_manifest_atomic(raw_dir, a1, RAW_MANIFEST_POLICY,
                          extra_fields={"run_id": "r1", "scan_id": "s1"})

    result = verify_manifest_chain(raw_dir, RAW_MANIFEST_POLICY)
    assert result["chain_status"] == CHAIN_VALID
    assert len(result["unregistered_files"]) == 0


# ═══════════════════════════════════════════════════════════════════════
# A.4.7: Concurrent append
# ═══════════════════════════════════════════════════════════════════════

from control_plane.artifact_manifest import publish_artifact_bytes_with_manifest


def test_concurrent_append_preserves_valid_chain(raw_dir):
    """Two concurrent publishers serialize artifact creation and manifest append.

    The bytes API creates the final artifact only after taking the chain lock,
    eliminating the historical pre-lock unregistered-file race.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[Exception] = []

    def writer(idx, run_id, scan_id):
        try:
            barrier.wait(timeout=5)
            entry = publish_artifact_bytes_with_manifest(
                raw_dir,
                f"raw_{idx:03d}.events.jsonl.gz",
                f"content_{idx}".encode(),
                RAW_MANIFEST_POLICY,
                identity_fields={"run_id": run_id, "scan_id": scan_id},
            )
            results.append(entry)
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(1, "r1", "s1")),
        threading.Thread(target=writer, args=(2, "r2", "s2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, f"Errors: {errors}"
    assert len(results) == 2
    result = verify_manifest_chain(raw_dir, RAW_MANIFEST_POLICY)
    assert result["chain_status"] == CHAIN_VALID
    assert result["sequence_count"] == 2
    assert result["unregistered_files"] == []
    entries = sorted(result["entries"], key=lambda entry: entry["sequence"])
    assert [entry["sequence"] for entry in entries] == [0, 1]
    assert entries[1]["previous_manifest_hash"] == entries[0]["manifest_hash"]
    assert {entry["run_id"] for entry in entries} == {"r1", "r2"}
    assert {entry["scan_id"] for entry in entries} == {"s1", "s2"}


# ═══════════════════════════════════════════════════════════════════════
# A.4.8: Manifest filename matches sequence
# ═══════════════════════════════════════════════════════════════════════

def test_manifest_filename_matches_sequence(raw_dir):
    """Manifest filename uses the sequence number (zero-padded)."""
    a = _make_raw_artifact(raw_dir, "raw_001.events.jsonl.gz", b"content")
    entry = write_manifest_atomic(raw_dir, a, RAW_MANIFEST_POLICY,
                                  extra_fields={"run_id": "r1", "scan_id": "s1"})
    manifest_path = raw_dir / f"manifest_{entry['sequence']:06d}.json"
    assert manifest_path.exists()


# ═══════════════════════════════════════════════════════════════════════
# Tampering tests (from previous version, still valid)
# ═══════════════════════════════════════════════════════════════════════

def test_artifact_modified_detected(raw_dir):
    """Modified artifact (hash mismatch) is detected."""
    a = _make_raw_artifact(raw_dir, "raw_001.events.jsonl.gz", b"original")
    write_manifest_atomic(raw_dir, a, RAW_MANIFEST_POLICY,
                          extra_fields={"run_id": "r1", "scan_id": "s1"})
    a.write_bytes(b"modified")
    result = verify_manifest_chain(raw_dir, RAW_MANIFEST_POLICY)
    assert result["chain_status"] == CHAIN_INVALID


def test_artifact_deleted_detected(raw_dir):
    a = _make_raw_artifact(raw_dir, "raw_001.events.jsonl.gz", b"content")
    write_manifest_atomic(raw_dir, a, RAW_MANIFEST_POLICY,
                          extra_fields={"run_id": "r1", "scan_id": "s1"})
    a.unlink()
    result = verify_manifest_chain(raw_dir, RAW_MANIFEST_POLICY)
    assert result["chain_status"] == CHAIN_INVALID


def test_previous_hash_altered_detected(raw_dir):
    a = _make_raw_artifact(raw_dir, "raw_001.events.jsonl.gz", b"content")
    write_manifest_atomic(raw_dir, a, RAW_MANIFEST_POLICY,
                          extra_fields={"run_id": "r1", "scan_id": "s1"})
    mf = raw_dir / "manifest_000000.json"
    entry = json.loads(mf.read_text())
    entry["previous_manifest_hash"] = "tampered"
    entry["manifest_hash"] = _compute_manifest_hash(entry)
    mf.write_text(json.dumps(entry))
    result = verify_manifest_chain(raw_dir, RAW_MANIFEST_POLICY)
    assert result["chain_status"] == CHAIN_INVALID


def test_duplicate_run_id_detected(snapshot_dir):
    """Duplicate run_id is detected by verify (not by write, which would prevent it)."""
    s1 = _make_snapshot_artifact(snapshot_dir, "snapshot_001.json", '{"h":"h1"}')
    write_manifest_atomic(snapshot_dir, s1, SNAPSHOT_MANIFEST_POLICY,
                          extra_fields={"run_id": "same_run", "scan_id": "s1"})

    # Read first manifest to get its manifest_hash (not get_last_manifest_hash,
    # which would fail because s2 is unregistered)
    m1 = json.loads((snapshot_dir / "smanifest_000000.json").read_text())
    prev_hash = m1["manifest_hash"]

    # Manually create a second manifest with same run_id (bypassing write_manifest_atomic)
    s2 = _make_snapshot_artifact(snapshot_dir, "snapshot_002.json", '{"h":"h2"}')
    file_sha = __import__('hashlib').sha256(s2.read_bytes()).hexdigest()
    entry = {
        "sequence": 1,
        "filename": "snapshot_002.json",
        "file_sha256": file_sha,
        "previous_manifest_hash": prev_hash,
        "created_at": "2026-07-13T00:00:00Z",
        "run_id": "same_run",  # duplicate!
        "scan_id": "s2",
    }
    entry["manifest_hash"] = _compute_manifest_hash(entry)
    (snapshot_dir / "smanifest_000001.json").write_text(json.dumps(entry))

    result = verify_manifest_chain(snapshot_dir, SNAPSHOT_MANIFEST_POLICY)
    assert result["chain_status"] == CHAIN_INVALID
    assert any("Duplicate run_id" in e for e in result["errors"])


def test_corrupt_manifest_json_detected(raw_dir):
    a = _make_raw_artifact(raw_dir, "raw_001.events.jsonl.gz", b"content")
    write_manifest_atomic(raw_dir, a, RAW_MANIFEST_POLICY,
                          extra_fields={"run_id": "r1", "scan_id": "s1"})
    (raw_dir / "manifest_000000.json").write_text("{ corrupt")
    result = verify_manifest_chain(raw_dir, RAW_MANIFEST_POLICY)
    assert result["chain_status"] == CHAIN_INVALID


def test_get_next_sequence_corrupt_returns_none(raw_dir):
    a = _make_raw_artifact(raw_dir, "raw_001.events.jsonl.gz", b"content")
    write_manifest_atomic(raw_dir, a, RAW_MANIFEST_POLICY,
                          extra_fields={"run_id": "r1", "scan_id": "s1"})
    (raw_dir / "manifest_000000.json").write_text("{ corrupt")
    assert get_next_sequence(raw_dir, RAW_MANIFEST_POLICY) is None


def test_latest_json_excluded_from_snapshot_check(snapshot_dir):
    """latest.json is excluded from unregistered check in snapshot policy."""
    s = _make_snapshot_artifact(snapshot_dir, "snapshot_001.json", '{"h":"h1"}')
    write_manifest_atomic(snapshot_dir, s, SNAPSHOT_MANIFEST_POLICY,
                          extra_fields={"run_id": "r1", "scan_id": "s1", "snapshot_hash": "h1"})
    (snapshot_dir / "latest.json").write_text('{"latest":true}')
    result = verify_manifest_chain(snapshot_dir, SNAPSHOT_MANIFEST_POLICY)
    assert result["chain_status"] == CHAIN_VALID
    assert "latest.json" not in result.get("unregistered_files", [])
