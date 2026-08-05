from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest

import polymarket.h011_v3_raw_transaction as rt
from polymarket.h011_v3_committed_snapshot import (
    CommittedChainError,
    NoCommittedScan,
    load_committed_snapshot,
    replay_latest_committed,
    validate_committed_chain,
)


def _exchange(dir_fd: int, old_name: str, new_name: str) -> None:
    temp = f".swap.{uuid.uuid4().hex}"
    os.rename(old_name, temp, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    os.rename(new_name, old_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    os.rename(temp, new_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def _event(scan_id: str) -> dict:
    payload = {"scan_id": scan_id, "value": 1}
    return {
        "received_at_utc": "2026-07-21T00:00:00+00:00",
        "source": "test",
        "endpoint": "/test",
        "request_params": {},
        "requested_condition_id": "condition",
        "payload": payload,
        "payload_sha256": rt.canonical_payload_sha256(payload),
        "cohort_id": "h011-v3-w300-vwap-structure-v2",
        "schema_version": "h011-raw-envelope-v1",
    }


def _publish(raw: Path, run_id: str, scan_id: str) -> dict:
    with rt.RawScanStager(run_id, scan_id, raw) as stager:
        stager.append_event(_event(scan_id))
        stager.seal()
        transfer = stager.transfer()
        with rt.RawChainLock(raw, "manifest").acquire() as guard:
            return rt.publish_raw_scan(
                transfer=transfer,
                guard=guard,
                raw_directory=raw,
                policy=rt.DEFAULT_MARKER_POLICY,
                manifest_created_at="2026-07-21T00:00:00+00:00",
            ).manifest_entry


def _write_snapshot(results: Path, entry: dict) -> None:
    state = results / "v3" / "state"
    state.mkdir(parents=True)
    payload = {
        "generated_at": "2026-07-21T00:00:00+00:00",
        "snapshot_hash": "a" * 64,
        "aggregate_metrics": {
            "raw_chain": {
                "chain_verified": True,
                "entry_count": entry["sequence"] + 1,
                "current_sequence": entry["sequence"],
                "manifest_hash": entry["manifest_hash"],
                "previous_manifest_hash": entry["previous_manifest_hash"],
                "artifact_name": entry["filename"],
                "artifact_sha256": entry["file_sha256"],
                "canonical_events_sha256": entry["canonical_events_sha256"],
                "event_count": entry["event_count"],
                "scan_id": entry["scan_id"],
                "run_id": entry["run_id"],
            }
        },
    }
    raw = json.dumps(payload, sort_keys=True).encode()
    (state / "latest.json").write_bytes(raw)
    (state / "latest.json.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n")


@pytest.fixture(autouse=True)
def portable_exchange(monkeypatch):
    monkeypatch.setattr(rt, "_renameat2_exchange", _exchange)


def test_empty_chain_is_valid_but_has_no_committed_scan(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    chain = validate_committed_chain(raw)
    assert chain.chain_verified is True
    assert chain.latest is None
    with pytest.raises(NoCommittedScan):
        replay_latest_committed(results_root=tmp_path, raw_directory=raw)


def test_latest_committed_sequence_selected_over_legacy_files(tmp_path):
    raw = tmp_path / "h011_v3" / "raw_chain_v1"
    raw.mkdir(parents=True)
    _publish(raw, "r0", "s0")
    latest = _publish(raw, "r1", "s1")
    legacy = tmp_path / "v3" / "raw"
    legacy.mkdir(parents=True)
    (legacy / "bundle_9999.json").write_text("{}")
    _write_snapshot(tmp_path, latest)
    snapshot, chain = load_committed_snapshot(results_root=tmp_path, raw_directory=raw)
    assert chain.latest["sequence"] == 1
    assert snapshot["aggregate_metrics"]["raw_chain"]["scan_id"] == "s1"


@pytest.mark.parametrize("target", ["artifact", "sidecar", "manifest"])
def test_tampered_committed_component_is_rejected(tmp_path, target):
    raw = tmp_path / "raw"
    raw.mkdir()
    entry = _publish(raw, "r0", "s0")
    names = {
        "artifact": entry["filename"],
        "sidecar": entry["filename"] + ".sha256",
        "manifest": "manifest_000000.json",
    }
    path = raw / names[target]
    path.chmod(0o644)
    path.write_bytes(path.read_bytes() + b"x")
    path.chmod(0o444)
    with pytest.raises(CommittedChainError):
        validate_committed_chain(raw)


def test_wrong_permissions_are_rejected(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    entry = _publish(raw, "r0", "s0")
    (raw / entry["filename"]).chmod(0o644)
    with pytest.raises(CommittedChainError, match="0444"):
        validate_committed_chain(raw)


def test_stale_latest_cache_is_rejected(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    entry = _publish(raw, "r0", "s0")
    _write_snapshot(tmp_path, entry)
    state = tmp_path / "v3" / "state" / "latest.json"
    payload = json.loads(state.read_text())
    payload["aggregate_metrics"]["raw_chain"]["artifact_sha256"] = "0" * 64
    raw_bytes = json.dumps(payload, sort_keys=True).encode()
    state.write_bytes(raw_bytes)
    state.with_suffix(".json.sha256").write_text(hashlib.sha256(raw_bytes).hexdigest() + "\n")
    with pytest.raises(CommittedChainError, match="not bound"):
        load_committed_snapshot(results_root=tmp_path, raw_directory=raw)
