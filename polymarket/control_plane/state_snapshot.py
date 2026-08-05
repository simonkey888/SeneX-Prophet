"""
SENECIO H-011 V3 — Immutable scan state snapshot.

Single source of truth consumed by dashboard, API, replay, and monitor.
Historical snapshots are append-only. latest.json can be replaced atomically.
"""
from __future__ import annotations

import hashlib
import os
import time
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ScanStateSnapshot:
    """Immutable snapshot of a complete scan state."""
    schema_version: str
    scan_id: str
    run_id: str
    generated_at: str
    pipeline_version: str
    cohort_id: str
    window_s: int
    estimator: str
    code_sha: str
    config_sha: str
    paper_only: bool
    live_capital_locked: bool
    orders_enabled: bool
    scan_status: str
    source_health: dict
    funnel: dict
    market_records: tuple[dict, ...]
    lifecycle: dict
    invariants: dict
    drift: dict
    alerts: tuple[dict, ...]
    aggregate_metrics: dict
    semantic_hash: str
    canonical_content_hash: str
    snapshot_hash: str

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "scan_id": self.scan_id,
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "pipeline_version": self.pipeline_version,
            "cohort_id": self.cohort_id,
            "window_s": self.window_s,
            "estimator": self.estimator,
            "code_sha": self.code_sha,
            "config_sha": self.config_sha,
            "paper_only": self.paper_only,
            "live_capital_locked": self.live_capital_locked,
            "orders_enabled": self.orders_enabled,
            "scan_status": self.scan_status,
            "source_health": self.source_health,
            "funnel": self.funnel,
            "market_records": list(self.market_records),
            "lifecycle": self.lifecycle,
            "invariants": self.invariants,
            "drift": self.drift,
            "alerts": list(self.alerts),
            "aggregate_metrics": self.aggregate_metrics,
            "semantic_hash": self.semantic_hash,
            "canonical_content_hash": self.canonical_content_hash,
            "snapshot_hash": self.snapshot_hash,
        }


def build_snapshot(
    *,
    scan_id: str,
    run_id: str,
    pipeline_version: str,
    cohort_id: str,
    window_s: int,
    estimator: str,
    code_sha: str,
    config_sha: str,
    scan_status: str,
    source_health: dict,
    funnel: dict,
    market_records: list[dict],
    lifecycle: dict | None = None,
    invariants: dict | None = None,
    drift: dict | None = None,
    alerts: list[dict] | None = None,
    aggregate_metrics: dict | None = None,
) -> ScanStateSnapshot:
    """Build a snapshot with a reproducible semantic hash."""
    generated_at = datetime.now(timezone.utc).isoformat()

    semantic_records = []
    for record in market_records:
        normalized = json.loads(json.dumps(record))
        normalized.pop("record_hash", None)
        normalized.pop("scan_id", None)
        normalized.pop("run_id", None)
        semantic_records.append(normalized)

    semantic_material = {
        "schema_version": "h011-v3-snapshot-v1",
        "pipeline_version": pipeline_version,
        "cohort_id": cohort_id,
        "window_s": window_s,
        "estimator": estimator,
        "code_sha": code_sha,
        "config_sha": config_sha,
        "paper_only": True,
        "live_capital_locked": True,
        "orders_enabled": False,
        "scan_status": scan_status,
        "source_health": source_health,
        "funnel": funnel,
        "market_records": semantic_records,
        "lifecycle": lifecycle or {},
        "invariants": invariants or {},
        "drift": drift or {},
        "alerts": alerts or [],
        "aggregate_metrics": aggregate_metrics or {},
    }
    hash_input = json.dumps(semantic_material, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    semantic_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

    return ScanStateSnapshot(
        schema_version="h011-v3-snapshot-v1",
        scan_id=scan_id,
        run_id=run_id,
        generated_at=generated_at,
        pipeline_version=pipeline_version,
        cohort_id=cohort_id,
        window_s=window_s,
        estimator=estimator,
        code_sha=code_sha,
        config_sha=config_sha,
        paper_only=True,
        live_capital_locked=True,
        orders_enabled=False,
        scan_status=scan_status,
        source_health=source_health,
        funnel=funnel,
        market_records=tuple(market_records),
        lifecycle=lifecycle or {},
        invariants=invariants or {},
        drift=drift or {},
        alerts=tuple(alerts or []),
        aggregate_metrics=aggregate_metrics or {},
        semantic_hash=semantic_hash,
        canonical_content_hash="",
        snapshot_hash=semantic_hash,
    )


SNAPSHOT_DIR = Path(__file__).parent.parent / "results" / "v3" / "state"


def _atomic_write(path: Path, payload: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}"
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    try:
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            written = os.write(fd, view[offset:])
            if written <= 0:
                raise OSError("short write while publishing snapshot cache")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def save_snapshot(snapshot: ScanStateSnapshot) -> Path:
    """Publish append-only history and an atomically replaced derived cache."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    payload = snapshot.to_dict()
    payload["snapshot_hash"] = snapshot.semantic_hash
    payload["canonical_content_hash"] = ""
    canonical_content_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    payload["canonical_content_hash"] = canonical_content_hash
    payload["snapshot_hash"] = snapshot.semantic_hash
    output = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")

    hist_path = SNAPSHOT_DIR / f"state_{snapshot.generated_at.replace(':', '')}_{snapshot.scan_id[:8]}.json"
    if hist_path.exists():
        raise FileExistsError(f"historical snapshot already exists: {hist_path}")
    _atomic_write(hist_path, output)
    file_sha = hashlib.sha256(output).hexdigest()
    _atomic_write(hist_path.with_suffix(hist_path.suffix + ".sha256"), (file_sha + "\n").encode("ascii"))

    latest_path = SNAPSHOT_DIR / "latest.json"
    _atomic_write(latest_path, output)
    latest_sha = hashlib.sha256(output).hexdigest()
    _atomic_write(SNAPSHOT_DIR / "latest.json.sha256", (latest_sha + "\n").encode("ascii"))
    return hist_path


def load_latest_snapshot() -> dict | None:
    """Load latest.json or return None if not found."""
    latest = SNAPSHOT_DIR / "latest.json"
    if not latest.exists():
        return None
    return json.loads(latest.read_text(encoding="utf-8"))
