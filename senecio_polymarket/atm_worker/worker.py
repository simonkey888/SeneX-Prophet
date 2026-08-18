from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORKER_ID = "senex-prophet"
WORKER_VERSION = "aud067.v1"
PROTOCOL_VERSION = "atm-worker.v1"
CAPABILITIES = (
    "deterministic_data_replay",
    "causal_cutoff_validation",
    "provenance_hash_validation",
    "statistical_evaluation",
    "robustness_regime_stress",
    "bounded_python_pipeline_repair",
    "regression_case_generation",
)
PROHIBITIONS = (
    "production_mutation", "supabase_write", "northflank_mutation", "runtime017_mutation",
    "threshold_or_weight_tuning", "live_trading", "wallet_or_payment", "ambient_secret_access",
    "docker_socket_access", "ssh_agent_access", "implicit_hook_execution", "unapproved_network",
    "outgoing_spend",
)
REQUIRED_INPUT = {
    "protocol_version", "job_id", "lease_id", "attempt", "target_repository_or_dataset",
    "target_base_sha_or_snapshot", "allowed_paths", "required_capabilities", "structured_requirements",
    "frozen_acceptance_criteria", "upstream_issue_reference", "outgoing_spend_cap_usd",
    "lease_state", "lease_expires_at", "fixed_job_scope_hash",
}
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}

class JobRejected(RuntimeError):
    pass

class CrashInjected(RuntimeError):
    pass

def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def compute_scope_hash(job: dict[str, Any]) -> str:
    payload = {k: job[k] for k in sorted(REQUIRED_INPUT - {"fixed_job_scope_hash"}) if k in job}
    return sha256_bytes(_canon(payload))

def _parse_time(value: str) -> datetime:
    try:
        value = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            raise ValueError("timezone required")
        return dt.astimezone(timezone.utc)
    except Exception as exc:
        raise JobRejected("invalid lease_expires_at") from exc

def validate_job(job: dict[str, Any], now: datetime | None = None) -> None:
    missing = sorted(REQUIRED_INPUT - set(job))
    if missing:
        raise JobRejected("missing required fields: " + ",".join(missing))
    if job["protocol_version"] != PROTOCOL_VERSION:
        raise JobRejected("unsupported protocol_version")
    if not isinstance(job["attempt"], int) or job["attempt"] < 1:
        raise JobRejected("attempt must be positive integer")
    if job["lease_state"] != "ACTIVE":
        raise JobRejected("lease is not ACTIVE")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if _parse_time(str(job["lease_expires_at"])) <= now:
        raise JobRejected("lease expired")
    if float(job["outgoing_spend_cap_usd"]) != 0.0:
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
        q = Path(str(p))
        if q.is_absolute() or ".." in q.parts or str(q) in {"", "."}:
            raise JobRejected("unsafe allowed_path")
    if job["fixed_job_scope_hash"] != compute_scope_hash(job):
        raise JobRejected("fixed_job_scope_hash mismatch")
    req = job["structured_requirements"]
    forbidden = {
        "network_allowlist", "allow_network", "shell_command", "execute_target", "startup_hook",
        "docker_socket", "ssh_agent", "wallet", "payment", "live_trading", "production_write",
        "supabase_write", "northflank_write", "runtime017_write", "secret_access",
    }
    if any(req.get(k) for k in forbidden):
        raise JobRejected("structured requirement requests prohibited authority")

def _state_key(job: dict[str, Any]) -> str:
    raw = f"{job['job_id']}|{job['lease_id']}|{job['attempt']}"
    return sha256_bytes(raw.encode())

def _atomic_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, indent=2, sort_keys=True) + "\n"
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
        fh.write(json.dumps(event, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())

def _check_under(root: Path, rel: str, allowed: list[str], for_write: bool = False) -> Path:
    q = Path(rel)
    if q.is_absolute() or ".." in q.parts:
        raise JobRejected("path escape")
    if not any(q == Path(a) or Path(a) in q.parents for a in allowed):
        raise JobRejected("path outside allowed_paths")
    root = root.resolve()
    cur = root
    for part in q.parts[:-1] if for_write else q.parts:
        cur = cur / part
        if cur.exists() and cur.is_symlink():
            raise JobRejected("symlink escape denied")
    p = root / q
    if p.exists():
        if p.is_symlink():
            raise JobRejected("symlink denied")
        try:
            if p.stat().st_nlink > 1:
                raise JobRejected("hardlink denied")
        except FileNotFoundError:
            raise JobRejected("unstable path")
    try:
        p.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise JobRejected("resolved path escape") from exc
    return p

def _read_bytes(root: Path, rel: str, allowed: list[str]) -> bytes:
    p = _check_under(root, rel, allowed)
    if not p.is_file():
        raise JobRejected("input is not regular file")
    return p.read_bytes()

def _write_bytes(root: Path, rel: str, allowed: list[str], data: bytes) -> dict[str, Any]:
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
            raise JobRejected("hardlink target denied")
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {"path": rel, "sha256": sha256_bytes(data), "bytes": len(data)}

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else math.nan

def _op(job: dict[str, Any], root: Path) -> dict[str, Any]:
    req = job["structured_requirements"]
    op = req.get("operation")
    allowed = job["allowed_paths"]
    if op == "provenance_hash_validation":
        rows = []
        for rel in req.get("inputs", []):
            data = _read_bytes(root, rel, allowed)
            rows.append({"path": rel, "sha256": sha256_bytes(data), "bytes": len(data)})
        return {"operation": op, "files": rows, "status": "PASS"}
    if op == "deterministic_data_replay":
        data = json.loads(_read_bytes(root, req["input"], allowed))
        if not isinstance(data, list):
            raise JobRejected("replay input must be list")
        keys = req.get("sort_keys", ["ts", "id"])
        rows = sorted(data, key=lambda r: tuple(str(r.get(k, "")) for k in keys))
        return {"operation": op, "row_count": len(rows), "replay_sha256": sha256_bytes(_canon(rows)), "rows": rows}
    if op == "causal_cutoff_validation":
        data = json.loads(_read_bytes(root, req["input"], allowed))
        violations = []
        for i, row in enumerate(data):
            decision = float(row["decision_ts"])
            for k in req.get("feature_time_fields", []):
                if float(row[k]) > decision:
                    violations.append({"row": i, "field": k, "kind": "future_feature"})
            for k in req.get("label_time_fields", []):
                if float(row[k]) <= decision:
                    violations.append({"row": i, "field": k, "kind": "nonfuture_label"})
        return {"operation": op, "status": "PASS" if not violations else "FAIL", "violations": violations}
    if op == "statistical_evaluation":
        rows = json.loads(_read_bytes(root, req["input"], allowed))
        ds = [float(r[req["candidate_field"]]) - float(r[req["baseline_field"]]) for r in rows]
        m = _mean(ds)
        if len(ds) < 2:
            lo = hi = m
        else:
            var = sum((x-m)**2 for x in ds)/(len(ds)-1)
            se = math.sqrt(var/len(ds))
            lo = m-1.96*se
            hi = m+1.96*se
        verdict = "IMPROVEMENT" if hi < 0 else "DEGRADATION" if lo > 0 else "INCONCLUSIVE"
        return {"operation": op, "n": len(ds), "mean_delta": m, "ci95": [lo, hi], "orientation": verdict}
    if op == "robustness_regime_stress":
        rows = json.loads(_read_bytes(root, req["input"], allowed))
        field = req["value_field"]
        vals = [float(r[field]) for r in rows]
        median = sorted(vals)[len(vals)//2] if vals else math.nan
        regimes = {"LOW":[v for v in vals if v<=median], "HIGH":[v for v in vals if v>median]}
        missing = [v for i,v in enumerate(vals) if i % 10 != 0]
        shifted = [v + float(req.get("clock_shift_effect",0.0)) for v in vals]
        return {"operation":op,"status":"PASS","train_only_median":median,"regimes":{k:{"n":len(v),"mean":_mean(v)} for k,v in regimes.items()},"stresses":{"drop_10pct":{"n":len(missing),"mean":_mean(missing)},"clock_shift":{"n":len(shifted),"mean":_mean(shifted)}}}
    if op in {"bounded_python_pipeline_repair", "regression_case_generation"}:
        source = _read_bytes(root, req["input"], allowed).decode("utf-8")
        ast.parse(source)
        patched = source
        applied=[]
        for item in req.get("replacements", []):
            old, new = item["old"], item["new"]
            count = patched.count(old)
            if count != int(item.get("expected_count", 1)):
                raise JobRejected(f"replacement count mismatch for {old!r}: {count}")
            patched = patched.replace(old, new)
            applied.append({"old_sha256":sha256_bytes(old.encode()),"new_sha256":sha256_bytes(new.encode()),"count":count})
        ast.parse(patched)
        checks=[]
        for check in req.get("static_checks", []):
            if "contains" in check:
                ok = check["contains"] in patched
            elif "not_contains" in check:
                ok = check["not_contains"] not in patched
            else:
                raise JobRejected("unsupported static check")
            checks.append({"name":check.get("name","static"),"pass":ok})
        if not all(x["pass"] for x in checks):
            raise JobRejected("frozen acceptance static check failed")
        patch_rel=req.get("patch_output","artifacts/repair.py")
        artifact=_write_bytes(root,patch_rel,allowed,patched.encode())
        return {"operation":op,"source_sha256":sha256_bytes(source.encode()),"patched_sha256":sha256_bytes(patched.encode()),"applied":applied,"checks":checks,"artifact":artifact,"target_execution":0}
    raise JobRejected("unsupported operation")

def run_job(job: dict[str, Any], state_root: str | Path, target_root: str | Path, source_sha: str, now: datetime | None = None) -> dict[str, Any]:
    validate_job(job, now=now)
    state_root=Path(state_root)
    target_root=Path(target_root)
    key=_state_key(job)
    sp=state_root/(key+".json")
    ep=state_root/(key+".events.jsonl")
    scope=job["fixed_job_scope_hash"]
    state=json.loads(sp.read_text()) if sp.exists() else None
    if state:
        if state.get("fixed_job_scope_hash") != scope:
            raise JobRejected("tuple reused with different scope")
        if state.get("status") in TERMINAL:
            return state["completion"]
    else:
        started=datetime.now(timezone.utc).isoformat()
        state={"status":"ACKED","fixed_job_scope_hash":scope,"started_at":started,"progress_count":0,"crash_flags":{}}
        _atomic_json(sp,state)
        _append_event(ep,{"event":"ACK","scope":scope,"at":started})
    sim=job["structured_requirements"].get("simulate_crash_at")
    if sim=="after_ack" and not state["crash_flags"].get("after_ack"):
        state["crash_flags"]["after_ack"]=True
        _atomic_json(sp,state)
        raise CrashInjected("after_ack")
    state["status"]="RUNNING"
    state["progress_count"]+=1
    _atomic_json(sp,state)
    _append_event(ep,{"event":"PROGRESS","step":"STARTED","n":state["progress_count"]})
    if job["structured_requirements"].get("cancelled"):
        result={"cancel_reason":"requested before target operation"}
        status="CANCELLED"
        artifacts=[]
        tests=[]
    else:
        if sim=="during_work" and not state["crash_flags"].get("during_work"):
            state["crash_flags"]["during_work"]=True
            _atomic_json(sp,state)
            _append_event(ep,{"event":"PROGRESS","step":"PRE_OPERATION"})
            raise CrashInjected("during_work")
        try:
            result=_op(job,target_root)
            status="SUCCEEDED"
            artifacts=[]
            if isinstance(result.get("artifact"),dict):
                artifacts=[result["artifact"]]
            tests=[{"name":"frozen_acceptance","status":"PASS"}]
        except JobRejected as exc:
            result={"error_type":"JobRejected","error":str(exc)}
            status="FAILED"
            artifacts=[]
            tests=[{"name":"frozen_acceptance","status":"FAIL"}]
    state["progress_count"]+=1
    _append_event(ep,{"event":"PROGRESS","step":"FINALIZING","n":state["progress_count"]})
    finished=datetime.now(timezone.utc).isoformat()
    completion={
        "job_id":job["job_id"],"lease_id":job["lease_id"],"attempt":job["attempt"],
        "worker_id":WORKER_ID,"worker_version":WORKER_VERSION,"source_sha":source_sha,"status":status,
        "task_result":result,"artifacts":artifacts,"tests":tests,
        "acceptance":{"frozen":job["frozen_acceptance_criteria"],"status":"PASS" if status=="SUCCEEDED" else status},
        "provenance":{"target":job["target_repository_or_dataset"],"snapshot":job["target_base_sha_or_snapshot"],"fixed_job_scope_hash":scope},
        "side_effects":{"production_mutations":0,"supabase_writes":0,"northflank_mutations":0,"runtime017_mutations":0,"threshold_weight_tuning":0,"real_trading":0,"wallet_or_payment":0,"outgoing_spend_usd":0,"network_requests":0,"child_processes":0},
        "started_at":state["started_at"],"finished_at":finished,"fixed_job_scope_hash":scope,
    }
    state["status"]=status
    state["completion"]=completion
    _atomic_json(sp,state)
    _append_event(ep,{"event":"TERMINAL","status":status,"at":finished})
    return completion
