from __future__ import annotations

import hashlib, json, math, os, re, tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORKER_ID="senex-prophet"
WORKER_VERSION="aud067.r2"
PROTOCOL_VERSION="atm-worker.v1"
ACCEPTANCE_CRITERIA_VERSION="atm-acceptance.v1"
CAPABILITIES=("deterministic_data_replay","causal_cutoff_validation","provenance_hash_validation","statistical_evaluation","robustness_regime_stress","bounded_python_pipeline_repair")
PROHIBITIONS=("production_mutation","supabase_write","northflank_mutation","runtime017_mutation","threshold_or_weight_tuning","live_trading","wallet_or_payment","ambient_secret_access","docker_socket_access","ssh_agent_access","implicit_hook_execution","unapproved_network","outgoing_spend","external_acceptance_authority","economic_truth_authority")
MAX_FILE_BYTES=1_048_576; MAX_TOTAL_BYTES=4_194_304; MAX_JSON_ROWS=10_000; MAX_DEPTH=24; MAX_LIST=20_000; MAX_DICT=2_000; MAX_STRING=262_144
MAX_ROBUSTNESS_REGIMES=8; MAX_ROBUSTNESS_PERTURBATIONS=8
TERMINAL={"SUCCEEDED","FAILED","CANCELLED"}
REQUIRED={"protocol_version","job_id","canonical_opportunity_id","worker_id","work_lease_id","attempt","scope_hash","target_repository_or_dataset","target_base_sha_or_snapshot","target_snapshot_manifest","allowed_paths","required_capabilities","structured_requirements","frozen_acceptance_criteria","expected_deliverable","deterministic_checks","data_provenance","as_of","cutoff","max_spend_usd","lease_state","lease_expires_at","job_deadline","workspace_id"}

class JobRejected(RuntimeError): pass
class WorkerInputError(RuntimeError): pass
class DeadlineExceeded(WorkerInputError): pass
class CancellationRequested(WorkerInputError): pass
class CrashInjected(RuntimeError): pass

def canon(x:Any)->bytes: return json.dumps(x,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()
def sha256_bytes(b:bytes)->str: return hashlib.sha256(b).hexdigest()
def git_blob_sha(b:bytes)->str: return hashlib.sha1(f"blob {len(b)}\0".encode()+b).hexdigest()
def compute_scope_hash(job:dict[str,Any])->str: return sha256_bytes(canon({k:job[k] for k in sorted(REQUIRED-{"scope_hash"}) if k in job}))

def validate_source_sha(source_sha:Any)->str:
    s=str(source_sha)
    if not re.fullmatch(r"[0-9a-f]{40}",s): raise JobRejected("invalid exact worker source_sha")
    return s

def parse_time(v:Any,field:str)->datetime:
    try:
        s=str(v); s=s[:-1]+"+00:00" if s.endswith("Z") else s; d=datetime.fromisoformat(s)
        if d.tzinfo is None: raise ValueError
        return d.astimezone(timezone.utc)
    except Exception as e: raise JobRejected(f"invalid {field}") from e

def safe_rel(v:Any)->Path:
    q=Path(str(v))
    if q.is_absolute() or ".." in q.parts or str(q) in {"","."}: raise JobRejected("unsafe relative path")
    return q

def validate_acceptance_criteria(criteria:Any)->None:
    if not isinstance(criteria,list) or not criteria or len(criteria)>32: raise JobRejected("frozen_acceptance_criteria must be non-empty bounded list")
    for c in criteria:
        if not isinstance(c,dict): raise JobRejected("malformed acceptance criterion")
        typ=c.get("type"); version=c.get("schema_version")
        if version!=ACCEPTANCE_CRITERIA_VERSION: raise JobRejected("unsupported acceptance criterion schema")
        if typ=="zero_spend": allowed={"schema_version","type"}
        elif typ=="static_check":
            allowed={"schema_version","type","name"}
            if not isinstance(c.get("name"),str) or not c["name"].strip(): raise JobRejected("malformed static_check criterion")
        else: raise JobRejected("unsupported acceptance criterion type")
        if set(c)!=allowed: raise JobRejected("malformed acceptance criterion fields")

def validate_job(job:dict[str,Any],now:datetime)->None:
    missing=sorted(REQUIRED-set(job))
    if missing: raise JobRejected("missing required fields: "+",".join(missing))
    if job["protocol_version"]!=PROTOCOL_VERSION: raise JobRejected("unsupported protocol_version")
    if job["worker_id"]!=WORKER_ID: raise JobRejected("worker_id mismatch")
    for f in ("job_id","canonical_opportunity_id","work_lease_id","workspace_id"):
        if not isinstance(job[f],str) or not job[f].strip(): raise JobRejected("invalid "+f)
    if not isinstance(job["attempt"],int) or job["attempt"]<1: raise JobRejected("invalid attempt")
    if job["lease_state"]!="ACTIVE": raise JobRejected("lease is not ACTIVE")
    if parse_time(job["lease_expires_at"],"lease_expires_at")<=now: raise JobRejected("lease expired")
    if parse_time(job["job_deadline"],"job_deadline")<=now: raise JobRejected("job deadline expired")
    if float(job["max_spend_usd"])!=0: raise JobRejected("worker is zero-spend only")
    caps=tuple(job["required_capabilities"])
    if not caps: raise JobRejected("capability required")
    bad=sorted(set(caps)-set(CAPABILITIES))
    if bad: raise JobRejected("unsupported capability: "+",".join(bad))
    if not isinstance(job["allowed_paths"],list) or not job["allowed_paths"]: raise JobRejected("allowed_paths required")
    for p in job["allowed_paths"]: safe_rel(p)
    if job["scope_hash"]!=compute_scope_hash(job): raise JobRejected("scope_hash mismatch")
    if not isinstance(job["target_snapshot_manifest"],dict) or not job["target_snapshot_manifest"].get("files"): raise JobRejected("target_snapshot_manifest required")
    prov=job["data_provenance"]
    if not isinstance(prov,dict) or not prov.get("source"): raise JobRejected("malformed data provenance")
    validate_acceptance_criteria(job["frozen_acceptance_criteria"])
    req=job["structured_requirements"]
    if not isinstance(req,dict): raise JobRejected("structured_requirements must be object")
    forbidden={"network_allowlist","allow_network","shell_command","execute_target","startup_hook","docker_socket","ssh_agent","wallet","payment","live_trading","production_write","supabase_write","northflank_write","runtime017_write","secret_access"}
    if any(req.get(k) for k in forbidden): raise JobRejected("structured requirement requests prohibited authority")
    temporal=bool({"causal_cutoff_validation","statistical_evaluation"}&set(caps)) or bool(req.get("temporal"))
    if temporal and (not job["as_of"] or not job["cutoff"]): raise JobRejected("temporal jobs require explicit as_of and cutoff")
    if job["as_of"]: parse_time(job["as_of"],"as_of")
    if job["cutoff"]: parse_time(job["cutoff"],"cutoff")

def check_workspace(workspace:Path,target:Path,state:Path,canonical:Path)->None:
    ws=workspace.resolve(); t=target.resolve(); s=state.resolve(); c=canonical.resolve()
    if len({str(t),str(s),str(c)})!=3: raise JobRejected("workspace roots must be distinct")
    for x in (t,s):
        try: x.relative_to(ws)
        except ValueError as e: raise JobRejected("target/state must be inside isolated workspace") from e
        try: x.relative_to(c); raise JobRejected("job workspace overlaps canonical SENEX checkout")
        except ValueError: pass
    low=str(ws).lower()
    if any(z in low for z in ("runtime017","runtime_017","/app/polymarket/results","northflank","supabase")): raise JobRejected("forbidden production/runtime workspace")

def check_path(root:Path,rel:str,allowed:list[str],write=False)->Path:
    q=safe_rel(rel)
    if not any(q==Path(a) or Path(a) in q.parents for a in allowed): raise WorkerInputError("path outside allowed_paths")
    base=root.resolve(); cur=base
    for part in (q.parts[:-1] if write else q.parts):
        cur=cur/part
        if cur.exists() and cur.is_symlink(): raise WorkerInputError("symlink denied")
    p=base/q
    if p.exists():
        if p.is_symlink(): raise WorkerInputError("symlink denied")
        if p.stat().st_nlink>1: raise WorkerInputError("hardlink denied")
    try: p.resolve(strict=False).relative_to(base)
    except ValueError as e: raise WorkerInputError("resolved path escape") from e
    return p

class Ctl:
    def __init__(self,deadline,clock,cancel): self.deadline=deadline; self.clock=clock; self.cancel=cancel
    def check(self,where):
        if self.cancel(): raise CancellationRequested("cancelled at "+where)
        if self.clock().astimezone(timezone.utc)>=self.deadline: raise DeadlineExceeded("deadline exceeded at "+where)

def read_bytes(root,rel,allowed,ctl,budget):
    ctl.check("before_read"); p=check_path(root,rel,allowed)
    if not p.is_file(): raise WorkerInputError("input is not regular file")
    size=p.stat().st_size
    if size>MAX_FILE_BYTES: raise WorkerInputError("input exceeds per-file byte limit")
    if budget[0]+size>MAX_TOTAL_BYTES: raise WorkerInputError("input exceeds aggregate byte limit")
    with p.open("rb") as f: data=f.read(MAX_FILE_BYTES+1)
    if len(data)!=size or len(data)>MAX_FILE_BYTES: raise WorkerInputError("input changed or exceeded read bound")
    budget[0]+=len(data); ctl.check("after_read"); return data

def validate_structure(x,depth=0):
    if depth>MAX_DEPTH: raise WorkerInputError("JSON structure too deep")
    if isinstance(x,dict):
        if len(x)>MAX_DICT: raise WorkerInputError("JSON object too large")
        for k,v in x.items():
            if not isinstance(k,str) or len(k)>1024: raise WorkerInputError("invalid JSON key")
            validate_structure(v,depth+1)
    elif isinstance(x,list):
        if len(x)>MAX_LIST: raise WorkerInputError("JSON list too large")
        for v in x: validate_structure(v,depth+1)
    elif isinstance(x,str) and len(x)>MAX_STRING: raise WorkerInputError("JSON string too large")

def load_json(root,rel,allowed,ctl,budget):
    raw=read_bytes(root,rel,allowed,ctl,budget)
    try: text=raw.decode("utf-8","strict")
    except UnicodeDecodeError as e: raise WorkerInputError("invalid UTF-8") from e
    try: x=json.loads(text)
    except json.JSONDecodeError as e: raise WorkerInputError("malformed JSON") from e
    validate_structure(x)
    if isinstance(x,list) and len(x)>MAX_JSON_ROWS: raise WorkerInputError("JSON row limit exceeded")
    return x

def verify_snapshot(job,root,ctl):
    m=job["target_snapshot_manifest"]; kind=m.get("kind")
    if kind not in {"dataset","git_repo_subset"}: raise JobRejected("unsupported snapshot kind")
    if kind=="dataset" and m.get("snapshot_id")!=job["target_base_sha_or_snapshot"]: raise JobRejected("dataset snapshot id mismatch")
    if kind=="git_repo_subset" and m.get("base_commit_sha")!=job["target_base_sha_or_snapshot"]: raise JobRejected("repo base SHA mismatch")
    files=m.get("files")
    if not isinstance(files,list) or not files or len(files)>256: raise JobRejected("invalid snapshot file manifest")
    out=[]; total=0; seen=set()
    for e in files:
        ctl.check("snapshot_verify")
        if not isinstance(e,dict): raise JobRejected("invalid snapshot entry")
        rel=str(e.get("path",""))
        if rel in seen: raise JobRejected("duplicate snapshot path")
        seen.add(rel)
        try: p=check_path(root,rel,job["allowed_paths"])
        except WorkerInputError as x: raise JobRejected(str(x)) from x
        if not p.is_file(): raise JobRejected("snapshot file missing")
        size=p.stat().st_size; total+=size
        if size>MAX_FILE_BYTES or total>MAX_TOTAL_BYTES: raise JobRejected("snapshot byte limit exceeded")
        with p.open("rb") as f: data=f.read(MAX_FILE_BYTES+1)
        if len(data)!=size or len(data)>MAX_FILE_BYTES or e.get("bytes")!=size or e.get("sha256")!=sha256_bytes(data): raise JobRejected("snapshot bytes mismatch")
        row={"path":rel,"bytes":size,"sha256":sha256_bytes(data)}
        if kind=="git_repo_subset":
            blob=git_blob_sha(data)
            if e.get("git_blob_sha")!=blob: raise JobRejected("git blob mismatch")
            row["git_blob_sha"]=blob
        out.append(row)
    return {"kind":kind,"target_base_sha_or_snapshot":job["target_base_sha_or_snapshot"],"files":out,"total_bytes":total}

def atomic_json(p,obj):
    p.parent.mkdir(parents=True,exist_ok=True); fd,tmp=tempfile.mkstemp(prefix=p.name+".",dir=str(p.parent))
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as f: json.dump(obj,f,indent=2,sort_keys=True,allow_nan=False); f.write("\n"); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,p)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
def append_event(p,obj):
    p.parent.mkdir(parents=True,exist_ok=True)
    with p.open("a",encoding="utf-8") as f: f.write(json.dumps(obj,sort_keys=True,allow_nan=False)+"\n"); f.flush(); os.fsync(f.fileno())
def state_key(j): return sha256_bytes(f"{j['job_id']}|{j['canonical_opportunity_id']}|{j['work_lease_id']}|{j['attempt']}".encode())

def finite(v,field):
    try: x=float(v)
    except Exception as e: raise WorkerInputError("invalid numeric "+field) from e
    if not math.isfinite(x): raise WorkerInputError("nonfinite numeric "+field)
    return x

def write_bytes(root,rel,allowed,data,ctl):
    if len(data)>MAX_FILE_BYTES: raise WorkerInputError("artifact exceeds byte limit")
    p=check_path(root,rel,allowed,True); p.parent.mkdir(parents=True,exist_ok=True); p=check_path(root,rel,allowed,True); ctl.check("before_write")
    fd,tmp=tempfile.mkstemp(prefix=p.name+".",dir=str(p.parent))
    try:
        with os.fdopen(fd,"wb") as f: f.write(data); f.flush(); os.fsync(f.fileno())
        if p.exists() and p.stat().st_nlink>1: raise WorkerInputError("hardlink target denied")
        os.replace(tmp,p)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    ctl.check("after_write"); return {"path":rel,"sha256":sha256_bytes(data),"git_blob_sha":git_blob_sha(data),"bytes":len(data)}
