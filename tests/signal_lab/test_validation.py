from __future__ import annotations

from polymarket.signal_lab.validation import EvaluationPoint, ValidationHarness


def points():
    return [
        EvaluationPoint("m1", "2026-08-01", "2026-08-01T00:05:00Z", "2026-08-01T00:04:59Z", 0.70, 0.55, 1, 0.02, True, "LIQUID"),
        EvaluationPoint("m1", "2026-08-01", "2026-08-01T00:10:00Z", "2026-08-01T00:09:58Z", 0.30, 0.45, 0, 0.02, True, "LIQUID"),
        EvaluationPoint("m2", "2026-08-02", "2026-08-02T00:05:00Z", "2026-08-02T00:04:59Z", 0.62, 0.52, 1, 0.04, False, "THIN"),
        EvaluationPoint("m2", "2026-08-02", "2026-08-02T00:10:00Z", "2026-08-02T00:09:58Z", 0.35, 0.48, 0, 0.03, True, "THIN"),
        EvaluationPoint("m3", "2026-08-03", "2026-08-03T00:05:00Z", "2026-08-03T00:04:59Z", 0.58, 0.50, 1, 0.01, True, "NORMAL"),
        EvaluationPoint("m3", "2026-08-03", "2026-08-03T00:10:00Z", "2026-08-03T00:09:58Z", 0.42, 0.50, 0, 0.01, True, "NORMAL"),
    ]


def test_validation_harness_supports_required_oos_surfaces():
    report = ValidationHarness.evaluate(points())
    assert report["baseline"] == "MID_PRICE"
    assert 0 <= report["oos_brier"] <= 1
    assert report["oos_log_loss"] >= 0
    assert 0 <= report["calibration_error"] <= 1
    assert 0 <= report["directional_information"] <= 1
    assert set(report["bootstrap_by_market"]) == {"mean", "p025", "p975"}
    assert set(report["bootstrap_by_day"]) == {"mean", "p025", "p975"}
    assert "permutation_p" in report["permutation_test"]
    assert report["walk_forward"]
    assert set(report["regime_splits"]) == {"LIQUID", "NORMAL", "THIN"}
    assert report["no_fill_simulation"]["requested"] == 6
    assert report["no_fill_simulation"]["filled"] == 5
    assert report["status"] == "PASS"


def test_future_shuffle_negative_lag_timestamp_join_and_resolution_gates_pass():
    leakage = ValidationHarness.anti_leakage(points())
    assert leakage["future_shuffle_test"] == "PASS"
    assert leakage["negative_lag_test"] == "PASS"
    assert leakage["timestamp_join_audit"] == "PASS"
    assert leakage["resolution_leak_test"] == "PASS"
    assert leakage["deterministic_replay_canary"] == "PASS"
    assert leakage["violations"] == []


def test_timestamp_future_read_is_detected():
    bad = EvaluationPoint(
        "leak", "2026-08-04", "2026-08-04T00:05:00Z", "2026-08-04T00:05:01Z",
        0.7, 0.5, 1,
    )
    report = ValidationHarness.anti_leakage([bad])
    assert report["future_shuffle_test"] == "FAIL"
    assert report["negative_lag_test"] == "FAIL"
    assert report["timestamp_join_audit"] == "FAIL"


def test_replay_canary_is_deterministic():
    first = ValidationHarness.anti_leakage(points())["replay_hash"]
    second = ValidationHarness.anti_leakage(points())["replay_hash"]
    assert first == second
