#!/bin/sh
# SENECIO ORACLE — FIX SENEX-SCORE-001
# Production launcher with exactly one settlement authority:
# backend.oracle_runner (1h primary + 15m/1h dual-window evidence).
# The legacy oracle_verifier.py is intentionally not started.
set -u

echo "[start_single_authority.sh] launching uvicorn (oracle_runner owns settlement)..."
uvicorn backend.main:app \
  --host 0.0.0.0 \
  --port 8080 \
  --workers 1 \
  --log-level info \
  --no-access-log &
UVICORN_PID=$!

cleanup() {
  echo "[start_single_authority.sh] cleanup: stopping uvicorn ($UVICORN_PID)"
  kill -TERM "$UVICORN_PID" 2>/dev/null || true
  wait "$UVICORN_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

while true; do
  if ! kill -0 "$UVICORN_PID" 2>/dev/null; then
    echo "[start_single_authority.sh] uvicorn exited — shutting down container"
    break
  fi
  sleep 1
done
exit 1
