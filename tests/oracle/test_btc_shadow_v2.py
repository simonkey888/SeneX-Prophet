from datetime import datetime, timedelta, timezone

from senecio_polymarket.backend.btc_shadow_v2 import evaluate_btc_shadow

NOW = datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc)


def _rows(n, wins, direction="LONG", confidence=0.65):
    rows = []
    for i in range(n):
        outcome = "WIN" if i < wins else "LOSS"
        price_1h = 101.0 if (direction == "LONG") == (outcome == "WIN") else 99.0
        predicted_at = NOW - timedelta(hours=i + 2)
        target_at = predicted_at + timedelta(hours=1)
        target_ms = int(target_at.timestamp() * 1000)
        candle_open_ms = target_ms - target_ms % 60_000
        rows.append({
            "id": i,
            "ts": predicted_at.isoformat(),
            "symbol": "BTCUSDT",
            "exchange_used": "okx",
            "prediction": direction,
            "confidence": confidence,
            "outcome": outcome,
            "price_now": 100.0,
            "audit": {
                "origin_price_proof": {
                    "proof_schema": "oracle-origin-price-v1",
                    "exchange": "okx",
                    "instrument": "BTCUSDT",
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
                    "instrument": "BTC/USDT",
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


def _current(direction, confidence=0.65):
    return {
        "symbol": "BTCUSDT",
        "prediction": direction,
        "confidence": confidence,
        "ts": (NOW - timedelta(minutes=5)).isoformat(),
    }


def test_insufficient_cohort_fails_closed():
    result = evaluate_btc_shadow(_current("LONG"), _rows(20, 15), now=NOW)
    assert result["gate_status"] == "UNKNOWN"
    assert result["shadow_action"] == "FLAT"


def test_recent_losing_cohort_is_rejected():
    result = evaluate_btc_shadow(_current("SHORT"), _rows(60, 29, "SHORT"), now=NOW)
    assert result["gate_status"] == "REJECT"
    assert result["shadow_action"] == "FLAT"


def test_strong_recent_cohort_can_confirm_without_orders():
    result = evaluate_btc_shadow(_current("LONG"), _rows(80, 60), now=NOW)
    assert result["gate_status"] == "PASS"
    assert result["shadow_action"] == "LONG"
    assert result["orders_enabled"] is False
    assert result["live_capital_locked"] is True


def test_flat_source_stays_flat():
    result = evaluate_btc_shadow(_current("FLAT", 0.9), _rows(80, 70), now=NOW)
    assert result["shadow_action"] == "FLAT"
    assert result["gate_status"] == "UNKNOWN"


def test_legacy_win_loss_rows_without_dual_proof_are_not_counted():
    legacy = [{"symbol": "BTCUSDT", "prediction": "LONG", "confidence": 0.9, "outcome": "WIN"}] * 100
    result = evaluate_btc_shadow(_current("LONG", 0.9), legacy, now=NOW)
    assert result["gate_status"] == "UNKNOWN"
    assert result["cohort"]["n"] == 0
