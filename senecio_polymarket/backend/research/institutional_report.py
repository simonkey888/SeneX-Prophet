"""
SENECIO ORACLE — ACT XXVIII Module 6: Institutional Report
============================================================

Aggregates outputs from every ACT-XXVIII validation module (walk-forward,
Monte Carlo, statistical battery, capacity, stress tests) — and the
ACT-XXVII modules (calibration, drift, IC, explainability, observability)
that produce evidence — into a single institutional research report.

Outputs:
  1. Robustness Score (0..1) — composite of:
       - Walk-forward pass rate + degradation
       - Monte Carlo ruin probability (inverted)
       - Statistical validation p-values (DSR / PSR / PBO)
       - Stress survival rate
       - Calibration ECE
       - Drift alerts (active count, inverted)
  2. Deployment Readiness Score (0..1) — composite of:
       - Robustness score (50 % weight)
       - Capacity headroom (max deployable / requested)
       - Live-gate status (6 conditions, from existing ACT-XXV gate)
       - Sample size (verified predictions ≥ 300)
  3. Live-gate explanation — explains WHY the gate is currently locked
       or unlocked (consumes the existing gate without modifying it)
  4. Single JSON report combining every metric
  5. Optional HTML / PDF rendering hook (HTML/PDF writer is left as a
     thin wrapper — production reports should be generated from JSON
     via a templating layer, e.g. Jinja2 or reportlab)

The report is stored as JSON under `data/research/institutional_reports/`
so it can be reviewed by a quantitative risk committee or audited later.

STRICT_ADDITIVE — does NOT touch prediction / feature / signal /
verifier / live-gate logic. The live-gate is read-only consumed.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

log = logging.getLogger("senecio.research.institutional_report")


DEFAULTS: dict[str, Any] = {
    "reports_dir":         "data/research/institutional_reports",
    "min_verified_n":      300,
    "min_global_win_rate": 0.52,
    "min_profit_factor":   1.20,
    "max_drawdown_pct":    0.10,
    "capacity_target_usd": 100_000.0,
}


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@dataclass
class RobustnessScorecard:
    """Robustness score (0..1) with per-component breakdown."""
    composite: float
    walk_forward_pass_rate: float
    walk_forward_degradation: float
    monte_carlo_ruin_probability: float
    monte_carlo_p95_drawdown: float
    statistical_dsr_pvalue: float
    statistical_psr: float
    statistical_pbo: float
    stress_survival_rate: float
    stress_worst_max_drawdown: float
    calibration_ece: float
    drift_alerts_active: int
    components: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeploymentReadinessScorecard:
    """Deployment readiness score (0..1) with per-component breakdown."""
    composite: float
    robustness_score: float
    capacity_headroom_ratio: float
    live_gate_unlocked: bool
    live_gate_pass_count: int
    live_gate_total: int
    verified_predictions_n: int
    components: dict[str, float] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LiveGateExplanation:
    """Human-readable explanation of the current live-gate state."""
    unlocked: bool
    pass_count: int
    total: int
    conditions: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InstitutionalReport:
    """Single comprehensive institutional research report."""
    run_at: str
    version: str
    summary: str
    n_trades: int
    n_predictions: int
    walk_forward: dict[str, Any] = field(default_factory=dict)
    monte_carlo: dict[str, Any] = field(default_factory=dict)
    statistical: dict[str, Any] = field(default_factory=dict)
    capacity: dict[str, Any] = field(default_factory=dict)
    stress: dict[str, Any] = field(default_factory=dict)
    calibration: dict[str, Any] = field(default_factory=dict)
    drift: dict[str, Any] = field(default_factory=dict)
    research_metrics: dict[str, Any] = field(default_factory=dict)
    explainability: dict[str, Any] = field(default_factory=dict)
    observability: dict[str, Any] = field(default_factory=dict)
    robustness: dict[str, Any] = field(default_factory=dict)
    readiness: dict[str, Any] = field(default_factory=dict)
    live_gate_explanation: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), default=str, indent=indent)

    def persist(self, reports_dir: Optional[str] = None) -> Optional[Path]:
        out_dir = Path(reports_dir or DEFAULTS["reports_dir"])
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = out_dir / f"institutional_report_{ts}.json"
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.to_json(indent=2))
            # Also append a JSONL index entry
            idx = out_dir / "institutional_reports.jsonl"
            with open(idx, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "run_at": self.run_at,
                    "version": self.version,
                    "path": str(path),
                    "robustness": self.robustness.get("composite", 0.0),
                    "readiness":  self.readiness.get("composite", 0.0),
                    "n_trades":   self.n_trades,
                    "n_predictions": self.n_predictions,
                }) + "\n")
            return path
        except Exception as e:
            log.warning("failed to persist institutional report: %s", e)
            return None

    def to_html(self) -> str:
        """Render a minimal standalone HTML view of the report."""
        r = self.to_dict()
        sections: list[str] = []
        sections.append(f"<h1>SENECIO ORACLE — Institutional Report</h1>")
        sections.append(f"<p><em>Generated {r['run_at']}</em></p>")
        sections.append(f"<p>{r['summary']}</p>")
        sections.append(
            f"<h2>Robustness: {r['robustness'].get('composite', 0):.3f} "
            f"/ Readiness: {r['readiness'].get('composite', 0):.3f}</h2>"
        )
        for name in [
            "walk_forward", "monte_carlo", "statistical", "capacity",
            "stress", "calibration", "drift", "research_metrics",
            "explainability", "observability", "robustness", "readiness",
            "live_gate_explanation",
        ]:
            v = r.get(name, {})
            if not v:
                continue
            sections.append(f"<h3>{name.replace('_', ' ').title()}</h3>")
            sections.append("<pre>" + json.dumps(v, default=str, indent=2) + "</pre>")
        if r.get("errors"):
            sections.append("<h3>Errors</h3><ul>")
            for e in r["errors"]:
                sections.append(f"<li>{e}</li>")
            sections.append("</ul>")
        return (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>SENECIO Institutional Report</title>"
            "<style>body{font-family:Inter,Arial,sans-serif;margin:2em;}"
            "pre{background:#f4f4f4;padding:1em;overflow:auto;}</style>"
            "</head><body>" + "\n".join(sections) + "</body></html>"
        )

    def persist_html(self, reports_dir: Optional[str] = None) -> Optional[Path]:
        out_dir = Path(reports_dir or DEFAULTS["reports_dir"])
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = out_dir / f"institutional_report_{ts}.html"
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.to_html())
            return path
        except Exception as e:
            log.warning("failed to persist HTML report: %s", e)
            return None


# ---------------------------------------------------------------------------
# Scorecard builders
# ---------------------------------------------------------------------------


def _safe_get(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
    return cur if cur is not None else default


def build_robustness_scorecard(
    walk_forward_report: Optional[dict[str, Any]] = None,
    monte_carlo_report: Optional[dict[str, Any]] = None,
    statistical_report: Optional[dict[str, Any]] = None,
    stress_report: Optional[dict[str, Any]] = None,
    calibration_report: Optional[dict[str, Any]] = None,
    drift_stats: Optional[dict[str, Any]] = None,
    extra: Optional[dict[str, Any]] = None,
) -> RobustnessScorecard:
    """Compute the composite robustness scorecard from sub-module outputs."""
    components: dict[str, float] = {}
    notes: list[str] = []

    # Walk-forward (weight 0.20)
    wf_pass = 0.5
    wf_deg = 1.0
    if walk_forward_report:
        wf = walk_forward_report
        wf_pass = float(_safe_get(wf, "stability", "pass_rate", default=0.5))
        wf_deg = float(_safe_get(wf, "stability", "degradation_ratio", default=1.0))
        if not math.isfinite(wf_deg) or wf_deg == 0:
            wf_deg = 1.0
    # degradation: 1.0 = no degradation; map |deg-1|>0.5 → 0
    deg_score = max(0.0, 1.0 - abs(wf_deg - 1.0) * 2.0)
    wf_score = 0.5 * wf_pass + 0.5 * deg_score
    components["walk_forward"] = float(wf_score)

    # Monte Carlo (weight 0.20) — penalise ruin probability
    mc_ruin = 0.5
    mc_p95_dd = -0.10
    if monte_carlo_report:
        mc = monte_carlo_report
        mc_ruin = float(_safe_get(mc, "ruin_probability", default=0.5))
        mc_p95_dd = float(_safe_get(mc, "drawdown_distribution", "p95", default=-0.10))
    # ruin score: 1.0 at ruin_prob=0, 0.0 at ruin_prob=1
    ruin_score = max(0.0, 1.0 - mc_ruin)
    # dd score: 1.0 if p95 dd >= -0.05, 0.0 if <= -0.30
    dd_norm = (mc_p95_dd + 0.30) / 0.25  # → 1 at -0.05, 0 at -0.30
    dd_score = max(0.0, min(1.0, dd_norm))
    mc_score = 0.5 * ruin_score + 0.5 * dd_score
    components["monte_carlo"] = float(mc_score)

    # Statistical (weight 0.20) — DSR p-value + PSR + PBO
    dsr_p = 1.0
    psr_v = 0.0
    pbo_v = 1.0
    if statistical_report:
        sr = statistical_report
        dsr_p = float(_safe_get(sr, "deflated_sharpe", "p_value", default=1.0))
        psr_v = float(_safe_get(sr, "probabilistic_sharpe", "psr", default=0.0))
        pbo_v = float(_safe_get(sr, "pbo", "pbo", default=1.0))
    dsr_score = 1.0 - max(0.0, min(1.0, dsr_p))   # low p = high score
    psr_score = max(0.0, min(1.0, psr_v))
    pbo_score = 1.0 - max(0.0, min(1.0, pbo_v))
    stat_score = (dsr_score + psr_score + pbo_score) / 3.0
    components["statistical"] = float(stat_score)

    # Stress (weight 0.20)
    stress_surv = 0.5
    stress_worst_dd = -0.20
    if stress_report:
        stress_surv = float(_safe_get(stress_report, "aggregate", "survival_rate", default=0.5))
        stress_worst_dd = float(_safe_get(stress_report, "aggregate", "worst_max_drawdown", default=-0.20))
    dd_norm = (stress_worst_dd + 0.30) / 0.25
    dd_score = max(0.0, min(1.0, dd_norm))
    stress_score = 0.5 * stress_surv + 0.5 * dd_score
    components["stress"] = float(stress_score)

    # Calibration (weight 0.10) — ECE
    ece = 0.5
    if calibration_report:
        ece = float(_safe_get(calibration_report, "ece_after", default=0.5))
    cal_score = max(0.0, 1.0 - min(1.0, ece * 4.0))  # ECE 0.25 → 0
    components["calibration"] = float(cal_score)

    # Drift (weight 0.10) — penalise active alerts
    drift_alerts = 0
    if drift_stats:
        # drift_stats may have many forms; try common keys
        drift_alerts = int(drift_stats.get("active_alerts",
                          drift_stats.get("n_alerts", 0)) or 0)
    drift_score = max(0.0, 1.0 - 0.25 * drift_alerts)  # 4 alerts → 0
    components["drift"] = float(drift_score)

    # Composite
    weights = {
        "walk_forward": 0.20,
        "monte_carlo":  0.20,
        "statistical":  0.20,
        "stress":       0.20,
        "calibration":  0.10,
        "drift":        0.10,
    }
    composite = sum(weights[k] * components.get(k, 0.0) for k in weights)
    composite = max(0.0, min(1.0, composite))

    # Optional notes
    if mc_ruin > 0.05:
        notes.append(f"Monte Carlo ruin probability elevated: {mc_ruin:.3f}")
    if stress_surv < 1.0:
        notes.append(f"Stress survival incomplete: {stress_surv:.2f}")
    if pbo_v > 0.5:
        notes.append(f"PBO elevated: {pbo_v:.3f} (likely backtest overfit)")
    if ece > 0.10:
        notes.append(f"Calibration ECE high: {ece:.3f}")

    return RobustnessScorecard(
        composite=float(composite),
        walk_forward_pass_rate=float(wf_pass),
        walk_forward_degradation=float(wf_deg),
        monte_carlo_ruin_probability=float(mc_ruin),
        monte_carlo_p95_drawdown=float(mc_p95_dd),
        statistical_dsr_pvalue=float(dsr_p),
        statistical_psr=float(psr_v),
        statistical_pbo=float(pbo_v),
        stress_survival_rate=float(stress_surv),
        stress_worst_max_drawdown=float(stress_worst_dd),
        calibration_ece=float(ece),
        drift_alerts_active=int(drift_alerts),
        components=components,
        notes=notes,
    )


def build_readiness_scorecard(
    robustness_score: float,
    capacity_report: Optional[dict[str, Any]] = None,
    capacity_target_usd: float = 100_000.0,
    live_gate_state: Optional[dict[str, Any]] = None,
    verified_predictions_n: int = 0,
    min_verified_n: int = 300,
    extra: Optional[dict[str, Any]] = None,
) -> DeploymentReadinessScorecard:
    """Compute deployment readiness from robustness + capacity + gate."""
    components: dict[str, float] = {}
    blockers: list[str] = []
    notes: list[str] = []

    # Robustness (50 %)
    components["robustness"] = float(max(0.0, min(1.0, robustness_score)))

    # Capacity headroom (20 %)
    max_capital = 0.0
    if capacity_report:
        max_capital = float(capacity_report.get("max_deployable_capital", 0.0))
    headroom_ratio = (max_capital / max(capacity_target_usd, 1.0)) if max_capital > 0 else 0.0
    headroom_ratio = max(0.0, min(1.0, headroom_ratio))
    components["capacity_headroom"] = headroom_ratio
    if max_capital < capacity_target_usd:
        blockers.append(
            f"capacity {max_capital:.0f} < target {capacity_target_usd:.0f}"
        )

    # Live gate (20 %)
    gate_unlocked = False
    pass_count = 0
    total = 6
    if live_gate_state:
        gate_unlocked = bool(live_gate_state.get("unlocked", False))
        pass_count = int(live_gate_state.get("pass_count",
                         live_gate_state.get("conditions_passed", 0)))
        total = int(live_gate_state.get("total",
                   live_gate_state.get("conditions_total", 6)))
    gate_score = pass_count / max(total, 1)
    components["live_gate"] = float(gate_score)
    if not gate_unlocked:
        blockers.append("live_gate locked")

    # Sample size (10 %)
    sample_score = min(1.0, verified_predictions_n / max(min_verified_n, 1))
    components["sample_size"] = float(sample_score)
    if verified_predictions_n < min_verified_n:
        blockers.append(
            f"verified predictions {verified_predictions_n} < {min_verified_n}"
        )

    weights = {
        "robustness":         0.50,
        "capacity_headroom":  0.20,
        "live_gate":          0.20,
        "sample_size":        0.10,
    }
    composite = sum(weights[k] * components.get(k, 0.0) for k in weights)
    composite = max(0.0, min(1.0, composite))

    if composite >= 0.8:
        notes.append("Ready for staged capital allocation (paper → small live)")
    elif composite >= 0.6:
        notes.append("Approaching readiness — close blockers before live capital")
    else:
        notes.append("Not ready — multiple blockers remain")

    return DeploymentReadinessScorecard(
        composite=float(composite),
        robustness_score=float(robustness_score),
        capacity_headroom_ratio=float(headroom_ratio),
        live_gate_unlocked=bool(gate_unlocked),
        live_gate_pass_count=int(pass_count),
        live_gate_total=int(total),
        verified_predictions_n=int(verified_predictions_n),
        components=components,
        blockers=blockers,
        notes=notes,
    )


def explain_live_gate(
    live_gate_state: Optional[dict[str, Any]] = None,
) -> LiveGateExplanation:
    """Build a human-readable explanation of the live-gate state.

    The `live_gate_state` dict is read-only — it is whatever the existing
    ACT-XXV LiveGate evaluator returned. We just consume it for
    presentation; we do not modify the gate logic.
    """
    if not live_gate_state:
        return LiveGateExplanation(
            unlocked=False, pass_count=0, total=6,
            conditions=[], blockers=["live_gate_state unavailable"],
            summary="Live gate state not provided — assumed locked.",
        )
    unlocked = bool(live_gate_state.get("unlocked", False))
    pass_count = int(live_gate_state.get("pass_count",
                     live_gate_state.get("conditions_passed", 0)))
    total = int(live_gate_state.get("total",
                 live_gate_state.get("conditions_total", 6)))
    failed_reasons = live_gate_state.get("failed_reasons", [])
    conditions_raw = live_gate_state.get("conditions", [])
    conditions: list[dict[str, Any]] = []
    if isinstance(conditions_raw, dict):
        for name, condition in conditions_raw.items():
            condition = condition if isinstance(condition, dict) else {}
            conditions.append({
                "name": name,
                "passed": bool(condition.get("pass", False)),
                "actual": condition.get("value"),
                "required": condition.get("threshold"),
                "detail": condition.get("detail", ""),
            })
    else:
        for condition in conditions_raw:
            condition = condition if isinstance(condition, dict) else {}
            conditions.append({
                "name": condition.get("name", "?"),
                "passed": bool(condition.get("passed", False)),
                "actual": condition.get("actual"),
                "required": condition.get("required"),
                "detail": condition.get("detail", ""),
            })
    blockers: list[str] = []
    if not unlocked:
        for r in failed_reasons:
            blockers.append(str(r))
    if unlocked:
        summary = (
            f"LIVE GATE UNLOCKED — all {total} conditions met. "
            "Live capital allocation permitted (manual review still advised)."
        )
    else:
        summary = (
            f"LIVE GATE LOCKED — {pass_count}/{total} conditions met. "
            f"{len(blockers)} blocker(s) remain."
        )
    return LiveGateExplanation(
        unlocked=unlocked,
        pass_count=pass_count,
        total=total,
        conditions=conditions,
        blockers=blockers,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------


def build_institutional_report(
    n_trades: int = 0,
    n_predictions: int = 0,
    walk_forward_report: Optional[dict[str, Any]] = None,
    monte_carlo_report: Optional[dict[str, Any]] = None,
    statistical_report: Optional[dict[str, Any]] = None,
    capacity_report: Optional[dict[str, Any]] = None,
    stress_report: Optional[dict[str, Any]] = None,
    calibration_report: Optional[dict[str, Any]] = None,
    drift_stats: Optional[dict[str, Any]] = None,
    research_metrics_report: Optional[dict[str, Any]] = None,
    explainer_stats: Optional[dict[str, Any]] = None,
    observability_snapshot: Optional[dict[str, Any]] = None,
    live_gate_state: Optional[dict[str, Any]] = None,
    verified_predictions_n: int = 0,
    capacity_target_usd: float = 100_000.0,
    min_verified_n: int = 300,
    extra: Optional[dict[str, Any]] = None,
    persist: bool = True,
    persist_html: bool = False,
    reports_dir: Optional[str] = None,
) -> InstitutionalReport:
    """Build the full institutional research report.

    All inputs are dicts (the `.to_dict()` of each sub-module's report
    dataclass).  Missing inputs are tolerated — the corresponding
    section will be empty and the scorecard will fall back to neutral
    values.

    Returns an `InstitutionalReport` dataclass that can be serialised
    to JSON or HTML.
    """
    run_at = datetime.now(timezone.utc).isoformat()
    robust = build_robustness_scorecard(
        walk_forward_report=walk_forward_report,
        monte_carlo_report=monte_carlo_report,
        statistical_report=statistical_report,
        stress_report=stress_report,
        calibration_report=calibration_report,
        drift_stats=drift_stats,
        extra=extra,
    )
    readiness = build_readiness_scorecard(
        robustness_score=robust.composite,
        capacity_report=capacity_report,
        capacity_target_usd=capacity_target_usd,
        live_gate_state=live_gate_state,
        verified_predictions_n=verified_predictions_n,
        min_verified_n=min_verified_n,
        extra=extra,
    )
    gate_exp = explain_live_gate(live_gate_state)

    summary = (
        f"Institutional research report — "
        f"robustness {robust.composite:.3f}, readiness {readiness.composite:.3f}. "
        f"Gate: {'UNLOCKED' if gate_exp.unlocked else 'LOCKED'} "
        f"({gate_exp.pass_count}/{gate_exp.total}). "
        f"n_trades={n_trades}, n_predictions={n_predictions}."
    )

    report = InstitutionalReport(
        run_at=run_at,
        version="ACT-XXVIII-institutional-validation",
        summary=summary,
        n_trades=int(n_trades),
        n_predictions=int(n_predictions),
        walk_forward=walk_forward_report or {},
        monte_carlo=monte_carlo_report or {},
        statistical=statistical_report or {},
        capacity=capacity_report or {},
        stress=stress_report or {},
        calibration=calibration_report or {},
        drift=drift_stats or {},
        research_metrics=research_metrics_report or {},
        explainability=explainer_stats or {},
        observability=observability_snapshot or {},
        robustness=robust.to_dict(),
        readiness=readiness.to_dict(),
        live_gate_explanation=gate_exp.to_dict(),
        config={
            "capacity_target_usd": float(capacity_target_usd),
            "min_verified_n":      int(min_verified_n),
        },
        extra=dict(extra or {}),
    )

    if persist:
        report.persist(reports_dir=reports_dir)
    if persist_html:
        report.persist_html(reports_dir=reports_dir)
    return report


__all__ = [
    "RobustnessScorecard",
    "DeploymentReadinessScorecard",
    "LiveGateExplanation",
    "InstitutionalReport",
    "DEFAULTS",
    "build_robustness_scorecard",
    "build_readiness_scorecard",
    "explain_live_gate",
    "build_institutional_report",
]
