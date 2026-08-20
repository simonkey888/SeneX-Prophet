"""ORDER-070 exact runtime/build provenance contract."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")


def _env_first(*names: str) -> str | None:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def canonical_build_digest(root: Path | None = None) -> str:
    root = root or Path(__file__).resolve().parents[2]
    files = [
        root / "senecio_polymarket" / "Dockerfile",
        root / "senecio_polymarket" / "requirements.txt",
        root / "senecio_polymarket" / "requirements.lock",
        root / "senecio_polymarket" / "start_single_authority.sh",
    ]
    h = hashlib.sha256()
    for path in files:
        h.update(path.relative_to(root).as_posix().encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return "sha256:" + h.hexdigest()


def runtime_provenance() -> dict[str, Any]:
    source_commit = _env_first("SENEX_SOURCE_COMMIT", "NORTHFLANK_GIT_COMMIT_SHA", "GITHUB_SHA")
    source_tree = _env_first("SENEX_SOURCE_TREE")
    image_digest = _env_first("SENEX_IMAGE_DIGEST", "NORTHFLANK_IMAGE_DIGEST")
    declared_build_digest = _env_first("SENEX_BUILD_DIGEST")
    try:
        computed_build_digest = canonical_build_digest()
    except Exception:
        computed_build_digest = None
    build_digest = declared_build_digest or computed_build_digest
    checks = {
        "commit_exact": bool(source_commit and _SHA40.fullmatch(source_commit.lower())),
        "tree_exact": bool(source_tree and _SHA40.fullmatch(source_tree.lower())),
        "image_digest_exact": bool(image_digest and _SHA256.fullmatch(image_digest.lower())),
        "build_digest_exact": bool(build_digest and _SHA256.fullmatch(build_digest.lower())),
        "build_digest_matches_runtime_files": (
            declared_build_digest.lower() == computed_build_digest.lower()
            if declared_build_digest and computed_build_digest
            else bool(declared_build_digest and _SHA256.fullmatch(declared_build_digest.lower()))
        ),
    }
    return {
        "contract": "senex-runtime-provenance-v1",
        "source_commit": source_commit,
        "source_tree": source_tree,
        "image_digest": image_digest,
        "build_digest": build_digest,
        "computed_build_digest": computed_build_digest,
        "checks": checks,
        "exact": all(checks.values()),
    }


def provenance_fingerprint(payload: dict[str, Any] | None = None) -> str:
    payload = payload or runtime_provenance()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()
