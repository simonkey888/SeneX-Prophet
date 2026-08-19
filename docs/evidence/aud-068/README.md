# AUD-068 — PROSPECTIVE_PROBABILITY_VALIDATION_V1

Authority: Issue #23 comment `5346442886`.

Canonical base: `43c8023d3a4623381e45da02d9efa8e9b5888f47` / tree `20ec5775ea37a7288e8cd8748ea304843d9b0866`.

Prospective cutoff is immutable: `prediction.ts > 2026-08-19T18:44:53Z` for `BTCUSDT`, 1h horizon.

Candidate mapping is frozen before outcomes:

- LONG: `p_correct_candidate = audit.pipeline.step2_features.up_prob`
- SHORT: `p_correct_candidate = 1 - audit.pipeline.step2_features.up_prob`
- FLAT: excluded

The source probability is the persisted decision-time value only. It is never recomputed or fitted. The existing `confidence` field remains `RAW_CONVICTION / UNVALIDATED`.

Maturity is the earliest deterministic independent-nonoverlap-1h prefix satisfying `GLOBAL_N>=100`, `LONG_N>=30`, `SHORT_N>=30`. Until then the only valid status is `WARMUP` and `PROBABILITY_SEMANTICS_VALIDATED=NO`.

`current-capture.json` is a minimized read-only snapshot. It contains the first post-cutoff BTCUSDT row observed during implementation. The offline validator derives eligibility, row-level evidence, cohort SHA, maturity and—only after maturity—frozen metrics from captured bytes. No API aggregate is used as metric authority.

Forbidden under this order: calibration, threshold search, feature/weight search, outcome-based cohort editing, production/runtime/RUNTIME017 mutation, Supabase writes, real trading, merge, deploy or restart.
