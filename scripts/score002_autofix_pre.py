from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
test = ROOT / "senecio_polymarket/tests/test_senex_score_002.py"
s = test.read_text(encoding="utf-8")
s = s.replace("https://example.supabase.co", "https://example.invalid")
test.write_text(s, encoding="utf-8")
