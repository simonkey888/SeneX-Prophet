from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[2]
MODULE_PATH = ROOT / "tools" / "verify_repository_contract.py"
spec = importlib.util.spec_from_file_location("verify_repository_contract_r1", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

class TargetArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.contract = mod.load_structured(ROOT / "governance/repository_contract.yaml")

    def test_01_exact_domain_set(self):
        architecture = mod.build_target_architecture()
        self.assertEqual([item["domain_id"] for item in architecture["domains"]], list(mod.DOMAINS))

    def test_02_domain_contract_fields(self):
        required = {"domain_id","purpose","allowed_responsibilities","forbidden_responsibilities","allowed_dependencies","forbidden_dependencies","network_authority","filesystem_read_authority","filesystem_write_authority","secret_authority","order_authority","wallet_authority","production_authority","human_approval_required","canonical_paths","legacy_paths","generated_paths","owner","evidence","confidence"}
        for domain in mod.build_target_architecture()["domains"]:
            self.assertTrue(required <= set(domain), domain["domain_id"])

    def test_03_permanent_safety(self):
        safety = mod.build_target_architecture()["safety_invariants"]
        self.assertEqual(safety, {"paper_only": True, "orders_enabled": False, "live_capital_locked": True})

    def test_04_live_authority_absent(self):
        live = next(item for item in mod.build_target_architecture()["domains"] if item["domain_id"] == "EXECUTION_LIVE_QUARANTINED")
        self.assertFalse(live["order_authority"])
        self.assertFalse(live["wallet_authority"])
        self.assertFalse(live["production_authority"])

    def test_05_read_only_api_cannot_write(self):
        api = next(item for item in mod.build_target_architecture()["domains"] if item["domain_id"] == "READ_ONLY_API")
        self.assertFalse(api["filesystem_write_authority"])

    def test_06_live_domain_forbidden_dependency(self):
        for item in mod.build_target_architecture()["domains"]:
            if item["domain_id"] != "EXECUTION_LIVE_QUARANTINED":
                self.assertNotIn("EXECUTION_LIVE_QUARANTINED", item["allowed_dependencies"])

    def test_07_manifest_deterministic(self):
        one = mod.canonical_json_bytes(mod.build_repository_manifest(ROOT, self.contract))
        two = mod.canonical_json_bytes(mod.build_repository_manifest(ROOT, self.contract))
        self.assertEqual(one, two)

    def test_08_manifest_fields(self):
        required = {"path","git_blob_sha","sha256","size","mode","current_domain","target_domain","classification","owner","runtime_imported","active_entrypoint","docker_included","workflow_referenced","shell_referenced","test_referenced","northflank_referenced","generated","sensitive","do_not_touch","wallet_capability","order_capability","network_capability","filesystem_write_capability","migration_action","migration_source","migration_target","evidence","confidence"}
        manifest = mod.build_repository_manifest(ROOT, self.contract)
        for record in manifest["files"]:
            self.assertTrue(required <= set(record), record["path"])

    def test_09_component_registry_paper_has_no_real_authority(self):
        manifest = mod.build_repository_manifest(ROOT, self.contract)
        registry = mod.build_component_registry(ROOT, manifest)
        paper = [item for item in registry["components"] if item["path"].startswith("polymarket/paper/")]
        self.assertTrue(paper)
        self.assertTrue(all(not item["wallet_access"] and not item["order_access"] and not item["secret_access"] for item in paper))

    def test_10_all_active_entrypoints_registered(self):
        manifest = mod.build_repository_manifest(ROOT, self.contract)
        registered = {item["path"] for item in mod.build_component_registry(ROOT, manifest)["components"]}
        self.assertFalse([item["path"] for item in manifest["files"] if item["active_entrypoint"] and item["path"] not in registered])

    def test_11_migration_waves_ordered(self):
        waves = [item["wave"] for item in mod.build_migration_map()["migrations"]]
        self.assertEqual(waves, [f"WAVE_{index}" for index in range(10)])

    def test_12_migrations_have_rollback_and_authorization(self):
        for migration in mod.build_migration_map()["migrations"]:
            self.assertTrue(migration["rollback"])
            self.assertTrue(migration["separate_aud_authorization_required"])

    def test_13_paper_runtime_static_safety(self):
        for path in sorted((ROOT / "polymarket/paper").glob("*.py")):
            self.assertEqual(mod.capability_findings(path), [], path.name)

    def test_14_evidence_chain_tamper_detection(self):
        from polymarket.paper.trial_runner import canonical_hash, verify_evidence_chain
        body = '{"ok":true}'
        import hashlib
        item = {"sequence":0,"previous_hash":"GENESIS","timestamp_utc":"2026-08-04T00:00:00Z","url":"https://example.invalid/public","sha256":hashlib.sha256(body.encode()).hexdigest(),"bytes":len(body),"content_type":"application/json","response_body_utf8":body}
        item["record_hash"] = canonical_hash(item)
        self.assertTrue(verify_evidence_chain([item]))
        item["response_body_utf8"] = "tampered"
        self.assertFalse(verify_evidence_chain([item]))

    def test_15_generated_files_match_tracked_count(self):
        manifest = mod.load_structured(ROOT / "governance/repository_manifest.json")
        self.assertEqual(manifest["files_tracked_total"], len(mod.tracked_files(ROOT)))

if __name__ == "__main__":
    unittest.main()
