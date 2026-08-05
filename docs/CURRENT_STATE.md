# SENEX / SENECIO H-011 V3 — Current State

## Canonical identity

- Product: **SENEX**
- Technical system: **SENECIO H-011 V3**
- Repository: `simonkey888/SeneX-Prophet`
- Delivery branch before integration: `feat/h011-v3-discovery-refresh`
- Pre-integration production and rollback SHA: `2f8503533543832147caf4c8e97a0cc6f5af3cbc`
- Integration candidate branch: `feat/senex-paper-execution-truth-v1`
- Candidate validated SHA: `72df6b78c27dcc20bec2405a4d4177c677468d9f`
- Candidate validated tree: `98f43935037f4fc1f38a67279725469ed330823b`
- Exact-head CI run: `30987358828`
- Exact-head CI job: `92245123499`
- Exact-head artifact: `8922658841`
- Artifact ZIP SHA-256: `0ad6412d20df3f689a2729056ebade1ea49b11da8fbbf6c330a793a76333bb0b`

## Permanent product boundary

```text
paper_only=true
orders_enabled=false
live_capital_locked=true
REAL_ORDER_NETWORK_CALLS=0
WALLET_OR_PRIVATE_KEY=ABSENT
REAL_CAPITAL=ABSENT
```

The current mission completes paper execution, durable evidence, replay, evaluation, deployment and read-only monitoring. Real-money execution is not a pending SENEX capability.

## Reconciled causal stack

The product stack is a strict linear ancestry chain:

```text
2f8503533543832147caf4c8e97a0cc6f5af3cbc  product/rollback baseline
  -> aeb50867738b7ae7199f621a730080e09465458e  PR #5 control plane and transactional runtime
  -> 39e1cf1bdad31a2b6f2178949a2977c837ebdf18  PR #24 executable repository constitution
  -> 00f018484f6e39f4cc7c518df02e1f1b0ab97df8  PR #25 paper-trial and architecture completion
  -> 72df6b78c27dcc20bec2405a4d4177c677468d9f  PR #26 execution truth and crash recovery
```

Each child is strictly ahead of its parent with the parent as merge base. No subsystem was reimplemented during reconciliation and no commit from PRs #5, #24, #25 or #26 is omitted by the final candidate.

PR #21 is optional, local-only TradingView research. It is non-authoritative, not a production dependency, not a raw-chain input and not part of the deployable candidate. It is to be closed as superseded research reference, with its branch and historical artifacts retained for final audit.

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

After the final integration candidate is merged into the delivery branch, capture an authenticated reversible Northflank baseline, verify backup and rollback, probe the real volume in isolation, and deploy only if all storage and paper-only gates pass.
