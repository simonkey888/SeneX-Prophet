import hashlib
import json
import os
import uuid

import polymarket.control_plane.state_snapshot as snapshots
import polymarket.h011_v3_pipeline as pipeline
import polymarket.h011_v3_raw_transaction as raw_tx
import control_plane.state_snapshot as runtime_snapshots
from polymarket.h011_v3_committed_snapshot import (
    load_committed_snapshot,
    replay_latest_committed,
)


class DataClient:
    def fetch_trades(self, condition_id, window_start, now):
        return [
            {"conditionId": condition_id, "asset": "token-up", "outcomeIndex": 0,
             "timestamp": now - 10, "price": 0.40, "size": 2},
            {"conditionId": condition_id, "asset": "token-down", "outcomeIndex": 1,
             "timestamp": now - 10, "price": 0.40, "size": 2},
        ]


class BookClient:
    def fetch_book(self, token_id):
        return {"asset_id": token_id, "asks": [{"price": 0.40, "size": 20}]}


def _portable_exchange(dir_fd: int, old_name: str, new_name: str) -> None:
    """Test-only exchange for filesystems without Linux RENAME_EXCHANGE."""
    temp = f".test-swap.{uuid.uuid4().hex}"
    os.rename(old_name, temp, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    os.rename(new_name, old_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    os.rename(temp, new_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def test_run_scan_commits_raw_chain_before_snapshot(tmp_path, monkeypatch):
    results_root = tmp_path / "results"
    results = results_root / "v3"
    chain = results_root / "h011_v3" / "raw_chain_v1"
    monkeypatch.setattr(pipeline, "RESULTS_ROOT", results_root)
    monkeypatch.setattr(pipeline, "V3_RESULTS_DIR", results)
    monkeypatch.setattr(pipeline, "V3_RAW_DIR", results / "raw")
    monkeypatch.setattr(pipeline, "V3_SCANS_DIR", results / "scans")
    monkeypatch.setattr(pipeline, "V3_REPLAY_DIR", results / "replay")
    monkeypatch.setattr(pipeline, "V3_MASTER_LOG", results / "_master_log_v3.jsonl")
    monkeypatch.setattr(pipeline, "RAW_CHAIN_DIR", chain)
    monkeypatch.setattr(snapshots, "SNAPSHOT_DIR", results / "state")
    monkeypatch.setattr(runtime_snapshots, "SNAPSHOT_DIR", results / "state")
    monkeypatch.setattr(pipeline.time, "sleep", lambda _: None)
    monkeypatch.setattr(raw_tx, "_renameat2_exchange", _portable_exchange)
    monkeypatch.setattr(pipeline.raw_tx, "_renameat2_exchange", _portable_exchange)
    deployment_sha = "43fc9f02e60167ae83d6b01d7f0615a4ae5e71b6"
    monkeypatch.setenv("NF_DEPLOYMENT_SHA", deployment_sha)
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("SENECIO_CODE_SHA", raising=False)

    slug_epoch = 1766162100
    market = {
        "conditionId": "0x" + "ab" * 32,
        "id": "market-1",
        "question": "Bitcoin Up or Down",
        "slug": f"btc-updown-5m-{slug_epoch}",
        "description": 'This market will resolve to "Up" if the Bitcoin price at the end '
                       'of the time range is greater than at the beginning.',
        "resolutionSource": "https://data.chain.link/streams/btc-usd",
        "startDate": "2025-12-18T16:35:00Z",
        "endDate": "2025-12-19T16:40:00Z",
        "eventStartTime": "2025-12-19T16:35:00Z",
        "outcomes": ["Up", "Down"],
        "clobTokenIds": ["token-up", "token-down"],
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "feesEnabled": False,
        "events": [{
            "id": "109968",
            "ticker": f"btc-updown-5m-{slug_epoch}",
            "slug": f"btc-updown-5m-{slug_epoch}",
            "title": "Bitcoin Up or Down",
        }],
    }
    result = pipeline.run_scan_v3(
        markets=[market], now_ts=1766162200, config=pipeline.H011V3Config(),
        data_api_client=DataClient(), clob_client=BookClient(),
    )

    summary = result["scan"]
    assert summary["transaction_status"] == "PUBLISHED"
    assert summary["current_sequence"] == 0
    assert summary["manifest_hash"]
    assert summary["file_sha256"]
    assert summary["snapshot_hash"]
    assert not (results / "raw").exists()
    assert not list(results_root.rglob("bundle_*.json"))
    assert not list(results_root.rglob("v3_scan_*.jsonl"))

    latest = snapshots.SNAPSHOT_DIR / "latest.json"
    state = json.loads(latest.read_text())
    assert state["code_sha"] == deployment_sha
    assert state["paper_only"] is True
    assert state["orders_enabled"] is False
    assert state["live_capital_locked"] is True
    assert state["invariants"]["summary"]["total"] == 31
    assert state["invariants"]["summary"]["fail"] == 0
    assert state["invariants"]["summary"]["unknown"] == 0
    exact_state_sha = hashlib.sha256(latest.read_bytes()).hexdigest()
    assert (snapshots.SNAPSHOT_DIR / "latest.json.sha256").read_text().strip() == exact_state_sha

    committed_snapshot, committed_chain = load_committed_snapshot(
        results_root=results_root, raw_directory=chain
    )
    assert committed_snapshot["snapshot_hash"] == state["snapshot_hash"]
    assert committed_chain.chain_verified is True
    assert committed_chain.latest["manifest_hash"] == summary["manifest_hash"]
    replay = replay_latest_committed(results_root=results_root, raw_directory=chain)
    assert replay["file_sha256_matches"] is True
    assert replay["chain_verified"] is True
    assert replay["replay_verified"] is True
    assert replay["transform_reexecuted"] is False
    assert replay["event_count"] >= 5
