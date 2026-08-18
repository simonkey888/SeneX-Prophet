from .atm_worker_aud067_common import *


class WorkerR1Part1(WorkerBase):
    def test_f001_canonical_contract_and_output_identity(self):
        out = self.exec(self.job())
        required = {"canonical_opportunity_id", "worker_id", "work_lease_id", "scope_hash", "protocol_version", "source_sha", "external_acceptance", "outgoing_spend_usd"}
        self.assertTrue(required <= set(out))
        self.assertEqual(out["worker_id"], "senex-prophet")
        self.assertEqual(out["external_acceptance"], {"status": "NOT_EVALUATED_BY_WORKER", "authoritative": False})
        self.assertEqual(out["outgoing_spend_usd"], 0)

    def test_f001_missing_or_mismatched_canonical_fields_fail_pre_ack(self):
        fields = ["canonical_opportunity_id", "worker_id", "work_lease_id", "scope_hash"]
        for field in fields:
            j = self.job(); j["job_id"] = "missing-" + field
            j.pop(field)
            if field != "scope_hash":
                j["scope_hash"] = compute_scope_hash(j)
            with self.assertRaises(JobRejected): self.exec(j)
        j = self.job(); j["worker_id"] = "other"; j["scope_hash"] = compute_scope_hash(j)
        with self.assertRaises(JobRejected): self.exec(j)

    def test_f001_temporal_cutoff_required(self):
        j = self.job(operation="statistical_evaluation", input="input/data.json", baseline_field="v", candidate_field="v", min_evidence_n=3)
        j["cutoff"] = None; j["scope_hash"] = compute_scope_hash(j)
        with self.assertRaises(JobRejected): self.exec(j)

    def test_f002_dataset_bytes_tamper_rejected_pre_ack(self):
        j = self.job()
        (self.target / "input/data.json").write_text("tampered", encoding="utf-8")
        with self.assertRaises(JobRejected): self.exec(j)
        self.assertEqual(list(self.state.glob("*.json")), [])

    def test_f002_git_repo_base_and_blob_are_verified(self):
        base = "a" * 40
        j = self.job(base=base, kind="git_repo_subset")
        out = self.exec(j)
        self.assertEqual(out["provenance"]["verified_snapshot"]["target_base_sha_or_snapshot"], base)
        j2 = self.job(base=base, kind="git_repo_subset"); j2["job_id"] = "badblob"
        j2["target_snapshot_manifest"]["files"][0]["git_blob_sha"] = "0" * 40
        j2["scope_hash"] = compute_scope_hash(j2)
        with self.assertRaises(JobRejected): self.exec(j2)
        j3 = self.job(base=base, kind="git_repo_subset"); j3["job_id"] = "badbase"
        j3["target_snapshot_manifest"]["base_commit_sha"] = "b" * 40
        j3["scope_hash"] = compute_scope_hash(j3)
        with self.assertRaises(JobRejected): self.exec(j3)

    def test_f002_workspace_isolation_rejects_canonical_overlap_and_outside(self):
        j = self.job()
        with self.assertRaises(JobRejected):
            run_job(j, self.state, self.canonical, SOURCE_SHA, self.workspace, self.canonical, now=NOW, clock=lambda: NOW)
        outside_state = self.root / "outside-state"
        with self.assertRaises(JobRejected):
            run_job(j, outside_state, self.target, SOURCE_SHA, self.workspace, self.canonical, now=NOW, clock=lambda: NOW)

    def test_f003_all_three_crash_points_recover(self):
        for point in ("after_ack", "during_work"):
            j = self.job(simulate_crash_at=point); j["job_id"] = point; j["scope_hash"] = compute_scope_hash(j)
            with self.assertRaises(CrashInjected): self.exec(j)
            out1 = self.exec(j); out2 = self.exec(j)
            self.assertEqual(out1, out2); self.assertEqual(out1["status"], "SUCCEEDED")
        (self.target / "input/source.py").write_text("x=1\n", encoding="utf-8")
        j = self.job(operation="bounded_python_pipeline_repair", caps=["bounded_python_pipeline_repair"], paths=["input/source.py"],
                     input="input/source.py", replacements=[{"old":"x=1","new":"x=2"}],
                     static_checks=[{"name":"x2","contains":"x=2"}], patch_output="artifacts/fixed.py",
                     simulate_crash_at="after_artifact_before_finalize")
        j["job_id"] = "post-artifact"; j["frozen_acceptance_criteria"] = [{"type":"static_check","name":"x2"}]; j["scope_hash"] = compute_scope_hash(j)
        with self.assertRaises(CrashInjected): self.exec(j)
        artifact_before = (self.target / "artifacts/fixed.py").read_bytes()
        out = self.exec(j); duplicate = self.exec(j)
        self.assertEqual(out, duplicate)
        self.assertEqual(out["artifacts"][0]["sha256"], sha256_bytes(artifact_before))

    def test_f003_deadline_and_dynamic_cancellation_terminalize(self):
        j = self.job(); j["job_id"] = "deadline"; j["job_deadline"] = "2026-08-18T10:00:02Z"; j["scope_hash"] = compute_scope_hash(j)
        ticks = iter([NOW, NOW, NOW + timedelta(seconds=3), NOW + timedelta(seconds=3)])
        out = self.exec(j, clock=lambda: next(ticks, NOW + timedelta(seconds=3)))
        self.assertEqual(out["status"], "FAILED"); self.assertEqual(out["error_class"], "DEADLINE_EXCEEDED")
        j = self.job(); j["job_id"] = "cancel"; j["scope_hash"] = compute_scope_hash(j)
        calls = {"n": 0}
        def cancel():
            calls["n"] += 1
            return calls["n"] >= 4
        out = self.exec(j, cancel_check=cancel)
        self.assertEqual(out["status"], "CANCELLED")

    def test_f004_oversized_input_bounded_pre_ack(self):
        p = self.target / "input/big.bin"; p.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        data = p.read_bytes(); j = self.job(paths=["input/data.json"]); j["job_id"] = "big"
        j["target_snapshot_manifest"] = {"kind":"dataset","snapshot_id":"snapshot-1","files":[{"path":"input/big.bin","sha256":sha256_bytes(data),"bytes":len(data)}]}
        j["scope_hash"] = compute_scope_hash(j)
        with self.assertRaises(JobRejected): self.exec(j)
