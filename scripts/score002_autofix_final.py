from pathlib import Path
import re

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "senecio_polymarket"


def replace_if_needed(path: Path, old: str, new: str):
    s = path.read_text(encoding="utf-8")
    if old in s:
        path.write_text(s.replace(old, new, 1), encoding="utf-8")

# Central proof qualification helpers.
p = APP / "backend/settlement_proof.py"
s = p.read_text(encoding="utf-8")
if "def filter_proof_qualified" not in s:
    marker = 'def proof_status(row: dict[str, Any]) -> str:\n'
    insert = '''def filter_proof_qualified(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:\n    """Return only rows satisfying the complete authoritative proof contract."""\n    return [row for row in rows if is_proof_qualified(row)]\n\n\ndef score_qualified_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:\n    """Compute the authoritative score from proof-qualified rows only."""\n    verified = filter_proof_qualified(rows)\n    wins = sum(1 for row in verified if row.get("outcome") == "WIN")\n    losses = sum(1 for row in verified if row.get("outcome") == "LOSS")\n    return {\n        "rows": verified,\n        "verified": len(verified),\n        "wins": wins,\n        "losses": losses,\n        "win_rate_pct": (wins / len(verified) * 100) if verified else 0.0,\n    }\n\n\n'''
    if marker not in s:
        raise RuntimeError("settlement_proof marker missing")
    p.write_text(s.replace(marker, insert + marker, 1), encoding="utf-8")

# Authoritative score/live-gate/research returns.
p = APP / "backend/main.py"
s = p.read_text(encoding="utf-8")
if "from .settlement_proof import filter_proof_qualified" not in s:
    s = s.replace("from . import oracle_runner\n", "from . import oracle_runner\nfrom .settlement_proof import filter_proof_qualified, is_proof_qualified\n", 1)
s = s.replace('verified = [r for r in rows if r.get("outcome") in ("WIN", "LOSS")]', "verified = filter_proof_qualified(rows)")
s = s.replace('''        outcome = (rec.get("outcome") or "").upper()\n        if outcome in ("WIN", "CORRECT"):\n''', '''        if not is_proof_qualified(rec):\n            continue\n        outcome = (rec.get("outcome") or "").upper()\n        if outcome in ("WIN", "CORRECT"):\n''', 1)
p.write_text(s, encoding="utf-8")

# Prediction-time origin proof and directional-stat qualification.
p = APP / "backend/oracle_runner.py"
s = p.read_text(encoding="utf-8")
if "from .settlement_proof import is_proof_qualified" not in s:
    s = s.replace("from typing import Any, Optional\n", "from typing import Any, Optional\n\nfrom .settlement_proof import is_proof_qualified\n", 1)
anchor = '''        if "_audit" in prediction:\n            prediction["_audit"]["exchange_used"] = exchange_used\n\n'''
origin = '''        if "_audit" in prediction:\n            prediction["_audit"]["exchange_used"] = exchange_used\n\n        # SCORE-002: immutable origin witness captured at prediction creation.\n        audit = prediction.setdefault("_audit", {})\n        if not isinstance(audit, dict):\n            audit = {}\n            prediction["_audit"] = audit\n        audit["origin_price_v1"] = {\n            "version": "origin-price-v1",\n            "price": float(prediction.get("price_now") or 0),\n            "timestamp": prediction.get("timestamp"),\n            "source": exchange_used,\n        }\n\n'''
if '"origin-price-v1"' not in s:
    if s.count(anchor) != 1:
        raise RuntimeError("oracle_runner origin anchor missing")
    s = s.replace(anchor, origin, 1)
s = s.replace('''        if direction not in ("LONG", "SHORT", "FLAT"):\n            continue\n        # 1h outcome = primary outcome column\n''', '''        if direction not in ("LONG", "SHORT", "FLAT"):\n            continue\n        if not is_proof_qualified(r):\n            continue\n        # 1h outcome = primary outcome column\n''', 1)
s = s.replace('''    _state["directional_stats"]["by_window"] = by_window\n\n    # Apply gates''', '''    _state["directional_stats"]["by_window"] = by_window\n    _state["verified_total"] = by_window["1h"]["global"]["verified"]\n\n    # Apply gates''', 1)
p.write_text(s, encoding="utf-8")

# Forensics and research matrix boundaries.
p = APP / "backend/forensics/pipeline.py"
s = p.read_text(encoding="utf-8")
if "from ..settlement_proof import is_proof_qualified" not in s:
    s = s.replace("from typing import Any, Optional\n", "from typing import Any, Optional\n\nfrom ..settlement_proof import is_proof_qualified\n", 1)
s = s.replace('if r.get("outcome") in ("WIN", "LOSS"):', "if is_proof_qualified(r):", 1)
p.write_text(s, encoding="utf-8")

p = APP / "backend/research/coordinator.py"
s = p.read_text(encoding="utf-8")
if "from ..settlement_proof import is_proof_qualified" not in s:
    s = s.replace("from .observability import get_registry, timed\n", "from .observability import get_registry, timed\nfrom ..settlement_proof import is_proof_qualified\n", 1)
s = s.replace('''            outcome = (rec.get("outcome") or "").upper()\n            if outcome not in ("WIN", "LOSS"):\n                continue\n''', '''            if not is_proof_qualified(rec):\n                continue\n            outcome = (rec.get("outcome") or "").upper()\n''', 1)
p.write_text(s, encoding="utf-8")

# Reconciler: repair evidence only; never overwrite WIN/LOSS; conditional PATCH; conflict marker.
p = APP / "backend/settlement_reconciler.py"
s = p.read_text(encoding="utf-8")
s = s.replace("total_scanned = repaired = skipped = errors = 0", "total_scanned = repaired = skipped = errors = conflicts = 0", 1)
if 'patch = {"outcome": o1h' in s:
    pattern = re.compile(r'''                    audit\["outcomes_dual"\] = \{.*?\n                    else:\n                        errors \+= 1\n                        log\.error\("reconcile update failed id=%s status=%s body=%r", row\["id"\], pr\.status_code, body\)''', re.S)
    replacement = '''                    stored_outcome = row.get("outcome")\n                    conflict = stored_outcome != o1h\n                    audit["outcomes_dual"] = {\n                        "outcome_15m": o15,\n                        "outcome_1h": o1h,\n                        "price_15m_later": p15,\n                        "price_1h_later": p1h,\n                        "primary_window": "1h",\n                        "reconciled_by": "SENEX-SCORE-002",\n                        "reconciled_at": datetime.now(timezone.utc).isoformat(),\n                    }\n                    if conflict:\n                        audit["reconciliation_conflict"] = {\n                            "stored_outcome": stored_outcome,\n                            "computed_outcome_1h": o1h,\n                            "detected_at": datetime.now(timezone.utc).isoformat(),\n                            "action": "NO_OUTCOME_OVERWRITE",\n                        }\n                    else:\n                        audit.pop("reconciliation_conflict", None)\n\n                    patch = {"price_15m_later": p15, "audit": audit}\n                    pr = await client.patch(\n                        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",\n                        params={\n                            "id": f"eq.{row['id']}",\n                            "outcome": f"eq.{stored_outcome}",\n                            "audit->outcomes_dual": "is.null",\n                        },\n                        json=patch,\n                    )\n                    try:\n                        body = pr.json() if pr.content else []\n                    except Exception:\n                        body = []\n                    if pr.status_code in (200, 204) and isinstance(body, list) and body:\n                        if conflict:\n                            conflicts += 1\n                            log.warning("reconciliation conflict id=%s stored=%s computed_1h=%s; outcome unchanged", row["id"], stored_outcome, o1h)\n                        else:\n                            repaired += 1\n                            log.info("reconciled evidence id=%s stored=%s dual15=%s dual1h=%s", row["id"], stored_outcome, o15, o1h)\n                    else:\n                        errors += 1\n                        log.error("reconcile update failed/no-op id=%s status=%s body=%r", row["id"], pr.status_code, body)'''
    s2, n = pattern.subn(replacement, s, count=1)
    if n != 1:
        raise RuntimeError("reconciler outcome-writing PATCH not found")
    s = s2
s = s.replace('result = {"scanned": total_scanned, "repaired": repaired, "skipped": skipped, "errors": errors}', 'result = {"scanned": total_scanned, "repaired": repaired, "skipped": skipped, "errors": errors, "conflicts": conflicts}', 1)
p.write_text(s, encoding="utf-8")

# Remove credential fallbacks repo-wide.
for p in REPO.rglob("*.py"):
    if ".git" in p.parts:
        continue
    s = p.read_text(encoding="utf-8")
    s = re.sub(r'os\.environ\.get\(\s*"SUPABASE_URL"\s*,\s*"[^"]+"\s*\)', 'os.environ.get("SUPABASE_URL")', s)
    s = re.sub(r'os\.environ\.get\(\s*"SUPABASE_KEY"\s*,\s*"[^"]+"\s*\)', 'os.environ.get("SUPABASE_KEY")', s)
    p.write_text(s, encoding="utf-8")

# Deterministic SCORE-002 test expansion.
p = APP / "tests/test_senex_score_002.py"
s = p.read_text(encoding="utf-8")
s = s.replace("from backend.settlement_proof import is_proof_qualified, proof_status", "from backend.settlement_proof import filter_proof_qualified, is_proof_qualified, proof_status, score_qualified_rows", 1)
s = s.replace('def run_reconcile(rows, patch_status=200):', 'def run_reconcile(rows, patch_status=200, patch_body=None):', 1)
s = s.replace('client = FakeClient(rows, patch_status=patch_status)', 'client = FakeClient(rows, patch_status=patch_status, patch_body=patch_body)', 1)
s = s.replace('self.assertEqual(patch["outcome"], "WIN")', 'self.assertNotIn("outcome", patch)\n        self.assertEqual(client.patch_calls[0][1]["audit->outcomes_dual"], "is.null")\n        self.assertEqual(client.patch_calls[0][1]["outcome"], "eq.WIN")', 1)
marker = '\n\nif __name__ == "__main__":\n'
extra = r'''\n\nclass AuthoritativeBoundaryTests(unittest.TestCase):\n    def qualified(self):\n        return {"ts": TS, "prediction": "LONG", "outcome": "WIN", "audit": {"origin_price_v1": {"version": "origin-price-v1", "price": 100, "timestamp": TS, "source": "okx"}, "outcomes_dual": {"outcome_15m": "WIN", "outcome_1h": "WIN", "price_15m_later": 101, "price_1h_later": 102, "primary_window": "1h"}}}\n\n    def test_scorer_counts_only_proof_qualified(self):\n        qualified = self.qualified()\n        raw = {"ts": TS, "prediction": "LONG", "outcome": "WIN", "audit": None}\n        dual_only = {"ts": TS, "prediction": "LONG", "outcome": "LOSS", "audit": {"outcomes_dual": {"outcome_15m": "LOSS", "outcome_1h": "LOSS", "price_15m_later": 99, "price_1h_later": 98, "primary_window": "1h"}}}\n        result = score_qualified_rows([qualified, raw, dual_only])\n        self.assertEqual(result["verified"], 1)\n        self.assertEqual(result["wins"], 1)\n        self.assertEqual(result["losses"], 0)\n        self.assertEqual(result["win_rate_pct"], 100.0)\n        self.assertEqual(len(filter_proof_qualified([qualified, raw, dual_only])), 1)\n\n    def test_missing_origin_proof_is_raw_unverified(self):\n        row = self.qualified()\n        row["audit"].pop("origin_price_v1")\n        self.assertFalse(is_proof_qualified(row))\n        self.assertEqual(proof_status(row), "RAW_UNVERIFIED")\n\n    def test_conflicting_1h_evidence_never_overwrites_outcome(self):\n        rows = [{"id": 6, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"}]\n        client = FakeClient(rows)\n        old = reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT\n        reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT = "https://example.supabase.co", "test-key", 50\n        try:\n            with patch.object(reconciler.httpx, "AsyncClient", return_value=client), patch.object(reconciler.ccxt, "okx", return_value=FakeExchange()), patch.object(reconciler, "_price_at", side_effect=lambda *args: 101.0 if args[-1] == reconciler.WINDOW_15M_S else 99.0):\n                result = asyncio.run(reconciler.reconcile_once())\n        finally:\n            reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT = old\n        self.assertEqual(result["repaired"], 0)\n        self.assertEqual(result["conflicts"], 1)\n        payload = client.patch_calls[0][2]\n        self.assertNotIn("outcome", payload)\n        self.assertEqual(payload["audit"]["reconciliation_conflict"]["action"], "NO_OUTCOME_OVERWRITE")\n\n    def test_race_conditional_patch_noop_is_not_repaired(self):\n        rows = [{"id": 7, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"}]\n        result, client = run_reconcile(rows, patch_status=200, patch_body=[])\n        self.assertEqual(result["repaired"], 0)\n        self.assertEqual(result["errors"], 1)\n        self.assertEqual(client.patch_calls[0][1]["audit->outcomes_dual"], "is.null")\n        self.assertEqual(client.patch_calls[0][1]["outcome"], "eq.WIN")\n\n    def test_patch_failure_retries_next_cycle(self):\n        rows = [{"id": 8, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"}]\n        first, first_client = run_reconcile(rows, patch_status=500)\n        second, second_client = run_reconcile(rows, patch_status=500)\n        self.assertEqual(first["repaired"], 0)\n        self.assertEqual(second["repaired"], 0)\n        self.assertEqual(first["errors"], 1)\n        self.assertEqual(second["errors"], 1)\n        self.assertEqual(len(first_client.patch_calls), 1)\n        self.assertEqual(len(second_client.patch_calls), 1)\n\n    def test_multi_batch_repairs_all_eligible_rows(self):\n        rows = [{"id": i, "ts": f"2026-08-10T00:{i:02d}:00+00:00", "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"} for i in range(51)]\n        pages = [rows[:50], rows[50:]]\n        class PagedClient(FakeClient):\n            def __init__(self, pages):\n                super().__init__([], patch_status=200)\n                self.pages, self.page_index = pages, 0\n            async def get(self, url, params=None):\n                self.get_calls.append((url, dict(params or {})))\n                page = self.pages[self.page_index] if self.page_index < len(self.pages) else []\n                self.page_index += 1\n                return FakeResponse(200, list(page))\n        client = PagedClient(pages)\n        old = reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT\n        reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT = "https://example.supabase.co", "test-key", 50\n        try:\n            with patch.object(reconciler.httpx, "AsyncClient", return_value=client), patch.object(reconciler.ccxt, "okx", return_value=FakeExchange()), patch.object(reconciler, "_price_at", return_value=101.0):\n                result = asyncio.run(reconciler.reconcile_once())\n        finally:\n            reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT = old\n        self.assertEqual(result["repaired"], 51)\n        self.assertEqual(len(client.get_calls), 3)\n\n'''
if "class AuthoritativeBoundaryTests" not in s:
    s = s.replace(marker, extra + marker, 1)
p.write_text(s, encoding="utf-8")

# Final hard credential invariant.
for p in REPO.rglob("*.py"):
    if ".git" in p.parts:
        continue
    text = p.read_text(encoding="utf-8")
    if "supabase.co" in text or "sb_publishable_" in text:
        raise RuntimeError(f"hardcoded Supabase credential remains: {p}")

print("SCORE-002 FINAL AUTOFIX COMPLETE")
