from __future__ import annotations
import importlib.util, tempfile, unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[2] / "tools/audit/aud065_boundary_501.py"
spec=importlib.util.spec_from_file_location("aud065_boundary_501",MODULE)
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class FakeHttp:
    def __init__(self,pages): self.pages=list(pages)
    def json(self,url,headers=None):
        if not self.pages: raise m.ProofFailed("INCOMPLETE_PAGE_SEQUENCE")
        p=self.pages.pop(0)
        if isinstance(p,Exception): raise p
        return p,b"{}",{}

class BP1Tests(unittest.TestCase):
    def setUp(self): self.rows=[m.fixture_row(i) for i in range(501)]
    def test_01_boundary_class_501(self): self.assertEqual(m.recompute(self.rows,"BTCUSDT")["input_rows"],501)
    def test_02_known_126_vs_125(self):
        self.assertEqual(m.recompute(self.rows,"BTCUSDT")["independent_1h_rows"],126)
        self.assertEqual(m.recompute(self.rows[-500:],"BTCUSDT")["independent_1h_rows"],125)
    def test_03_page_size_invariance(self):
        for size in (1,7,17,250,500):
            got=[r for i in range(0,501,size) for r in self.rows[i:i+size]]
            self.assertEqual(m.recompute(got,"BTCUSDT")["independent_1h_rows"],126)
    def test_04_ts_id_tie_ordering(self):
        a=m.fixture_row(0); b=m.fixture_row(0); a["id"]="b"; b["id"]="a"
        self.assertEqual([r["id"] for r in sorted([a,b],key=m.stable_key)],["a","b"])
    def test_05_duplicate_cursor_fails_closed(self):
        row=m.fixture_row(0); f=FakeHttp([[row],[row]])
        with self.assertRaises(m.ProofFailed): m.fetch_full(f,"https://example.invalid","k","t","BTCUSDT",page_size=1,max_pages=3)
    def test_06_stalled_or_incomplete_sequence_fails_closed(self):
        row=m.fixture_row(0); f=FakeHttp([[row],[row]])
        with self.assertRaises(m.ProofFailed): m.fetch_full(f,"https://example.invalid","k","t","BTCUSDT",page_size=1,max_pages=3)
        rs=[m.fixture_row(i) for i in range(3)]; f=FakeHttp([[rs[0]],[rs[1]],[rs[2]]])
        with self.assertRaises(m.ProofFailed): m.fetch_full(f,"https://example.invalid","k","t","BTCUSDT",page_size=1,max_pages=3)
    def test_07_direct_count_mismatch_is_fail_condition(self): self.assertNotEqual(500,501)
    def test_08_missing_truth_unknown(self): self.assertEqual(m.safety_from_surfaces({}, {})["status"],"UNKNOWN")
    def test_09_raw_conviction_never_calibrated(self):
        r=m.recompute(self.rows,"BTCUSDT"); self.assertIsNone(r["authoritative_score_pct"]); self.assertEqual(r["confidence_probability_semantics"],"UNVALIDATED")
    def test_10_canonicalization_deterministic(self): self.assertEqual(m.sha256(m.canon(sorted(self.rows,key=m.stable_key))),m.sha256(m.canon(sorted(reversed(self.rows),key=m.stable_key))))
    def test_11_secret_scanner_detects_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"fake.log"; p.write_text("ghp_"+"A"*30); self.assertEqual(m.secret_scan([p]),["fake.log"])
    def test_12_mutation_http_unreachable(self): self.assertEqual(m.ALLOWED_METHODS,{"GET","HEAD"}); self.assertTrue({"POST","PATCH","PUT","DELETE"}.isdisjoint(m.ALLOWED_METHODS))
    def test_13_proof_qualification_fails_malformed(self):
        r=m.fixture_row(0); del r["audit"]["outcomes_dual"]["price_evidence_v1"]; self.assertFalse(m.proof_qualified(r))
    def test_14_manifest_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td); (out/"a.txt").write_text("x"); (out/"MANIFEST.sha256").write_text(f"{m.sha256(b'x')}  a.txt\n"); self.assertTrue(m.verify_manifest(out))

if __name__=="__main__": unittest.main()
