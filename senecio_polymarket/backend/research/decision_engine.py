"""SENECIO research decision engine — AUD-059 hardened.

This module only recommends research patches. It never modifies runtime logic.
For hypothesis families (hour/regime bucket searches), a candidate cannot pass
unless its matching test survives Bonferroni correction in addition to the
existing effect-size, permutation/bootstrap and CPCV checks.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

log = logging.getLogger("senecio.decision_engine")

THRESHOLDS = {
    "delta_wr_pp": 10.0,
    "p_value": 0.05,
    "permutation_p": 0.05,
    "bootstrap_positive": True,
    "cpcv_positive": True,
    "bonferroni_survives": True,
}


def _evaluate_candidate_filter(candidate: dict, study: dict) -> dict:
    cf = candidate.get("filter", {})
    delta_pp = candidate.get("wr_delta_pp", 0.0)
    n_kept = candidate.get("n_kept", 0)
    n_removed = candidate.get("n_removed", 0)
    feature = cf.get("feature")
    bucket = cf.get("excluded_bucket")
    perm_p = None
    if feature == "hour_utc":
        info = study.get("tests", {}).get("permutation_hour_buckets", {}).get(bucket)
        if info:
            perm_p = info.get("p_value")
    elif feature == "regime_4h":
        info = study.get("tests", {}).get("permutation_regime_buckets", {}).get(bucket)
        if info:
            perm_p = info.get("p_value")

    kept_wr = candidate.get("wr_with_filter", 0.0)
    if n_kept > 0:
        z = 1.96
        p = kept_wr
        denom = 1 + z * z / n_kept
        center = (p + z * z / (2 * n_kept)) / denom
        margin = (z * math.sqrt(p * (1 - p) / n_kept + z * z / (4 * n_kept * n_kept))) / denom
        wilson_low = max(0.0, center - margin)
    else:
        wilson_low = 0.0

    cpcv_pbo = study.get("tests", {}).get("cpcv", {}).get("pbo")
    cpcv_positive = bool(cpcv_pbo is not None and cpcv_pbo < 0.5)
    multiple_testing_family = feature in {"hour_utc", "regime_4h"}
    bonf_survives = False
    for test in study.get("tests", {}).get("multiple_testing_corrections", {}).get("tests", []):
        expected = None
        if feature == "hour_utc":
            expected = f"permutation_hour_{bucket}"
        elif feature == "regime_4h":
            expected = f"permutation_regime_{bucket}"
        if expected and test.get("test") == expected:
            bonf_survives = bool(test.get("survives_bonferroni", False))
            break
    bonferroni_passes = bonf_survives if multiple_testing_family else True

    checks = {
        "delta_wr_pp": round(delta_pp, 2),
        "delta_wr_passes": delta_pp >= THRESHOLDS["delta_wr_pp"],
        "p_value": perm_p,
        "p_value_passes": perm_p is not None and perm_p < THRESHOLDS["p_value"],
        "permutation_p": perm_p,
        "permutation_p_passes": perm_p is not None and perm_p < THRESHOLDS["permutation_p"],
        "bootstrap_lower_bound": round(wilson_low, 4),
        "bootstrap_positive": wilson_low > 0.0,
        "cpcv_pbo": cpcv_pbo,
        "cpcv_positive": cpcv_positive,
        "multiple_testing_family": multiple_testing_family,
        "bonferroni_survives": bonf_survives,
        "bonferroni_required": multiple_testing_family,
        "bonferroni_passes": bonferroni_passes,
    }
    all_pass = (
        checks["delta_wr_passes"]
        and checks["p_value_passes"]
        and checks["permutation_p_passes"]
        and checks["bootstrap_positive"]
        and checks["cpcv_positive"]
        and checks["bonferroni_passes"]
    )
    return {
        "candidate": cf,
        "n_kept": n_kept,
        "n_removed": n_removed,
        "wr_with_filter": kept_wr,
        "wr_without_filter": candidate.get("wr_without_filter"),
        "wr_delta_pp": round(delta_pp, 2),
        "checks": checks,
        "all_checks_pass": all_pass,
        "recommendation": "PROPOSE_PATCH" if all_pass else "REJECT_PATCH",
    }


def evaluate_all_candidates(study: dict) -> dict:
    cf_results = (study.get("tests", {}).get("counterfactual_search", {}) or {}).get("results", [])
    if not cf_results:
        return {
            "n_candidates": 0,
            "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
            "candidates": [],
            "any_proposed": False,
            "decision": "REJECT_PATCH",
            "reason": "no counterfactual candidates available",
        }
    evaluations = []
    for cf in cf_results:
        try:
            evaluations.append(_evaluate_candidate_filter(cf, study))
        except Exception as exc:
            log.warning("candidate evaluation failed: %s", exc)
    evaluations.sort(key=lambda item: -item.get("wr_delta_pp", 0))
    proposed = [item for item in evaluations if item["recommendation"] == "PROPOSE_PATCH"]
    return {
        "n_candidates": len(evaluations),
        "n_proposed": len(proposed),
        "n_rejected": len(evaluations) - len(proposed),
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
        "thresholds": THRESHOLDS,
        "candidates": evaluations,
        "any_proposed": bool(proposed),
        "decision": "PROPOSE_PATCH" if proposed else "REJECT_PATCH",
        "top_proposed": proposed[:3],
        "top_rejected": [item for item in evaluations if item["recommendation"] == "REJECT_PATCH"][:3],
    }


def make_decision(study: dict, evidence_progress: dict) -> dict:
    targets_met = evidence_progress.get("all_targets_met", False)
    candidate_eval = evaluate_all_candidates(study)
    if not targets_met:
        return {
            "decision": "DEFER",
            "reason": "evidence targets not yet met",
            "evidence_progress": evidence_progress,
            "candidate_evaluation": candidate_eval,
            "decided_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    return {
        "decision": candidate_eval["decision"],
        "reason": (
            "all required criteria including multiple-testing correction passed for at least one candidate"
            if candidate_eval["any_proposed"]
            else "no candidate passed all required criteria"
        ),
        "evidence_progress": evidence_progress,
        "candidate_evaluation": candidate_eval,
        "decided_at_utc": datetime.now(timezone.utc).isoformat(),
    }
