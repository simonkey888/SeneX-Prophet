"""AUD-059 probability calibration with temporal out-of-sample truth semantics.

Calibration primitives live in ``_calibration_primitives.py``. This public
module never presents fit-and-evaluate-on-the-same-sample metrics as evidence of
generalization. When there is enough ordered evidence it uses a chronological
train/purge/test split. Otherwise it reports ``INSUFFICIENT_OOS_EVIDENCE`` and
keeps any in-sample calculation explicitly diagnostic-only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

from . import _calibration_primitives as _p

Calibrator = _p.Calibrator
IdentityCalibrator = _p.IdentityCalibrator
PlattCalibrator = _p.PlattCalibrator
IsotonicCalibrator = _p.IsotonicCalibrator
BetaCalibrator = _p.BetaCalibrator
brier_score = _p.brier_score
reliability_curve = _p.reliability_curve
expected_calibration_error = _p.expected_calibration_error
maximum_calibration_error = _p.maximum_calibration_error

DEFAULTS = dict(_p.DEFAULTS)
DEFAULTS.update({
    "min_calibration_samples": 100,
    "oos_train_fraction": 0.70,
    "oos_purge_fraction": 0.05,
    "min_oos_test_samples": 20,
})


@dataclass
class CalibrationReport:
    method: str
    n_samples: int
    brier_before: float | None
    brier_after: float | None
    ece_before: float | None
    ece_after: float | None
    mce_before: float | None
    mce_after: float | None
    reliability_before: dict[str, Any]
    reliability_after: dict[str, Any]
    calibrator_params: dict[str, Any]
    fitted_at: str
    evaluation_status: str
    evaluation_scope: str
    authority_eligible: bool
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _make_calibrator(method: str) -> Calibrator:
    method_lc = (method or "identity").lower()
    if method_lc == "platt":
        return PlattCalibrator()
    if method_lc == "isotonic":
        return IsotonicCalibrator()
    if method_lc == "beta":
        return BetaCalibrator()
    if method_lc == "identity":
        return IdentityCalibrator()
    raise ValueError(f"unknown calibrator method: {method}")


def _diagnostic_same_sample(y_t: np.ndarray, y_p: np.ndarray, method: str, n_bins: int) -> dict[str, Any]:
    cal = _make_calibrator(method)
    cal.fit(y_t, y_p)
    transformed = cal.predict(y_p)
    return {
        "diagnostic_only": True,
        "same_sample_fit_and_evaluation": True,
        "brier_before": brier_score(y_t, y_p),
        "brier_after": brier_score(y_t, transformed),
        "ece_before": expected_calibration_error(y_t, y_p, n_bins=n_bins),
        "ece_after": expected_calibration_error(y_t, transformed, n_bins=n_bins),
        "mce_before": maximum_calibration_error(y_t, y_p, n_bins=n_bins),
        "mce_after": maximum_calibration_error(y_t, transformed, n_bins=n_bins),
        "calibrator_params": cal.to_dict(),
    }


def fit_and_evaluate(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    method: str = "isotonic",
    n_bins: int = 10,
    calibrators_dir: str = DEFAULTS["calibrators_dir"],
    reliability_dir: str = DEFAULTS["reliability_dir"],
    extra: Optional[dict] = None,
) -> CalibrationReport:
    """Fit/evaluate calibration using chronological OOS evidence or fail closed.

    Input order is treated as temporal order. With >=100 samples, the first 70%
    forms training, the next 5% is purged, and the remainder is untouched OOS
    evaluation. With insufficient OOS evidence, no calibrator is persisted for
    authoritative use and ``brier_after/ece_after`` are ``None``.
    """
    y_t = np.asarray(y_true, dtype=float).reshape(-1)
    y_p = np.asarray(y_prob, dtype=float).reshape(-1)
    if y_t.shape != y_p.shape:
        raise ValueError("y_true and y_prob must have equal length")
    n = int(y_t.shape[0])
    if n == 0:
        raise ValueError("cannot calibrate on empty data")
    if not np.all(np.isfinite(y_t)) or not np.all(np.isfinite(y_p)):
        raise ValueError("calibration inputs must be finite")
    if np.any((y_p < 0.0) | (y_p > 1.0)):
        raise ValueError("probabilities must be in [0,1]")

    method_lc = (method or "identity").lower()
    fitted_at = datetime.now(timezone.utc).isoformat()
    base_extra = dict(extra or {})
    diagnostic = _diagnostic_same_sample(y_t, y_p, method_lc, n_bins)

    min_n = int(DEFAULTS["min_calibration_samples"])
    train_end = int(n * float(DEFAULTS["oos_train_fraction"]))
    purge_n = max(1, int(n * float(DEFAULTS["oos_purge_fraction"])))
    test_start = train_end + purge_n
    test_n = max(0, n - test_start)
    enough = (
        n >= min_n
        and train_end >= 50
        and test_n >= int(DEFAULTS["min_oos_test_samples"])
        and len(np.unique(y_t[:train_end])) >= 2
        and len(np.unique(y_t[test_start:])) >= 2
    )

    if not enough:
        report = CalibrationReport(
            method=method_lc,
            n_samples=n,
            brier_before=None,
            brier_after=None,
            ece_before=None,
            ece_after=None,
            mce_before=None,
            mce_after=None,
            reliability_before={},
            reliability_after={},
            calibrator_params={"method": method_lc, "authority_eligible": False},
            fitted_at=fitted_at,
            evaluation_status="INSUFFICIENT_OOS_EVIDENCE",
            evaluation_scope="IN_SAMPLE_DIAGNOSTIC_ONLY",
            authority_eligible=False,
            extra={
                **base_extra,
                "ordered_input_assumed_temporal": True,
                "n_total": n,
                "n_train_candidate": train_end,
                "n_purged": purge_n,
                "n_oos_candidate": test_n,
                "in_sample_diagnostic": diagnostic,
            },
        )
        _p._persist_reliability(report, method_lc, reliability_dir)
        return report

    y_train = y_t[:train_end]
    p_train = y_p[:train_end]
    y_test = y_t[test_start:]
    p_test = y_p[test_start:]
    cal = _make_calibrator(method_lc)
    cal.fit(y_train, p_train)
    p_test_cal = cal.predict(p_test)

    report = CalibrationReport(
        method=method_lc,
        n_samples=n,
        brier_before=brier_score(y_test, p_test),
        brier_after=brier_score(y_test, p_test_cal),
        ece_before=expected_calibration_error(y_test, p_test, n_bins=n_bins),
        ece_after=expected_calibration_error(y_test, p_test_cal, n_bins=n_bins),
        mce_before=maximum_calibration_error(y_test, p_test, n_bins=n_bins),
        mce_after=maximum_calibration_error(y_test, p_test_cal, n_bins=n_bins),
        reliability_before=reliability_curve(y_test, p_test, n_bins=n_bins),
        reliability_after=reliability_curve(y_test, p_test_cal, n_bins=n_bins),
        calibrator_params=cal.to_dict(),
        fitted_at=fitted_at,
        evaluation_status="OOS_TEMPORAL_HOLDOUT",
        evaluation_scope="CHRONOLOGICAL_TRAIN_PURGE_TEST",
        authority_eligible=True,
        extra={
            **base_extra,
            "ordered_input_assumed_temporal": True,
            "n_total": n,
            "n_train": int(len(y_train)),
            "n_purged": purge_n,
            "n_oos": int(len(y_test)),
            "in_sample_diagnostic": diagnostic,
        },
    )
    _p._persist_calibrator(cal, method_lc, calibrators_dir, len(y_train))
    _p._persist_reliability(report, method_lc, reliability_dir)
    return report


__all__ = [
    "Calibrator", "IdentityCalibrator", "PlattCalibrator", "IsotonicCalibrator",
    "BetaCalibrator", "CalibrationReport", "brier_score", "reliability_curve",
    "expected_calibration_error", "maximum_calibration_error",
    "fit_and_evaluate", "DEFAULTS",
]
