# SENECIO ORACLE — Root Dockerfile for Fly.io
# Build context = repo root. The production launcher is the single-authority
# supervisor: reconciler + uvicorn/oracle_runner.

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY senecio_polymarket/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY senecio_polymarket/backend ./backend
COPY senecio_polymarket/frontend ./frontend
COPY senecio_polymarket/oracle ./oracle
COPY senecio_polymarket/start_single_authority.sh ./start.sh

RUN chmod +x /app/start.sh \
    && mkdir -p /app/data/audit \
    && chmod -R 777 /app/data

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD curl -fsS http://localhost:8080/api/health || exit 1

CMD ["./start.sh"]
