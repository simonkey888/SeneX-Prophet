from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "senecio_polymarket"

# The generated test block is inserted from a raw string; normalize literal escape sequences.
t = APP / "tests/test_senex_score_002.py"
s = t.read_text(encoding="utf-8")
if "\\n\\nclass AuthoritativeBoundaryTests" in s:
    s = s.replace("\\n", "\n")
t.write_text(s, encoding="utf-8")

# Remove accidental duplicate qualification guard from the main research-return path.
p = APP / "backend/main.py"
s = p.read_text(encoding="utf-8")
dup = '''        if not is_proof_qualified(rec):\n            continue\n        if not is_proof_qualified(rec):\n            continue\n'''
s = s.replace(dup, '''        if not is_proof_qualified(rec):\n            continue\n''', 1)
p.write_text(s, encoding="utf-8")

# Top-level legacy connector was outside the original commit staging scope.
for p in [ROOT / "polymarket/polymarket_connector.py", APP / "oracle/oracle_verifier.py"]:
    if not p.exists():
        continue
    s = p.read_text(encoding="utf-8")
    s = re.sub(r'os\.environ\.get\(\s*"SUPABASE_URL"\s*,\s*"[^"]+"\s*\)', 'os.environ.get("SUPABASE_URL")', s)
    s = re.sub(r'os\.environ\.get\(\s*"SUPABASE_KEY"\s*,\s*"[^"]+"\s*\)', 'os.environ.get("SUPABASE_KEY")', s)
    p.write_text(s, encoding="utf-8")

print("SCORE-002 POST2 NORMALIZATION COMPLETE")
