from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

WORKER_ID = "senex-prophet"
WORKER_VERSION = "aud067.r1"
PROTOCOL_VERSION = "atm-worker.v1"
CAPABILITIES = (
    "deterministic_data_replay",
    "causal_cutoff_validation",
    "provenance_hash_validation",
    "statistical_evaluation",
    "robustness_regime_stress",
    "bounded_python_pipeline_repair",
)
PROHIBITIONS = (
    "production_mutation", "supabase_write", "northflank_mutation", "runtime017_mutation",
    "threshold_or_weight_tuning", "live_trading", "wallet_or_payment", "ambient_secret_access",
    "docker_socket_access", "ssh_agent_access", "implicit_hook_execution", "unapproved_network",
    "outgoing_spend", "external_acceptance_authority", "economic_truth_authority",
)
REQUIRED_INPUT = {
    "protocol_version", "job_id", "canonical_opportunity_id", "worker_id", "work_lease_id",
    "attempt", "scope_hash", "target_repository_or_dataset", "target_base_sha_or_snapshot",
    "target_snapshot_manifest", "allowed_paths", "required_capabilities", "structured_requirements",
    "frozen_acceptance_criteria", "expected_deliverable", "deterministic_checks",
    "data_provenance", "as_of", "cutoff", "max_spend_usd", "lease_state", "lease_expires_at",
    "job_deadline", "workspace_id",
}
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
MAX_FILE_BYTES = 1_048_576
MAX_TOTAL_BYTES = 4_194_304
MAX_JSON_ROWS = 10_000
MAX_JSON_DEPTH = 24
MAX_LIST_ITEMS = 20_000
MAX_DICT_ITEMS = 2_000
MAX_STRING_CHARS = 262_144


class JobRejected(RuntimeError):
    """Pre-ACK contract/authority/snapshot/isolation rejection."""


class WorkerInputError(RuntimeError):
    """Post-ACK bounded target/input failure; must become terminal FAILED."""


class CrashInjected(RuntimeError):
    pass


class DeadlineExceeded(WorkerInputError):
    pass


class CancellationRequested(WorkerInputError):
    pass


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


def compute_scope_hash(job: dict[str, Any]) -> str:
    payload = {k: job[k] for k in sorted(REQUIRED_INPUT - {"scope_hash"}) if k in job}
    return sha256_bytes(_canon(payload))


def _parse_time(value: str, field: str) -> datetime:
    try:
        value = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            raise ValueError("timezone required")
        return dt.astimezone(timezone.utc)
    except Exception as exc:
        raise JobRejected(f"invalid {field}") from exc


def _safe_rel(value: str) -> Path:
    q = Path(str(value))
    if q.is_absolute() or ".." in q.parts or str(q) in {"", "."}:
        raise JobRejected("unsafe relative path")
    return q


def _validate_structure(obj: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise WorkerInputError("JSON structure too deep")
    if isinstance(obj, dict):
        if len(obj) > MAX_DICT_ITEMS:
            raise WorkerInputError("JSON object too large")
        for k, v in obj.items():
            if not isinstance(k, str) or len(k) > 1024:
                raise WorkerInputError("invalid JSON key")
            _validate_structure(v, depth + 1)
    elif isinstance(obj, list):
        if len(obj) > MAX_LIST_ITEMS:
            raise WorkerInputError("JSON list too lare")
        for v in obj:
            _validate_structure(v, depth + 1)
    elif isinstance(obj, str) and len(obj) > MAX_STRING_CHARS:
        raise WorkerInputError("JSON string too lare")


def validate_job(job: dict[str, Any], now: datetime | None = None) -> None:
    missing = sorted(REQUIRED_INPUT - set(job))
    if missing:
        raise JobRejected("missing required fields: " + ",".join(missing))
    if job["protocol_version"] != PROTOCOL_VERSION:
        raise JobRejected("unsupported protocol_version")
    if job["worker_id"] != WORKER_ID:
        raise JobRejected("worker_id mismatch")
    for field in ("job_id", "canonical_opportunity_id", "work_lease_id", "workspace_id"):
        if not isinstance(job[field], str) or not job[field].strip():
            raise JobRejected(f"invalid {field}")
    if not isinstance(job["attempt"], int) or job["attempt"] < 1:
        raise JobRejected("attempt must be positive integer")
    if job["lease_state"] != "ACTIVE":
        raise JobRejected("lease is not ACTIVE")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if _parse_time(str(job["lease_expires_at"]), "lease_expires_at") <= now:
        raise JobRejected("lease expired")
    if _parse_time(str(job["job_deadline"]), "job_deadline") <= now:
        raise JobRejected("job deadline expired")
    if float(job["max_spend_usd"]) != 0.0:
        raise JobRejected("worker is zero-spend only")
    caps = tuple(job["required_capabilities"])
    unknown = sorted(set(caps) - set(CAPABILITIES))
    if unknown:
        raise JobRejected("unsupported capability: " + ",".join(unknown))
    if not caps:
        raise JobRejected("at least one capability required")
    if not isinstance(job["allowed_paths"], list) or not job["allowed_paths"]:
        raise JobRejected("allowed_paths must be non-empty")
    for p in job["allowed_paths"]:
        _safe_rel(str(p))
    if job["scope_hash"] != compute_scope_hash(job):
        raise JobRejected("scope_hash mismatch")
    if not isinstance(job["target_snapshot_manifest"], dict) or not job["target_snapshot_manifest"].get("files"):
        raise JobRejected("target_snapshot_manifest required")
    if not isinstance(job["data_provenance"], dict) or not job["data_provenance"].get("source"):
        raise JobRejected("malformed data provenance")
    req = job["structured_requirements"]
    if not isinstance(req, dict):
        raise JobRejected("structured_requirements must be object")
    forbidden = {
        "network_allowlist", "allow_network", "shell_command", "execute_target", "startup_hook",
        "docker_socket", "ssh_agent", "wallet", "payment", "live_trading", "production_write",
        "supabase_write", "northflank_write", "runtime017_write", "secret_access",
    }
    if any(req.get(k) for k in forbidden):
        raise JobRejected("structured requirement requests prohibited authority")
    temporal = bool({"causal_cutoff_validation", "statistical_evaluation"} & set(caps)) or bool(req.get("temporal"))
    if temporal and (not job["as_of"] or not job["cutoff"]):
        raise JobRejected("temporal jobs require explicit as_of and cutoff")
    if job["as_of"]:
        _parse_time(str(job["as_of"]), "as_of")
    if job["cutoff"]:
        _parse_time(str(job["cutoff"]), "cutoff")


def _state_key(job: dict[str, Any]) -> str:
    raw = f"{job['job_id']}|{job['canonical_opportunity_id']}|{job['work_lease_id']}|{job['attempt']}"
    return sha256_bytes(raw.encode())


def _atomic_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, indent=2, sort_keys=True, allow_nan=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True, allow_nan=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _validate_workspace_isolation(workspace_root: Path, target_root: Path, state_root: Path, canonical_senex_root: Path) -> None:
    roots = [workspace_root.resolve(), target_root.resolve(), state_root.resolve(), canonical_senex_root.resolve()]
    workspace, target, state, canonical = roots
    if len({str(target), str(state), str(canonical)}) != 3:
        raise JobRejected("workspace roots must be distinct")
    for candidate in (target, state):
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise JobRejected("target/state must be inside isolated workspace") from exc
        try:
            candidate.relative_to(canonical)
            raise JobRejected("job workspace overlaps canonical SENEX checkout")
        except ValueError:
            pass
    lowered = str(workspace).lower()
    if any(token in lowered for token in ("runtime017", "runtime_017", "/app/polymarket/results", "northflank", "supabase")):
        raise JobRejected("forbidden production/runtime workspace")


def _check_under(root: Path, rel: str, allowed: list[str], for_write: bool = False) -> Path:
    q = _safe_rel(rel)
    if not any(q == Path(a) or Path(a) in q.parents for a in allowed):
        raise WorkerInputError("path outside allowed_paths")
    root = root.resolve()
    cur = root
    for part in q.parts[:-1] if for_write else q.parts:
        cur = cur / part
        if cur.exists() and cur.is_symlink():
            raise WorkerInputError("symlink escape denied")
    p = root / q
    if p.exists():
        if p.is_symlink():
            raise WorkerInputError("symlink denied")
        try:
            if p.stat().st_nlink > 1:
                raise WorkerInputError("hardlink denied")
        except FileNotFoundError as exc:
            raise WorkerInputError("unstable path") from exc
    try:
        p.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise WorkerInputError("resolved path escape") from exc
    return p


@dataclass
class RuntimeControl:
    deadline: datetime
    clock: Callable[[], datetime]
    cancel_check: Callable[[], bool]
    checkpoints: int = 0

    def check(self, where: str) -> None:
        self.checkpoints += 1
        if self.cancel_check():
            raise CancellationRequested(f"cancelled at {where}")
        if self.clock().astimezone(timezone.utc) >= self.deadline:
            raise DeadlineExceeded(f"deadline exceeded at {where}")


def _read_bytes(root: Path, rel: str, allowed: list[str], ctl: RuntimeControl, budget: dict[str, int]) -> bytes:
    ctl.check("before_read")
    p = _check_under(root, rel, allowed)
    if not p.is_file():
        raise WorkerInputError("input is not regular file")
    size = p.stat().st_size
    if size > MAX_FILE_BYTES:
        raise WorkerInputError("input exceeds per-file byte limit")
    if budget["bytes"] + size > MAX_TOTAL_BYTES:
        raise WorkerInputError("input exceeds aggregate byte limit")
    with p.open("rb") as fh:
        data = fh.read(MAX_FILE_BYTES + 1)
    if len(data) != size or len(data) > MAX_FILE_BYTES:
        raise WorkerInputError("input changed or exceeded read bound")
    budget["bytes"] += len(data)
    ctl.check("after_read")
    return data


def _load_json(root: Path, rel: str, allowed: list[str], ctl: RuntimeControl, budget: dict[str, int]) -> Any:
    raw = _read_bytes(root, rel, allowed, ctl, budget)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise WorkerInputError("invalid UTF-8") from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkerInputError("malformed JSON") from exc
    _validate_structure(obj)
    if isinstance(obj, list) and len(obj) > MAX_JSON_ROWS:
        raise WorkerInputError("JSON row limit exceeded")
    return obj


def _write_bytes(root: Path, rel: str, allowed: list[str], data: bytes, ctl: RuntimeControl) -> dict[str, Any]:
    ctl.check("before_write")
    if len(data) > MAX_FILE_BYTES:
        raise WorkerInputError("artifact exceeds byte limit")
    p = _check_under(root, rel, allowed, for_write=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    _check_under(root, rel, allowed, for_write=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if p.exists() and p.stat().st_nlink > 1:
            raise WorkerInputError("hardlink target denied")
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    ctl.check("after_write")
    return {"path": rel, "sha256": sha256_bytes(data), "git_blob_sha": git_blob_sha(data), "bytes": len(data)}


def _verify_target_snapshot(job: dict[str, Any], root: Path, ctl: RuntimeControl) -> dict[str, Any]:
    manifest = job["target_snapshot_manifest"]
    kind = manifest.get("kind")
    if kind not in {"git_repo_subset", "dataset"}:
        raise JobRejected("unsupported snapshot manifest kind")
    if kind == "git_repo_subset" and manifest.get("base_commit_sha") != job["target_base_sha_or_snapshot"]:
        raise JobRejected("repo base SHA mismatch")
    if kind == "dataset" and manifest.get("snapshot_id") != job["target_base_sha_or_snapshot"]:
        raise JobRejected("dataset snapshot id mismatch")
    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > 256:
        raise JobRejected("invalid snapshot file manifest")
    verified = []
    total = 0
    seen: set[str] = set()
    for entry in files:
        ctl.check("snapshot_verify")
        if not isinstance(x”^:r´≤⁄Óù∆≠y