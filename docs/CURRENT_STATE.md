# SENEX / SENECIO H-011 V3 — Current State

## Canonical identity

- Product: **SENEX**
- Technical system: **SENECIO H-011 V3**
- Repository: `simonkey888/SeneX-Prophet`
- Canonical delivery branch: `feat/h011-v3-discovery-refresh`
- Accepted product merge before correction: `d5047b2055d30d199954dd4e68ae2e676aa8a4ba`
- Accepted product tree before correction: `781ee4166d37c041c609d151bd308661c6569191`
- Corrective authority: Issue #23 comments `5190770249` and `5191330569`
- Production rollback SHA before promotion: `2f8503533543832147caf4c8e97a0cc6f5af3cbc`
- Production remains on the rollback SHA until repository-wide paper-only correction, backup, isolated restore and rollback gates pass.

## Permanent product boundary

```text
paper_only=true
orders_enabled=false
live_capital_locked=true
REAL_ORDER_NETWORK_CALLS=0
WALLET_OR_PRIVATE_KEY_ACCESS=0
REAL_CAPITAL_ACTIONS=0
```

The deployable product contains public-data observation and deterministic paper simulation only. Authenticated exchange credentials, private positions or balances, order creation or cancellation, wallets, signing and real-capital routes are prohibited. The Binance testnet credential/order route is removed from both connector copies. The Oracle workflow is read-only and cannot commit, push or deploy Pages. `tools/verify_paper_only_repository.py` scans tracked root code, packages, scripts, workflows, Dockerfiles and configuration and fails closed on these capabilities.

## Candidate contracts

The candidate contains:

1. one authoritative transactional raw chain under `/app/polymarket/results/h011_v3/raw_chain_v1`;
2. startup recovery before scanner or publication enablement;
3. committed manifest-chain readers for state, integrity and replay;
4. fail-closed runtime states and no silent legacy-writer fallback;
5. public-GET paper execution with dynamic market fee authority;
6. sequential first/second-leg execution against distinct snapshots;
7. append-only crash-consistent orchestration recovery;
8. startup hydration of portfolio, deterministic fill IDs, pending executions and terminal windows;
9. full replay of portfolio, orchestration and sequential execution results;
10. exact-head monitoring evidence which distinguishes runtime, harness, fixture and historical data.

## Gate A evidence

```text
FOCUSED_TESTS=99_PASS
GLOBAL_TESTS=660_PASS
REPOSITORY_GATES=28_OF_28_PASS
MISSION_GATES=11_OF_11_PASS
CRASH_FAULT_POINTS=6_OF_6_PASS
FIRST_LEG_EXACTLY_ONCE_ACROSS_RESTART=PASS
SECOND_LEG_EXACTLY_ONCE_ACROSS_RESTART=PASS
ORCHESTRATION_REPLAY=PASS
SEQUENTIAL_RESULT_REPLAY=PASS
ALL_INTERNAL_SHA_BINDINGS=72df6b78c27dcc20bec2405a4d4177c677468d9f
PAPER_ONLY_STATIC_EXCLUSION=PASS
ARTIFACT_INTERNAL_SHA256SUMS=PASS
```

The six validated interruption boundaries are:

```text
AFTER_FIRST_STATE_DURABLE
AFTER_FIRST_FILL_DURABLE
AFTER_FIRST_COMMIT_DURABLE
AFTER_SECOND_STATE_DURABLE
AFTER_SECOND_FILL_DURABLE
AFTER_TERMINAL_DURABLE
```

## Delivery and rollback

Repository history and deployment documentation establish the product delivery path as:

```text
GitHub delivery branch
  -> pinned GitHub Actions / reproducible Python 3.11 image
  -> supervised FastAPI/Uvicorn runtime
  -> Northflank SENEX service
  -> public code.run endpoints
```

Cloudflare is not the authoritative SENEX runtime. The pre-integration rollback point is permanently recorded as:

```text
ROLLBACK_SHA=2f8503533543832147caf4c8e97a0cc6f5af3cbc
```

No branch or artifact in the historical stack is to be deleted before final AUD.

## Production state

The last independently verified production SHA remains `2f8503533543832147caf4c8e97a0cc6f5af3cbc`. Its storage/replay state was degraded. Until a fresh authenticated Northflank inventory and public GET reconciliation are completed, production must be represented as:

```text
CURRENT_PRODUCTION_STATE=UNKNOWN_OR_DEGRADED
CANDIDATE_DEPLOYED=NO
```

## Next operational phase

The exact retargeted integration head must pass `SENEX Stack Integration`. It may then be merged into the delivery branch. After merge, capture an authenticated reversible Northflank baseline, verify backup and rollback, probe the real volume in isolation, and deploy only if all storage and paper-only gates pass.
