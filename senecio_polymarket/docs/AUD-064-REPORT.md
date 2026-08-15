# AUD-064 — Learning Authority Freeze

## Authority and lineage

- Base `main`: `2c4dbf284b23d3cf81b93dcfbd262660ab03dd43`, tree `0cb5abaa024f1325bf88e5fd3390dcec8f5f972d`.
- AUD-062 reference only: PR #58 head `f65e1723953ac23caf1ca3741ec894577c97aae7`, tree `d8c8b734bdfd0d0a33e31bdd80557e9dafb71b06`.
- AUD-063 remains the settlement/proof authority baseline and its settlement files are intentionally outside this implementation diff.
- Exact final candidate SHA/tree are produced by the candidate-specific `AUD_064_EXACT_HEAD_GATE`; they are not embedded in a tracked self-referential file.

## Independent reproduction

Before the product fix, the current-main learning projection was executed against a canonical AUD-063 proof-qualified synthetic row. Run `31894929555`, job `95036671879`, demonstrated:

- persisted row has `exchange_used=okx` and passes canonical proof qualification before projection;
- current-main learning SELECT omits `exchange_used`;
- the fetched row therefore lacks `exchange_used`;
- the same canonical `backend.settlement_proof.is_proof_qualified()` rejects the projected row;
- replay receives zero qualified rows.

The diagnosis is therefore `REPRODUCED_PROJECTION_MISMATCH`; it is not attributed to missing settlement outcomes.

## Integration change

Production learning now reads persisted `exchange_used` explicitly in `oracle_runtime/institutional_core_real.py`. It does not default, infer, backfill, or synthesize a source. Missing or invalid source remains excluded by the unchanged canonical proof gate.

Adaptive replay is executed on a detached shadow core. The production core is kept/restored at its code-defined frozen base weights. Returned learning state explicitly separates:

- `learning_mutation_authority=SHADOW_ONLY`;
- `production_learning_mutation_enabled=false`;
- `size_calibration_authority=FROZEN_BASE_ONLY`;
- `mutations=0` for production;
- `shadow_mutations`, `shadow_weights`, and `shadow_weights_hash` for observation only;
- base, decision, and effective production weight hashes;
- source prediction IDs, settlement-observation epochs, evidence hash, and decision cutoff.

AUD-064 introduces no new N threshold and does not authorize activation of learned weights. Existing `MIN_LEARNING_EXAMPLES`, score evidence thresholds, win-rate gates, Wilson gate, model weights, EV semantics, and directional semantics remain unchanged.

## Temporal/causal contract

For a decision cutoff `T`, shadow learning reuses the existing causal replay and canonical proof gate and admits only same-symbol rows whose 1h horizon has elapsed, whose settlement evidence was observed at or before `T`, and which survive the deterministic independent/non-overlap cohort. Future/self outcomes remain excluded.

## AUD-063 preservation

The candidate is gated against changes to:

- `backend/oracle_runner.py`;
- `backend/supabase_client.py`;
- `backend/settlement_contract.py`;
- `backend/settlement_proof.py`;
- `backend/settlement_reconciler.py`.

T18–T23 additionally re-exercise open-candle rejection, same-origin proof, NULL-only CAS, repair-only reconciliation, FLAT-starvation resistance, and startup zero-I/O quarantine.

## Repository governance

The legacy manual oracle workflow is downgraded to a diagnostic artifact workflow with `contents: read`, no persisted checkout credential, no `git push`, and no Pages deployment path.

`docs/evidence/aud-064-governance-ruleset-proposal.json` is an unapplied single-owner proposal only. It requires PRs, zero GitHub approvals, resolved review threads, no broad bypass, deletion/non-fast-forward protection, and only generic current-main PR checks (`score-001`, `score-002`, `act_final_audit_smoke (T1-T12)`). One-off AUD exact-base checks are not proposed as permanent required checks. `GITHUB_SETTINGS_APPLIED=false`.

## Verification contract

The candidate-specific exact-head workflow executes T01–T32, relevant AUD-061 learning regressions, all AUD-063/R1 regressions, full repository unittest discovery, full Python compile, frozen-path/scope checks, maintainer workflow boundary checks, deterministic double evidence generation, publication sanitization, and an exact-head evidence receipt/artifact.

No merge, deploy, GitHub settings mutation, RUNTIME017 change, model tuning, threshold tuning, base-weight change, external directional activation, or live-capital action is part of AUD-064. No edge or calibrated-probability claim is made.
