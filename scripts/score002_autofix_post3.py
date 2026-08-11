from pathlib import Path
import re

p = Path(__file__).resolve().parents[1] / "senecio_polymarket/backend/main.py"
s = p.read_text(encoding="utf-8")
block = r'(?:        if not is_proof_qualified\(rec\):\n            continue\n)+'
s = re.sub(block, '        if not is_proof_qualified(rec):\n            continue\n', s)
p.write_text(s, encoding="utf-8")
print("SCORE-002 POST3 NORMALIZATION COMPLETE")
