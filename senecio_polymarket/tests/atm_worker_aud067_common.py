from __future__ import annotations

import hashlib, json, os, subprocess, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from atm_worker.worker import (
    ACCEPTANCE_CRITERIA_VERSION,
    CAPABILITIES,
    MAX_FILE_BYTES,
    CrashInjected,
    JobRejected,
    compute_scope_hash,
    git_blob_sha,
    independent_check_completion,
    run_job,
    sha256_bytes,
)

def _exact_source_sha():
    explicit=os.environ.get("AUD067_SOURCE_SHA")
    if explicit: return explicit
    try:
        repo=Path(__file__).resolve().parents[2]
        return subprocess.check_output(["git","rev-parse","HEAD"],cwd=repo,text=True).strip()
    except Exception:
        return "a"*40

SOURCE_SHA=_exact_source_sha()
NOW=datetime(2026,8,18,10,0,tzinfo=timezone.utc)

class WorkerBase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.workspace=self.root/"job-workspace"; self.target=self.workspace/"target"; self.state=self.workspace/"state"; self.canonical=self.root/"canonical-senex"
        self.canonical.mkdir(parents=True); (self.target/"input").mkdir(parents=True); (self.target/"artifacts").mkdir()
        (self.target/"input/data.json").write_text('[{"ts":2,"id":"b","v":2},{"ts":1,"id":"a","v":1}]',encoding="utf-8")
    def tearDown(self): self.tmp.cleanup()
    def criterion(self,typ="zero_spend",name=None):
        c={"schema_version":ACCEPTANCE_CRITERIA_VERSION,"type":typ}
        if name is not None: c["name"]=name
        return c
    def manifest(self,paths=None,kind="dataset",base="snapshot-1"):
        paths=paths or ["input/data.json"]; files=[]
        for rel in paths:
            data=(self.target/rel).read_bytes(); row={"path":rel,"sha256":sha256_bytes(data),"bytes":len(data)}
            if kind=="git_repo_subset": row["git_blob_sha"]=git_blob_sha(data)
            files.append(row)
        m={"kind":kind,"files":files}; m["base_commit_sha" if kind=="git_repo_subset" else "snapshot_id"]=base; return m
    def job(self,*,operation="provenance_hash_validation",caps=None,paths=None,base="snapshot-1",kind="dataset",criteria=None,**req):
        paths=paths or ["input/data.json"]; structured={"operation":operation}
        if operation=="provenance_hash_validation": structured["inputs"]=paths
        structured.update(req); caps=caps or [operation]; temporal=operation in {"causal_cutoff_validation","statistical_evaluation"}
        j={"protocol_version":"atm-worker.v1","job_id":"job-1","canonical_opportunity_id":"opp-1","worker_id":"senex-prophet","work_lease_id":"lease-1","attempt":1,"target_repository_or_dataset":"shadow/example","target_base_sha_or_snapshot":base,"target_snapshot_manifest":self.manifest(paths,kind,base),"allowed_paths":["input","artifacts"],"required_capabilities":caps,"structured_requirements":structured,"frozen_acceptance_criteria":criteria or [self.criterion()],"expected_deliverable":"bounded evidence","deterministic_checks":["sha256"],"data_provenance":{"source":"fixture","complete":True,"retrieved_at":"2026-08-18T09:00:00Z"},"as_of":"2026-08-18T09:59:00Z" if temporal else None,"cutoff":"2026-08-18T09:58:00Z" if temporal else None,"max_spend_usd":0,"lease_state":"ACTIVE","lease_expires_at":"2099-01-01T00:00:00Z","job_deadline":"2099-01-01T00:00:00Z","workspace_id":"ws-1"}
        j["scope_hash"]=compute_scope_hash(j); return j
    def exec(self,j,source_sha=None,**kwargs):
        return run_job(j,self.state,self.target,source_sha or SOURCE_SHA,self.workspace,self.canonical,now=NOW,clock=kwargs.pop("clock",lambda:NOW),cancel_check=kwargs.pop("cancel_check",lambda:False),**kwargs)
    def rebind(self,j,paths=None,kind=None,base=None):
        base=base or j["target_base_sha_or_snapshot"]
        if paths is not None or kind is not None: j["target_snapshot_manifest"]=self.manifest(paths,kind or j["target_snapshot_manifest"]["kind"],base)
        j["scope_hash"]=compute_scope_hash(j); return j
