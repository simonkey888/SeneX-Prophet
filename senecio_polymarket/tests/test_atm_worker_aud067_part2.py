try:
    from .atm_worker_aud067_common import *
except ImportError:
    from atm_worker_aud067_common import *


class WorkerR1Part2(WorkerBase):
    def _malformed_job(self, content: bytes, operation: str, caps, **req):
        token = hashlib.sha256(content + operation.encode()).hexdigest()[:12]
        rel = f"input/bad-{token}.json"; p = self.target / rel; p.write_bytes(content)
        j = self.job(operation=operation, caps=caps, paths=[rel], input=rel, **req); j["job_id"] = token; j["scope_hash"] = compute_scope_hash(j); return j

    def test_f004_malformed_json_temporal_stats_utf8_terminal_failed(self):
        cases = [
            self._malformed_job(b"{", "deterministic_data_replay", ["deterministic_data_replay"]),
            self._malformed_job(b'[{"decision_ts":"x"}]', "causal_cutoff_validation", ["causal_cutoff_validation"], availability_time_fields=["feature_ts"], label_time_fields=["label_ts"]),
            self._malformed_job(b'[{"a":"x","b":1}]', "statistical_evaluation", ["statistical_evaluation"], baseline_field="a", candidate_field="b", min_evidence_n=3),
            self._malformed_job(b"\xff\xfe", "deterministic_data_replay", ["deterministic_data_replay"]),
        ]
        for j in cases:
            out = self.exec(j); self.assertEqual(out["status"], "FAILED"); self.assertNotEqual(out["worker_assessment"]["status"], "PASS")

    def test_f005_causal_fail_is_top_level_failed_and_duplicate_idempotent(self):
        rows = [{"event_ts":10,"receipt_ts":12,"decision_ts":11,"label_ts":13}]; (self.target/"input/data.json").write_text(json.dumps(rows))
        j = self.job(operation="causal_cutoff_validation", caps=["causal_cutoff_validation"], input="input/data.json", event_time_fields=["event_ts"], availability_time_fields=["receipt_ts"], label_time_fields=["label_ts"])
        j["job_id"]="causal-fail"; j["cutoff"]="1970-01-01T00:00:11Z"; j["as_of"]="1970-01-01T00:00:11Z"; self.rebind(j,["input/data.json"])
        a=self.exec(j); b=self.exec(j); self.assertEqual(a,b); self.assertEqual(a["task_result"]["status"],"FAIL"); self.assertEqual(a["status"],"FAILED"); self.assertEqual(a["external_acceptance"]["status"],"NOT_EVALUATED_BY_WORKER")

    def test_f005_provenance_failed_predicate_is_failed(self):
        data=(self.target/"input/data.json").read_bytes(); j=self.job(expected_files=[{"path":"input/data.json","sha256":"0"*64,"bytes":len(data)}]); j["job_id"]="provfail"; j["scope_hash"]=compute_scope_hash(j)
        out=self.exec(j); self.assertEqual(out["task_result"]["status"],"FAIL"); self.assertEqual(out["status"],"FAILED")

    def test_f006_independent_checker_rejects_tampered_artifact_despite_local_pass(self):
        (self.target/"input/source.py").write_text("x=1\n")
        j=self.job(operation="bounded_python_pipeline_repair",caps=["bounded_python_pipeline_repair"],paths=["input/source.py"],input="input/source.py", replacements=[{"old":"x=1","new":"x=2"}],static_checks=[{"name":"x2","contains":"x=2"}],patch_output="artifacts/fixed.py")
        j["job_id"]="accept-boundary"; j["frozen_acceptance_criteria"]=[self.criterion("static_check","x2")]; j["scope_hash"]=compute_scope_hash(j)
        out=self.exec(j); self.assertEqual(out["worker_assessment"]["status"],"PASS"); self.assertEqual(independent_check_completion(j,out,self.target)["status"],"PASS")
        (self.target/"artifacts/fixed.py").write_text("x=999\n"); checked=independent_check_completion(j,out,self.target); self.assertEqual(checked["status"],"FAIL"); self.assertIn("artifact_hash_mismatch",checked["reasons"]); self.assertEqual(out["external_acceptance"]["status"],"NOT_EVALUATED_BY_WORKER")

    def test_f007_n1_is_inconclusive_and_nonfinite_fails(self):
        (self.target/"input/data.json").write_text('[{"a":1,"b":0}]'); j=self.job(operation="statistical_evaluation",caps=["statistical_evaluation"],input="input/data.json",baseline_field="a",candidate_field="b",min_evidence_n=3); j["job_id"]="n1"; self.rebind(j,["input/data.json"])
        out=self.exec(j); self.assertEqual(out["status"],"SUCCEEDED"); self.assertEqual(out["task_result"]["orientation"],"INCONCLUSIVE")
        (self.target/"input/data.json").write_text('[{"a":1,"b":"NaN"}]'); j=self.job(operation="statistical_evaluation",caps=["statistical_evaluation"],input="input/data.json",baseline_field="a",candidate_field="b",min_evidence_n=3); j["job_id"]="nan"; self.rebind(j,["input/data.json"]); self.assertEqual(self.exec(j)["status"],"FAILED")

    def test_f007_missing_history_or_incomplete_provenance_downgrades_unknown(self):
        rows=[{"event_ts":9,"receipt_ts":9,"decision_ts":10,"label_ts":11,"history":None}]; (self.target/"input/data.json").write_text(json.dumps(rows))
        j=self.job(operation="causal_cutoff_validation",caps=["causal_cutoff_validation"],input="input/data.json",event_time_fields=["event_ts"],availability_time_fields=["receipt_ts"],label_time_fields=["label_ts"],required_history_fields=["history"])
        j["job_id"]="missinghist"; j["cutoff"]="1970-01-01T00:00:10Z"; j["as_of"]="1970-01-01T00:00:10Z"; self.rebind(j,["input/data.json"]); out=self.exec(j); self.assertEqual(out["task_result"]["status"],"UNKNOWN"); self.assertEqual(out["status"],"SUCCEEDED")

    def test_f007_robustness_uses_explicit_train_only_boundary(self):
        rows=[{"x":1},{"x":2},{"x":3},{"x":4},{"x":2.1},{"x":2.2}]; (self.target/"input/data.json").write_text(json.dumps(rows))
        j=self.job(operation="robustness_regime_stress",caps=["robustness_regime_stress"],input="input/data.json",value_field="x",train_end_index=4,threshold_method="train_max_abs_deviation",threshold_multiplier=1.0,min_within_threshold_rate=1.0,regimes=[{"name":"eval","start_index":4,"end_index":6}],perturbations=[{"name":"identity","type":"identity"}])
        j["job_id"]="stress"; self.rebind(j,["input/data.json"]); out=self.exec(j)["task_result"]; self.assertEqual(out["status"],"PASS"); self.assertEqual(out["train_only"]["n"],4); self.assertEqual(len(out["matrix"]),1); self.assertFalse(out["post_hoc_selection"])
        j=self.job(operation="robustness_regime_stress",caps=["robustness_regime_stress"],input="input/data.json",value_field="x"); j["job_id"]="nostrain"; self.rebind(j,["input/data.json"]); self.assertEqual(self.exec(j)["status"],"FAILED")

    def test_f009_capability_matrix_surface_is_proven_only_no_unproved_regression_cap(self):
        self.assertEqual(set(CAPABILITIES), {"deterministic_data_replay","causal_cutoff_validation","provenance_hash_validation","statistical_evaluation","robustness_regime_stress","bounded_python_pipeline_repair"}); self.assertNotIn("regression_case_generation", CAPABILITIES)

    def test_security_no_target_execution_symlink_hardlink_network_or_spend(self):
        for key in ("allow_network","network_allowlist","shell_command","startup_hook","docker_socket","ssh_agent","secret_access","live_trading","wallet","payment"):
            j=self.job(**{key:True}); j["job_id"]="deny-"+key; j["scope_hash"]=compute_scope_hash(j)
            with self.assertRaises(JobRejected): self.exec(j)
        outside=self.root/"outside"; outside.write_text("secret"); (self.target/"input/data.json").unlink(); (self.target/"input/data.json").symlink_to(outside)
        data=outside.read_bytes(); j=self.job(paths=["input/data.json"]); j["target_snapshot_manifest"]={"kind":"dataset","snapshot_id":"snapshot-1","files":[{"path":"input/data.json","sha256":sha256_bytes(data),"bytes":len(data)}]}; j["scope_hash"]=compute_scope_hash(j)
        with self.assertRaises(JobRejected): self.exec(j)
