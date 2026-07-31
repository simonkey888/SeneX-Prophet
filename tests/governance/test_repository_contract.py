from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import sys
from datetime import datetime, timezone
from pathlib import Path

MODULE_PATH = Path(__file__).parents[2] / "tools" / "verify_repository_contract.py"
spec = importlib.util.spec_from_file_location("verify_repository_contract", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
FIX = Path(__file__).parent / "fixtures"

class ContractTests(unittest.TestCase):
    def test_01_canonical_json_is_stable(self):
        self.assertEqual(mod.canonical_json_bytes({"b":1,"a":2}), b'{"a":2,"b":1}\n')
    def test_02_sha256(self):
        self.assertEqual(mod.sha256_bytes(b"x"), "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881")
    def test_03_docs_wallet_order_not_python(self):
        self.assertEqual((FIX/"documentation_wallet_order/README.md").suffix, ".md")
    def test_04_executable_signing_detected(self):
        self.assertTrue(mod.capability_findings(FIX/"executable_signing_order/capability.py"))
    def test_05_dynamic_signing_detected(self):
        self.assertTrue(mod.capability_findings(FIX/"dynamic_signing_order/capability.py"))
    def test_06_subprocess_order_detected(self):
        self.assertIn("subprocess execution capability", mod.capability_findings(FIX/"subprocess_order/capability.py"))
    def test_07_unpinned_action_detected(self):
        self.assertTrue(any("unpinned" in x for x in mod.workflow_findings(FIX/"workflow_unpinned/workflow.yml")))
    def test_08_temp_no_expiry_detected(self):
        self.assertTrue(any("expiry" in x for x in mod.workflow_findings(FIX/"temp_no_expiry/temp-audit.yml")))
    def test_09_temp_write_detected(self):
        self.assertTrue(any("contents:write" in x for x in mod.workflow_findings(FIX/"temp_write_permissions/temp-audit.yml")))
    def test_10_product_legacy_import(self):
        self.assertIn("senecio_polymarket.oracle", mod.python_imports(FIX/"product_legacy_import/product.py"))
    def test_11_research_raw_fixture(self):
        self.assertIn("raw_chain_v1", (FIX/"research_raw_writer/research_writer.py").read_text())
    def test_12_second_writer_fixture(self):
        self.assertIn("raw_chain_v1", (FIX/"second_raw_writer/second_writer.py").read_text())
    def test_13_runtime_fixture(self):
        self.assertTrue(json.loads((FIX/"runtime_output/runtime.json").read_text())["runtime"])
    def _auth(self):
        return {"mission_id":"m","base_sha":"b","head_sha":"h","allowed_paths":["x"],"issued_by":"AUD","issued_at":"2026-07-31T00:00:00Z","expires_at":"2099-01-01T00:00:00Z","reason":"test","old_hashes":{},"new_hashes":{}}
    def test_14_exact_override_passes(self):
        self.assertEqual(mod.validate_override(self._auth(),base_sha="b",head_sha="h",paths=["x"],now=datetime(2026,7,31,tzinfo=timezone.utc)),[])
    def test_15_missing_override_fields(self):
        self.assertTrue(mod.validate_override({},base_sha="b",head_sha="h",paths=[]))
    def test_16_wrong_base_fails(self):
        a=self._auth(); a["base_sha"]="z"; self.assertIn("wrong base_sha",mod.validate_override(a,base_sha="b",head_sha="h",paths=["x"]))
    def test_17_wrong_head_fails(self):
        a=self._auth(); a["head_sha"]="z"; self.assertIn("wrong head_sha",mod.validate_override(a,base_sha="b",head_sha="h",paths=["x"]))
    def test_18_extra_path_fails(self):
        a=self._auth(); a["allowed_paths"]=["x","y"]; self.assertTrue(mod.validate_override(a,base_sha="b",head_sha="h",paths=["x"]))
    def test_19_expired_fails(self):
        a=self._auth(); a["expires_at"]="2020-01-01T00:00:00Z"; self.assertIn("authorization expired",mod.validate_override(a,base_sha="b",head_sha="h",paths=["x"],now=datetime(2026,7,31,tzinfo=timezone.utc)))
    def test_20_match_exact(self):
        self.assertTrue(mod.matches("x/y",["x/y"]))
    def test_21_match_glob(self):
        self.assertTrue(mod.matches("tests/governance/a.py",["tests/governance/**"]))
    def test_22_sidecar_passes(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"m.json"; p.write_text("{}\n"); s=Path(d)/"m.json.sha256"; s.write_text(f"{mod.sha256_bytes(p.read_bytes())}  m.json\n")
            self.assertTrue(mod.verify_sidecar(p,s)[0])
    def test_23_sidecar_wrong_name_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"m.json"; p.write_text("{}\n"); s=Path(d)/"m.json.sha256"; s.write_text(f"{mod.sha256_bytes(p.read_bytes())}  other.json\n")
            self.assertFalse(mod.verify_sidecar(p,s)[0])

if __name__ == "__main__": unittest.main()
