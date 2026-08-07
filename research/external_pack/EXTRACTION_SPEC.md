# SENEX External Research Extraction Pack V1 — Order 019

Authority: `AUD-SENEX-EXTERNAL-RESEARCH-EXTRACTION-PACK-019`
Parent: `2f84a38d6037c8e5a94bc96566b791a9d4f4e680`
Mode: isolated, paper-only, zero-cost, no external code execution.

## Frozen no-regression contract

- CURRENT_BEHAVIOR_CAPTURED=YES
- GOLDEN_TEST_EXISTS=YES
- NEW_CAPABILITY_ISOLATED=YES
- DEFAULT_BEHAVIOR_UNCHANGED=YES
- FEATURE_FLAG_OR_EXPLICIT_CONFIG_WHERE_NEEDED=YES
- ROLLBACK_BY_SINGLE_COMMIT_OR_DISABLE=YES

## Supply-chain rule

External repositories are read statically only. No clone script, hook, Docker image,
package install, submodule, workflow, credential, wallet, private key, signing path,
authenticated order method, token approval or paid service is consumed. License uncertainty
never blocks this pack because `copy_eligible=false` and all implementation is independent.

## Capability specifications

### CAP-A — External Market Data Schema Cross-Check

Add an **additive projection** over the existing `RawEvent`; never rewrite the raw event.
Normalize only public fields actually present: market/token identity, source cursor,
event/received time, tick size, minimum order size, neg-risk marker, public book hash,
best bid and best ask. Missing fields remain `None`; no queue identity is manufactured.

### CAP-B — Realistic Paper Fill Model V1

Given a captured aggregate L2 book, side, quantity, limit and book age, deterministically
consume eligible visible depth and report full/partial/no-fill. Fail closed on stale books.
Expose latency buffer and an adverse-selection observation window. Exact queue position is
always false; aggregate L2 cannot prove order-level queue identity.

### CAP-C — Deterministic Event Replay Contract V2

Finite captured input only. Strict event ordering, explicit point-in-time cutoff, explicit
clock, explicit seed, pinned event/replay schema, and zero external live reads. Identical
input/config must produce the same output hash. Existing 018 replay remains unchanged.

### CAP-D — Cross-Market State Interface DISABLED V1

Provide only a typed fixture/synthetic interface. External live adapter is disabled and its
network-read counter remains zero. Enabling/fetching live state raises fail-closed.

### CAP-E — Microstructure Terminal Enhancements

Read-only projection for microprice, imbalance, visible depth, spread, liquidity shock,
staleness, signed flow, flow burst and paper-fill quality plus data-quality badges. It
contains no wallet/order/deposit/withdraw controls and does not modify 018 default UI.

## Extraction decisions

- `Polymarket/py-clob-client-v2` and `Polymarket/clob-client-v2`: protocol/type authority
  for public schema semantics only; authenticated execution surfaces quarantined.
- `Polymarket/conditional-tokens`: requested path returned 404; no capability depends on it.
- `Polymarket/ctf-exchange`: official but archived; historical semantics only.
- Tier B sources: methodology/data/UI references only; no literal source copied.
- Live execution frameworks/bots/market makers: `EXECUTION_QUARANTINE`.
- Unknown/unverified license: reference-only, `copy_eligible=false`.

## Rollback

All 019 runtime code lives under `polymarket/research_pack/` and tests under
`tests/research_pack/`; remove/disable that isolated package to restore 018 behavior.
No existing 018 product file is modified by the capability implementation.
