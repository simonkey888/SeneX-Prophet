#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
BASE_SHA = "2c4dbf284b23d3cf81b93dcfbd262660ab03dd43"
BASE_TREE = "0cb5abaa024f1325bf88e5fd3390dcec8f5f972d"
AUD062_HEAD = "f65e1723953ac23caf1ca3741ec894577c97aae7"
AUD062_TREE = "d8c8b734bdfd0d0a33e31bdd80557e9dafb71b06"
PREFX_RUN = 31894929555
PREFX_JOB = 95036671879


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    learning_path = ROOT / "oracle_runtime" / "institutional_core.py"
    real_path = ROOT / "oracle_runtime" / "institutional_core_real.py"
    oracle_workflow = REPO / ".github" / "workflows" / "oracle.yml"
    governance_path = ROOT / "docs" / "evidence" / "aud-064-governance-ruleset-proposal.json"
    proof_path = ROOT / "backend" / "settlement_proof.py"

    legacy = learning_path.read_text(encoding="utf-8")
    real = real_path.read_text(encoding="utf-8")
    oracle = oracle_workflow.read_text(encoding="utf-8")
    governance = json.loads(governance_path.read_text(encoding="utf-8"))

    old_select = re.search(r'"select":\s*"([^"]+)"', legacy)
    if not old_select:
        raise SystemExit("legacy learning select not found")
    projection = list(__import__("oracle_runtime.institutional_core_real", fromlist=["SHADOW_FETCH_PROJECTION"]).SHADOW_FETCH_PROJECTION)

    source = {
        "version": "AUD-064-source-provenance-v1",
        "base": {"sha": BASE_SHA, "tree": BASE_TREE},
        "aud062_reference": {
            "pr": 58,
            "head": AUD062_HEAD,
            "tree": AUD062_TREE,
            "role": "IMMUTABLE_REFERENCE_ONLY",
        },
        "prefx_reproduction": {
            "run_id": PREFX_RUN,
            "job_id": PREFX_JOB,
            "result": "REPRODUCED_PROJECTION_MISMATCH",
        },
        "candidate_file_sha256": {
            "oracle_runtime/institutional_core_real.py": sha(real_path),
            ".github/workflows/oracle.yml": sha(oracle_workflow),
            "backend/settlement_proof.py": sha(proof_path),
            "docs/evidence/aud-064-governance-ruleset-proposal.json": sha(governance_path),
        },
        "candidate_head_tree_source": "MATERIALIZED_BY_EXACT_HEAD_CI_TO_AVOID_SELF_REFERENCE",
    }

    learning = {
        "version": "AUD-064-learning-projection-v1",
        "before": {
            "projection": old_select.group(1).split(","),
            "exchange_used_present": "exchange_used" in old_select.group(1).split(","),
            "observed_failure": "CANONICAL_PROOF_VALID_BEFORE_PROJECTION_BECOMES_INVALID_AFTER_PROJECTION",
        },
        "after": {
            "projection": projection,
            "exchange_used_present": "exchange_used" in projection,
            "exchange_used_policy": "PERSISTED_VALUE_ONLY_NO_DEFAULT_NO_INFERENCE",
            "canonical_proof_gate": "backend.settlement_proof.is_proof_qualified",
        },
        "causality": {
            "same_symbol": True,
            "horizon_elapsed": True,
            "settlement_observed_at_lte_decision_cutoff": True,
            "independent_nonoverlap_cohort": True,
            "future_or_self_outcome_excluded": True,
        },
    }

    authority = {
        "version": "AUD-064-learning-authority-v1",
        "learning_mutation_authority": "SHADOW_ONLY",
        "production_learning_mutation_enabled": False,
        "size_calibration_authority": "FROZEN_BASE_ONLY",
        "production_mutations": 0,
        "production_decision_weights": "FROZEN_BASE",
        "shadow_fields": ["shadow_mutations", "shadow_weights", "shadow_weights_hash"],
        "no_new_activation_threshold": True,
        "existing_min_learning_examples_unchanged": True,
        "activation_authorized": False,
        "source_assertions": {
            "detached_shadow_core": "shadow_core = LearningSingleDecisionCore()" in real,
            "production_reset_after_shadow": "decision_payload, decision_hash = _frozen_base_state(core)" in real,
            "production_mutation_field_zero": '"mutations": 0' in real,
            "shadow_only_field": '"learning_mutation_authority": "SHADOW_ONLY"' in real,
        },
    }

    workflow = {
        "version": "AUD-064-repo-write-boundary-v1",
        "legacy_oracle_workflow": {
            "permissions_contents_read": "contents: read" in oracle,
            "permissions_contents_write": "contents: write" in oracle,
            "direct_git_push_present": bool(re.search(r"(?m)^\s*git\s+push\b", oracle)),
            "pages_write_present": "pages: write" in oracle,
            "deploy_pages_present": "actions/deploy-pages" in oracle,
            "persist_credentials_false": "persist-credentials: false" in oracle,
        },
        "ruleset": {
            "status": governance["status"],
            "github_settings_applied": governance["github_settings_applied"],
            "required_checks": governance["required_checks"],
            "required_approving_review_count": governance["required_approving_review_count"],
            "require_last_push_approval": governance["require_last_push_approval"],
            "bypass_actors": governance["bypass_actors"],
        },
    }

    if source["aud062_reference"]["role"] != "IMMUTABLE_REFERENCE_ONLY":
        raise SystemExit("reference role changed")
    if learning["before"]["exchange_used_present"]:
        raise SystemExit("pre-fix evidence no longer represents base diagnosis")
    if not learning["after"]["exchange_used_present"]:
        raise SystemExit("fixed projection missing exchange_used")
    if not all(authority["source_assertions"].values()):
        raise SystemExit("shadow-only source assertion failed")
    wf = workflow["legacy_oracle_workflow"]
    if not wf["permissions_contents_read"] or wf["permissions_contents_write"] or wf["direct_git_push_present"] or wf["pages_write_present"] or wf["deploy_pages_present"] or not wf["persist_credentials_false"]:
        raise SystemExit("legacy oracle workflow still has a repository mutation boundary")
    if governance["github_settings_applied"] or governance["required_approving_review_count"] != 0 or governance["bypass_actors"]:
        raise SystemExit("ruleset proposal violates single-owner/unapplied contract")

    write_json(out / "aud-064-source-provenance.json", source)
    write_json(out / "aud-064-learning-projection.json", learning)
    write_json(out / "aud-064-learning-authority.json", authority)
    write_json(out / "aud-064-governance-audit.json", workflow)

    report = f"""# AUD-064 — learning authority freeze integration evidence\n\n- Base: `{BASE_SHA}` / tree `{BASE_TREE}`.\n- AUD-062 reference PR #58: `{AUD062_HEAD}` / tree `{AUD062_TREE}`; immutable reference only.\n- Independent pre-fix reproduction: run `{PREFX_RUN}`, job `{PREFX_JOB}` — `REPRODUCED_PROJECTION_MISMATCH`.\n- Fixed production learning projection reads persisted `exchange_used`; missing/invalid remains excluded by the canonical AUD-063 proof gate.\n- Adaptive replay executes only on a detached shadow core. Production mutations are `0`; production decision/effective weights remain frozen base.\n- No new learning activation threshold is introduced; AUD-064 does not authorize learned weights.\n- AUD-063 settlement/proof files are outside the implementation diff and are guarded by T18–T23 plus the exact-head scope gate.\n- Legacy oracle workflow is read-only and cannot commit/push or deploy Pages.\n- Main ruleset is a proposal only; settings are not applied and one-off AUD checks are excluded from required checks.\n- Exact candidate SHA/tree and changed-path inventory are generated by `AUD_064_EXACT_HEAD_GATE` after the final head is materialized.\n- No edge, calibration, win-rate, EV, or live-capital claim is made by this evidence pack.\n"""
    (out / "AUD-064-REPORT.md").write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
