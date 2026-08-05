# SENEX production runbook

## Safety boundary

SENEX is permanently paper-only. Every deployment must expose and preserve:

- `paper_only=true`
- `orders_enabled=false`
- `live_capital_locked=true`
- no wallet or private-key material
- no authenticated trading endpoint
- no real-capital action

## Promotion gate

Production mutation is prohibited until an authenticated Northflank inventory identifies the exact project, service, active deployment/build, source SHA, routes, health configuration, environment variable names, persistent volume identity, mount path and backup surface. Secret values must never enter evidence.

Before deployment, create a backup of every attached persistent volume, restore it to an isolated target, verify byte-level or manifest-level equality, and record the rollback deployment/build and source SHA. Any unknown storage identity, durability or restore result is a hard stop.

## Deployment sequence

1. Confirm the delivery branch equals the AUD-accepted SHA.
2. Confirm backup, isolated restore and rollback evidence are PASS.
3. Build/deploy only the accepted source SHA.
4. Preserve the three safety flags and reject wallet/order variables.
5. Validate `/`, `/healthz`, `/api/v3/state`, `/api/v3/integrity` and `/api/v3/replay` from two independent clients.
6. Require the public runtime to report the deployed SHA exactly.
7. Require storage, raw-chain integrity and replay status to be truthful; degraded states must remain visible.
8. Start the evidence-bound paper trial only after all earlier gates pass.

## Rollback triggers

Rollback immediately for SHA mismatch, missing persistent storage, failed integrity/replay, safety-flag drift, unknown active deployment, failed readiness, or evidence-chain divergence. Rollback uses the recorded pre-deployment build/deployment and verified backup; it never enables orders or capital.
