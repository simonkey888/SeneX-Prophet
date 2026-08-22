from pathlib import Path

p = Path('senecio_polymarket/tests/test_order_070.py')
s = p.read_text()
old = '''        docker=(ROOT/"senecio_polymarket/Dockerfile").read_text()\n        self.assertIn("--require-hashes -r requirements.lock",docker)\n'''
new = '''        docker=(ROOT/"Dockerfile").read_text()\n        self.assertFalse((ROOT/"senecio_polymarket/Dockerfile").exists())\n        self.assertIn("--require-hashes -r requirements.lock",docker)\n        self.assertIn("USER senex:senex",docker)\n        self.assertNotIn("chmod -R 777",docker)\n        self.assertNotIn("mv /app/oracle/predict_only.py",docker)\n        self.assertNotIn("cp /app/oracle_runtime/predict_only.py",docker)\n'''
if s.count(old) != 1:
    raise RuntimeError(f'historical dependency test drifted: {s.count(old)} matches')
p.write_text(s.replace(old,new,1))
print('HISTORICAL_BUILD_TEST_ALIGNED_TO_SUPERSEDING_F3=PASS')
