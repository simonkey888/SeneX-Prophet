"""
SENECIO — Immutable raw event store and deterministic H-011 replay.

FASE A.4 redesign: One immutable artifact per scan (not per day).
Each scan produces a single raw_scan_<scan_id>.events.jsonl.gz file that
is written to a staging area, then atomically renamed to its final
immutable name, with a mandatory sidecar SHA256 and manifest entry.

The old daily-append model (YYYY-MM-DD.events.jsonl.gz) is DEPRECATED
for H-011 V3. It remains available for legacy V2 but V3 never uses it.

Storage path (V3): polymarket/results/v3/raw/raw_scan_<safe_scan_id>.events.jsonl.gz

Each record schema:
  {
    "received_at_utc": "ISO-8601",
    "source": "polymarket_data_api",
    "endpoint": "/trades",
    "request_params": {},
    "requested_condition_id": "str",
    "payload": {},
    "payload_sha256": "str",
    "cohort_id": "str",
    "schema_version": "raw_trade_event_v1"
  }
"""
from __future__ import annotations

import gzip
import json
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validation_semantics import classify_window_cohort

RAW_DIR = Path(__file__).parent / "results" / "raw"


# ═══════════════════════════════════════════════════════════════════════
# Legacy daily-append model (V2 only, do not use in V3)
# ═══════════════════════════════════════════════════════════════════════

def append_raw_event(
    path: Path,
    event: dict[str, Any],
) -> None:
    """Append a raw event to a gzipped JSONL file (atomic line write).

    DEPRECATED for V3 — use RawScanStager instead.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "at", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )


def create_raw_event(
    condition_id: str,
    payload: list[dict] | dict,
    request_params: dict | None = None,
    window_s: int = 300,
    endpoint: str = "/trades",
) -> dict[str, Any]:
    """Create a raw event record from an API response."""
    payload_str = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    payload_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

    return {
        "received_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "polymarket_data_api",
        "endpoint": endpoint,
        "request_params": request_params or {},
        "requested_condition_id": condition_id,
        "payload": payload,
        "payload_sha256": payload_hash,
        "cohort_id": classify_window_cohort(window_s),
        "schema_version": "raw_trade_event_v1",
    }


def save_raw_events(
    condition_id: str,
    trades: list[dict],
    request_params: dict | None = None,
    window_s: int = 300,
) -> Path:
    """Save raw trades for a market to the daily gzip file.

    DEPRECATED for V3 — use RawScanStager instead.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = RAW_DIR / f"{date_str}.events.jsonl.gz"

    event = create_raw_event(
        condition_id=condition_id,
        payload=trades,
        request_params=request_params,
        window_s=window_s,
    )
    append_raw_event(path, event)
    return path


# ═══════════════════════════════════════════════════════════════════════
# V3: Immutable per-scan raw storage
# ═══════════════════════════════════════════════════════════════════════

import uuid as _uuid
from dataclasses import dataclass


def _safe_scan_id(scan_id: str) -> str:
    """Convert scan_id to a filesystem-safe string."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in scan_id)
    return safe[:100]  # cap length


@dataclass(frozen=True)
class SealedRawArtifact:
    """B2: Sealed descriptor returned by RawScanStager.seal().

    The stager does NOT publish anything. This descriptor contains
    all metadata needed for the publisher to create the final artifact
    + sidecar + manifest in a single transaction.
    """
    staging_path: Path
    final_name: str
    run_id: str
    scan_id: str
    event_count: int
    condition_ids: tuple[str, ...]
    file_sha256: str
    canonical_events_sha256: str

    def to_manifest_fields(self) -> dict[str, Any]:
        """Return fields to include in the manifest entry."""
        return {
            "run_id": self.run_id,
            "scan_id": self.scan_id,
            "event_count": self.event_count,
            "condition_ids": list(self.condition_ids),
            "canonical_events_sha256": self.canonical_events_sha256,
        }


class RawScanStager:
    """B2/B3: Stages raw events for a single scan in an exclusive temporary file.

    Events are written to a staging file in .pending/ (outside artifact glob).
    The staging file is created with O_CREAT | O_EXCL and a UUID to ensure
    uniqueness. seal() returns a SealedRawArtifact descriptor — it does NOT
    publish the final artifact.

    The final artifact is published by publish_staged_artifact_with_manifest()
    under the manifest lock, ensuring atomicity.
    """

    def __init__(self, run_id: str, scan_id: str, raw_dir: Path):
        self.run_id = run_id
        self.scan_id = scan_id
        self.raw_dir = raw_dir
        self._staging_path: Path | None = None
        self._events: list[dict[str, Any]] = []
        self._condition_ids: set[str] = set()
        self._sealed = False
        self._staging_fd: int | None = None

    def __enter__(self):
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        staging_dir = self.raw_dir / ".pending"
        staging_dir.mkdir(exist_ok=True)

        # B3: Unique staging file with UUID + O_CREAT | O_EXCL
        safe_id = _safe_scan_id(self.scan_id)
        unique_suffix = _uuid.uuid4().hex[:12]
        staging_name = f"raw_scan_{safe_id}_{unique_suffix}.jsonl.gz.tmp"
        self._staging_path = staging_dir / staging_name

        # Create with O_CREAT | O_EXCL to prevent collision
        fd = os.open(str(self._staging_path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
        os.close(fd)  # We'll reopen with gzip

        return self

    def append_event(self, event: dict[str, Any]) -> None:
        """Append a raw event to the staging file.

        B1: Writes immediately to disk (flush + fsync) to preserve
        INV-025: raw persisted before transform.
        """
        if self._sealed:
            raise RuntimeError("Cannot append to sealed stager")
        if self._staging_path is None:
            raise RuntimeError("Stager not initialized — use 'with' statement")

        with gzip.open(self._staging_path, "at", encoding="utf-8") as handle:
            handle.write(
                json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())

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

    def canonical_events_sha256(self) -> str:
        """Compute SHA256 of the canonical events representation."""
        canonical = json.dumps(
            self._events,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def seal(self) -> SealedRawArtifact:
        """B2: Seal the stager and return a descriptor.

        Does NOT publish the final artifact. The caller must use
        publish_staged_artifact_with_manifest() to atomically publish
        the artifact + sidecar + manifest.

        B9: Re-reads the staging file from disk and recalculates all
        metadata from the actual file content, not from in-memory state.
        """
        if self._sealed:
            raise RuntimeError("Already sealed")
        if self._staging_path is None or not self._staging_path.exists():
            raise RuntimeError("Staging file does not exist")

        # B9: Re-read the gzip from disk and verify content
        disk_events = load_raw_events(self._staging_path)
        if len(disk_events) != len(self._events):
            raise RuntimeError(
                f"Disk event count ({len(disk_events)}) != memory event count ({len(self._events)})"
            )

        # Recalculate metadata from disk content
        disk_condition_ids = set()
        for ev in disk_events:
            cid = ev.get("requested_condition_id", "")
            if cid:
                disk_condition_ids.add(cid)

        # Canonical hash from disk events
        canonical = json.dumps(
            disk_events,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        disk_canonical_sha = hashlib.sha256(canonical).hexdigest()

        # File SHA256 from disk
        file_sha = hashlib.sha256(self._staging_path.read_bytes()).hexdigest()

        # Build final name with scan_id hash for collision resistance
        safe_id = _safe_scan_id(self.scan_id)
        scan_id_hash = hashlib.sha256(self.scan_id.encode()).hexdigest()[:12]
        final_name = f"raw_scan_{safe_id}_{scan_id_hash}.events.jsonl.gz"

        descriptor = SealedRawArtifact(
            staging_path=self._staging_path,
            final_name=final_name,
            run_id=self.run_id,
            scan_id=self.scan_id,
            event_count=len(disk_events),
            condition_ids=tuple(sorted(disk_condition_ids)),
            file_sha256=file_sha,
            canonical_events_sha256=disk_canonical_sha,
        )

        self._sealed = True
        return descriptor

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None and self._staging_path and self._staging_path.exists() and not self._sealed:
            # Clean up staging on error (only if not sealed)
            try:
                self._staging_path.unlink()
            except OSError:
                pass
        return False


def load_raw_events(path: Path) -> list[dict]:
    """Load all raw events from a gzipped JSONL file."""
    events = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def replay_file(
    path: Path,
    window_s: int = 300,
) -> dict:
    """Deterministic replay of raw events."""
    events = load_raw_events(path)
    events.sort(key=lambda e: e.get("received_at_utc", ""))

    total_trades = 0
    markets_processed = 0
    market_summaries = []

    for event in events:
        payload = event.get("payload", [])
        if isinstance(payload, list):
            total_trades += len(payload)
            markets_processed += 1
            cid = event.get("requested_condition_id", "")
            market_summaries.append({
                "condition_id": cid,
                "trade_count": len(payload),
                "payload_sha256": event.get("payload_sha256", ""),
            })

    canonical = json.dumps(
        {
            "total_events": len(events),
            "total_trades": total_trades,
            "markets_processed": markets_processed,
            "market_summaries": sorted(market_summaries, key=lambda m: m["condition_id"]),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    output_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return {
        "total_events": len(events),
        "total_trades": total_trades,
        "markets_processed": markets_processed,
        "market_summaries": sorted(market_summaries, key=lambda m: m["condition_id"]),
        "output_sha256": output_hash,
    }
