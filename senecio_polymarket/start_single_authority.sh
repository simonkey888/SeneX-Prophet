#!/bin/sh
# SENECIO ORACLE — SENEX-SCORE-002
# Production settlement authority remains backend.oracle_runner.
# settlement_reconciler is repair-only: it may repair already-populated
# WIN/LOSS rows that lack dual 15m/1h evidence, but it never settles NULL rows.
set -u

echo "[start_single_authority.sh] launching settlement authority + reconciliation guard..."
python -m backend.settlement_reconciler &
RECONCILER_PID=$!

uvicorn backend.main:app \
  --host 0.0.0.0 \
  --port 8080 \
  --workers 1 \
  --log-level info \
  --no-access-log &
UVICORN_PID=$!

cleanup() {
  echo "[start_single_authority.sh] cleanup: stopping reconciler ($RECONCILER_PID) and uvicorn ($UVICORN_PID)"
  kill -TERM "$RECONCILER_PID" 2>/dev/null || true
  kill -TERM "$UVICORN_PID" 2>/dev/null || true
  wait "$RECONCILER_PID" 2>/dev/null || true
  wait "$UVICORN_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

while true; do
  if ! kill -0 "$UVICORN_PID" 2>/dev/null; then
    echo "[start_single_authority.sh] uvicorn exited — shutting down container"
    break
  fi
  if ! kill -0 "$RECONCILER_PID" 2>/dev/null; then
    echo "[start_single_authority.sh] reconciler exited — shutting down container"
    break
  fi
  sleep 1
done
exit 1
