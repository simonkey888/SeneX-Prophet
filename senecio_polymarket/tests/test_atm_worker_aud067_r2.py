from __future__ import annotations

import json
from pathlib import Path

try:
    from .atm_worker_aud067_common import *
except ImportError:
    from atm_worker_aud067_common import *

class WorkerR2Residuals(WorkerBase):
    def _ids(self,j,suffix):
        j["job_id"]="job-"+suffix; j["canonical_opportunity_id"]="opp-"+suffix; j["work_lease_id"]="lease-"+suffix; j["workspace_id"]="ws-"+suffix; j["scope_hash"]=compute_scope_hash(j); return j

    def test_f010_closed_acceptance_schema_positive(self):
        (self.target/"input/s.py").write_text("x=1\n")
        criteria=[self.criterion(),self.criterion("static_check","x2")]
        j=self.job(operation="bounded_python_pipeline_repair",paths=["input/s.py"],criteria=criteria,input="input/s.py",patch_output="artifacts/f.py",replacements=[{"old":"x=1","new":"x=2"}],static_checks=[{"name":"x2","contains":"x=2"}])
        out=self.exec(j)
        check=independent_check_completion(j,out,self.target)
        self.assertEqual(out["status"],"SUCCEEDED"); self.assertEqual(check["status"],"PASS"); self.assertEqual(check["criteria_schema_version"],ACCEPTANCE_CRITERIA_VERSION)

    def test_f010_unsupported_acceptance_type_rejected_pre_ack(self):
        j=self.job(); j["frozen_acceptance_criteria"]=[{"schema_version":ACCEPTANCE_CRITERIA_VERSION,"type":"magic_auto_pass"}]; j["scope_hash"]=compute_scope_hash(j)
        with self.assertRaises(JobRejected): self.exec(j)
        self.assertFalse(self.state.exists() and any(self.state.iterdir()))

    def test_f010_malformed_acceptance_criterion_rejected_pre_ack(self):
        j=self.job(); j["frozen_acceptance_criteria"]=[{"schema_version":ACCEPTANCE_CRITERIA_VERSION,"type":"static_check"}]; j["scope_hash"]=compute_scope_hash(j)
        with self.assertRaises(JobRejected): self.exec(j)

    def test_f011_crash_resume_requires_same_source_and_worker_version(self):
        j=self.job(operation="deterministic_data_replay",input="input/data.json",simulate_crash_at="after_ack")
        source_a="1"*40; source_b="2"*40
        with self.assertRaises(CrashInjected): self.exec(j,source_sha=source_a)
        with self.assertRaises(JobRejected): self.exec(j,source_sha=source_b)
        state_file=next(self.state.glob("*.json")); state=json.loads(state_file.read_text()); state["worker_version"]="aud067.r1"; state_file.write_text(json.dumps(state))
        with self.assertRaises(JobRejected): self.exec(j,source_sha=source_a)

    def test_f012_temporal_completion_echoes_frozen_asof_cutoff(self):
        (self.target/"input/t.json").write_text('[{"event_ts":1,"receipt_ts":2,"decision_ts":3,"label_ts":4,"history":1}]')
        j=self.job(operation="causal_cutoff_validation",paths=["input/t.json"],input="input/t.json",event_time_fields=["event_ts"],availability_time_fields=["receipt_ts"],label_time_fields=["label_ts"],required_history_fields=["history"])
        j["as_of"]="2026-08-18T09:57:00Z"; j["cutoff"]="2026-08-18T09:58:00Z"; j["scope_hash"]=compute_scope_hash(j)
        out=self.exec(j)
        self.assertEqual(out["provenance"]["as_of"],j["as_of"]); self.assertEqual(out["provenance"]["cutoff"],j["cutoff"])
        self.assertEqual(out["task_result"]["status"],"PASS")

    def test_f013_predeclared_robustness_matrix_is_bounded_and_train_only(self):
        p=self.target/"input/r.json"; p.write_text('[{"x":1},{"x":2},{"x":3},{"x":4},{"x":2.1},{"x":2.2},{"x":2.3},{"x":2.4}]')
        req={"input":"input/r.json","value_field":"x","train_end_index":4,"threshold_method":"train_max_abs_deviation","threshold_multiplier":1.0,"min_within_threshold_rate":1.0,"regimes":[{"name":"eval-a","start_index":4,"end_index":6},{"name":"eval-b","start_index":6,"end_index":8}],"perturbations":[{"name":"identity","type":"identity"},{"name":"plus-0.1","type":"additive_delta","value":0.1}]}
        j=self.job(operation="robustness_regime_stress",paths=["input/r.json"],**req); out=self.exec(j); r=out["task_result"]
        self.assertEqual(r["status"],"PASS"); self.assertEqual(len(r["matrix"]),4); self.assertFalse(r["post_hoc_selection"]); threshold=r["train_only"]["threshold"]
        p.write_text('[{"x":1},{"x":2},{"x":3},{"x":4},{"x":200},{"x":220},{"x":230},{"x":240}]')
        j2=self.job(operation="robustness_regime_stress",paths=["input/r.json"],**req); self._ids(j2,"robust-2"); out2=self.exec(j2); r2=out2["task_result"]
        self.assertEqual(r2["train_only"]["threshold"],threshold); self.assertEqual(r2["robustness_verdict"],"FRAGILE"); self.assertEqual(r["predeclared_spec_sha256"],r2["predeclared_spec_sha256"])
