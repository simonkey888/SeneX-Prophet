#!/usr/bin/env python3
"""Deterministic AUD-063 evidence generator (no network, no DB mutation)."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CANON = ROOT / "docs" / "evidence"
GENERATED_AT = "2026-08-15T04:05:00+00:00"
BASE_SHA = "49c5f0a69609c005da80e48b585e91d8582a5ac6"
BASE_TREE = "3e323bcc2795f97b29242883d3bf2a015c092ccd"
PREFIX_RUN = 31860917485
PREFIX_ARTIFACT = 9240533706


def load(name: str) -> Any:
    return json.loads((CANON / name).read_text(encoding="utf-8"))


def write_json(out: Path, name: str, value: Any) -> None:
    (out / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=CANON)
    args = parser.parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    baseline = load("aud-063-runtime-baseline.json")
    reproduction = load("aud-063-starvation-reproduction.json")
    write_json(out, "aud-063-runtime-baseline.json", baseline)
    write_json(out, "aud-063-starvation-reproduction.json", reproduction)

    common = {
        "order": "AUD-063-R1",
        "parent_order": "AUD-063",
        "audited_parent_head": "0320f47657d4433bfc4dc3396fd0d31ffabe2270",
        "base_sha": BASE_SHA,
        "base_tree": BASE_TREE,
        "generated_at_utc": GENERATED_AT,
    }

    backlog = {
        **common,
        "source_class": "SANITIZED_PUBLIC_READ_ONLY_BASELINE_DERIVATION",
        "eligible_directional_null_gt_1h": baseline["counts"]["eligible_directional_null_gt_1h"],
        "oldest_eligible_directional": baseline["oldest_eligible_directional"],
        "old_flat_null_rows_ahead_of_oldest_directional": baseline["counts"]["old_flat_null_rows_ahead_of_oldest_directional"],
        "directional_backlog_by_symbol_age_bucket": baseline["counts"]["directional_backlog_by_symbol_age_bucket"],
        "first_100_null_by_direction": baseline["counts"]["first_100_null_by_direction"],
        "runtime_verifier": baseline["verifier"],
        "production_mutations": 0,
    }
    write_json(out, "aud-063-backlog-inventory.json", backlog)

    selection = {
        **common,
        "source_class": "SOURCE_CONTRACT",
        "server_side_filters": {"outcome": "is.null", "prediction": "in.(LONG,SHORT)", "horizon": "ts<=now-1h-60s"},
        "order": "ts.asc,id.asc",
        "pagination": "STATELESS_INTRA_INVOCATION_KEYSET_TS_ID",
        "page_size": 100,
        "max_pages_per_invocation": 2,
        "fairness_bound_rows_per_invocation": 200,
        "restart_dependency": "NONE_WITHIN_DECLARED_BOUND",
        "beyond_bound_claim": "PREFIX_ONLY_EXPLICIT_CAP_NO_UNIVERSAL_STARVATION_CLAIM",
        "flat_can_enter_verifier_page": False,
        "failed_row_retryable_next_invocation": True,
        "maturity_buffer_seconds": 60,
        "observability": [
            "eligible_directional_pending_count", "oldest_eligible_directional_pending_id",
            "oldest_eligible_directional_pending_ts", "oldest_eligible_directional_pending_age_seconds",
            "last_verify_rows_scanned", "last_verify_pages_scanned", "last_verify_count",
            "last_verify_unresolved_proof", "last_verify_unresolved_price", "last_verify_scan_cap_hit",
            "last_verify_cursor", "last_verify_scan_pass_complete", "last_verify_restart_safe_stateless",
            "last_verify_fairness_bound_rows", "last_verify_fairness_scope", "last_verify_no_progress_reason"
        ],
    }
    write_json(out, "aud-063-selection-contract.json", selection)

    cursor = {
        **common,
        "source_class": "DETERMINISTIC_ADVERSARIAL_SIMULATION",
        "mechanism": "LOCAL_KEYSET_CURSOR_LIVES_ONLY_INSIDE_ONE_BOUNDED_INVOCATION",
        "page_size": 100,
        "max_pages_per_invocation": 2,
        "fairness_bound_rows_per_invocation": 200,
        "cases": {
            "flat_head": {"old_flat": 125, "later_directional": 1, "post_fix_directional_seen": True},
            "restart_125_poison_then_healthy": {"eligible": 126, "cursor_reset_between_invocations": True, "healthy_seen_each_invocation": True},
            "healthy_180": {"eligible": 180, "pages": [100, 80], "unique_visited": 180},
            "over_bound_250": {"eligible": 250, "visited": 200, "scan_cap_hit": True, "fairness_scope": "RESTART_SAFE_PREFIX_ONLY_EXPLICIT_CAP"},
            "keyset": {"second_page_uses_ts_id_seek": True, "offset_pagination": False},
        },
        "invariant_within_bound": "RESTART_SAFE_LATER_ROWS_REACHABLE_WITHOUT_CROSS_CYCLE_MEMORY",
        "universal_starvation_claim": False,
    }
    write_json(out, "aud-063-cursor-fairness.json", cursor)

    historical = {
        **common,
        "source_class": "SOURCE_AND_DETERMINISTIC_CANDLE_CONTRACT",
        "canonical_rule": "ONE_MINUTE_CANDLE_CONTAINING_EXACT_TARGET",
        "containment": "candle_open_ms <= target_ms < candle_close_ms",
        "candle_close_identity": "candle_open_epoch_ms + candle_interval_ms",
        "maturity_rule": "observed_at >= candle_close_epoch_ms",
        "open_or_incomplete_candle": "INADMISSIBLE_NO_CAS",
        "windows_seconds": [900, 3600],
        "both_windows_required_and_mature_before_primary_cas": True,
        "same_source_as_origin_required": True,
        "allowed_public_sources": ["okx", "kraken", "gate", "mexc", "bitget"],
        "unsupported_source_fallback": None,
        "live_current_price_fallback": False,
        "external_directional_market_price_source": False,
        "equal_price_rule": "LOSS_FOR_LONG_AND_SHORT",
        "legacy_without_maturity_or_historical_price_evidence": "RAW_UNVERIFIED",
    }
    write_json(out, "aud-063-historical-price-contract.json", historical)

    race = {
        **common,
        "source_class": "DETERMINISTIC_IN_MEMORY_POSTGREST_CAS_SIMULATION",
        "cas_predicates": ["id=prediction_id", "outcome IS NULL", "audit.outcomes_dual IS NULL"],
        "first_writer": "SUCCESS_ONLY_WITH_RETURNED_CHANGED_ROW",
        "second_writer": "NO_OP_FALSE",
        "http_200_or_204_without_changed_row": "NOT_SUCCESS",
        "restart_before_patch": "SAFE_RETRY",
        "restart_after_patch": "IDEMPOTENT_NO_OVERWRITE",
        "audit_metadata_preserved": True,
        "reconciler_null_writer": False,
        "maturity_validation_before_cas": True,
    }
    write_json(out, "aud-063-race-idempotence.json", race)

    recovery = {
        **common,
        "source_class": "CODE_ONLY_DETERMINISTIC_RECOVERY_SIMULATION_NOT_PRODUCTION",
        "public_baseline_eligible_directional_null": baseline["counts"]["eligible_directional_null_gt_1h"],
        "production_recovery_executed": False,
        "production_rows_mutated": 0,
        "live_potential_settleable": "NOT_ESTIMABLE_WITHOUT_PER_ROW_CAUSAL_PROOF_AND_HISTORICAL_PRICE_READ",
        "synthetic_fixture_counts": {
            "eligible": 7,
            "simulation_settled": 2,
            "unresolved_missing_origin_or_source_proof": 1,
            "unresolved_missing_historical_price": 1,
            "conflict": 1,
            "already_settled": 1,
            "legacy_not_authority_eligible": 1
        },
        "recovery_observation_rule": "persist actual recovery observation time; never backdate to target/prediction time",
        "outcome_authority_rule": "only primary NULL CAS creates outcome; reconciler is already-settled repair only",
    }
    write_json(out, "aud-063-backlog-recovery-simulation.json", recovery)

    learning = {
        **common,
        "source_class": "DETERMINISTIC_CAUSAL_REPLAY_CONTRACT",
        "scenario": {
            "prediction_time": "2026-01-01T00:00:00+00:00",
            "one_hour_target": "2026-01-01T01:00:00+00:00",
            "recovery_observed_at": "2026-01-01T02:00:00+00:00",
            "earlier_decision_cutoff": "2026-01-01T01:30:00+00:00",
            "later_decision_cutoff": "2026-01-01T03:00:00+00:00"
        },
        "earlier_cutoff_source_ids": [],
        "later_cutoff_source_ids_if_fully_proof_qualified": [1],
        "availability_authority": "SETTLEMENT_OBSERVATION_TIME_NOT_TARGET_TIME",
        "same_row_leakage": False,
        "symbol_isolation": True,
        "weight_changes": 0,
    }
    write_json(out, "aud-063-learning-temporal-safety.json", learning)

    authority = {
        **common,
        "source_class": "SANITIZED_PUBLIC_BASELINE_PLUS_FAIL_CLOSED_COUNTERFACTUAL",
        "baseline": {
            "BTCUSDT": baseline["scores"]["BTCUSDT"],
            "ETHUSDT": baseline["scores"]["ETHUSDT"],
        },
        "observable_eligible_directional_backlog": baseline["counts"]["eligible_directional_null_gt_1h"],
        "post_fix_actual_settleable_rows": "NOT_ESTIMABLE_WITHOUT_MUTATING_OR_READING_PRIVATE_PER_ROW_PROOF",
        "post_fix_authority_delta": "NOT_ESTIMABLE",
        "edge_claim": "NO",
        "threshold_changes": 0,
        "weight_tuning": 0,
    }
    write_json(out, "aud-063-authority-impact.json", authority)

    findings = {
        **common,
        "source_class": "AUD063_R1_SOURCE_FORENSICS_AND_REGRESSION_EVIDENCE",
        "findings": [
            {"id":"AUD063-F001","severity":"HIGH","status":"CLOSED","fix":"server-side LONG/SHORT filter + stable ts,id keyset"},
            {"id":"AUD063-F002","severity":"HIGH","status":"CLOSED","fix":"same-source historical evidence tied to origin witness; unsupported source fails closed"},
            {"id":"AUD063-F003","severity":"MEDIUM","status":"CLOSED","fix":"exact 1m containing candle identity"},
            {"id":"AUD063-F004","severity":"HIGH","status":"CLOSED_FAIL_CLOSED_LEGACY","fix":"both price evidence records required by proof gate"},
            {"id":"AUD063-F005","severity":"MEDIUM","status":"CLOSED","fix":"reconciler repair-only; never NULL writer"},
            {"id":"AUD063-F006","severity":"MEDIUM","status":"CLOSED","fix":"backlog/scan/no-progress observability"},
            {"id":"AUD063-F007","severity":"HIGH","status":"CLOSED","fix":"actual persistence observation time governs causal learning"},
            {"id":"R1-F001","severity":"HIGH","status":"CLOSED","root_cause":"containing 1m candle could be current/incomplete at evidence observation","fix":"persist candle_close_epoch_ms; writer and validator reject observed_at before candle close; 60s eligibility buffer","regression":"T28-T30"},
            {"id":"R1-F002","severity":"MEDIUM","status":"CLOSED_BOUNDED","root_cause":"fairness cursor existed only in process memory across verifier cycles","fix":"stateless per-invocation two-page ts,id keyset traversal; restart-safe for first 200 eligible rows each invocation; explicit cap/no universal claim beyond bound","regression":"T4-T7,T31-T32"},
            {"id":"R1-F003","severity":"MEDIUM","status":"CLOSED","root_cause":"obsolete startup resettlement read historical data then called a NULL-only CAS that could never mutate settled rows","fix":"remove obsolete resettlement loop; explicit synchronous zero-read/zero-write quarantine; reconciler remains sole settled-row repair path","regression":"T33"}
        ]
    }
    write_json(out, "aud-063-findings.json", findings)

    report = f"""# AUD-063-R1 — Settlement starvation remediation hardening

Generated: {GENERATED_AT}
Parent AUD-063 head: `0320f47657d4433bfc4dc3396fd0d31ffabe2270`

## Preserved AUD-063 corrections

Server-side LONG/SHORT filtering, stable `(ts,id)` keyset semantics, same-origin public historical evidence, both windows, NULL-only CAS, repair-only reconciler, actual recovery observation time, independent authority N, temporal learning cutoffs, symbol isolation, PAPER/live-capital locks, and external-directional OFF remain intact.

## R1-F001 — closed candle maturity

Historical evidence now persists `candle_close_epoch_ms` and is invalid unless `observed_at >= candle_close_epoch_ms`. The primary selector waits an additional 60 seconds beyond the one-hour horizon, and the validator independently rejects open/incomplete candles. No current ticker, nearest-candle, or alternate-venue fallback is authoritative.

## R1-F002 — restart-safe bounded fairness

The cross-cycle process-memory cursor was removed. Each verifier invocation starts from the oldest eligible directional row and performs at most two 100-row pages using local `(ts,id)` keyset seek. This is restart-safe for the first 200 eligible rows in every invocation. If the visible backlog exceeds 200, diagnostics explicitly report `RESTART_SAFE_PREFIX_ONLY_EXPLICIT_CAP`; there is no universal no-starvation claim beyond that bound. Failed rows remain retryable because scan progress is not persisted.

## R1-F003 — obsolete startup backfill quarantined

The legacy startup resettlement loop was removed. Startup performs only a synchronous zero-read/zero-write quarantine marker, then reaches the primary verifier. Existing settled-row evidence repair remains exclusively in `settlement_reconciler`, which cannot create NULL->WIN/LOSS authority.

## Safety

No production/database/Northflank/GitHub-settings/RUNTIME017 mutation. No merge or deploy. No threshold or weight tuning. External directional activation remains off. Production backlog recovery remains unexecuted and no edge claim is made.
"""
    (out / "../AUD-063-REPORT.md").resolve().parent.mkdir(parents=True, exist_ok=True)
    report_path = (out / "../AUD-063-REPORT.md").resolve()
    report_path.write_text(report, encoding="utf-8")

    artifact_names = [
        "aud-063-runtime-baseline.json", "aud-063-backlog-inventory.json",
        "aud-063-starvation-reproduction.json", "aud-063-selection-contract.json",
        "aud-063-cursor-fairness.json", "aud-063-historical-price-contract.json",
        "aud-063-race-idempotence.json", "aud-063-backlog-recovery-simulation.json",
        "aud-063-learning-temporal-safety.json", "aud-063-authority-impact.json",
        "aud-063-findings.json",
    ]
    inventory = {name: sha256(out / name) for name in artifact_names}
    inventory["../AUD-063-REPORT.md"] = sha256(report_path)
    manifest = {
        **common,
        "source_class": "DETERMINISTIC_SHA256_INVENTORY",
        "prefix_capture": {"run_id": PREFIX_RUN, "artifact_id": PREFIX_ARTIFACT},
        "artifacts": inventory,
        "artifact_count_excluding_manifest": len(inventory),
        "publication_secret_scan_required": True,
        "threshold_changes": 0,
        "weight_changes": 0,
        "external_directional_activation": 0,
        "runtime017_mutations": 0,
        "production_mutations": 0,
        "database_mutations": 0,
        "merge": False,
        "deploy": False,
    }
    write_json(out, "aud-063-manifest.json", manifest)


if __name__ == "__main__":
    main()
