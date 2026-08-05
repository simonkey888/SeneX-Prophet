# H-011 V3 Control-Plane Coverage — Invariant Matrix

## 31 Declared Invariants

| invariant_id | description | severity | data required | current implementation | status |
|---|---|---|---|---|---|
| INV-001 | run_id unique | CRITICAL | run_id field in scan metadata | Declared, NOT executed | UNKNOWN (not wired) |
| INV-002 | scan_id unique | CRITICAL | scan_id field in scan metadata | Declared, NOT executed | UNKNOWN (not wired) |
| INV-003 | prediction_id unique | CRITICAL | prediction_id in lifecycle events | Declared, NOT executed | UNKNOWN (not wired) |
| INV-004 | lifecycle event_id unique | CRITICAL | event_id in lifecycle events | Declared, NOT executed | UNKNOWN (not wired) |
| INV-005 | raw events append-only | BLOCKING | raw_event_store hash chain | Declared, NOT executed | UNKNOWN (not wired) |
| INV-006 | historical snapshots append-only | BLOCKING | snapshot directory listing | Declared, NOT executed | UNKNOWN (not wired) |
| INV-007 | no hidden rejected records | WARNING | market_records vs funnel counts | Declared, NOT executed | UNKNOWN (not wired) |
| INV-008 | UNKNOWN not collapsed to 0 | BLOCKING | invariant summary counts | Implemented in invariant_monitor but NOT wired to run_scan_v3 | UNKNOWN (not wired) |
| INV-009 | UNKNOWN not collapsed to False | BLOCKING | invariant status values | Implemented in invariant_monitor but NOT wired | UNKNOWN (not wired) |
| INV-010 | n=0 produces no numeric metric | BLOCKING | market_records with 0 trades | Declared, NOT executed | UNKNOWN (not wired) |
| INV-011 | conditionId not used as token_id | BLOCKING | market structure legs | Implemented in structure_from_gamma validation but NOT wired as invariant | UNKNOWN (not wired) |
| INV-012 | token leg_0 != token leg_1 | BLOCKING | market structure legs | Implemented in structure_from_gamma (unique check) but NOT wired | UNKNOWN (not wired) |
| INV-013 | both tokens belong to MarketTruthContract | BLOCKING | MarketTruthContract fields | Declared, NOT executed | UNKNOWN (not wired) |
| INV-014 | V3 never accepts [ACTIVE] stub | BLOCKING | is_market_stub result | Implemented in process_market_v3 (stub check) but NOT wired as invariant | UNKNOWN (not wired) |
| INV-015 | V3 never writes legacy ledger | BLOCKING | filesystem check (no dry_run_ledger) | Declared, NOT executed | UNKNOWN (not wired) |
| INV-016 | V3 no fallback to V2 | BLOCKING | pipeline_version field | Declared, NOT executed | UNKNOWN (not wired) |
| INV-017 | W=300 for confirmatory cohort | BLOCKING | config.window_s | Implemented in check_invariants but NOT wired to run_scan_v3 | UNKNOWN (not wired) |
| INV-018 | W=3600 always legacy | BLOCKING | config.window_s | Declared, NOT executed | UNKNOWN (not wired) |
| INV-019 | realized_pnl null without real fills | BLOCKING | market_records realized_pnl | Implemented in check_invariants but NOT wired | UNKNOWN (not wired) |
| INV-020 | balance/NAV absent in H-011 V3 | BLOCKING | market_records fields | Implemented in check_invariants but NOT wired | UNKNOWN (not wired) |
| INV-021 | shadow executable requires two books | BLOCKING | shadow_execution fields | Declared, NOT executed | UNKNOWN (not wired) |
| INV-022 | shadow executable requires equal fillable | BLOCKING | shadow_execution fields | Declared, NOT executed | UNKNOWN (not wired) |
| INV-023 | shadow executable requires known fee | BLOCKING | shadow_execution fields | Declared, NOT executed | UNKNOWN (not wired) |
| INV-024 | shadow executable requires net_edge > 0 | BLOCKING | shadow_execution fields | Declared, NOT executed | UNKNOWN (not wired) |
| INV-025 | raw payload persisted before transform | BLOCKING | raw_event_store evidence | Declared, NOT executed | UNKNOWN (not wired) |
| INV-026 | snapshot_hash verifiable | WARNING | snapshot hash + sidecar | Implemented in state_snapshot but NOT wired as invariant | UNKNOWN (not wired) |
| INV-027 | lifecycle hash chain valid | WARNING | lifecycle event hashes | Declared, NOT executed | UNKNOWN (not wired) |
| INV-028 | dashboard and API use same snapshot_hash | WARNING | snapshot_hash in /api/v3/state vs /api/v3/integrity | Declared, NOT executed | UNKNOWN (not wired) |
| INV-029 | paper_only = true | BLOCKING | config.paper_only | Implemented in check_invariants but NOT wired to run_scan_v3 | UNKNOWN (not wired) |
| INV-030 | live_capital_locked = true | BLOCKING | config.live_capital_locked | Implemented in check_invariants but NOT wired | UNKNOWN (not wired) |
| INV-031 | orders_enabled = false | BLOCKING | config.normalized().orders_enabled | Implemented in check_invariants but NOT wired | UNKNOWN (not wired) |

## Summary

- **Implemented and executed:** 0 (none are wired to run_scan_v3)
- **Implemented but not connected:** 6 (INV-017, INV-019, INV-020, INV-029, INV-030, INV-031 exist in invariant_monitor.check_invariants but run_scan_v3 calls _unevaluated_control_plane_state instead)
- **Declared without implementation:** 25
- **Not applicable to cycle:** 0 (will be determined per-scan)
- **Impossible to evaluate:** 0

## Root Cause

`run_scan_v3` at line 1214 calls `_unevaluated_control_plane_state()` which hardcodes all 31 invariants as UNKNOWN with a single placeholder result "CONTROL_PLANE_EXECUTION_COVERAGE". The existing `check_invariants()` function in `invariant_monitor.py` is never called.

## Fix Strategy

1. Replace `_unevaluated_control_plane_state()` with a real `_compute_control_plane_state()` that:
   - Collects source health telemetry from actual HTTP calls
   - Runs all 31 invariants against real scan data
   - Produces real PASS/FAIL/UNKNOWN/NOT_APPLICABLE results

2. Wire `check_invariants()` into `run_scan_v3`

3. Add source health telemetry to discovery, data_api, and clob calls
