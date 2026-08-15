# AUD-062-R1 — consolidated remediation candidate

## Lineage and authority

- Exact base: `49c5f0a69609c005da80e48b585e91d8582a5ac6`
- Reviewed parent head: `8ceac269cf8f59cc6a03ed036c99fa0500bcacdb`
- Candidate head/tree: bound by exact-head CI and the final Issue #23 checkpoint.
- PR/branch: existing PR #58, `audit/aud-062-decision-causality-publication`.
- Merge, deploy, Northflank, database, production, GitHub-settings and RUNTIME017 mutations: none.

## Decision result

R1 defines one canonical 1h PAPER instrument contract for `BTC-USDT` spot and
`ETH-USDT` spot. It does not treat the sigmoid heuristic as a calibrated win
probability and it does not accept account-independent fee or fill constants as
an executable cost authority. Therefore canonical EV is currently
`NOT_ESTIMABLE` and every candidate directional PAPER decision fails closed as
`COST_MODEL_NOT_AUTHORITATIVE`.

The complete frozen 348-row counterfactual contains 348 fail-closed rows. The
95 historical directional decisions become diagnostic FLAT shadows; the 253
historical FLAT decisions stay FLAT. This is not evidence of improvement, loss,
win rate, or edge. No threshold or weight was selected or optimized from these
rows.

The historical parallel market-anchor EV remains fully reconstructed as audit
evidence but has no R1 decision authority. There is no `min`, `max`, fallback,
or hidden second EV model in the canonical contract.

## Finding disposition

| Finding | R1 disposition | Evidence boundary |
| --- | --- | --- |
| F001 | `FAIL_CLOSED_WITH_EXPLICIT_LIMITATION` | Single canonical EV authority; EV remains not estimable. |
| F002 | `CLOSED` | One survivability calculation supplies both its machine probability and reason. |
| F003 | `FAIL_CLOSED_WITH_EXPLICIT_LIMITATION` | Repository direct-push path removed; settings manifest proposed but deliberately not applied. |
| F004 | `FAIL_CLOSED_WITH_EXPLICIT_LIMITATION` | Future persisted-only round trip is complete; legacy rows remain insufficient. |
| F005 | `FAIL_CLOSED_WITH_EXPLICIT_LIMITATION` | Instruments are named; account-tier/order/fill costs remain unauthoritative. |
| F006 | `CLOSED` | Truthful heuristic score names and explicit deprecated aliases. |
| F007 | `CLOSED` | Reporting, learning-mutation and size-calibration authorities are separated; decisions use frozen weights. |
| F008 | `CLOSED` | Reproducible first-binding machine reason classes distinguish EV sign from threshold failure. |
| F009 | `CLOSED` | The 5m same-market metric is descriptive agreement only; value-add and blend remain not estimable. |

## Cost and provenance truth

One basis point is exactly `0.0001` decimal return. Fee, spread, slippage,
depth, impact and round-trip conventions are individually serialized. No R1
cost term is applied while the cost authority is unresolved, so no literal or
semantic double counting is possible in the candidate contract.

The intended PAPER instrument identifiers and public market-data contract are
grounded in the [OKX API guide](https://www.okx.com/docs-v5/en/). Fee
interpretation is explicitly unresolved because the official
[OKX fee rules](https://www.okx.com/help/trading-fee-rules-faq) distinguish
maker/taker behavior and account-dependent rates. R1 does not access an account
or create an execution venue.

Future rows persist source identity, exchange/observation/query time, external
observation time, learning source IDs and settlement-observation epochs,
learning/weight/code/config hashes, and market snapshot identity. A JSON
serialize/reload test proves the same source-evidence hash and cutoff
classification from the persisted contract alone. No legacy timestamp is
invented.

## Learning, external context and action semantics

Learned weights are recomputed only as shadow research. Frozen base weights and
empty frozen calibration state remain the PAPER decision and size authority.
No new sample threshold is invented; prospective independent cohort,
predeclared cutoff, separation and rollback requirements remain explicit.

`heuristic_up_score` and `heuristic_down_score` are uncalibrated directional
scores. The old `up_prob` and `down_prob` aliases are machine-marked deprecated
and non-calibrated. Raw conviction, market-implied prices, empirical rates and
any future calibrated probability remain separate claim classes.

Polymarket, Kalshi and Boros stay read-only and `external_applied=0`. Even an
environment flag cannot activate external directional pressure in this
candidate. `BLENDED_SHADOW=NOT_ESTIMABLE`; the observed Polymarket statistic is
named only `same_market_5m_resolved_label_agreement`.

## Governance and CI

The manual oracle workflow now has `contents: read`, cannot commit or push, and
emits diagnostic artifacts only. The proposed main ruleset is materialized in
`docs/evidence/aud-062-r1-governance-settings-manifest.json`; it is not applied.

Actions introduced or modified in the AUD-062 lineage are pinned to full
immutable commit SHAs. The forensic job checks exact base/head ancestry,
RUNTIME017 isolation, frozen core isolation, no threshold tuning, no external
activation, deterministic artifact regeneration and publication sanitization.
Exact run IDs belong in the final Issue #23 checkpoint after CI completes on
the published head.
