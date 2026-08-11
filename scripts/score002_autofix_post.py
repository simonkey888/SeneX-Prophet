from pathlib import Path
import re

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "senecio_polymarket"

# Test fixture must not look like a real service credential/domain.
t = APP / "tests/test_senex_score_002.py"
ts = t.read_text(encoding="utf-8").replace("https://example.supabase.co", "https://example.invalid")

def add_once(text, old, new):
    return text.replace(old, new, 1) if old in text and new not in text else text

# Proof helpers.
p = APP / "backend/settlement_proof.py"
s = p.read_text(encoding="utf-8")
if "def filter_proof_qualified" not in s:
    marker = 'def proof_status(row: dict[str, Any]) -> str:\n'
    insert = '''def filter_proof_qualified(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:\n    return [row for row in rows if is_proof_qualified(row)]\n\n\ndef score_qualified_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:\n    verified = filter_proof_qualified(rows)\n    wins = sum(1 for row in verified if row.get("outcome") == "WIN")\n    losses = sum(1 for row in verified if row.get("outcome") == "LOSS")\n    return {"rows": verified, "verified": len(verified), "wins": wins, "losses": losses, "win_rate_pct": (wins / len(verified) * 100) if verified else 0.0}\n\n\n'''
    s = s.replace(marker, insert + marker, 1)
p.write_text(s, encoding="utf-8")

# Main score/gates.
p = APP / "backend/main.py"
s = p.read_text(encoding="utf-8")
if "from .settlement_proof import filter_proof_qualified" not in s:
    s = s.replace("from . import oracle_runner\n", "from . import oracle_runner\nfrom .settlement_proof import filter_proof_qualified, is_proof_qualified\n", 1)
s = s.replace('verified = [r for r in rows if r.get("outcome") in ("WIN", "LOSS")]', "verified = filter_proof_qualified(rows)")
s = s.replace('''        outcome = (rec.get("outcome") or "").upper()\n        if outcome in ("WIN", "CORRECT"):\n''', '''        if not is_proof_qualified(rec):\n            continue\n        outcome = (rec.get("outcome") or "").upper()\n        if outcome in ("WIN", "CORRECT"):\n''', 1)
p.write_text(s, encoding="utf-8")

# Oracle producer.
p = APP / "backend/oracle_runner.py"
s = p.read_text(encoding="utf-8")
if "from .settlement_proof import is_proof_qualified" not in s:
    s = s.replace("from typing import Any, Optional\n", "from typing import Any, Optional\n\nfrom .settlement_proof import is_proof_qualified\n", 1)
if '"origin-price-v1"' not in s:
    anchor = '''        if "_audit" in prediction:\n            prediction["_audit"]["exchange_used"] = exchange_used\n\n'''
    origin = '''        if "_audit" in prediction:\n            prediction["_audit"]["exchange_used"] = exchange_used\n\n        audit = prediction.setdefault("_audit", {})\n        if not isinstance(audit, dict):\n            audit = {}\n            prediction["_audit"] = audit\n        audit["origin_price_v1"] = {"version": "origin-price-v1", "price": float(prediction.get("price_now") or 0), "timestamp": prediction.get("timestamp"), "source": exchange_used}\n\n'''
    s = s.replace(anchor, origin, 1)
s = s.replace('''        if direction not in ("LONG", "SHORT", "FLAT"):\n            continue\n        # 1h outcome = primary outcome column\n''', '''        if direction not in ("LONG", "SHORT", "FLAT"):\n            continue\n        if not is_proof_qualified(r):\n            continue\n        # 1h outcome = primary outcome column\n''', 1)
s = s.replace('''    _state["directional_stats"]["by_window"] = by_window\n\n    # Apply gates''', '''    _state["directional_stats"]["by_window"] = by_window\n    _state["verified_total"] = by_window["1h"]["global"]["verified"]\n\n    # Apply gates''', 1)
p.write_text(s, encoding="utf-8")

# Forensics/research.
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

# Reconciler.
p = APP / "backend/settlement_reconciler.py"
s = p.read_text(encoding="utf-8")
s = s.replace("total_scanned = repaired = skipped = errors = 0", "total_scanned = repaired = skipped = errors = conflicts = 0", 1)
if '"reconciliation_conflict"' not in s:
    s = s.replace('''                    audit["outcomes_dual"] = {\n''', '''                    stored_outcome = row.get("outcome")\n                    conflict = stored_outcome != o1h\n                    if conflict:\n                        audit["reconciliation_conflict"] = {"stored_outcome": stored_outcome, "computed_outcome_1h": o1h, "detected_at": datetime.now(timezone.utc).isoformat(), "action": "NO_OUTCOME_OVERWRITE"}\n                    else:\n                        audit.pop("reconciliation_conflict", None)\n                    audit["outcomes_dual"] = {\n''', 1)
s = s.replace('patch = {"outcome": o1h, "price_15m_later": p15, "audit": audit}', 'patch = {"price_15m_later": p15, "audit": audit}', 1)
s = s.replace('params={"id": f"eq.{row[\'id\']}"},', 'params={"id": f"eq.{row[\'id\']}", "outcome": f"eq.{stored_outcome}", "audit->outcomes_dual": "is.null"},', 1)
old = '''                    if pr.status_code in (200, 204) and isinstance(body, list) and body:\n                        repaired += 1\n                        log.info("reconciled id=%s primary=%s dual15=%s dual1h=%s", row["id"], o1h, o15, o1h)\n                    else:\n                        errors += 1\n                        log.error("reconcile update failed id=%s status=%s body=%r", row["id"], pr.status_code, body)'''
new = '''                    if pr.status_code in (200, 204) and isinstance(body, list) and body:\n                        if conflict:\n                            conflicts += 1\n                            log.warning("reconciliation conflict id=%s stored=%s computed_1h=%s; outcome unchanged", row["id"], stored_outcome, o1h)\n                        else:\n                            repaired += 1\n                            log.info("reconciled evidence id=%s stored=%s dual15=%s dual1h=%s", row["id"], stored_outcome, o15, o1h)\n                    else:\n                        errors += 1\n                        log.error("reconcile update failed/no-op id=%s status=%s body=%r", row["id"], pr.status_code, body)'''
s = s.replace(old, new, 1)
s = s.replace('result = {"scanned": total_scanned, "repaired": repaired, "skipped": skipped, "errors": errors}', 'result = {"scanned": total_scanned, "repaired": repaired, "skipped": skipped, "errors": errors, "conflicts": conflicts}', 1)
p.write_text(s, encoding="utf-8")

# Credentials repo-wide.
for p in REPO.rglob("*.py"):
    if ".git" in p.parts:
        continue
    text = p.read_text(encoding="utf-8")
    text = re.sub(r'os\.environ\.get\(\s*"SUPABASE_URL"\s*,\s*"[^"]+"\s*\)', 'os.environ.get("SUPABASE_URL")', text)
    text = re.sub(r'os\.environ\.get\(\s*"SUPABASE_KEY"\s*,\s*"[^"]+"\s*\)', 'os.environ.get("SUPABASE_KEY")', text)
    p.write_text(text, encoding="utf-8")

# Tests.
p = APP / "tests/test_senex_score_002.py"
s = p.read_text(encoding="utf-8")
s = s.replace("from backend.settlement_proof import is_proof_qualified, proof_status", "from backend.settlement_proof import filter_proof_qualified, is_proof_qualified, proof_status, score_qualified_rows", 1)
s = s.replace('def run_reconcile(rows, patch_status=200):', 'def run_reconcile(rows, patch_status=200, patch_body=None):', 1)
s = s.replace('client = FakeClient(rows, patch_status=patch_status)', 'client = FakeClient(rows, patch_status=patch_status, patch_body=patch_body)', 1)
s = s.replace('self.assertEqual(patch["outcome"], "WIN")', 'self.assertNotIn("outcome", patch)\n        self.assertEqual(client.patch_calls[0][1]["audit->outcomes_dual"], "is.null")\n        self.assertEqual(client.patch_calls[0][1]["outcome"], "eq.WIN")', 1)
marker = '\n\nif __name__ == "__main__":\n'
extra = r'''\n\nclass AuthoritativeBoundaryTests(unittest.TestCase):\n    def qualified(self):\n        return {"ts": TS, "prediction": "LONG", "outcome": "WIN", "audit": {"origin_price_v1": {"version": "origin-price-v1", "price": 100, "timestamp": TS, "source": "okx"}, "outcomes_dual": {"outcome_15m": "WIN", "outcome_1h": "WIN", "price_15m_later": 101, "price_1h_later": 102, "primary_window": "1h"}}}\n\n    def test_scorer_counts_only_proof_qualified(self):\n        q = self.qualified(); raw = {"ts": TS, "prediction": "LONG", "outcome": "WIN", "audit": None}; dual = {"ts": TS, "prediction": "LONG", "outcome": "LOSS", "audit": {"outcomes_dual": {"outcome_15m": "LOSS", "outcome_1h": "LOSS", "price_15m_later": 99, "price_1h_later": 98, "primary_window": "1h"}}}\n        score = score_qualified_rows([q, raw, dual])\n        self.assertEqual((score["verified"], score["wins"], score["losses"]), (1, 1, 0))\n        self.assertEqual(len(filter_proof_qualified([q, raw, dual])), 1)\n\n    def test_missing_origin_is_raw_unverified(self):\n        row = self.qualified(); row["audit"].pop("origin_price_v1")\n        self.assertFalse(is_proof_qualified(row)); self.assertEqual(proof_status(row), "RAW_UNVERIFIED")\n\n    def test_conflict_does_not_overwrite_outcome(self):\n        rows = [{"id": 6, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"}]\n        client = FakeClient(rows); old = reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT\n        reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT = "https://example.invalid", "test-key", 50\n        try:\n            with patch.object(reconciler.httpx, "AsyncClient", return_value=client), patch.object(reconciler.ccxt, "okx", return_value=FakeExchange()), patch.object(reconciler, "_price_at", side_effect=lambda *args: 101.0 if args[-1] == reconciler.WINDOW_15M_S else 99.0): result = asyncio.run(reconciler.reconcile_once())\n        finally: reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT = old\n        self.assertEqual(result["repaired"], 0); self.assertEqual(result["conflicts"], 1); self.assertNotIn("outcome", client.patch_calls[0][2])\n\n    def test_race_conditional_noop_is_not_repaired(self):\n        rows = [{"id": 7, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"}]\n        result, client = run_reconcile(rows, patch_status=200, patch_body=[])\n        self.assertEqual((result["repaired"], result["errors"]), (0, 1)); self.assertEqual(client.patch_calls[0][1]["audit->outcomes_dual"], "is.null")\n\n    def test_patch_failure_retries_next_cycle(self):\n        rows = [{"id": 8, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"}]\n        a, ca = run_reconcile(rows, patch_status=500); b, cb = run_reconcile(rows, patch_status=500)\n        self.assertEqual((a["repaired"], b["repaired"], a["errors"], b["errors"]), (0, 0, 1, 1)); self.assertEqual((len(ca.patch_calls), len(cb.patch_calls)), (1, 1))\n\n'''
if "class AuthoritativeBoundaryTests" not in s:
    s = s.replace(marker, extra + marker, 1)
p.write_text(s, encoding="utf-8")

print("SCORE-002 POST REPAIR COMPLETE")
