"""
SENECIO ORACLE — ACT XXVII Priority 2: Probability Calibration
===============================================================

Implements probability calibration for the oracle's confidence scores, plus
the metrics needed to measure whether predictions are well-calibrated.

Methods:
  - Platt Scaling       : logistic regression on (prob -> label)
  - Isotonic Regression : non-parametric monotone mapping
  - Beta Calibration    : 2-parameter beta-based mapping (optional)

Metrics:
  - Brier Score
  - Expected Calibration Error (ECE)
  - Maximum Calibration Error (MCE)
  - Reliability Curve (bins predictions by prob, compares to empirical freq)

Calibrators are fit on a held-out calibration set (typically from a PurgedKFold
out-of-fold predictions), stored as JSON, and can be applied to live
predictions to produce calibrated probabilities.

This module is STRICT_ADDITIVE — it does NOT touch the prediction model,
feature engineering, signal generation, or verifier.
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

log = logging.getLogger("senecio.research.calibration")


DEFAULTS: dict[str, Any] = {
    "n_bins":              10,
    "calibrators_dir":     "data/research/calibrators",
    "reliability_dir":     "data/research/reliability_curves",
    "min_calibration_samples": 100,
}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def brier_score(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    """Mean squared error between predicted probabilities and binary outcomes."""
    y_t = np.asarray(y_true, dtype=float).reshape(-1)
    y_p = np.asarray(y_prob, dtype=float).reshape(-1)
    if y_t.shape[0] == 0:
        return 0.0
    return float(np.mean((y_p - y_t) ** 2))


def reliability_curve(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    n_bins: int = 10,
) -> dict[str, Any]:
    """Compute the reliability (calibration) curve.

    Returns:
        {
            "bins": [(lo, hi), ...],
            "bin_centers": [...],
            "mean_predicted": [...],
            "fraction_positive": [...],
            "counts": [...],
            "n_bins": n_bins,
        }
    """
    y_t = np.asarray(y_true, dtype=float).reshape(-1)
    y_p = np.asarray(y_prob, dtype=float).reshape(-1)
    n = y_t.shape[0]
    if n == 0:
        return {
            "bins": [], "bin_centers": [], "mean_predicted": [],
            "fraction_positive": [], "counts": [], "n_bins": n_bins,
        }
    # Use equal-width bins in [0, 1]
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bin_centers = []
    mean_pred = []
    frac_pos = []
    counts = []
    bins = []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        # Include right edge only on last bin
        if i == n_bins - 1:
            mask = (y_p >= lo) & (y_p <= hi)
        else:
            mask = (y_p >= lo) & (y_p < hi)
        n_bin = int(mask.sum())
        bins.append((float(lo), float(hi)))
        bin_centers.append(float((lo + hi) / 2.0))
        if n_bin == 0:
            mean_pred.append(float("nan"))
            frac_pos.append(float("nan"))
        else:
            mean_pred.append(float(y_p[mask].mean()))
            frac_pos.append(float(y_t[mask].mean()))
        counts.append(n_bin)
    return {
        "bins": bins,
        "bin_centers": bin_centers,
        "mean_predicted": mean_pred,
        "fraction_positive": frac_pos,
        "counts": counts,
        "n_bins": n_bins,
    }


def expected_calibration_error(
    y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 10
) -> float:
    """ECE = Σ_bin (|bin_count| / N) × |mean_predicted - fraction_positive|."""
    rc = reliability_curve(y_true, y_prob, n_bins=n_bins)
    n = len(y_true)
    if n == 0:
        return 0.0
    ece = 0.0
    for i in range(rc["n_bins"]):
        c = rc["counts"][i]
        if c == 0:
            continue
        mp = rc["mean_predicted"][i]
        fp = rc["fraction_positive"][i]
        if math.isnan(mp) or math.isnan(fp):
            continue
        ece += (c / n) * abs(mp - fp)
    return float(ece)


def maximum_calibration_error(
    y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 10
) -> float:
    """MCE = max_bin |mean_predicted - fraction_positive|."""
    rc = reliability_curve(y_true, y_prob, n_bins=n_bins)
    mce = 0.0
    for i in range(rc["n_bins"]):
        c = rc["counts"][i]
        if c == 0:
            continue
        mp = rc["mean_predicted"][i]
        fp = rc["fraction_positive"][i]
        if math.isnan(mp) or math.isnan(fp):
            continue
        mce = max(mce, abs(mp - fp))
    return float(mce)


# ---------------------------------------------------------------------------
# Calibrators
# ---------------------------------------------------------------------------


class Calibrator:
    """Base class for calibrators. Subclasses implement fit() + predict()."""

    method_name: str = "base"

    def fit(self, y_true: np.ndarray, y_prob: np.ndarray) -> "Calibrator":
        raise NotImplementedError

    def predict(self, y_prob: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:
        return {"method": self.method_name}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Calibrator":
        method = d.get("method", "identity")
        if method == "platt":
            return PlattCalibrator.from_dict(d)
        if method == "isotonic":
            return IsotonicCalibrator.from_dict(d)
        if method == "beta":
            return BetaCalibrator.from_dict(d)
        if method == "identity":
            return IdentityCalibrator()
        raise ValueError(f"unknown calibrator method: {method}")


class IdentityCalibrator(Calibrator):
    """No-op calibrator (returns probabilities unchanged)."""
    method_name = "identity"

    def fit(self, y_true: np.ndarray, y_prob: np.ndarray) -> "IdentityCalibrator":
        return self

    def predict(self, y_prob: np.ndarray) -> np.ndarray:
        return np.asarray(y_prob, dtype=float).reshape(-1)

    def to_dict(self) -> dict[str, Any]:
        return {"method": "identity"}


class PlattCalibrator(Calibrator):
    """Platt scaling: P(y=1 | p) = sigmoid(a * p + b).

    Fit via logistic regression on (p, y).
    """
    method_name = "platt"

    def __init__(self):
        self.a: float = 1.0
        self.b: float = 0.0
        self.n_fit: int = 0

    @staticmethod
    def _sigmoid(x: np.ndarray) -> np.ndarray:
        # Numerically stable sigmoid
        out = np.empty_like(x, dtype=float)
        pos = x >= 0
        out[pos] = 1.0 / (1.0 + np.exp(-x[pos]))
        ex = np.exp(x[~pos])
        out[~pos] = ex / (1.0 + ex)
        return out

    def fit(self, y_true: np.ndarray, y_prob: np.ndarray) -> "PlattCalibrator":
        from sklearn.linear_model import LogisticRegression
        y_t = np.asarray(y_true, dtype=float).reshape(-1)
        y_p = np.asarray(y_prob, dtype=float).reshape(-1)
        self.n_fit = int(y_t.shape[0])
        if self.n_fit == 0:
            self.a, self.b = 1.0, 0.0
            return self
        # If only one class, no transformation needed
        if len(np.unique(y_t)) < 2:
            self.a, self.b = 1.0, 0.0
            return self
        # Fit LR on a single feature (the probability)
        lr = LogisticRegression(C=1e6, solver="lbfgs", max_iter=1000)
        lr.fit(y_p.reshape(-1, 1), y_t.astype(int))
        # LR form: p = sigmoid(coef * x + intercept)
        self.a = float(lr.coef_[0, 0])
        self.b = float(lr.intercept_[0])
        return self

    def predict(self, y_prob: np.ndarray) -> np.ndarray:
        y_p = np.asarray(y_prob, dtype=float).reshape(-1)
        return self._sigmoid(self.a * y_p + self.b)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": "platt",
            "a": self.a,
            "b": self.b,
            "n_fit": self.n_fit,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlattCalibrator":
        c = cls()
        c.a = float(d.get("a", 1.0))
        c.b = float(d.get("b", 0.0))
        c.n_fit = int(d.get("n_fit", 0))
        return c


class IsotonicCalibrator(Calibrator):
    """Isotonic regression: piecewise monotone mapping.

    Wraps sklearn.isotonic.IsotonicRegression with explicit handling of
    out-of-range inputs (clamps to [0,1]).
    """
    method_name = "isotonic"

    def __init__(self, y_min: float = 0.0, y_max: float = 1.0):
        self.y_min = float(y_min)
        self.y_max = float(y_max)
        self._x_min: float = 0.0
        self._x_max: float = 1.0
        self._x_sorted: Optional[np.ndarray] = None
        self._y_sorted: Optional[np.ndarray] = None
        self.n_fit: int = 0
        # Lazy import so the module is importable even if sklearn is missing
        self._iso = None

    def fit(self, y_true: np.ndarray, y_prob: np.ndarray) -> "IsotonicCalibrator":
        from sklearn.isotonic import IsotonicRegression
        y_t = np.asarray(y_true, dtype=float).reshape(-1)
        y_p = np.asarray(y_prob, dtype=float).reshape(-1)
        self.n_fit = int(y_t.shape[0])
        if self.n_fit == 0:
            self._iso = None
            return self
        self._iso = IsotonicRegression(
            y_min=self.y_min, y_max=self.y_max, increasing=True,
            out_of_bounds="clip",
        )
        self._iso.fit(y_p, y_t)
        # Cache the training range for extrapolation handling
        self._x_min = float(np.min(y_p))
        self._x_max = float(np.max(y_p))
        # Keep sorted arrays for inspection / persistence
        order = np.argsort(y_p, kind="stable")
        self._x_sorted = y_p[order]
        self._y_sorted = np.asarray(self._iso.transform(y_p[order]), dtype=float)
        return self

    def predict(self, y_prob: np.ndarray) -> np.ndarray:
        y_p = np.asarray(y_prob, dtype=float).reshape(-1)
        if self._iso is None:
            return y_p
        return np.asarray(self._iso.predict(y_p), dtype=float)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": "isotonic",
            "y_min": self.y_min,
            "y_max": self.y_max,
            "x_min": self._x_min,
            "x_max": self._x_max,
            "n_fit": self.n_fit,
            "x_sorted": (self._x_sorted.tolist() if self._x_sorted is not None else []),
            "y_sorted": (self._y_sorted.tolist() if self._y_sorted is not None else []),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "IsotonicCalibrator":
        c = cls(
            y_min=float(d.get("y_min", 0.0)),
            y_max=float(d.get("y_max", 1.0)),
        )
        c._x_min = float(d.get("x_min", 0.0))
        c._x_max = float(d.get("x_max", 1.0))
        c.n_fit = int(d.get("n_fit", 0))
        x_sorted = d.get("x_sorted") or []
        y_sorted = d.get("y_sorted") or []
        if x_sorted and y_sorted and len(x_sorted) == len(y_sorted):
            c._x_sorted = np.asarray(x_sorted, dtype=float)
            c._y_sorted = np.asarray(y_sorted, dtype=float)
            # Reconstruct interpolator from sorted arrays
            try:
                from sklearn.isotonic import IsotonicRegression
                c._iso = IsotonicRegression(
                    y_min=c.y_min, y_max=c.y_max, increasing=True,
                    out_of_bounds="clip",
                )
                c._iso.fit(c._x_sorted, c._y_sorted)
            except Exception as e:
                log.warning("isotonic reconstruct failed: %s", e)
                c._iso = None
        return c


class BetaCalibrator(Calibrator):
    """Beta calibration (Kull et al., 2017).

    Three-parameter family: cal(p) = I_p(a, b) where I is the incomplete
    beta function. We fit log-odds form: logit(cal(p)) = a*log(p) + b*log(1-p) + c.
    """
    method_name = "beta"

    def __init__(self):
        self.a: float = 1.0
        self.b: float = 1.0
        self.c: float = 0.0
        self.n_fit: int = 0

    @staticmethod
    def _logit(p: np.ndarray) -> np.ndarray:
        # Numerically stable logit
        eps = 1e-12
        p = np.clip(p, eps, 1 - eps)
        return np.log(p) - np.log1p(-p)

    @staticmethod
    def _sigmoid(x: np.ndarray) -> np.ndarray:
        out = np.empty_like(x, dtype=float)
        pos = x >= 0
        out[pos] = 1.0 / (1.0 + np.exp(-x[pos]))
        ex = np.exp(x[~pos])
        out[~pos] = ex / (1.0 + ex)
        return out

    def fit(self, y_true: np.ndarray, y_prob: np.ndarray) -> "BetaCalibrator":
        from sklearn.linear_model import LogisticRegression
        y_t = np.asarray(y_true, dtype=float).reshape(-1)
        y_p = np.asarray(y_prob, dtype=float).reshape(-1)
        self.n_fit = int(y_t.shape[0])
        if self.n_fit == 0 or len(np.unique(y_t)) < 2:
            self.a, self.b, self.c = 1.0, 1.0, 0.0
            return self
        # Build features: [log(p), log(1-p)]
        eps = 1e-12
        p_clip = np.clip(y_p, eps, 1 - eps)
        X = np.column_stack([np.log(p_clip), np.log1p(-p_clip)])
        lr = LogisticRegression(C=1e6, solver="lbfgs", max_iter=1000)
        lr.fit(X, y_t.astype(int))
        self.a = float(lr.coef_[0, 0])
        self.b = float(-lr.coef_[0, 1])  # sign flip per Kull formulation
        self.c = float(lr.intercept_[0])
        return self

    def predict(self, y_prob: np.ndarray) -> np.ndarray:
        y_p = np.asarray(y_prob, dtype=float).reshape(-1)
        eps = 1e-12
        p_clip = np.clip(y_p, eps, 1 - eps)
        logits = self.a * np.log(p_clip) - self.b * np.log1p(-p_clip) + self.c
        return self._sigmoid(logits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": "beta",
            "a": self.a, "b": self.b, "c": self.c,
            "n_fit": self.n_fit,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BetaCalibrator":
        c = cls()
        c.a = float(d.get("a", 1.0))
        c.b = float(d.get("b", 1.0))
        c.c = float(d.get("c", 0.0))
        c.n_fit = int(d.get("n_fit", 0))
        return c


# ---------------------------------------------------------------------------
# Calibration report + persistence
# ---------------------------------------------------------------------------


@dataclass
class CalibrationReport:
    """Full calibration evaluation report."""
    method: str
    n_samples: int
    brier_before: float
    brier_after: float
    ece_before: float
    ece_after: float
    mce_before: float
    mce_after: float
    reliability_before: dict[str, Any]
    reliability_after: dict[str, Any]
    calibrator_params: dict[str, Any]
    fitted_at: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def fit_and_evaluate(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    method: str = "isotonic",
    n_bins: int = 10,
    calibrators_dir: str = DEFAULTS["calibrators_dir"],
    reliability_dir: str = DEFAULTS["reliability_dir"],
    extra: Optional[dict] = None,
) -> CalibrationReport:
    """Fit a calibrator of the given method and compute before/after metrics.

    Args:
        y_true : 0/1 binary outcomes
        y_prob : raw model probabilities
        method : "platt" | "isotonic" | "beta" | "identity"
        n_bins : bin count for reliability curve / ECE
        calibrators_dir : where to persist the fitted calibrator JSON
        reliability_dir : where to persist reliability curve JSON
        extra   : extra metadata to embed
    """
    y_t = np.asarray(y_true, dtype=float).reshape(-1)
    y_p = np.asarray(y_prob, dtype=float).reshape(-1)
    n = int(y_t.shape[0])
    if n == 0:
        raise ValueError("cannot calibrate on empty data")

    method_lc = (method or "identity").lower()
    if method_lc == "platt":
        cal = PlattCalibrator()
    elif method_lc == "isotonic":
        cal = IsotonicCalibrator()
    elif method_lc == "beta":
        cal = BetaCalibrator()
    elif method_lc == "identity":
        cal = IdentityCalibrator()
    else:
        raise ValueError(f"unknown calibrator method: {method}")

    cal.fit(y_t, y_p)
    y_p_cal = cal.predict(y_p)

    brier_before = brier_score(y_t, y_p)
    brier_after  = brier_score(y_t, y_p_cal)
    ece_before   = expected_calibration_error(y_t, y_p, n_bins=n_bins)
    ece_after    = expected_calibration_error(y_t, y_p_cal, n_bins=n_bins)
    mce_before   = maximum_calibration_error(y_t, y_p, n_bins=n_bins)
    mce_after    = maximum_calibration_error(y_t, y_p_cal, n_bins=n_bins)
    rc_before    = reliability_curve(y_t, y_p, n_bins=n_bins)
    rc_after     = reliability_curve(y_t, y_p_cal, n_bins=n_bins)

    report = CalibrationReport(
        method=method_lc,
        n_samples=n,
        brier_before=brier_before,
        brier_after=brier_after,
        ece_before=ece_before,
        ece_after=ece_after,
        mce_before=mce_before,
        mce_after=mce_after,
        reliability_before=rc_before,
        reliability_after=rc_after,
        calibrator_params=cal.to_dict(),
        fitted_at=datetime.now(timezone.utc).isoformat(),
        extra=extra or {},
    )
    _persist_calibrator(cal, method_lc, calibrators_dir, n)
    _persist_reliability(report, method_lc, reliability_dir)
    return report


def _persist_calibrator(
    cal: Calibrator, method: str, calibrators_dir: str, n: int,
) -> None:
    try:
        out_dir = Path(calibrators_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        fname = f"calibrator_{method}_{ts}_n{n}.json"
        path = out_dir / fname
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cal.to_dict(), f, indent=2, default=str)
        log.info("calibrator persisted to %s", path)
    except Exception as e:
        log.warning("failed to persist calibrator: %s", e)


def _persist_reliability(
    report: CalibrationReport, method: str, reliability_dir: str,
) -> None:
    try:
        out_dir = Path(reliability_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = out_dir / f"reliability_{day}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(report.to_dict(), default=str) + "\n")
        log.info("reliability report appended to %s", path)
    except Exception as e:
        log.warning("failed to persist reliability report: %s", e)


__all__ = [
    "Calibrator",
    "IdentityCalibrator",
    "PlattCalibrator",
    "IsotonicCalibrator",
    "BetaCalibrator",
    "CalibrationReport",
    "brier_score",
    "reliability_curve",
    "expected_calibration_error",
    "maximum_calibration_error",
    "fit_and_evaluate",
    "DEFAULTS",
]
