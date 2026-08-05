# Paper trial remediation log

## BLOCKER-NF-001

- CLASS: infrastructure capability
- EVIDENCE: current connector exposes no safe Northflank secret-presence check or workflow dispatch for the historical GET-only audit
- ROOT_CAUSE: execution surface unavailable, not a product defect
- ATTEMPT: verified historical order and preserved zero access to secrets/production
- CHANGE: none to Northflank
- FOCUSED_TEST: not applicable
- FULL_GATE: GitHub Actions container fallback
- RESULT: fallback selected
- FALLBACK: `.github/workflows/senex-paper-trial.yml`

## BLOCKER-CONCURRENCY-001

- CLASS: deterministic concurrency defect
- EVIDENCE: artifact finalization occurred before chain lock, permitting a transient unregistered file during concurrent append
- ROOT_CAUSE: lock boundary too narrow
- ATTEMPT: moved final artifact/sidecar/manifest publication under the existing chain lock through `publish_artifact_bytes_with_manifest`
- CHANGE: minimal transaction publication helper and strengthened test
- FOCUSED_TEST: repeated concurrent append test
- FULL_GATE: complete test suite and exact-head CI
- RESULT: locally stable; CI required
- FALLBACK: none

## BLOCKER-PUBLIC-CLOB-001

- CLASS: observed-trial runtime-semantics defect
- EVIDENCE: observed run `30958766816` declared 60 minutes but ended after 4.160064 seconds; 36 completed Gamma windows were counted although all 36 historical CLOB `/book` requests returned HTTP 404
- ROOT_CAUSE: current-window offset exclusion, discovery-before-book accounting, Gamma-only early exit, failed-window promotion into observed counts, and CI assertions that did not require real duration or valid two-book sets
- ATTEMPT: prioritize the current five-minute bucket; prefilter `active=true`, `closed=false`, `acceptingOrders=true`, and future `endDate`; classify expected closed/no-book separately from actionable source failures; count a window only after both token books validate and are evidence-backed; make elapsed duration the only non-fixture stop condition
- CHANGE: public source error classification, active-window discovery contract, distinct Gamma/valid-book/closed/unexpected counters, clock-injected duration semantics, summary schema, exact-head workflow assertions, and deterministic hostile tests
- FOCUSED_TEST: current-window inclusion, closed-window exclusion, ended-versus-active CLOB 404 classification, Gamma-only no-early-exit, valid two-book counting, and injected 60-second duration
- FULL_GATE: 65 governance+paper tests, 626 global tests, 28/28 repository gates, deterministic manifest generation, exact-head CI, fixture artifact, real 60-minute observed artifact, and digest audit
- RESULT: local validation PASS; exact-head remote revalidation required
- FALLBACK: none; acceptance requires both at least 3600 elapsed seconds and at least 12 distinct valid two-book windows
