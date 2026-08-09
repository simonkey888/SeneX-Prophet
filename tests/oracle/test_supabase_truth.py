import asyncio
from datetime import datetime, timedelta, timezone

from senecio_polymarket.backend import supabase_client


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)
        self.content = b"json" if payload is not None else b""

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, get_payload):
        self.get_payload = get_payload
        self.get_params = None
        self.patch_body = None

    async def get(self, path, params=None):
        self.get_params = params
        return FakeResponse(200, self.get_payload)

    async def patch(self, path, params=None, json=None):
        self.patch_body = json
        return FakeResponse(200, [{"id": 7}])


def _origin_proof(predicted_at, symbol="BTCUSDT", price=100.0):
    return {
        "proof_schema": "oracle-origin-price-v1",
        "exchange": "okx",
        "instrument": symbol,
        "price_source": "public_ticker_best_bid",
        "observed_at": predicted_at.isoformat(),
        "price": price,
    }


def _observation(predicted_at, window_s, price, instrument="BTC/USDT"):
    target = predicted_at + timedelta(seconds=window_s)
    target_ms = int(target.timestamp() * 1000)
    candle_open_ms = target_ms - target_ms % 60_000
    return {
        "proof_schema": "oracle-settlement-observation-v1",
        "exchange": "okx",
        "instrument": instrument,
        "timeframe": "1m",
        "target_ts": target.isoformat(),
        "target_ts_ms": target_ms,
        "candle_open_ts_ms": candle_open_ms,
        "candle_close_ts_ms": candle_open_ms + 60_000,
        "price_field": "close",
        "price": price,
    }


def test_pending_queue_excludes_legacy_rows_without_origin_proof(monkeypatch):
    predicted_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    rows = [
        {"id": 1, "audit": {}},
        {"id": 2, "audit": {"origin_price_proof": _origin_proof(predicted_at)}},
    ]
    client = FakeClient(rows)
    monkeypatch.setattr(supabase_client, "_get_client", lambda: client)

    result = asyncio.run(supabase_client.fetch_pending_outcomes(limit=1))

    assert [row["id"] for row in result] == [2]
    assert client.get_params["order"] == "ts.desc"
    assert client.get_params["limit"] == "5"


def test_dual_outcome_persists_only_after_complete_proof(monkeypatch):
    predicted_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    row = {
        "id": 7,
        "ts": predicted_at.isoformat(),
        "symbol": "BTCUSDT",
        "exchange_used": "okx",
        "prediction": "LONG",
        "price_now": 100.0,
        "audit": {"origin_price_proof": _origin_proof(predicted_at)},
    }
    client = FakeClient([row])
    monkeypatch.setattr(supabase_client, "_get_client", lambda: client)
    observations = {
        "15m": _observation(predicted_at, 900, 101.0),
        "1h": _observation(predicted_at, 3600, 102.0),
    }

    result = asyncio.run(supabase_client.update_outcome_dual(
        prediction_id=7,
        outcome_15m="WIN",
        outcome_1h="WIN",
        price_15m_later=101.0,
        price_1h_later=102.0,
        settlement_observations=observations,
    ))

    assert result is True
    proof = client.patch_body["audit"]["outcomes_dual"]
    assert proof["settlement_observations"] == observations
    assert client.patch_body["outcome"] == "WIN"


def test_dual_outcome_rejects_legacy_row_without_origin_proof(monkeypatch):
    predicted_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    row = {
        "id": 7,
        "ts": predicted_at.isoformat(),
        "symbol": "BTCUSDT",
        "exchange_used": "okx",
        "prediction": "LONG",
        "price_now": 100.0,
        "audit": {},
    }
    client = FakeClient([row])
    monkeypatch.setattr(supabase_client, "_get_client", lambda: client)

    result = asyncio.run(supabase_client.update_outcome_dual(
        prediction_id=7,
        outcome_15m="WIN",
        outcome_1h="WIN",
        price_15m_later=101.0,
        price_1h_later=102.0,
        settlement_observations={
            "15m": _observation(predicted_at, 900, 101.0),
            "1h": _observation(predicted_at, 3600, 102.0),
        },
    ))

    assert result is False
    assert client.patch_body is None
