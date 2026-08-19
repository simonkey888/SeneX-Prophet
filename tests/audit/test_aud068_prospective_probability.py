from __future__ import annotations

import importlib.util
import math
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODULE = Path(__file__).resolve().parents[2] / "tools/audit/aud068_prospective_probability.py"
spec = importlib.util.spec_from_file_location("aud068", MODULE)
m = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(m)


def make_row(
    idx: int,
    *,
    direction: str = "LONG",
    win: bool = True,
    dt: datetime | None = None,
    up_prob: object = 0.70,
    include_up_prob: bool = True,
) -> dict:
    cutoff = m.parse_utc(m.COHORT_START_TS)
    assert cutoff is not None
    dt = dt or cutoff + timedelta(minutes=1 + 61 * idx)
    ts = dt.isoformat()
    origin = 100.0
    if direction == "LONG":
        later = 101.0 if win else 99.0
    elif direction == "SHORT":
        later = 99.0 if win else 101.0
    else:
        later = 101.0
    outcome = "WIN" if win else "LOSS"

    def evidence(seconds: int) -> dict:
        target = int((dt + timedelta(seconds=seconds)).timestamp() * 1000)
        candle_open = (target // 60000) * 60000
        candle_close = candle_open + 60000
        return {
            "version": "historical-price-evidence-v1",
            "source": "okx",
            "symbol": "BTC/USDT",
            "window_seconds": seconds,
            "target_epoch_ms": target,
            "candle_open_epoch_ms": candle_open,
            "candle_close_epoch_ms": candle_close,
            "candle_interval_ms": 60000,
            "price": later,
            "observed_at": datetime.fromtimestamp(candle_close / 1000, tz=timezone.utc).isoformat(),
        }

    step2 = {}
    if include_up_prob:
        step2["up_prob"] = up_prob
    return {
        "id": idx,
        "ts": ts,
        "symbol": "BTCUSDT",
        "prediction": direction,
        "outcome": outcome,
        "exchange_used": "okx",
        "price_now": origin,
        "audit": {
            "pipeline": {"step2_features": step2},
            "origin_price_v1": {
                "version": "origin-price-v1",
                "timestamp": ts,
                "source": "okx",
                "price": origin,
            },
            "outcomes_dual": {
                "primary_window": "1h",
                "settlement_contract_version": "aud063-v1",
                "outcome_15m": outcome,
                "outcome_1h": outcome,
                "price_15m_later": later,
                "price_1h_later": later,
                "price_evidence_v1": {"15m": evidence(900), "1h": evidence(3600)},
                "settlement_observation_v1": {
                    "version": "settlement-observation-v1",
                    "observed_at": (dt + timedelta(hours=1, minutes=2)).isoformat(),
                },
            },
        },
    }


class AUD068Tests(unittest.TestCase):
    def test_01_cutoff_is_strict_even_if_row_is_later_settled(self):
        cutoff = m.parse_utc(m.COHORT_START_TS)
        assert cutoff is not None
        row = make_row(1, dt=cutoff)
        ok, reason, _ = m.eligibility(row, cutoff)
        self.assertFalse(ok)
        self.assertEqual(reason, "AT_OR_BEFORE_PROSPECTIVE_CUTOFF")

    def test_02_long_short_mapping_is_exact(self):
        long_row = make_row(1, direction="LONG", up_prob=0.73)
        short_row = make_row(2, direction="SHORT", up_prob=0.73)
        self.assertAlmostEqual(m.candidate_probability(long_row)[0], 0.73)
        self.assertAlmostEqual(m.candidate_probability(short_row)[0], 0.27)

    def test_03_flat_cannot_enter_probability_validation(self):
        row = make_row(1, direction="FLAT")
        cutoff = m.parse_utc(m.COHORT_START_TS)
        assert cutoff is not None
        ok, reason, _ = m.eligibility(row, cutoff)
        self.assertFalse(ok)
        self.assertEqual(reason, "PREDICTION_NOT_DIRECTIONAL")

    def test_04_missing_malformed_and_out_of_range_up_prob_fail_closed(self):
        cutoff = m.parse_utc(m.COHORT_START_TS)
        assert cutoff is not None
        cases = [
            (make_row(1, include_up_prob=False), "MISSING_PERSISTED_UP_PROB"),
            (make_row(2, up_prob="not-a-number"), "MALFORMED_PERSISTED_UP_PROB"),
            (make_row(3, up_prob=float("inf")), "MALFORMED_PERSISTED_UP_PROB"),
            (make_row(4, up_prob=1.1), "PERSISTED_UP_PROB_OUT_OF_RANGE"),
            (make_row(5, up_prob=-0.01), "PERSISTED_UP_PROB_OUT_OF_RANGE"),
        ]
        for row, expected in cases:
            ok, reason, _ = m.eligibility(row, cutoff)
            self.assertFalse(ok)
            self.assertEqual(reason, expected)

    def test_05_same_input_bytes_produce_same_ids_and_cohort_sha(self):
        rows = [make_row(i, direction="LONG" if i % 2 == 0 else "SHORT") for i in range(1, 8)]
        a = m.validate_capture({"rows": rows})
        b = m.validate_capture({"rows": list(reversed(rows))})
        self.assertEqual([r["id"] for r in a["selected_rows"]], [r["id"] for r in b["selected_rows"]])
        self.assertEqual(a["selected_cohort_sha256"], b["selected_cohort_sha256"])

    def test_06_independent_nonoverlap_1h_is_greedy_and_deterministic(self):
        cutoff = m.parse_utc(m.COHORT_START_TS)
        assert cutoff is not None
        rows = [
            make_row(1, dt=cutoff + timedelta(minutes=1)),
            make_row(2, dt=cutoff + timedelta(minutes=30)),
            make_row(3, dt=cutoff + timedelta(minutes=61)),
            make_row(4, dt=cutoff + timedelta(minutes=90)),
            make_row(5, dt=cutoff + timedelta(minutes=121)),
        ]
        selected = m.independent_nonoverlap(rows)
        self.assertEqual([r["id"] for r in selected], [1, 3, 5])

    def test_07_outcome_changes_cannot_change_maturity_prefix(self):
        wins = [make_row(i, direction="LONG" if i % 2 == 0 else "SHORT", win=True) for i in range(100)]
        losses = [make_row(i, direction="LONG" if i % 2 == 0 else "SHORT", win=False) for i in range(100)]
        a = m.validate_capture({"rows": wins})
        b = m.validate_capture({"rows": losses})
        self.assertEqual(a["mature_prefix_n"], 100)
        self.assertEqual(b["mature_prefix_n"], 100)
        self.assertEqual([r["id"] for r in a["selected_rows"]], [r["id"] for r in b["selected_rows"]])

    def test_08_metrics_reconcile_from_row_level_evidence(self):
        rows = [
            {"prediction": "LONG", "p_correct_candidate": 0.8, "outcome": "WIN"},
            {"prediction": "SHORT", "p_correct_candidate": 0.2, "outcome": "LOSS"},
        ]
        got = m.metrics(rows)
        self.assertEqual(got["global"]["n"], 2)
        self.assertEqual(got["global"]["wins"], 1)
        self.assertEqual(got["global"]["losses"], 1)
        self.assertAlmostEqual(got["mean_p_correct_candidate"], 0.5)
        self.assertAlmostEqual(got["brier"], 0.04)
        self.assertAlmostEqual(got["ece_10_equal_width"], 0.2)

    def test_09_warmup_never_validates_probability_semantics(self):
        rows = [make_row(i, direction="LONG" if i % 2 == 0 else "SHORT") for i in range(20)]
        got = m.validate_capture({"rows": rows})
        self.assertEqual(got["prospective_eval_status"], "WARMUP")
        self.assertEqual(got["probability_semantics_validated"], "NO")
        self.assertIsNone(got["terminal_metrics"])

    def test_10_mature_metrics_use_earliest_predeclared_prefix_only(self):
        rows = [make_row(i, direction="LONG" if i % 2 == 0 else "SHORT", up_prob=0.6) for i in range(105)]
        got = m.validate_capture({"rows": rows})
        self.assertEqual(got["prospective_eval_status"], "MATURE")
        self.assertEqual(got["mature_prefix_n"], 100)
        self.assertEqual(got["terminal_metrics"]["global"]["n"], 100)
        self.assertEqual(got["probability_semantics_validated"], "NO")

    def test_11_proof_malformed_is_ineligible_not_exception(self):
        row = make_row(1)
        del row["audit"]["outcomes_dual"]["price_evidence_v1"]
        cutoff = m.parse_utc(m.COHORT_START_TS)
        assert cutoff is not None
        ok, reason, _ = m.eligibility(row, cutoff)
        self.assertFalse(ok)
        self.assertEqual(reason, "AUD063_PROOF_NOT_QUALIFIED")

    def test_12_current_capture_shape_flat_pending_means_n_zero(self):
        capture = {
            "rows": [{
                "id": 1246,
                "ts": "2026-08-19T19:00:23.117851+00:00",
                "symbol": "BTCUSDT",
                "prediction": "FLAT",
                "outcome": None,
                "exchange_used": "okx",
                "price_now": 68365.6,
                "audit": {"pipeline": {"step2_features": {"up_prob": 0.998007}}},
            }]
        }
        got = m.validate_capture(capture)
        self.assertEqual(got["current_prospective_n"], 0)
        self.assertEqual(got["prospective_eval_status"], "WARMUP")
        self.assertEqual(got["excluded_rows"][0]["reason"], "PREDICTION_NOT_DIRECTIONAL")


if __name__ == "__main__":
    unittest.main()
