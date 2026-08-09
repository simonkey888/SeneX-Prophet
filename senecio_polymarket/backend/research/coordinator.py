"""
SENECIO ORACLE — ACT XXVII: Research Coordinator
=================================================

Ties all 6 ACT-XXVII research modules together. The coordinator consumes
existing prediction records (from `predictions.jsonl` or `supabase_client`)
and exposes a unified `run_full_research_pass()` that:

  1. Loads predictions + outcomes from disk / Supabase
  2. Builds a feature matrix X from confidence × ev × price-change fields
     (NO modification to predict_only.py — uses only fields already
     persisted in the prediction record)
  3. Runs PurgedKFold + CPCV on (X, y) and stores validation reports
  4. Fits Isotonic + Platt + Beta calibrators on (y_prob=confidence, y_true)
     and stores reliability curves
  5. Updates the DriftMonitor with the latest confidence stream
  6. Computes research metrics (IC, rolling Sharpe/PF/MDD)
  7. Fits an Explainer (surrogate model) and persists feature importance
  8. Updates observability gauges for the research layer

The coordinator is OPTIONAL — the oracle pipeline runs fine without it.
When wired in (via `main.py`'s lifespan or scheduled task), it provides
institutional-grade research visibility into prediction quality.

STRICT_ADDITIVE: does not touch prediction/feature/signal/verifier.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from .purged_cv import run_purged_kfold, run_cpcv, DEFAULTS as CV_DEFAULTS
from .calibration import fit_and_evaluate, DEFAULTS as CAL_DEFAULTS
from .drift_detector import DriftMonitor, DEFAULTS as DRIFT_DEFAULTS
from .research_metrics import compute_research_metrics, DEFAULTS as RM_DEFAULTS
from .explainability import fit_explainer, Explainer, DEFAULTS as EXPL_DEFAULTS
from .observability import get_registry, timed

log = logging.getLogger("senecio.research.coordinator")


DEFAULTS: dict[str, Any] = {
    "predictions_path": "oracle/senecio_output/predictions.jsonl",
    "reports_dir":      "data/research/coordinator_reports",
    # Feature columns to extract from prediction records (must already be
    # present in the record — these are NOT computed by the research layer).
    "feature_fields": [
        "confidence",
        "ev",
        "price_now",
        "vol_pct",
        "spread_bps",
        "depth_usd",
        "bidask_imbalance",
        "momentum_5m",
        "momentum_15m",
        "funding_rate",
    ],
    "min_samples_for_run": 50,
    "purge_td_seconds":    900.0,
    "embargo_td_seconds":  900.0,
    "n_splits":            5,
    "n_groups":            6,
    "n_test_groups":       2,
    "rolling_window":      50,
    "rolling_step":        5,
    "calibration_methods": ["isotonic", "platt", "beta"],
    "drift_alpha":         0.05,
    "psi_warn_threshold":  0.10,
    "psi_alert_threshold": 0.25,
    "explainer_model_type": "tree",
    "explainer_prefer_shap": True,
    "explainer_top_k":      10,
}


@dataclass
class ResearchPassReport:
    """Aggregate report from one full research pass."""
    run_at: str
    n_samples: int
    n_features: int
    feature_names: list[str]
    purged_kfold_report: Optional[dict[str, Any]] = None
    cpcv_report: Optional[dict[str, Any]] = None
    calibration_reports: list[dict[str, Any]] = field(default_factory=list)
    drift_stats: Optional[dict[str, Any]] = None
    research_metrics_report: Optional[dict[str, Any]] = None
    explainer_stats: Optional[dict[str, Any]] = None
    errors: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResearchCoordinator:
    """Orchestrates the 6 ACT-XXVII research modules.

    Usage:
        coord = ResearchCoordinator()
        coord.load_predictions()       # from predictions.jsonl
        report = coord.run_full_pass() # runs all 6 modules

    Or for incremental updates (one new prediction at a time):
        coord.ingest_prediction(pred_dict)  # updates drift monitor only
    """

    def __init__(self, config: Optional[dict[str, Any]] = None):
        self.cfg = {**DEFAULTS, **(config or {})}
        self.predictions: list[dict[str, Any]] = []
        self.X: Optional[np.ndarray] = None
        self.y: Optional[np.ndarray] = None
        self.confidences: Optional[np.ndarray] = None
        self.timestamps: Optional[list[Any]] = None
        self.feature_names: list[str] = list(self.cfg["feature_fields"])
        self.drift_monitor = DriftMonitor(
            config={
                "psi_warn_threshold":  self.cfg["psi_warn_threshold"],
                "psi_alert_threshold": self.cfg["psi_alert_threshold"],
            },
        )
        self.explainer: Optional[Explainer] = None
        self._last_report: Optional[ResearchPassReport] = None
        self._registry = get_registry()

    # -------- data loading --------

    def load_predictions(
        self, path: Optional[str] = None, limit: Optional[int] = None,
    ) -> int:
        """Load prediction records from JSONL.

        Returns the number of records loaded.
        """
        pred_path = Path(path or self.cfg["predictions_path"])
        if not pred_path.is_absolute():
            # Treat as relative to project root
            project_root = Path(__file__).resolve().parent.parent.parent
            pred_path = project_root / pred_path
        if not pred_path.exists():
            log.warning("predictions file not found: %s", pred_path)
            self.predictions = []
            return 0
        out: list[dict[str, Any]] = []
        try:
            with open(pred_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
        except Exception as e:
            log.warning("failed to read predictions %s: %s", pred_path, e)
        if limit is not None:
            out = out[-limit:]
        self.predictions = out
        log.info("loaded %d predictions from %s", len(out), pred_path)
        return len(out)

    def load_predictions_from_records(
        self, records: list[dict[str, Any]],
    ) -> None:
        """Load predictions directly from a list of dicts (e.g. from Supabase)."""
        self.predictions = list(records)
        log.info("loaded %d predictions from in-memory records", len(records))

    def _build_feature_matrix(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[Any]]:
        """Extract (X, y, confidences, timestamps) from self.predictions.

        Each prediction record must have an `outcome` field of "WIN"/"LOSS"
        to be included (others are dropped). Features are pulled from the
        configured `feature_fields` list — missing fields default to 0.0.
        """
        X_rows: list[list[float]] = []
        y_vals: list[float] = []
        confs: list[float] = []
        ts_vals: list[Any] = []
        from ..score_truth import validate_1h_outcome

        for rec in self.predictions:
            clean, _ = validate_1h_outcome(rec)
            if clean is None:
                continue
            row: list[float] = []
            for fname in self.feature_names:
                v = rec.get(fname)
                if v is None or v == "":
                    row.append(0.0)
                else:
                    try:
                        row.append(float(v))
                    except (TypeError, ValueError):
                        row.append(0.0)
            X_rows.append(row)
            y_vals.append(1.0 if clean["outcome"] == "WIN" else 0.0)
            confs.append(clean["confidence"])
            ts_vals.append(clean["ts"])
        if not X_rows:
            return (
                np.zeros((0, len(self.feature_names)), dtype=float),
                np.zeros((0,), dtype=float),
                np.zeros((0,), dtype=float),
                [],
            )
        return (
            np.asarray(X_rows, dtype=float),
            np.asarray(y_vals, dtype=float),
            np.asarray(confs, dtype=float),
            ts_vals,
        )

    # -------- incremental updates --------

    def ingest_prediction(self, prediction: dict[str, Any]) -> list[dict[str, Any]]:
        """Update the DriftMonitor with one new prediction's confidence.

        Returns any drift warnings that were triggered.
        """
        conf = prediction.get("confidence")
        if conf is None:
            return []
        try:
            conf_f = float(conf)
        except (TypeError, ValueError):
            return []
        warnings = self.drift_monitor.update(conf_f)
        # Update observability
        self._registry.observe(
            "senecio_predictions_total", 1,
            labels={
                "direction": (prediction.get("prediction") or "FLAT").upper(),
                "outcome_window": "1h",
            },
        )
        if warnings:
            for w in warnings:
                self._registry.observe(
                    "senecio_drift_warnings_total", 1,
                    labels={"detector": w.detector, "severity": w.severity},
                )
            # Update active-alerts gauge
            alert_count = sum(1 for w in warnings if w.severity == "alert")
            self._registry.set_gauge(
                "senecio_drift_alerts_active", float(alert_count),
            )
        return [w.to_dict() for w in warnings]

    def set_drift_reference(self, confidences: np.ndarray) -> None:
        """Set the DriftMonitor's reference distribution (baseline period)."""
        self.drift_monitor.set_reference(np.asarray(confidences, dtype=float))

    # -------- full pass --------

    def run_full_pass(self, persist: bool = True) -> ResearchPassReport:
        """Run all 6 research modules on the loaded predictions.

        Args:
            persist: if True (default), persist every report to disk.
        """
        started = datetime.now(timezone.utc)
        self._registry.observe(
            "senecio_research_runs_total", 1,
            labels={"module": "full_pass"},
        )
        report = ResearchPassReport(
            run_at=started.isoformat(),
            n_samples=0,
            n_features=len(self.feature_names),
            feature_names=list(self.feature_names),
        )

        # Build matrices
        with timed("senecio_research_module_latency_seconds",
                   labels={"module": "feature_build"}):
            X, y, confs, ts = self._build_feature_matrix()
        self.X, self.y, self.confidences, self.timestamps = X, y, confs, ts
        report.n_samples = int(X.shape[0])

        if X.shape[0] < self.cfg["min_samples_for_run"]:
            msg = (
                f"insufficient samples for full research pass "
                f"({X.shape[0]} < {self.cfg['min_samples_for_run']})"
            )
            log.warning(msg)
            report.errors.append(msg)
            self._last_report = report
            return report

        # 1) Purged K-Fold
        try:
            with timed("senecio_research_module_latency_seconds",
                       labels={"module": "purged_kfold"}):
                pk = run_purged_kfold(
                    X=X, y=y, times=ts,
                    n_splits=self.cfg["n_splits"],
                    purge_td_seconds=self.cfg["purge_td_seconds"],
                    embargo_td_seconds=self.cfg["embargo_td_seconds"],
                    feature_names=self.feature_names,
                    extra={"coordinator_run_at": started.isoformat()},
                )
            report.purged_kfold_report = pk.to_dict()
        except Exception as e:
            log.exception("purged_kfold failed: %s", e)
            report.errors.append(f"purged_kfold: {e}")

        # 2) CPCV
        try:
            with timed("senecio_research_module_latency_seconds",
                       labels={"module": "cpcv"}):
                cv = run_cpcv(
                    X=X, y=y, times=ts,
                    n_groups=self.cfg["n_groups"],
                    n_test_groups=self.cfg["n_test_groups"],
                    purge_td_seconds=self.cfg["purge_td_seconds"],
                    embargo_td_seconds=self.cfg["embargo_td_seconds"],
                    feature_names=self.feature_names,
                    extra={"coordinator_run_at": started.isoformat()},
                )
            report.cpcv_report = cv.to_dict()
        except Exception as e:
            log.exception("cpcv failed: %s", e)
            report.errors.append(f"cpcv: {e}")

        # 3) Calibration (3 methods)
        for method in self.cfg["calibration_methods"]:
            try:
                with timed("senecio_research_module_latency_seconds",
                           labels={"module": f"calibration_{method}"}):
                    cal_report = fit_and_evaluate(
                        y_true=y, y_prob=confs,
                        method=method,
                        n_bins=CAL_DEFAULTS["n_bins"],
                        extra={"coordinator_run_at": started.isoformat()},
                    )
                report.calibration_reports.append(cal_report.to_dict())
                self._registry.observe(
                    "senecio_calibration_fits_total", 1,
                    labels={"method": method},
                )
                self._registry.set_gauge(
                    "senecio_last_calibration_ece",
                    float(cal_report.ece_after),
                    labels={"method": method},
                )
            except Exception as e:
                log.exception("calibration %s failed: %s", method, e)
                report.errors.append(f"calibration_{method}: {e}")

        # 4) Drift — set reference to first 50% of confidences, current to last 50%
        try:
            half = max(1, confs.shape[0] // 2)
            self.drift_monitor.set_reference(confs[:half])
            # Replay the second half through the monitor
            for c in confs[half:]:
                self.drift_monitor.update(float(c))
            report.drift_stats = self.drift_monitor.stats()
        except Exception as e:
            log.exception("drift monitor failed: %s", e)
            report.errors.append(f"drift: {e}")

        # 5) Research metrics (IC + rolling Sharpe/PF/MDD)
        try:
            with timed("senecio_research_module_latency_seconds",
                       labels={"module": "research_metrics"}):
                # Realized return per sample: y * confidence * 0.01 (heuristic)
                # — actual returns come from the trade journal, not predictions.
                # For research metrics on the prediction layer, we use signed
                # confidence × outcome as the realized-return proxy.
                preds_signed = confs * (2 * y - 1)  # +1 for WIN, -1 for LOSS
                realized = (2 * y - 1).astype(float)  # signed outcome
                rm_report = compute_research_metrics(
                    predictions=preds_signed,
                    realized_returns=realized,
                    window=self.cfg["rolling_window"],
                    step=self.cfg["rolling_step"],
                    extra={"coordinator_run_at": started.isoformat()},
                )
            report.research_metrics_report = rm_report.to_dict()
            if math.isfinite(rm_report.ic):
                self._registry.set_gauge("senecio_last_ic", float(rm_report.ic))
            if rm_report.rolling_sharpe:
                last_sharpe = rm_report.rolling_sharpe[-1].get("sharpe", 0.0)
                if last_sharpe is not None and math.isfinite(last_sharpe):
                    self._registry.set_gauge(
                        "senecio_rolling_sharpe", float(last_sharpe),
                    )
        except Exception as e:
            log.exception("research metrics failed: %s", e)
            report.errors.append(f"research_metrics: {e}")

        # 6) Explainability — fit surrogate + capture feature importance
        try:
            with timed("senecio_research_module_latency_seconds",
                       labels={"module": "explainability"}):
                self.explainer = fit_explainer(
                    X=X, y=y,
                    feature_names=self.feature_names,
                    model_type=self.cfg["explainer_model_type"],
                    prefer_shap=self.cfg["explainer_prefer_shap"],
                    top_k=self.cfg["explainer_top_k"],
                )
            report.explainer_stats = self.explainer.stats()
            self._registry.observe(
                "senecio_explainer_fits_total", 1,
                labels={
                    "model_type": self.explainer.model_type,
                    "explainer_kind": self.explainer._explainer_kind,
                },
            )
        except Exception as e:
            log.exception("explainability failed: %s", e)
            report.errors.append(f"explainability: {e}")

        # Persist aggregate report
        if persist:
            self._persist_report(report)
        self._last_report = report
        return report

    # -------- introspection --------

    def get_drift_stats(self) -> dict[str, Any]:
        return self.drift_monitor.stats()

    def get_explainer(self) -> Optional[Explainer]:
        return self.explainer

    def get_last_report(self) -> Optional[ResearchPassReport]:
        return self._last_report

    def explain_prediction(
        self, prediction: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Explain a single prediction using the fitted surrogate.

        Returns None if the explainer hasn't been fitted yet.
        """
        if self.explainer is None:
            return None
        row = []
        for fname in self.feature_names:
            v = prediction.get(fname)
            if v is None or v == "":
                row.append(0.0)
            else:
                try:
                    row.append(float(v))
                except (TypeError, ValueError):
                    row.append(0.0)
        X_row = np.asarray(row, dtype=float).reshape(1, -1)
        try:
            explanation = self.explainer.explain_one(X_row)
            self.explainer.persist_attribution(
                explanation, prediction_id=prediction.get("id"),
            )
            return explanation.to_dict()
        except Exception as e:
            log.warning("explain_prediction failed: %s", e)
            return None

    def _persist_report(self, report: ResearchPassReport) -> None:
        try:
            out_dir = Path(self.cfg["reports_dir"])
            out_dir.mkdir(parents=True, exist_ok=True)
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            path = out_dir / f"research_pass_{day}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(report.to_dict(), default=str) + "\n")
            log.info("research pass report persisted to %s", path)
        except Exception as e:
            log.warning("failed to persist research pass report: %s", e)


__all__ = [
    "ResearchCoordinator",
    "ResearchPassReport",
    "DEFAULTS",
]
