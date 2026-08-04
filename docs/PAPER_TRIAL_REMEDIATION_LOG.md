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
