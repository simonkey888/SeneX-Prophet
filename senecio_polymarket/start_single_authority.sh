#!/bin/sh
# SENECIO ORACLE — SENEX-SCORE-002
# Production settlement authority remains backend.oracle_runner.
# settlement_reconciler is repair-only and never settles NULL rows.
set -u

echo "[start_single_authority.sh] launching settlement authority + reconciliation guard..."

if [ -z "${SUPABASE_URL:-}" ] || [ -z "${SUPABASE_KEY:-}" ]; then
  echo "[start_single_authority.sh] FATAL: SUPABASE_URL and SUPABASE_KEY are required" >&2
  exit 78
fi

start_reconciler() {
  echo "[start_single_authority.sh] launching reconciliation guard..."
  python -m backend.settlement_reconciler &
  RECONCILER_PID=$!
}

start_reconciler

uvicorn backend.main:app \
  --host 0.0.0.0 \
  --port 8080 \
  --workers 1 \
  --log-level info \
  --no-access-log &
UVICORN_PID=$!

cleanup() {
  echo "[start_single_authority.sh] cleanup: stopping reconciler (${RECONCILER_PID:-none}) and uvicorn (${UVICORN_PID:-none})"
  kill -TERM "${RECONCILER_PID:-}" 2>/dev/null || true
  kill -TERM "${UVICORN_PID:-}" 2>/dev/null || true
  wait "${RECONCILER_PID:-}" 2>/dev/null || true
  wait "${UVICORN_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

while true; do
  if ! kill -0 "$UVICORN_PID" 2>/dev/null; then
    echo "[start_single_authority.sh] uvicorn exited — shutting down container"
    break
  fi

  if ! kill -0 "$RECONCILER_PID" 2>/dev/null; then
    echo "[start_single_authority.sh] reconciler exited — restarting repair guard in 5s"
    start_reconciler
  fi

  sleep 1
done
exit 1
