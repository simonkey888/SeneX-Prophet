from datetime import datetime, timedelta, timezone

from senecio_polymarket.backend.score_truth import (
    build_score_report,
    decorate_prediction,
    validate_1h_outcome,
)


NOW = datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc)


def _proved_rows(symbol="BTCUSDT", n=100, wins=80, confidence=0.8):
    rows = []
    for index in range(n):
        outcome = "WIN" if index < wins else "LOSS"
        direction = "LONG"
        price_1h = 101.0 if outcome == "WIN" else 99.0
        predicted_at = NOW - timedelta(hours=index + 2)
        target_at = predicted_at + timedelta(hours=1)
        target_ms = int(target_at.timestamp() * 1000)
        candle_open_ms = target_ms - target_ms % 60_000
        rows.append({
            "id": index,
            "ts": predicted_at.isoformat(),
            "symbol": symbol,
            "exchange_used": "okx",
            "prediction": direction,
            "confidence": confidence,
            "outcome": outcome,
            "price_now": 100.0,
            "audit": {
                "origin_price_proof": {
                    "proof_schema": "oracle-origin-price-v1",
                    "exchange": "okx",
                    "instrument": symbol,
                    "price_source": "public_ticker_best_bid",
                    "observed_at": predicted_at.isoformat(),
                    "price": 100.0,
                },
                "outcomes_dual": {
                "proof_schema": "oracle-settlement-proof-v1",
                "price_source": "okx_public_ohlcv",
                "settlement_method": "historical_1m_close_containing_target",
                "settled_at": NOW.isoformat(),
                "primary_window": "1h",
                "outcome_1h": outcome,
                "price_1h_later": price_1h,
                "settlement_observations": {"1h": {
                    "proof_schema": "oracle-settlement-observation-v1",
                    "exchange": "okx",
                    "instrument": symbol,
                    "timeframe": "1m",
                    "target_ts": target_at.isoformat(),
                    "target_ts_ms": target_ms,
                    "candle_open_ts_ms": candle_open_ms,
                    "candle_close_ts_ms": candle_open_ms + 60_000,
                    "price_field": "close",
                    "price": price_1h,
                }},
            }},
        })
    return rows


def test_legacy_outcomes_cannot_create_an_authoritative_score():
    legacy = [
        {
            "ts": NOW.isoformat(), "symbol": "BTCUSDT", "prediction": "LONG",
            "confidence": 0.99, "outcome": "WIN", "price_now": 100.0,
        }
        for _ in range(500)
    ]
    report = build_score_report(legacy, requested_symbol="BTCUSDT")
    assert report["score_status"] == "UNKNOWN"
    assert report["authoritative_score_pct"] is None
    assert report["proof_qualified_rows"] == 0
    assert report["exclusion_reasons"]["MISSING_DUAL_WINDOW_PROOF"] == 500


def test_stored_label_must_match_recomputed_price_direction():
    row = _proved_rows(n=1, wins=1)[0]
    row["audit"]["outcomes_dual"]["price_1h_later"] = 99.0
    row["audit"]["outcomes_dual"]["settlement_observations"]["1h"]["price"] = 99.0
    clean, reason = validate_1h_outcome(row)
    assert clean is None
    assert reason == "RECOMPUTED_OUTCOME_MISMATCH"


def test_settlement_target_must_match_exact_one_hour_horizon():
    row = _proved_rows(n=1, wins=1)[0]
    observation = row["audit"]["outcomes_dual"]["settlement_observations"]["1h"]
    observation["target_ts_ms"] += 60_000
    clean, reason = validate_1h_outcome(row)
    assert clean is None
    assert reason == "INVALID_SETTLEMENT_OBSERVATION"


def test_cross_exchange_origin_is_not_scoreable():
    row = _proved_rows(n=1, wins=1)[0]
    row["exchange_used"] = "binance"
    clean, reason = validate_1h_outcome(row)
    assert clean is None
    assert reason == "INVALID_ORIGIN_PRICE_PROOF"


def test_proved_calibrated_cohort_can_publish_nullable_score():
    report = build_score_report(_proved_rows(), requested_symbol="BTCUSDT")
    assert report["score_status"] == "CALIBRATED"
    assert report["authoritative_score_pct"] is not None
    assert report["selected"]["verified"] == 100
    assert report["selected"]["raw_confidence_brier"] < 0.25


def test_cross_instrument_aggregate_is_never_promoted():
    rows = _proved_rows("BTCUSDT") + _proved_rows("ETHUSDT")
    report = build_score_report(rows)
    assert report["score_status"] == "MULTI_INSTRUMENT_REPORT"
    assert report["authoritative_score_pct"] is None
    assert set(report["by_symbol"]) == {"BTCUSDT", "ETHUSDT"}


def test_prediction_publication_labels_raw_confidence_honestly():
    row = decorate_prediction({"prediction": "LONG", "confidence": 0.73})
    assert row["publication_status"] == "RAW_UNCALIBRATED"
    assert row["authoritative_score_pct"] is None
    assert row["orders_enabled"] is False
