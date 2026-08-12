#!/bin/sh
# SENECIO ORACLE — SENEX-SCORE-002
# Production settlement authority remains backend.oracle_runner.
# settlement_reconciler is repair-only and never settles NULL rows.
set -u

HEARTBEAT_FILE="${SENEX_RECONCILER_HEARTBEAT_FILE:-/tmp/senex-reconciler-heartbeat}"
HEALTH_GRACE_S="${SENEX_RECONCILER_HEALTH_GRACE_SEC:-120}"
HEALTH_STALE_S="${SENEX_RECONCILER_HEALTH_STALE_SEC:-1200}"

# Portable integer validation: invalid operator overrides fail closed.
case "$HEALTH_GRACE_S" in ''|*[!0-9]*) echo "[start_single_authority.sh] FATAL: invalid SENEX_RECONCILER_HEALTH_GRACE_SEC" >&2; exit 78;; esac
case "$HEALTH_STALE_S" in ''|*[!0-9]*) echo "[start_single_authority.sh] FATAL: invalid SENEX_RECONCILER_HEALTH_STALE_SEC" >&2; exit 78;; esac

if [ -z "${SUPABASE_URL:-}" ] || [ -z "${SUPABASE_KEY:-}" ]; then
  echo "[start_single_authority.sh] FATAL: SUPABASE_URL and SUPABASE_KEY are required" >&2
  exit 78
fi

rm -f "$HEARTBEAT_FILE"

echo "[start_single_authority.sh] launching settlement authority + reconciliation guard..."

start_reconciler() {
  echo "[start_single_authority.sh] launching reconciliation guard..."
  python -m backend.settlement_reconciler &
  RECONCILER_PID=$!
  RECONCILER_STARTED_AT=$(date +%s)
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
    echo "[start_single_authority.sh] uvicorn exited — shutting down container" >&2
    break
  fi

  NOW=$(date +%s)
  if ! kill -0 "$RECONCILER_PID" 2>/dev/null; then
    echo "[start_single_authority.sh] reconciler exited — restarting repair guard" >&2
    start_reconciler
  fi

  # PID liveness alone is insufficient: require a recent completed reconcile cycle.
  if [ -f "$HEARTBEAT_FILE" ]; then
    HEARTBEAT_MTIME=$(python -c 'import os,sys; print(int(os.path.getmtime(sys.argv[1])))' "$HEARTBEAT_FILE" 2>/dev/null || echo 0)
    AGE=$((NOW - HEARTBEAT_MTIME))
    if [ "$AGE" -gt "$HEALTH_STALE_S" ]; then
      echo "[start_single_authority.sh] FATAL: reconciler heartbeat stale age=${AGE}s limit=${HEALTH_STALE_S}s" >&2
      exit 1
    fi
  else
    START_AGE=$((NOW - RECONCILER_STARTED_AT))
    if [ "$START_AGE" -gt "$HEALTH_GRACE_S" ]; then
      echo "[start_single_authority.sh] FATAL: reconciler produced no heartbeat within ${HEALTH_GRACE_S}s" >&2
      exit 1
    fi
  fi

  sleep 2
done
exit 1
