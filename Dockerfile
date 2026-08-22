# SENEX ORDER-070 — canonical production Dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY senecio_polymarket/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

ARG SENEX_SOURCE_COMMIT=unknown
ARG SENEX_SOURCE_TREE=unknown
ARG SENEX_IMAGE_DIGEST=unknown
ARG SENEX_BUILD_DIGEST=unknown
ENV SENEX_SOURCE_COMMIT=${SENEX_SOURCE_COMMIT}
ENV SENEX_SOURCE_TREE=${SENEX_SOURCE_TREE}
ENV SENEX_IMAGE_DIGEST=${SENEX_IMAGE_DIGEST}
ENV SENEX_BUILD_DIGEST=${SENEX_BUILD_DIGEST}
LABEL org.opencontainers.image.revision=${SENEX_SOURCE_COMMIT} \
      org.senex.source-tree=${SENEX_SOURCE_TREE} \
      org.senex.build-digest=${SENEX_BUILD_DIGEST}

COPY senecio_polymarket/backend ./backend
COPY senecio_polymarket/frontend ./frontend
COPY senecio_polymarket/oracle ./oracle
COPY senecio_polymarket/oracle_runtime ./oracle_runtime
COPY senecio_polymarket/start_single_authority.sh ./start_single_authority.sh
COPY senecio_polymarket/start_single_authority.sh /app/start.sh
COPY senecio_polymarket/start_single_authority.sh /start.sh

RUN addgroup --system --gid 10001 senex \
    && adduser --system --uid 10001 --ingroup senex --home /app --no-create-home senex \
    && mkdir -p /app/data/audit /app/oracle/senecio_output \
    && chown -R senex:senex /app/data /app/oracle/senecio_output \
    && chmod -R u=rwX,g=rX,o= /app/data /app/oracle/senecio_output \
    && chmod 0555 /app/start_single_authority.sh /app/start.sh /start.sh

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD curl -fsS http://localhost:8080/healthz || exit 1
USER senex:senex
CMD ["/app/start_single_authority.sh"]
