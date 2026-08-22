from pathlib import Path

# Preserve the historical launcher contract without reintroducing predictor overlay.
p = Path('Dockerfile')
s = p.read_text()
old = '''COPY senecio_polymarket/start_single_authority.sh /app/start.sh\nCOPY senecio_polymarket/start_single_authority.sh /start.sh\n'''
new = '''COPY senecio_polymarket/start_single_authority.sh ./start_single_authority.sh\nCOPY senecio_polymarket/start_single_authority.sh /app/start.sh\nCOPY senecio_polymarket/start_single_authority.sh /start.sh\n'''
if s.count(old) != 1:
    raise RuntimeError(f'root launcher block drifted: {s.count(old)}')
s = s.replace(old, new, 1)
s = s.replace('    && chmod 0555 /app/start.sh /start.sh\n', '    && chmod 0555 /app/start_single_authority.sh /app/start.sh /start.sh\n', 1)
s = s.replace('CMD ["./start.sh"]\n', 'CMD ["/app/start_single_authority.sh"]\n', 1)
p.write_text(s)

# The old learning test asserted the now-forbidden build-time mv/cp overlay.
p = Path('senecio_polymarket/tests/test_authoritative_learning.py')
s = p.read_text()
old = '''    def test_dockerfile_installs_bridge_at_historical_predictor_path(self):\n        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")\n        self.assertIn("mv /app/oracle/predict_only.py /app/oracle/predict_only_base.py", dockerfile)\n        self.assertIn("cp /app/oracle_runtime/predict_only.py /app/oracle/predict_only.py", dockerfile)\n'''
new = '''    def test_canonical_root_dockerfile_preserves_frozen_predictor_without_overlay(self):\n        root = Path(__file__).resolve().parents[2]\n        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")\n        runner = (root / "senecio_polymarket/backend/oracle_runner.py").read_text(encoding="utf-8")\n        self.assertFalse((root / "senecio_polymarket/Dockerfile").exists())\n        self.assertNotIn("mv /app/oracle/predict_only.py", dockerfile)\n        self.assertNotIn("cp /app/oracle_runtime/predict_only.py", dockerfile)\n        self.assertIn("from oracle_runtime.predict_only import", runner)\n'''
if s.count(old) != 1:
    raise RuntimeError(f'obsolete overlay test drifted: {s.count(old)}')
p.write_text(s.replace(old, new, 1))
print('REGRESSION_CONTRACTS_ALIGNED_WITH_F3=PASS')
