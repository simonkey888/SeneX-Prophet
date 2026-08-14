# SENEX current statistical authority — AUD-061

Status: current policy for the active `senecio_polymarket` PAPER lineage.
This document does not edit or replace the historical preregistration in
`GO_NOGO_CRITERIA.md`; that file remains byte-identical with SHA-256
`0b56cef11d21fe74c211de8f0f72e06fd09a6669b45aea8a58d02a9d9670d6ea`.

## Runtime fields and authority

- Score scope is `PER_SYMBOL`. BTC and ETH rows never share a control cohort.
- The only authority window is `1h`.
- The authority cohort is `INDEPENDENT_NONOVERLAP_1H`; `independent_1h_rows`
  is the effective N. Raw overlapping proof rows are diagnostic only.
- `authoritative_score_pct` remains `null` unless every sample, uncertainty,
  and probability-semantics gate passes.
- Persisted `confidence` is `RAW_CONVICTION`, not a calibrated probability of
  correctness. `raw_confidence_brier` and `raw_confidence_ece` are diagnostic
  only while `confidence_probability_semantics=UNVALIDATED`.
- `proof_qualified_rows_raw`, `by_window`, and cross-symbol aggregates are
  descriptive diagnostics and cannot unlock a gate.

## Learning authority

`pipeline.step2_features.learning_state_v1` is a decision-time snapshot. Its
effective evidence is symbol-scoped, proof-qualified, non-overlapping at 1h,
and known at least one full authority horizon before the decision. It records
source prediction IDs, source evidence hash, effective weight hash, code hash,
config hash, and decision cutoff. Replays are bounded to 50 examples and each
weight remains within 25% of its code-defined base weight.

The learning A/B report is research evidence only. It compares frozen base
weights with bounded learned weights on paired timestamps using chronological
purging and never writes weights or thresholds back to production.

## Feature observation contract

The six model inputs are `orderflow`, `volume_delta`, `bidask_imbalance`,
`funding_signal`, `oi_momentum`, and `price_momentum`. Every candidate decision
records one of `REAL_OBSERVED_ZERO`, `REAL_NONZERO`, `MISSING`,
`NOT_APPLICABLE`, or `SOURCE_ERROR`; fallback-chain use is recorded separately
as `transport_status=FALLBACK_USED`. A numeric zero retained for deterministic
model compatibility is accompanied by `fallback_value=0.0` and is not called
an observation. A point-in-time OI amount is not OI momentum.

## Safety and future GO

Current runtime policy is `PAPER`, `orders_enabled=false`, and
`live_capital_locked=true`. No score, research result, or historical GO/NO-GO
criterion grants live authorization. A future GO requires a separate explicit
owner decision after current authority gates pass; it is not implied by this
document or by `GO_NOGO_CRITERIA.md`.
