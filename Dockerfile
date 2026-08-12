# SENECIO ORACLE — Root Dockerfile for Fly.io
# This file lives at repo root so Fly auto-detects it (no [build] section needed in fly.toml).
# Build context = repo root, so COPY paths reference senecio_polymarket/ subfolder.

FROM python:3.11-slim

# Minimal system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy ONLY senecio_polymarket requirements (NOT the root requirements.txt which is for V4)
COPY senecio_polymarket/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend + frontend + the production single-authority launcher
COPY senecio_polymarket/backend ./backend
COPY senecio_polymarket/frontend ./frontend
COPY senecio_polymarket/start_single_authority.sh ./start_single_authority.sh
RUN chmod +x /app/start_single_authority.sh

# Ensure data dir exists (audit JSONL writes here)
RUN mkdir -p /app/data/audit && chmod -R 777 /app/data

# Python optimizations for low-memory container
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8080

# Backup healthcheck (Fly's primary check is in fly.toml)
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD curl -fsS http://localhost:8080/api/health || exit 1

# Single production entrypoint: oracle service + repair-only reconciler
CMD ["/app/start_single_authority.sh"]
