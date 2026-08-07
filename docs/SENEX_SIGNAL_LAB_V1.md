# SENEX SIGNAL LAB + SENEX LIVE TERMINAL V1

Status: `DEV_CANDIDATE` on the isolated 018 lane. This document does not authorize merge or deployment.

## Safety boundary

- `paper_only=true`
- `orders_enabled=false`
- `live_capital_locked=true`
- no wallet UI, signing, order entry, deposit/withdrawal, or authenticated trading client
- official Polymarket public market-data sources only
- zero paid services; no new managed infrastructure
- lane 017 runtime, bundle, OCI state, and trial evidence are untouched

Development base: `74429bb0dfd36a24ec01f0b94856603a9298ab37` / tree `181c254a0fd945c8c521dc0072df23fc8a5a6a4d`.

## Workflow push-safety audit

All workflows present at the base were reviewed before the first 018 branch write. `SAFE_FOR_018_BRANCH` means that a push to `feat/senex-signal-lab-live-terminal-v1` cannot provision, deploy, migrate, or mutate an external provider.

| Workflow | Trigger / branch filter | Secrets | External network | Deploy/provision/migration/provider mutation | SAFE_FOR_018_BRANCH |
|---|---|---|---|---|---|
| `h011-arm64-reproducibility.yml` | push/PR only `feat/h011-v3-discovery-refresh` | none | package/container reads | none | YES |
| `h011-integrity.yml` | push/PR only `feat/h011-v3-discovery-refresh` | none | package reads | none | YES |
| `h011-pr3-docker-smoke.yml` | PR only to product branch with path filters | none | package/container reads | none | YES |
| `h011-pr5-control-plane-smoke.yml` | PR only to product branch with path filters | none | package/container reads | none | YES |
| `h011-pr5-phase-iic-exact-head.yml` | PR only to product branch with path filters | none | package/container reads | none | YES |
| `h011-pr7-recovery-smoke.yml` | PR only to recovery/control-plane branches | none | package reads | none | YES |
| `h011-publisher-sol-audit.yml` | push/PR only transaction-publisher branch | none | package reads | none | YES |
| `h011-runtime-transaction-integration.yml` | PR only to control-plane branch | none | package/container reads | none | YES |
| `oracle.yml` | `workflow_call` only; quarantined read-only assertion | none | none material | explicitly disabled | YES |
| `senex-execution-truth.yml` | PR only to product branch with path filters | none | package/container reads | none | YES |
| `senex-paper-trial.yml` | push only release paper-trial branch, PR only governance branch, manual dispatch | none | public data/package reads | no provider provisioning | YES |
| `senex-phase-fh-predeployment.yml` | PR only to product branch with path filters | none | package reads | explicitly predeployment/read-only | YES |
| `senex-repository-contract.yml` | PR only to product branch with path filters | none | package reads | none | YES |
| `senex-stack-integration.yml` | PR only to product branch | none | package/container reads | none | YES |
| `smoke-tests.yml` | push/PR only `main` | none | package reads | comments explicitly forbid deploy/mutation | YES |

Conclusion: `WORKFLOW_PUSH_SAFETY=PASS`. A push to the 018 branch does not match any mutation-capable workflow; existing workflows are read/test/build gates with `contents: read` or are scoped to unrelated branches. A draft PR to the product base may run read-only test/build workflows, which is intended.

## Architecture

```text
OFFICIAL_POLYMARKET_SOURCES
        |
        v
RAW_APPEND_ONLY_CHAIN
        |
        v
NORMALIZATION_DEDUP_GAP_STALENESS
        |
        v
POINT_IN_TIME_STATE_STORE
        |
        +--> FEATURE_ENGINE F01..F15
        |       |
        |       +--> UNVALIDATED FAIR VALUE / SIGNAL RESEARCH
        |       +--> LIVE PAPER EVALUATION HARNESS
        |
        +--> REPLAY / ANTI-LEAKAGE
        +--> EXPERIMENT REGISTRY
        +--> CONTRADICTION LEDGER
        |
        v
SENEX LIVE TERMINAL
```

The development app is `polymarket.signal_lab.app:app`. It is intentionally not installed on or deployed to the active 017 host. It can later be mounted into the existing FastAPI process, avoiding an additional paid service.

## Point-in-time contract

Every `RawEvent` includes `event_id`, `event_type`, `market_id`, `token_id`, `event_time`, `received_time`, `sequence_or_source_cursor`, `source`, `payload_hash`, and `schema_version`. `PointInTimeStore.events_as_of(t)` excludes every event with `event_time > t`. Feature values carry `input_event_max_time`, and construction fails if it exceeds `as_of_event_time`.

The raw extension chain is hash-linked and append-only. Existing H-011 raw evidence is not rewritten.

## Feature Engine V1

Implemented as versioned point-in-time functions:

`F01_TOP_BOOK_IMBALANCE`, `F02_DEPTH_WEIGHTED_IMBALANCE`, `F03_MICROPRICE_DIVERGENCE`, `F04_SPREAD`, `F05_TOTAL_VISIBLE_DEPTH`, `F06_DEPTH_CONCENTRATION`, `F07_QUOTE_VELOCITY`, `F08_SIGNED_TRADE_FLOW`, `F09_BOOK_STALENESS`, `F10_TIME_TO_CLOSE`, `F11_DEPTH_COLLAPSE`, `F12_LIQUIDITY_SHOCK`, `F13_CROSS_MARKET_DIVERGENCE`, `F14_REGIME_SCORE`, `F15_NEG_RISK_RESIDUAL`.

Implementation does not claim predictive edge. The fair-value surface remains explicitly `UNVALIDATED` / `RESEARCH_ONLY_NOT_ALPHA` until a preregistered OOS experiment passes.

## Validation harness

Supports MID_PRICE baseline, OOS Brier, log loss, calibration error, directional information, market/day bootstrap, deterministic permutation test, walk-forward reports, regime splits, spread-adjusted paper edge, no-fill simulation, and the required anti-leakage gates. Statistical pass/fail thresholds remain experiment-specific and must be preregistered before evaluation.

## Experiment and contradiction truth

Both registries are hash-linked append-only JSONL contracts. Experiment results can only be recorded after preregistration and cannot overwrite a prior result. Contradiction resolution appends a superseding record rather than deleting history.

## SENEX-MIRROR-001

Result: `NOT_OBSERVABLE_FROM_PUBLIC_DATA`.

Official Polymarket documentation for `GET /book` exposes aggregate `bids` and `asks` as price/size levels plus market/token/timestamp/hash/tick/min-size/neg-risk metadata. The public market WebSocket exposes book snapshots and price-level changes. Neither documented public schema supplies a direct-vs-mirrored/synthetic provenance field. Therefore V1 does not invent a heuristic and does not promote this hypothesis into a production feature.

Official references:

- `https://docs.polymarket.com/api-reference/market-data/get-order-book`
- `https://docs.polymarket.com/market-data/websocket/market-channel`
- `https://docs.polymarket.com/api-reference/introduction`

## UI truth contract

The terminal is dark, desktop-first and responsive. It contains Market Radar, selected-market context, price/depth panel, order book/recent public trades, F01/F03/F08/F09/F11/F12/F14/F15 intelligence, explicit UNVALIDATED signal/fair value, experiments/evidence/system-truth tabs, and mobile breakpoints.

System Truth always includes `paper_only`, `orders_enabled`, `live_capital_locked`, `ws_connected`, `last_event_age`, `sequence_gaps`, `stale_data_count`, `raw_chain_tip_hash`, `replay_verified`, `active_experiment_id`, and `featureset_hash`.
