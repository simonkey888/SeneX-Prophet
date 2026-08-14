"""AUD-059 probability calibration with causal temporal OOS truth semantics.

Calibration primitives live in ``_calibration_primitives.py``. This public
module never presents fit-and-evaluate-on-the-same-sample metrics as evidence of
generalization. Temporal authority requires real, parseable timestamps for every
sample; inputs are deterministically reordered by those timestamps before a
chronological train/purge/test split. If chronology cannot be demonstrated, the
module fails closed with ``INSUFFICIENT_OOS_EVIDENCE``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

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


def _diagnostic_same_sample(
    y_t: np.ndarray,
    y_p: np.ndarray,
    method: str,
    n_bins: int,
) -> dict[str, Any]:
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


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse one evidence timestamp to an aware UTC datetime, or return None."""
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            text = str(value or "").strip()
            if not text:
                return None
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _verify_and_order_chronology(
    timestamps: Optional[Sequence[Any]],
    n: int,
) -> tuple[np.ndarray | None, list[datetime], dict[str, Any]]:
    """Return deterministic chronological order only for fully verified evidence.

    Authority requires exactly one parseable timestamp per sample and strictly
    unique instants. Source order is never trusted: DB-descending or shuffled
    inputs are explicitly reordered by timestamp before any OOS split.
    """
    meta: dict[str, Any] = {
        "chronology_verified": False,
        "chronology_source": "EXPLICIT_SAMPLE_TIMESTAMPS",
        "input_reordered_by_timestamp": False,
    }
    if timestamps is None:
        meta["chronology_reason"] = "TIMESTAMPS_MISSING"
        return None, [], meta
    values = list(timestamps)
    if len(values) != n:
        meta["chronology_reason"] = "TIMESTAMP_COUNT_MISMATCH"
        meta["timestamp_count"] = len(values)
        return None, [], meta

    parsed: list[datetime] = []
    for value in values:
        dt = _parse_timestamp(value)
        if dt is None:
            meta["chronology_reason"] = "TIMESTAMP_INVALID"
            return None, [], meta
        parsed.append(dt)

    if len(set(parsed)) != n:
        meta["chronology_reason"] = "TIMESTAMPS_NOT_UNIQUE"
        return None, [], meta

    order = np.asarray(sorted(range(n), key=lambda idx: parsed[idx]), dtype=int)
    ordered = [parsed[int(idx)] for idx in order]
    if any(ordered[i] >= ordered[i + 1] for i in range(len(ordered) - 1)):
        meta["chronology_reason"] = "TIMESTAMPS_NOT_STRICTLY_INCREASING"
        return None, [], meta

    meta.update({
        "chronology_verified": True,
        "chronology_reason": "VERIFIED_AND_SORTED",
        "timestamp_count": n,
        "input_reordered_by_timestamp": bool(
            not np.array_equal(order, np.arange(n, dtype=int))
        ),
        "first_timestamp": ordered[0].isoformat() if ordered else None,
        "last_timestamp": ordered[-1].isoformat() if ordered else None,
    })
    return order, ordered, meta


def fit_and_evaluate(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    method: str = "isotonic",
    n_bins: int = 10,
    calibrators_dir: str = DEFAULTS["calibrators_dir"],
    reliability_dir: str = DEFAULTS["reliability_dir"],
    extra: Optional[dict] = None,
    timestamps: Optional[Sequence[Any]] = None,
) -> CalibrationReport:
    """Fit/evaluate calibration using verified chronological OOS evidence.

    Raw input order is never accepted as proof of chronology. ``timestamps``
    must contain one unique parseable timestamp per sample. Samples are sorted
    deterministically by those timestamps, then split into chronological train,
    purge and untouched test partitions. Missing/invalid chronology or
    insufficient OOS evidence fails closed without persisting an authoritative
    calibrator and keeps any same-sample metrics diagnostic-only.
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

    order, ordered_ts, chronology = _verify_and_order_chronology(timestamps, n)
    if order is not None:
        y_t = y_t[order]
        y_p = y_p[order]

    min_n = int(DEFAULTS["min_calibration_samples"])
    train_end = int(n * float(DEFAULTS["oos_train_fraction"]))
    purge_n = max(1, int(n * float(DEFAULTS["oos_purge_fraction"])))
    test_start = train_end + purge_n
    test_n = max(0, n - test_start)
    enough = bool(
        chronology["chronology_verified"]
        and n >= min_n
        and train_end >= 50
        and test_n >= int(DEFAULTS["min_oos_test_samples"])
        and len(np.unique(y_t[:train_end])) >= 2
        and len(np.unique(y_t[test_start:])) >= 2
    )

    if not enough:
        scope = (
            "CHRONOLOGY_VERIFIED_IN_SAMPLE_DIAGNOSTIC_ONLY"
            if chronology["chronology_verified"]
            else "UNVERIFIED_TEMPORAL_ORDER"
        )
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
            evaluation_scope=scope,
            authority_eligible=False,
            extra={
                **base_extra,
                **chronology,
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
            **chronology,
            "n_total": n,
            "n_train": int(len(y_train)),
            "n_purged": purge_n,
            "n_oos": int(len(y_test)),
            "train_last_timestamp": ordered_ts[train_end - 1].isoformat(),
            "test_first_timestamp": ordered_ts[test_start].isoformat(),
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
