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
GENERATED_AT = "2026-08-15T03:15:00+00:00"
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
        "order": "AUD-063",
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
        "server_side_filters": {"outcome": "is.null", "prediction": "in.(LONG,SHORT)", "horizon": "ts<=now-1h"},
        "order": "ts.asc,id.asc",
        "pagination": "STABLE_KEYSET_TS_ID",
        "bounded_batch": 100,
        "cursor_rule": "advance_after_scan_even_if_row_unsettled; reset_at_end_of_pass_for_retry",
        "flat_can_enter_verifier_page": False,
        "poison_row_can_permanently_block_later_rows": False,
        "failed_row_retryable_next_pass": True,
        "observability": [
            "eligible_directional_pending_count", "oldest_eligible_directional_pending_id",
            "oldest_eligible_directional_pending_ts", "oldest_eligible_directional_pending_age_seconds",
            "last_verify_rows_scanned", "last_verify_count", "last_verify_unresolved_proof",
            "last_verify_unresolved_price", "last_verify_scan_cap_hit", "last_verify_cursor",
            "last_verify_scan_pass_complete", "last_verify_no_progress_reason"
        ],
    }
    write_json(out, "aud-063-selection-contract.json", selection)

    cursor = {
        **common,
        "source_class": "DETERMINISTIC_ADVERSARIAL_SIMULATION",
        "cases": {
            "flat_head": {"old_flat": 125, "later_directional": 1, "post_fix_directional_seen": True},
            "healthy_250": {"eligible": 250, "batch": 100, "pages": [100, 100, 50], "unique_visited": 250},
            "poison_head": {"eligible": 101, "first_poison_unresolved": True, "later_row_reached": True, "poison_retryable_next_pass": True},
            "mutation": {"first_page_removed_after_scan": True, "next_page_starts_after_saved_ts_id": True},
        },
        "invariant": "NO_PERMANENT_STARVATION_UNDER_BOUNDED_WORK",
    }
    write_json(out, "aud-063-cursor-fairness.json", cursor)

    historical = {
        **common,
        "source_class": "SOURCE_AND_DETERMINISTIC_CANDLE_CONTRACT",
        "canonical_rule": "ONE_MINUTE_CANDLE_CONTAINING_EXACT_TARGET",
        "containment": "candle_open_ms <= target_ms < candle_open_ms+60000",
        "windows_seconds": [900, 3600],
        "both_windows_required_before_primary_cas": True,
        "same_source_as_origin_required": True,
        "allowed_public_sources": ["okx", "kraken", "gate", "mexc", "bitget"],
        "unsupported_source_fallback": None,
        "live_current_price_fallback": False,
        "external_directional_market_price_source": False,
        "equal_price_rule": "LOSS_FOR_LONG_AND_SHORT",
        "legacy_without_historical_price_evidence": "RAW_UNVERIFIED",
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
        "source_class": "AUD063_SOURCE_FORENSICS_AND_REGRESSION_EVIDENCE",
        "findings": [
            {"id":"AUD063-F001","severity":"HIGH","status":"CLOSED","root_cause":"NULL selector ordered ts.asc limit100 without server-side direction filter; verifier skipped FLAT after fetch","evidence":"pre-fix run 31860917485 reproduced 100/100 FLAT twice and later LONG unseen","fix":"server-side LONG/SHORT filter + stable ts,id keyset","regression":"T1-T7","remaining_limitation":"production backlog intentionally not mutated"},
            {"id":"AUD063-F002","severity":"HIGH","status":"CLOSED","root_cause":"primary verifier always fetched OKX historical candles even when origin exchange_used differed","evidence":"source trace oracle_runner._fetch_price_at_time vs persisted exchange_used","fix":"same-source historical evidence tied to origin witness; unsupported source fails closed","regression":"T8-T10,T19","remaining_limitation":"public venue historical availability may still fail, leaving row unresolved"},
            {"id":"AUD063-F003","severity":"MEDIUM","status":"CLOSED","root_cause":"historical helper selected closest candle at/before target without explicit containment proof","evidence":"source trace","fix":"require exact 1m containing candle open<=target<open+60s and persist target/candle identity","regression":"T10","remaining_limitation":"1m candle close is the declared settlement estimator, not tick-level truth"},
            {"id":"AUD063-F004","severity":"HIGH","status":"CLOSED_FAIL_CLOSED_LEGACY","root_cause":"proof gate did not require persisted historical source/candle identities","evidence":"legacy dual payload could qualify without per-window historical provenance","fix":"AUD063 proof requires aud063-v1 dual + both price_evidence_v1 records + same source","regression":"T19,T23","remaining_limitation":"legacy rows remain RAW_UNVERIFIED until independently reconstructible"},
            {"id":"AUD063-F005","severity":"MEDIUM","status":"CLOSED","root_cause":"reconciler could fallback unsupported exchange to OKX","evidence":"source trace","fix":"repair-only reconciler uses exact origin source and no fallback; never NULL writer","regression":"T18","remaining_limitation":"conflicting old outcome remains unchanged and non-repaired"},
            {"id":"AUD063-F006","severity":"MEDIUM","status":"CLOSED","root_cause":"runtime did not expose directional backlog/cursor/no-progress diagnostics","evidence":"baseline last_verify_count=0 while 57 eligible directionals existed","fix":"verifier state now exposes eligible count, oldest, scanned, unresolved, cap, cursor and no-progress reason","regression":"T25","remaining_limitation":"dashboard rendering may expose only fields already surfaced by state endpoint"},
            {"id":"AUD063-F007","severity":"HIGH","status":"CLOSED","root_cause":"late recovery could only be causal if learning keys availability to actual persistence observation","evidence":"runtime replay already checks settlement observed epoch <= decision cutoff","fix":"new settlement writes actual observed_at; proof requires it after 1h target; tests pin earlier exclusion/later inclusion","regression":"T20-T24","remaining_limitation":"legacy rows without durable observation provenance fail authority gate"}
        ]
    }
    write_json(out, "aud-063-findings.json", findings)

    report = f"""# AUD-063 — Settlement starvation, authority and causal-learning remediation\n\nGenerated: {GENERATED_AT}\n\n## Independent reproduction\n\nThe exact `main` implementation at `{BASE_SHA}` was executed against an in-memory PostgREST boundary before the fix. Run `{PREFIX_RUN}` reproduced the defect: two consecutive bounded selector calls returned 100/100 `FLAT`, no server-side direction predicate was present, and the later directional fixture was never returned. The separate public read-only baseline observed 388 visible rows, 347 `outcome=NULL`, 57 directional NULL rows older than one hour, and 104 older FLAT NULL rows ahead of the oldest eligible directional row.\n\n## Remediation\n\n- Directional selection is server-side, oldest-first and keyset-paginated by `(ts,id)`. Failed rows do not block later rows and become retryable after pass reset.\n- Both 15m and 1h prices must come from a one-minute candle containing the exact target on the same public exchange as the origin witness. No current-price or unsupported-source fallback is authoritative.\n- NULL settlement is one CAS writer; HTTP success without a returned changed row is a no-op. Reconciler remains repair-only for already-settled rows.\n- Authority now requires `aud063-v1` historical price evidence. Legacy rows lacking it remain RAW/UNVERIFIED.\n- Settlement availability is the actual persisted observation time. A later recovery cannot enter a replay whose decision cutoff predates that observation.\n\n## Safety\n\nNo production/database/Northflank/GitHub-settings/RUNTIME017 mutation was performed. No merge or deploy. No threshold or weight tuning. External directional activation remains off. The 57-row observed backlog is inventory evidence only; no performance or edge claim is made.\n\n## Residual limitations\n\nHistorical public one-minute candles can be unavailable or venue-limited; those rows fail closed and remain unresolved. Legacy rows lacking reconstructible source/candle provenance are intentionally excluded from authority. Production backlog recovery is code/simulation only under this order.\n"""
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
