"""AUD-066-R1 inference and robustness helpers (stdlib only, research branch only)."""
from __future__ import annotations

import copy
from collections import defaultdict

from aud066_liquidation import (
    BASE_FEATURES,
    REALIZED_FEATURES,
    NORMALIZED_FEATURES,
    fit_logit,
    predict,
)
from aud066_analysis import metrics

ARMS = {
    "A": BASE_FEATURES,
    "B": BASE_FEATURES + REALIZED_FEATURES,
    "C": BASE_FEATURES + REALIZED_FEATURES + NORMALIZED_FEATURES,
}

REALIZED_1M = (
    "long_liq_usd_30s", "short_liq_usd_30s",
    "long_liq_usd_1m", "short_liq_usd_1m",
    "net_forced_flow_1m", "liq_imbalance_1m",
    "liq_acceleration", "liq_burst_zscore",
)
REALIZED_5M = (
    "long_liq_usd_5m", "short_liq_usd_5m",
    "net_forced_flow_5m", "liq_imbalance_5m",
    "liq_burst_zscore",
)

LEGITIMATE_PERTURBATIONS = (
    "clock_minus_1m",
    "clock_plus_1m",
    "missing_liquidations_10pct",
    "reconnect_fail_closed",
)


def _orientation(db: float | None, dl: float | None) -> str:
    if db is None or dl is None:
        return "NOT_TESTABLE"
    if db < 0 and dl < 0:
        return "IMPROVEMENT"
    if db > 0 and dl > 0:
        return "DEGRADATION"
    return "MIXED"


def terminal_inference(proxy_result: dict, baseline_parity: str) -> dict:
    """Fail-closed terminal contract for R1.

    NO is deliberately unavailable because AUD-066 did not predeclare an
    equivalence/no-value margin before terminal data were observed. Crossing-zero
    intervals therefore remain INCONCLUSIVE rather than being mapped to NO.
    """
    if baseline_parity != "PASS":
        return {
            "REALIZED_LIQUIDATION_VALUE": "INCONCLUSIVE",
            "NET_NEW_VALUE": "INCONCLUSIVE",
            "reason": "CURRENT_SENEX_BASELINE_PARITY_NOT_PROVEN",
            "no_value_equivalence_criterion": "NOT_PREDECLARED_IN_PARENT_ORDER",
        }

    if proxy_result.get("status") != "COMPLETE":
        return {
            "REALIZED_LIQUIDATION_VALUE": "INCONCLUSIVE",
            "NET_NEW_VALUE": "INCONCLUSIVE",
            "reason": "INCOMPLETE_OOS_EVIDENCE",
            "no_value_equivalence_criterion": "NOT_PREDECLARED_IN_PARENT_ORDER",
        }

    # A positive claim remains allowed only under the original strict positive gate.
    if proxy_result.get("ci_excludes_zero_pass") is True and proxy_result.get("mean_improvement_pass") is True:
        return {
            "REALIZED_LIQUIDATION_VALUE": "YES",
            "NET_NEW_VALUE": "YES",
            "reason": "STRICT_POSITIVE_GATE_PASSED_AGAINST_PARITY_BASELINE",
            "no_value_equivalence_criterion": "NOT_PREDECLARED_IN_PARENT_ORDER",
        }

    # Absence of proof of improvement is not proof of no value.
    return {
        "REALIZED_LIQUIDATION_VALUE": "INCONCLUSIVE",
        "NET_NEW_VALUE": "INCONCLUSIVE",
        "reason": "UNCERTAINTY_SPANS_POTENTIAL_BENEFIT_AND_HARM",
        "no_value_equivalence_criterion": "NOT_PREDECLARED_IN_PARENT_ORDER",
    }


def _quantile(values, q: float) -> float:
    xs = sorted(float(x) for x in values)
    if not xs:
        return 0.0
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def _day_rows(samples: list[dict]) -> dict[str, list[dict]]:
    out = defaultdict(list)
    for row in samples:
        out[row["day"]].append(row)
    return dict(out)


def _delta(m: dict, a: dict) -> dict:
    return {
        "n": m.get("n", 0),
        "brier_delta_vs_A": (m.get("brier") - a.get("brier")) if m.get("brier") is not None and a.get("brier") is not None else None,
        "log_loss_delta_vs_A": (m.get("log_loss") - a.get("log_loss")) if m.get("log_loss") is not None and a.get("log_loss") is not None else None,
    }


def _mean_delta(records: list[dict]) -> dict:
    if not records:
        return {"n_blocks": 0, "mean_brier_delta_vs_A": None, "mean_log_loss_delta_vs_A": None, "orientation": "NOT_TESTABLE"}
    db = [r["brier_delta_vs_A"] for r in records if r.get("brier_delta_vs_A") is not None]
    dl = [r["log_loss_delta_vs_A"] for r in records if r.get("log_loss_delta_vs_A") is not None]
    mb = sum(db) / len(db) if db else None
    ml = sum(dl) / len(dl) if dl else None
    return {
        "n_blocks": len(records),
        "mean_brier_delta_vs_A": mb,
        "mean_log_loss_delta_vs_A": ml,
        "orientation": _orientation(mb, ml),
    }


def _caps(train: list[dict], names: tuple[str, ...] | list[str]) -> dict[str, tuple[float, float]]:
    out = {}
    for name in names:
        xs = [float(r["features"].get(name, 0.0)) for r in train]
        out[name] = (_quantile(xs, 0.01), _quantile(xs, 0.99))
    return out


def _clip_rows(rows: list[dict], caps: dict[str, tuple[float, float]]) -> list[dict]:
    out = []
    for row in rows:
        x = copy.deepcopy(row)
        for name, (lo, hi) in caps.items():
            v = float(x["features"].get(name, 0.0))
            x["features"][name] = max(lo, min(hi, v))
        out.append(x)
    return out


def _metric_set(models: dict, rows: list[dict]) -> dict:
    y = [r["y"] for r in rows]
    return {arm: metrics(y, predict(model, rows)) for arm, model in models.items()}


def evaluate_robustness(primary_samples: list[dict], variants: dict[str, list[dict]], proxy_result: dict) -> dict:
    """Execute the AUD-066-R1 zero-cost stress matrix without terminal-label tuning."""
    days = sorted({r["day"] for r in primary_samples})
    if len(days) < 7:
        return {"status": "PARTIAL_NOT_TESTABLE", "reason": "INSUFFICIENT_INDEPENDENT_DAYS", "tests": {}}

    by_day = _day_rows(primary_samples)
    variant_by_day = {name: _day_rows(rows) for name, rows in variants.items()}
    variant_deltas = {name: {"B": [], "C": []} for name in variants}
    regime_records = []
    window_deltas = {"W1": [], "W5": []}
    winsor_deltas = {"B": [], "C": []}
    not_testable = []

    for td in days[-3:]:
        i = days.index(td)
        vd = days[i - 1]
        trdays = days[: i - 1]
        train = [r for d in trdays for r in by_day.get(d, [])]
        test = by_day.get(td, [])
        if min(len(train), len(test)) < 30:
            not_testable.append(f"BLOCK:{td}:INSUFFICIENT_N")
            continue

        models = {arm: fit_logit(train, names) for arm, names in ARMS.items()}
        primary_metrics = _metric_set(models, test)

        # Regime thresholds are derived from TRAIN ONLY; terminal labels never choose cut points.
        thresholds = {
            "VOL": _quantile([r["features"].get("volatility_5m", 0.0) for r in train], 0.50),
            "DEPTH": _quantile([r.get("context", {}).get("depth_usd", 0.0) for r in train], 0.50),
            "SPREAD": _quantile([r["features"].get("spread_pct", 0.0) for r in train], 0.50),
            "OI_CHANGE": _quantile([abs(r["features"].get("oi_delta_5m", 0.0)) for r in train], 0.50),
            "LIQ_BURST": _quantile([r["features"].get("liq_burst_zscore", 0.0) for r in train], 0.75),
        }
        families = {
            "HIGH_VOL": lambda r: r["features"].get("volatility_5m", 0.0) >= thresholds["VOL"],
            "LOW_VOL": lambda r: r["features"].get("volatility_5m", 0.0) < thresholds["VOL"],
            "HIGH_DEPTH": lambda r: r.get("context", {}).get("depth_usd", 0.0) >= thresholds["DEPTH"],
            "LOW_DEPTH": lambda r: r.get("context", {}).get("depth_usd", 0.0) < thresholds["DEPTH"],
            "WIDE_SPREAD": lambda r: r["features"].get("spread_pct", 0.0) >= thresholds["SPREAD"],
            "TIGHT_SPREAD": lambda r: r["features"].get("spread_pct", 0.0) < thresholds["SPREAD"],
            "HIGH_OI_CHANGE": lambda r: abs(r["features"].get("oi_delta_5m", 0.0)) >= thresholds["OI_CHANGE"],
            "LOW_OI_CHANGE": lambda r: abs(r["features"].get("oi_delta_5m", 0.0)) < thresholds["OI_CHANGE"],
            "LIQUIDATION_BURST": lambda r: r["features"].get("liq_burst_zscore", 0.0) >= thresholds["LIQ_BURST"],
            "NORMAL_LIQUIDATION": lambda r: r["features"].get("liq_burst_zscore", 0.0) < thresholds["LIQ_BURST"],
        }
        for name, pred in families.items():
            subset = [r for r in test if pred(r)]
            if len(subset) < 30:
                regime_records.append({"test_day": td, "regime": name, "status": "NOT_TESTABLE", "n": len(subset), "train_thresholds": thresholds})
                not_testable.append(f"REGIME:{td}:{name}:N={len(subset)}")
                continue
            mm = _metric_set(models, subset)
            regime_records.append({
                "test_day": td,
                "regime": name,
                "status": "TESTED",
                "n": len(subset),
                "train_thresholds": thresholds,
                "B": _delta(mm["B"], mm["A"]),
                "C": _delta(mm["C"], mm["A"]),
            })

        # Timing/data perturbations use the SAME models fitted on unperturbed train data.
        for vname, daymap in variant_by_day.items():
            rows = daymap.get(td, [])
            if len(rows) < 30:
                not_testable.append(f"VARIANT:{td}:{vname}:N={len(rows)}")
                continue
            mm = _metric_set(models, rows)
            for arm in ("B", "C"):
                variant_deltas[vname][arm].append(_delta(mm[arm], mm["A"]))

        # Feature-window sensitivity: predeclared 1m and 5m realized-flow families.
        for wname, names in (("W1", BASE_FEATURES + REALIZED_1M), ("W5", BASE_FEATURES + REALIZED_5M)):
            model = fit_logit(train, names)
            mm = metrics([r["y"] for r in test], predict(model, test))
            window_deltas[wname].append(_delta(mm, primary_metrics["A"]))

        # Train-only p01/p99 winsorization. No terminal-test value chooses a cap.
        union = tuple(dict.fromkeys(ARMS["C"]))
        caps = _caps(train, union)
        train_w = _clip_rows(train, caps)
        test_w = _clip_rows(test, caps)
        models_w = {arm: fit_logit(train_w, names) for arm, names in ARMS.items()}
        mmw = _metric_set(models_w, test_w)
        for arm in ("B", "C"):
            winsor_deltas[arm].append(_delta(mmw[arm], mmw["A"]))

    variant_summary = {
        name: {arm: _mean_delta(records) for arm, records in arms.items()}
        for name, arms in variant_deltas.items()
    }
    window_summary = {name: _mean_delta(records) for name, records in window_deltas.items()}
    winsor_summary = {arm: _mean_delta(records) for arm, records in winsor_deltas.items()}

    primary_orientation = {}
    for arm in ("B", "C"):
        d = (proxy_result.get("deltas_vs_A") or {}).get(arm, {})
        primary_orientation[arm] = _orientation(d.get("mean_brier_delta"), d.get("mean_log_loss_delta"))

    fragility_reasons = []
    for vname in LEGITIMATE_PERTURBATIONS:
        for arm in ("B", "C"):
            alt = variant_summary.get(vname, {}).get(arm, {}).get("orientation")
            pri = primary_orientation.get(arm)
            if alt not in (None, "NOT_TESTABLE", "MIXED") and pri not in (None, "NOT_TESTABLE", "MIXED") and alt != pri:
                fragility_reasons.append(f"{arm}:{vname}:{pri}->{alt}")

    # Small, predeclared feature-window changes changing sign are also fragility.
    w1 = window_summary["W1"]["orientation"]
    w5 = window_summary["W5"]["orientation"]
    if w1 not in ("NOT_TESTABLE", "MIXED") and w5 not in ("NOT_TESTABLE", "MIXED") and w1 != w5:
        fragility_reasons.append(f"FEATURE_WINDOW:{w1}->{w5}")

    for arm in ("B", "C"):
        win = winsor_summary[arm]["orientation"]
        pri = primary_orientation.get(arm)
        if win not in ("NOT_TESTABLE", "MIXED") and pri not in (None, "NOT_TESTABLE", "MIXED") and win != pri:
            fragility_reasons.append(f"{arm}:WINSORIZATION:{pri}->{win}")

    if fragility_reasons:
        status = "FRAGILE"
    elif not_testable:
        status = "PARTIAL_NOT_TESTABLE"
    else:
        status = "PASS"

    return {
        "status": status,
        "terminal_days": days[-3:],
        "primary_proxy_orientation": primary_orientation,
        "regime_matrix": regime_records,
        "perturbation_matrix": variant_summary,
        "feature_window_sensitivity": window_summary,
        "winsorization_sensitivity": winsor_summary,
        "not_testable": sorted(set(not_testable)),
        "fragility_reasons": fragility_reasons,
        "predeclared_rules": {
            "regime_cutpoints": "train-only median; liquidation burst=train-only q75",
            "winsorization": "train-only p01/p99",
            "clock_alignment": "whole-minute grid offsets -1m/+1m",
            "missing_packet": "deterministic 10% realized-liquidation event removal",
            "reconnect": "deterministic 5-minute fail-closed sample gaps at 06:00/12:00/18:00 UTC",
            "fragility": "aggregate proper-score orientation flips under legitimate timing/data perturbation, winsorization, or 1m-vs-5m window sign flip",
        },
        "exchange_timestamp_sensitivity_authority": "DIAGNOSTIC_ONLY_NON_CAUSAL",
        "one_source_removal_authority": "FAIL_CLOSED_DIAGNOSTIC_ONLY",
    }
