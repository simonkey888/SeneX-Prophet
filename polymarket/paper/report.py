"""Sanitized, hashed paper-trial artifact writer."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import canonical_json_bytes

REQUIRED_ARTIFACTS = (
    "trial_manifest.json",
    "config.json",
    "source_request_ledger.jsonl",
    "raw_evidence_manifest.json",
    "paper_decisions.jsonl",
    "paper_orders.jsonl",
    "paper_fills.jsonl",
    "portfolio_ledger.jsonl",
    "portfolio_snapshots.jsonl",
    "risk_decisions.jsonl",
    "abstentions.jsonl",
    "trial_summary.json",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class TrialArtifactWriter:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write_json(self, name: str, value: Any) -> Path:
        path = self.root / name
        path.write_bytes(canonical_json_bytes(value))
        return path

    def write_jsonl(self, name: str, records: Iterable[Mapping[str, Any]]) -> Path:
        path = self.root / name
        with path.open("wb") as handle:
            for record in records:
                handle.write(canonical_json_bytes(dict(record)))
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def finalize(self) -> dict[str, str]:
        missing = [name for name in REQUIRED_ARTIFACTS if not (self.root / name).is_file()]
        if missing:
            raise ValueError(f"missing required artifacts: {missing}")
        hashes = {name: sha256_file(self.root / name) for name in REQUIRED_ARTIFACTS}
        sidecar = self.root / "SHA256SUMS"
        sidecar.write_text("".join(f"{digest}  {name}\n" for name, digest in sorted(hashes.items())), encoding="utf-8")
        hashes["SHA256SUMS"] = sha256_file(sidecar)
        return hashes


def verify_artifact_bundle(root: Path) -> dict[str, str]:
    root = Path(root)
    lines = (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    observed: dict[str, str] = {}
    for line in lines:
        digest, name = line.split(None, 1)
        name = name.strip()
        actual = sha256_file(root / name)
        if digest != actual:
            raise ValueError(f"artifact digest mismatch for {name}: {digest} != {actual}")
        observed[name] = actual
    missing = [name for name in REQUIRED_ARTIFACTS if name not in observed]
    if missing:
        raise ValueError(f"SHA256SUMS missing required artifacts: {missing}")
    return observed
