from __future__ import annotations

import gzip
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from backend.research import aud062_forensics as audit
from backend.research.aud062_r1_contracts import (
    canonical_ev_contract,
    verify_persisted_roundtrip,
)


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "docs" / "evidence" / "aud062-public-inputs.json.gz"
FIXED_NOW = datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc)
FIXED_EPOCH = FIXED_NOW.timestamp()
FIXED_EPOCH_MS = int(FIXED_EPOCH * 1000)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is not None else FIXED_NOW.replace(tzinfo=None)


def _learning_row() -> dict:
    origin = FIXED_NOW - timedelta(days=2)
    settled = origin + timedelta(hours=1)
    return {
        "id": "r2-prior-1",
        "ts": origin.isoformat(),
        "symbol": "BTCUSDT",
        "prediction": "LONG",
        "confidence": 0.6,
        "price_now": 100.0,
        "outcome": "WIN",
        "audit": {
            "origin_price_v1": {
                "version": "origin-price-v1",
                "price": 100.0,
                "timestamp": origin.isoformat(),
                "source": "okx",
            },
            "outcomes_dual": {
                "outcome_15m": "WIN",
                "outcome_1h": "WIN",
                "price_15m_later": 100.5,
                "price_1h_later": 101.0,
                "primary_window": "1h",
                "settled_at": settled.isoformat(),
            },
            "pipeline": {
                "step2_features": {
                    "conviction": 0.6,
                    "regime_4h": "NEUTRAL",
                    "pressures": {"orderflow": 0.25},
                }
            },
        },
    }


def _market_fixture() -> dict:
    ohlcv = []
    for index in range(20):
        close = 100_000.0 + index * 10.0
        ohlcv.append([
            FIXED_EPOCH_MS - (19 - index) * 900_000,
            close - 5.0,
            close + 20.0,
            close - 20.0,
            close,
            100.0 + index,
        ])
    observations = {
        "orderflow": "okx:BTC-USDT:SPOT:public-trades",
        "volume_delta": "okx:BTC-USDT:SPOT:ohlcv",
        "bidask_imbalance": "okx:BTC-USDT:SPOT:orderbook",
        "price_momentum": "okx:BTC-USDT:SPOT:ohlcv",
    }
    feature_observations = {
        name: {
            "status": "REAL_NONZERO",
            "source": source,
            "observed_at": FIXED_NOW.isoformat(),
            "exchange_timestamp": FIXED_EPOCH_MS,
            "query_observation_epoch": FIXED_EPOCH,
        }
        for name, source in observations.items()
    }
    feature_observations.update({
        "funding_signal": {
            "status": "MISSING", "source": "not_applicable_spot",
            "observed_at": None, "exchange_timestamp": None,
            "query_observation_epoch": FIXED_EPOCH,
        },
        "oi_momentum": {
            "status": "MISSING", "source": "not_applicable_spot",
            "observed_at": None, "exchange_timestamp": None,
            "query_observation_epoch": FIXED_EPOCH,
        },
    })
    return {
        "symbol": "BTC/USDT",
        "timeframe": "15m",
        "exchange_used": "okx",
        "timestamp": FIXED_EPOCH_MS,
        "candle_ts": FIXED_EPOCH_MS,
        "ohlcv": ohlcv,
        "ticker": {
            "bid": 100_190.0,
            "ask": 100_191.0,
            "last": 100_190.5,
            "spread_pct": 0.00001,
            "spread_bps": 0.1,
            "timestamp": FIXED_EPOCH_MS,
        },
        "orderbook": {
            "bid_depth": 250_000.0,
            "ask_depth": 200_000.0,
            "bid": 100_190.0,
            "ask": 100_191.0,
            "timestamp": FIXED_EPOCH_MS,
        },
        "funding": {"rate": 0.0},
        "open_interest": {"oi_change_24h_pct": 0.0},
        "liquidity_quality": 0.99,
        "feature_observations": feature_observations,
        "irrelevant_metadata": {"z": 2, "a": 1},
    }


def _external_fixtures() -> tuple[dict, dict, dict]:
    observed_at = FIXED_NOW.isoformat()
    return (
        {
            "source": "POLYMARKET_PUBLIC", "status": "LIVE_WS",
            "observed_at": observed_at, "eligible_for_prediction": True,
            "up_probability": 0.55, "down_probability": 0.45,
            "directional_pressure": 0.1, "slug": "r2-fixture",
        },
        {
            "source": "KALSHI_PUBLIC_REST", "status": "LIVE",
            "observed_at": observed_at, "directional_use": False,
            "market": {"yes_probability": 0.54},
        },
        {
            "source": "BOROS_PUBLIC_API", "status": "LIVE",
            "observed_at": observed_at, "directional_use": False,
            "markets": [],
        },
    )


class Aud062R2RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with gzip.open(INPUT, "rt", encoding="utf-8") as handle:
            cls.bundle = json.load(handle)

    def test_r2_f001_core_ev_never_falls_back_to_anchor_adjusted_ev(self):
        explicit = canonical_ev_contract(
            "BTCUSDT",
            {"direction": "LONG", "heuristic_up_score": 0.91},
            {"ruin_prob": 0.0, "survivability_ruin_prob": 0.5},
            {
                "base_ev": 0.01,
                "core_survival_ev": 0.008,
                "historical_adjusted_ev": -0.004,
                "adjusted_ev": -0.004,
                "market_anchor_ev": -0.004,
            },
        )
        core = explicit["historical_core_ev"]
        anchor = explicit["historical_parallel_market_anchor"]
        self.assertEqual(core["survival_adjusted_ev"], 0.008)
        self.assertEqual(core["status"], "DIAGNOSTIC_ONLY")
        self.assertEqual(anchor["serialized_value"], -0.004)
        self.assertEqual(anchor["historical_final_adjusted_ev"], -0.004)
        self.assertNotEqual(core["survival_adjusted_ev"], anchor["historical_final_adjusted_ev"])
        self.assertFalse(explicit["tradeable"])
        self.assertEqual(explicit["authority_status"], "NOT_ESTIMABLE")

        missing = canonical_ev_contract(
            "BTCUSDT",
            {"direction": "LONG", "heuristic_up_score": 0.91},
            {},
            {"base_ev": 0.01, "adjusted_ev": -0.004},
        )
        self.assertIsNone(missing["historical_core_ev"]["survival_adjusted_ev"])
        self.assertEqual(missing["historical_core_ev"]["status"], "NOT_RECONSTRUCTIBLE")

        neutral = canonical_ev_contract(
            "BTCUSDT",
            {"direction": "NEUTRAL", "heuristic_up_score": 0.9, "heuristic_down_score": 0.1},
            {},
            {"base_ev": 0.0, "core_survival_ev": 0.0},
        )
        self.assertIsNone(neutral["probability_input"]["value"])
        self.assertEqual(neutral["probability_input"]["semantic_class"], "NOT_APPLICABLE")

    def test_r2_f001_runtime_passes_reconstructed_core_ev_not_original_payload(self):
        from oracle_runtime import institutional_core as runtime_core

        historical = {
            "base_ev": 0.01,
            "adjusted_ev": -0.004,
            "survival_discount": 0.8,
            "market_anchor_ev": -0.004,
            "tradeable": False,
        }
        with mock.patch.object(
            runtime_core.OriginalSingleDecisionCore,
            "compute_ev",
            return_value=historical,
        ):
            result = runtime_core.SingleDecisionCore().compute_ev(
                {
                    "direction": "LONG", "conviction": 0.8, "noise": 0.1,
                    "up_prob": 0.9, "down_prob": 0.1,
                    "heuristic_up_score": 0.9, "heuristic_down_score": 0.1,
                },
                {"risk_score": 0.0, "size_multiplier": 1.0},
                {"symbol": "BTC/USDT", "volatility": 0.01},
            )
        canonical = result["canonical_ev_v1"]
        self.assertEqual(result["core_survival_ev"], 0.008)
        self.assertEqual(canonical["historical_core_ev"]["survival_adjusted_ev"], 0.008)
        self.assertEqual(canonical["historical_parallel_market_anchor"]["serialized_value"], -0.004)
        self.assertEqual(canonical["historical_parallel_market_anchor"]["historical_final_adjusted_ev"], -0.004)

    def test_r2_f002_ruleset_is_mergeable_for_owner_authored_pr_without_bypass(self):
        artifacts, _ = audit.build_artifacts(self.bundle)
        manifest = artifacts["aud-062-r1-governance-settings-manifest.json"]
        request = json.loads(json.dumps(manifest["rest_ruleset_request_body"], sort_keys=True))
        self.assertEqual(request["target"], "branch")
        self.assertEqual(request["enforcement"], "active")
        self.assertEqual(request["bypass_actors"], [])
        rules = {item["type"]: item for item in request["rules"]}
        self.assertIn("deletion", rules)
        self.assertIn("non_fast_forward", rules)
        self.assertIn("pull_request", rules)
        self.assertIn("required_status_checks", rules)
        pull = rules["pull_request"]["parameters"]
        self.assertEqual(pull["required_approving_review_count"], 0)
        self.assertFalse(pull["require_last_push_approval"])
        self.assertFalse(pull["dismiss_stale_reviews_on_push"])
        checks = rules["required_status_checks"]["parameters"]
        self.assertTrue(checks["strict_required_status_checks_policy"])
        self.assertEqual(
            [item["context"] for item in checks["required_status_checks"]],
            ["score-001", "score-002", "act_final_audit_smoke (T1-T12)", "AUD_EXACT_HEAD_GATE"],
        )
        self.assertEqual(manifest["owner_aud_authorization"], "ISSUE23_PROCESS_GATE")
        self.assertFalse(manifest["self_approval_required"])
        self.assertFalse(manifest["normal_author_can_bypass_required_checks"])
        self.assertFalse(manifest["github_settings_applied"])

    def test_r2_f003_whole_runtime_row_round_trips_from_persisted_json_only(self):
        from oracle_runtime import institutional_core as learning
        from oracle_runtime import predict_only as runtime_predictor

        poly, kalshi, boros = _external_fixtures()
        fixture = _market_fixture()

        def execute(market: dict) -> dict:
            with mock.patch.dict(
                os.environ,
                {"SUPABASE_URL": "https://example.invalid", "SUPABASE_KEY": "test-only-placeholder"},
                clear=False,
            ), mock.patch.object(
                learning, "fetch_authoritative_rows", return_value=[_learning_row()]
            ), mock.patch.object(
                runtime_predictor, "_poly_snapshot_for_prediction", return_value=poly
            ), mock.patch.object(
                runtime_predictor, "_kalshi_snapshot_for_audit", return_value=kalshi
            ), mock.patch.object(
                runtime_predictor, "_boros_snapshot_for_audit", return_value=boros
            ), mock.patch.object(
                runtime_predictor._base, "_feed_calibration_from_predictions", return_value=None
            ), mock.patch.object(
                runtime_predictor._base, "datetime", FixedDateTime
            ):
                return runtime_predictor.run_prediction(market)

        first = execute(fixture)
        first_contract = first["_audit"]["decision_provenance_roundtrip_v2"]
        reordered = {key: fixture[key] for key in reversed(list(fixture))}
        second = execute(reordered)
        second_contract = second["_audit"]["decision_provenance_roundtrip_v2"]
        self.assertEqual(first_contract["source_evidence_hash"], second_contract["source_evidence_hash"])
        self.assertEqual(first_contract["market_snapshot_identity"], second_contract["market_snapshot_identity"])

        persisted_json = json.dumps(first, sort_keys=True, separators=(",", ":"))
        del first, first_contract, fixture, reordered, second, second_contract
        reloaded = json.loads(persisted_json)
        persisted = reloaded["_audit"]["decision_provenance_roundtrip_v2"]
        check = verify_persisted_roundtrip(persisted)
        self.assertTrue(check["hash_matches"])
        self.assertEqual(check["feature_observation_cutoff_classification"], "COMPLETE")
        self.assertEqual(check["external_observation_cutoff_classification"], "COMPLETE")
        self.assertEqual(check["learning_provenance_classification"], "COMPLETE")
        real = {
            "REAL_NONZERO", "REAL_OBSERVED_ZERO",
        }
        for item in persisted["feature_observations"]:
            if item["status"] in real:
                self.assertTrue(item["source_identity"])
                self.assertIsNotNone(item["observed_at"])
        for item in persisted["external_observations"]:
            if item["status"] != "NOT_APPLICABLE":
                self.assertTrue(item["source_identity"])
                self.assertIsNotNone(item["observed_at"])
        self.assertEqual(persisted["learning_source_prediction_ids"], ["r2-prior-1"])
        self.assertEqual(len(persisted["learning_source_settlement_observation_epochs"]), 1)
        self.assertTrue(persisted["learning_evidence_hash"])
        self.assertEqual(len(persisted["decision_weights_hash"]), 64)
        self.assertEqual(len(persisted["market_snapshot_identity"]), 64)
        self.assertFalse(persisted["legacy_timestamp_invention"])


if __name__ == "__main__":
    unittest.main()
