# SENECIO ORACLE — Dockerfile (FIX SENEX-SCORE-001)
# Builds a single image containing the dashboard and real oracle pipeline.
# Production settlement authority: backend/oracle_runner.py only.
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
COPY senecio_polymarket/oracle_runtime ./oracle_runtime

# Preserve the canonical predictor implementation and install a minimal runtime
# bridge at the historical import path. This is explicit because the original
# predict_only.py prepends /app/oracle to sys.path, so PYTHONPATH precedence
# alone cannot guarantee that the learning-aware institutional_core is used.
RUN mv /app/oracle/predict_only.py /app/oracle/predict_only_base.py \
    && cp /app/oracle_runtime/predict_only.py /app/oracle/predict_only.py

# Northflank customCommand="./start.sh" remains unchanged.
# Install the single-authority launcher at both expected runtime paths.
COPY senecio_polymarket/start_single_authority.sh /start.sh
COPY senecio_polymarket/start_single_authority.sh /app/start.sh
RUN chmod +x /start.sh /app/start.sh

RUN mkdir -p /app/data/audit && chmod -R 777 /app/data

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app:/app/oracle

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD curl -fsS http://localhost:8080/api/health || exit 1

CMD ["./start.sh"]