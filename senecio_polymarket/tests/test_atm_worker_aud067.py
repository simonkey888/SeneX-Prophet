from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from atm_worker.worker import CAPABILITIES, CrashInjected, JobRejected, compute_scope_hash, run_job

SOURCE_SHA="43c8023d3a4623381e45da02d9efa8e9b5888f47"

def make_job(**req_overrides):
    req={"operation":"provenance_hash_validation","inputs":["input/data.json"]}
    req.update(req_overrides)
    job={
        "protocol_version":"atm-worker.v1","job_id":"job-1","lease_id":"lease-1","attempt":1,
        "target_repository_or_dataset":"shadow/example","target_base_sha_or_snapshot":"snapshot-1",
        "allowed_paths":["input","artifacts"],"required_capabilities":["provenance_hash_validation"],
        "structured_requirements":req,"frozen_acceptance_criteria":["deterministic","zero-spend"],
        "upstream_issue_reference":"https://github.com/example/repo/issues/1","outgoing_spend_cap_usd":0,
        "lease_state":"ACTIVE","lease_expires_at":"2099-01-01T00:00:00Z",
    }
    job["fixed_job_scope_hash"]=compute_scope_hash(job)
    return job

class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); self.target=self.root/"target"; self.state=self.root/"state"
        (self.target/"input").mkdir(parents=True); (self.target/"artifacts").mkdir()
        (self.target/"input/data.json").write_text('[{"ts":2,"id":"b","v":2},{"ts":1,"id":"a","v":1}]')
    def tearDown(self): self.tmp.cleanup()
    def exec_job(self,job): return run_job(job,self.state,self.target,SOURCE_SHA,now=datetime(2026,8,18,tzinfo=timezone.utc))

    def test_capabilities_are_immutable_bounded_and_sufficient(self):
        self.assertGreaterEqual(len(CAPABILITIES),6); self.assertEqual(len(CAPABILITIES),len(set(CAPABILITIES)))
        self.assertIn("bounded_python_pipeline_repair",CAPABILITIES); self.assertIn("causal_cutoff_validation",CAPABILITIES)
    def test_completion_contract_and_zero_side_effects(self):
        out=self.exec_job(make_job()); required={"job_id","lease_id","attempt","worker_id","worker_version","source_sha","status","task_result","artifacts","tests","acceptance","provenance","side_effects","started_at","finished_at","fixed_job_scope_hash"}
        self.assertTrue(required<=set(out)); self.assertEqual(out["status"],"SUCCEEDED"); self.assertEqual(out["worker_id"],"senex-prophet")
        self.assertTrue(all(v==0 for v in out["side_effects"].values())); self.assertEqual(out["fixed_job_scope_hash"],make_job()["fixed_job_scope_hash"])
    def test_scope_hash_tamper_fails_closed(self):
        job=make_job(); job["target_base_sha_or_snapshot"]="moving-branch"
        with self.assertRaises(JobRejected): self.exec_job(job)
    def test_expired_and_terminal_leases_fail_closed(self):
        for patch in ({"lease_expires_at":"2026-08-17T00:00:00Z"},{"lease_state":"EXPIRED"}):
            job=make_job(); job.update(patch); job["fixed_job_scope_hash"]=compute_scope_hash(job)
            with self.assertRaises(JobRejected): self.exec_job(job)
    def test_unknown_capability_and_spend_fail_closed(self):
        job=make_job(); job["required_capabilities"]=["deploy"]; job["fixed_job_scope_hash"]=compute_scope_hash(job)
        with self.assertRaises(JobRejected): self.exec_job(job)
        job=make_job(); job["outgoing_spend_cap_usd"]=0.01; job["fixed_job_scope_hash"]=compute_scope_hash(job)
        with self.assertRaises(JobRejected): self.exec_job(job)
    def test_crash_after_ack_recovers_and_duplicate_is_idempotent(self):
        job=make_job(simulate_crash_at="after_ack")
        with self.assertRaises(CrashInjected): self.exec_job(job)
        files=list(self.state.glob("*.json")); self.assertEqual(len(files),1); self.assertEqual(json.loads(files[0].read_text())["status"],"ACKED")
        out1=self.exec_job(job); out2=self.exec_job(job); self.assertEqual(out1,out2); self.assertEqual(out1["status"],"SUCCEEDED")
        events=list(self.state.glob("*.events.jsonl"))[0].read_text().splitlines(); self.assertGreaterEqual(sum('PROGRESS' in e for e in events),2)
    def test_crash_during_work_recovers_without_orphan(self):
        job=make_job(simulate_crash_at="during_work")
        with self.assertRaises(CrashInjected): self.exec_job(job)
        out=self.exec_job(job); self.assertEqual(out["status"],"SUCCEEDED"); self.assertEqual(out["side_effects"]["child_processes"],0)
    def test_cancellation_is_terminal_without_target_effect(self):
        job=make_job(cancelled=True); out=self.exec_job(job); self.assertEqual(out["status"],"CANCELLED"); self.assertEqual(out["artifacts"],[])
    def test_symlink_and_hardlink_escape_fail_closed(self):
        outside=self.root/"outside.json"; outside.write_text("{}")
        (self.target/"input/data.json").unlink(); (self.target/"input/data.json").symlink_to(outside)
        with self.assertRaises(JobRejected): self.exec_job(make_job())
        (self.target/"input/data.json").unlink(); os.link(outside,self.target/"input/data.json")
        job=make_job(); job["job_id"]="hardlink"; job["fixed_job_scope_hash"]=compute_scope_hash(job)
        with self.assertRaises(JobRejected): self.exec_job(job)
    def test_outside_write_and_network_shell_resource_requests_denied(self):
        source=self.target/"input/source.py"; source.write_text("x=1\n")
        base={"operation":"bounded_python_pipeline_repair","input":"input/source.py","replacements":[],"static_checks":[]}
        job=make_job(**base,patch_output="../escape.py"); job["required_capabilities"]=["bounded_python_pipeline_repair"]; job["fixed_job_scope_hash"]=compute_scope_hash(job)
        with self.assertRaises(JobRejected): self.exec_job(job)
        for key in ("allow_network","shell_command","docker_socket","ssh_agent","secret_access","live_trading"):
            job=make_job(**{key:True}); job["job_id"]="deny-"+key; job["fixed_job_scope_hash"]=compute_scope_hash(job)
            with self.assertRaises(JobRejected): self.exec_job(job)
    def test_untrusted_python_is_parsed_not_executed_and_cannot_steal_env(self):
        secret="AUD067_NEVER_EXFILTRATE_9f1c"; os.environ["ATM_WORKER_TEST_SECRET"]=secret
        marker=self.root/"pwned"; src=f'import os\nvalue=os.getenv("ATM_WORKER_TEST_SECRET")\nopen({str(marker)!r},"w").write(value)\n'
        (self.target/"input/source.py").write_text(src)
        job=make_job(operation="bounded_python_pipeline_repair",input="input/source.py",replacements=[],static_checks=[],patch_output="artifacts/source.py")
        job["required_capabilities"]=["bounded_python_pipeline_repair"]; job["job_id"]="noexec"; job["fixed_job_scope_hash"]=compute_scope_hash(job)
        out=self.exec_job(job); self.assertFalse(marker.exists()); self.assertEqual(out["task_result"]["target_execution"],0); self.assertNotIn(secret,(self.target/"artifacts/source.py").read_text())
    def test_deterministic_replay_and_causal_cutoff(self):
        job=make_job(operation="deterministic_data_replay",input="input/data.json",sort_keys=["ts","id"]); job["required_capabilities"]=["deterministic_data_replay"]; job["job_id"]="replay"; job["fixed_job_scope_hash"]=compute_scope_hash(job)
        a=self.exec_job(job); self.assertEqual([r["id"] for r in a["task_result"]["rows"]],["a","b"])
        rows=[{"feature_ts":10,"decision_ts":10,"label_ts":11},{"feature_ts":12,"decision_ts":11,"label_ts":12}]; (self.target/"input/data.json").write_text(json.dumps(rows))
        job=make_job(operation="causal_cutoff_validation",input="input/data.json",feature_time_fields=["feature_ts"],label_time_fields=["label_ts"]); job["required_capabilities"]=["causal_cutoff_validation"]; job["job_id"]="causal"; job["fixed_job_scope_hash"]=compute_scope_hash(job)
        self.assertEqual(self.exec_job(job)["task_result"]["status"],"FAIL")
    def test_statistics_and_robustness(self):
        rows=[{"a":.2,"b":.19,"x":1},{"a":.3,"b":.31,"x":2},{"a":.4,"b":.39,"x":3}]; (self.target/"input/data.json").write_text(json.dumps(rows))
        job=make_job(operation="statistical_evaluation",input="input/data.json",baseline_field="a",candidate_field="b"); job["required_capabilities"]=["statistical_evaluation"]; job["job_id"]="stats"; job["fixed_job_scope_hash"]=compute_scope_hash(job)
        self.assertIn(self.exec_job(job)["task_result"]["orientation"],{"IMPROVEMENT","DEGRADATION","INCONCLUSIVE"})
        job=make_job(operation="robustness_regime_stress",input="input/data.json",value_field="x",clock_shift_effect=.1); job["required_capabilities"]=["robustness_regime_stress"]; job["job_id"]="stress"; job["fixed_job_scope_hash"]=compute_scope_hash(job)
        out=self.exec_job(job)["task_result"]; self.assertEqual(out["status"],"PASS"); self.assertIn("drop_10pct",out["stresses"])
    def test_shadow_tableau_repair_end_to_end(self):
        src='''class ColumnType:\n    AUTH = 7\n    MAX = 7\n\ndef parse(line):\n    line = line.strip().lower()\n    values = [v.strip() for v in line.split(",")]\n    return values\n'''
        (self.target/"input/tableau_shadow.py").write_text(src)
        job=make_job(operation="bounded_python_pipeline_repair",input="input/tableau_shadow.py",patch_output="artifacts/tableau_shadow_fixed.py",replacements=[{"old":"MAX = 7","new":"MAX = 8"},{"old":"line = line.strip().lower()","new":"line = line.strip()"}],static_checks=[{"name":"max-eight","contains":"MAX = 8"},{"name":"preserve-username-case","not_contains":"line = line.strip().lower()"}])
        job["required_capabilities"]=["bounded_python_pipeline_repair","regression_case_generation"]; job["job_id"]="shadow-tableau-1809"; job["target_repository_or_dataset"]="https://github.com/tableau/server-client-python/issues/1809"; job["target_base_sha_or_snapshot"]="aa9e3a0bd3114e0dbb7ec41abd4784483fb89277"; job["upstream_issue_reference"]="https://github.com/tableau/server-client-python/issues/1809"; job["fixed_job_scope_hash"]=compute_scope_hash(job)
        out=self.exec_job(job); self.assertEqual(out["status"],"SUCCEEDED"); self.assertTrue(all(x["pass"] for x in out["task_result"]["checks"])); self.assertTrue((self.target/"artifacts/tableau_shadow_fixed.py").exists())

if __name__=="__main__": unittest.main()
