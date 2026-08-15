# AUD-062-R2 correction report

Order: Issue #23 comment `5299876166`.

This correction remains on PR #58 and branch
`audit/aud-062-decision-causality-publication`, directly after reviewed R1
head `7eab1047e7610aa8bd29053ef8d0bf6cebdee3ee`. It does not authorize or
perform merge, deploy, GitHub settings, production, Northflank, database, or
RUNTIME017 mutations.

## R2-F001 — fixed

`historical_core_ev.survival_adjusted_ev` now accepts only the explicitly
reconstructed `base_ev * survival_discount` value. It never falls back to the
final `adjusted_ev`, which may contain the historical parallel market anchor.
When reconstruction is impossible the field is null with
`NOT_RECONSTRUCTIBLE` and `INSUFFICIENT_PROVENANCE`. The historical final
adjusted value and any explicit anchor remain separately named diagnostics.
Neutral direction now serializes a null, `NOT_APPLICABLE` probability input.

## R2-F002 — fixed

The proposed, unapplied ruleset is internally consistent with the current
single-owner workflow: PRs and all four strict exact-head checks remain
required, while GitHub review count and last-push approval are zero/false.
There are no bypass actors; deletion, force-push, and direct push remain
blocked. Owner/AUD authorization remains the Issue #23 process gate. No
GitHub setting was applied.

## R2-F003 — fixed

The R2 acceptance test executes `oracle_runtime.predict_only.run_prediction()`
with deterministic BTC market, external-context, and prior-learning fixtures.
It serializes the whole returned prediction row, discards the originating
objects, reloads JSON only, and verifies feature, external, learning, weight,
market-identity, timestamp, and hash provenance. Reordered irrelevant input
representation produces the same contract and market identity. No real
network, database, secret, or mutation is used.

## Preserved locks

- Historical dataset: 348 rows unchanged.
- Canonical R1 counterfactual: diagnostic and fail-closed; no edge claim.
- Threshold changes: 0.
- Post-hoc weight tuning: 0.
- External directional activation: 0.
- PAPER only; orders disabled; live capital locked.
- RUNTIME017 mutations: 0.
