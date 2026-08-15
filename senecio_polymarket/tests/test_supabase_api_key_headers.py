import asyncio
import unittest
from unittest.mock import patch

from backend import settlement_reconciler as reconciler
from backend import supabase_client
from backend.settlement_contract import price_evidence_from_candles, target_epoch_ms
from backend.supabase_client import build_supabase_headers

TS = "2026-08-10T00:00:00+00:00"


def _evidence(window_seconds, price):
    target = target_epoch_ms(TS, window_seconds)
    opened = target - (target % 60_000)
    return price_evidence_from_candles(
        candles=[[opened, price, price, price, price, 1.0]],
        exchange="okx",
        symbol="BTCUSDT",
        ts_iso=TS,
        window_seconds=window_seconds,
        observed_at="2026-08-10T02:00:00+00:00",
    )


class SupabaseApiKeyHeaderTests(unittest.TestCase):
    def test_primary_settlement_persists_observation_provenance(self):
        class Response:
            status_code = 200
            content = b"1"
            text = "ok"

            def __init__(self, body):
                self.body = body

            def json(self):
                return self.body

        class Client:
            def __init__(self):
                self.patch_body = None

            async def get(self, *args, **kwargs):
                return Response([{
                    "id": 7,
                    "ts": TS,
                    "symbol": "BTCUSDT",
                    "prediction": "LONG",
                    "price_now": 100.0,
                    "exchange_used": "okx",
                    "outcome": None,
                    "audit": {
                        "origin_price_v1": {
                            "version": "origin-price-v1",
                            "price": 100.0,
                            "timestamp": TS,
                            "source": "okx",
                        }
                    },
                }])

            async def patch(self, *args, **kwargs):
                self.patch_body = kwargs["json"]
                return Response([{"id": 7}])

        client = Client()
        with patch.object(supabase_client, "_get_client", return_value=client):
            ok = asyncio.run(
                supabase_client.update_outcome_dual(
                    7,
                    "WIN",
                    "WIN",
                    101.0,
                    102.0,
                    price_evidence_15m=_evidence(900, 101.0),
                    price_evidence_1h=_evidence(3600, 102.0),
                )
            )
        self.assertTrue(ok)
        dual = client.patch_body["audit"]["outcomes_dual"]
        self.assertEqual(dual["settled_at"], dual["settlement_observation_v1"]["observed_at"])
        self.assertEqual(
            dual["settlement_observation_v1"]["writer"],
            "SENEX_PRIMARY_DUAL_WINDOW_VERIFIER_V2",
        )
        self.assertEqual(dual["price_evidence_v1"]["15m"]["source"], "okx")
        self.assertEqual(dual["price_evidence_v1"]["1h"]["source"], "okx")

    def test_secret_key_uses_apikey_only(self):
        key = "sb_secret_example_for_test_only"
        headers = build_supabase_headers(key)
        self.assertEqual(headers["apikey"], key)
        self.assertNotIn("Authorization", headers)

    def test_publishable_key_uses_apikey_only(self):
        key = "sb_" + "publishable_example_for_test_only"
        headers = build_supabase_headers(key)
        self.assertEqual(headers["apikey"], key)
        self.assertNotIn("Authorization", headers)

    def test_legacy_jwt_key_keeps_bearer_compatibility(self):
        key = "eyJlegacy.header.signature"
        headers = build_supabase_headers(key)
        self.assertEqual(headers["apikey"], key)
        self.assertEqual(headers["Authorization"], f"Bearer {key}")

    def test_reconciler_uses_secret_key_safe_headers(self):
        key = "sb_secret_reconciler_test_only"
        with (
            patch.object(reconciler, "SUPABASE_URL", "https://example." + "supabase" + ".co"),
            patch.object(reconciler, "SUPABASE_KEY", key),
        ):
            headers = reconciler._headers()
        self.assertEqual(headers["apikey"], key)
        self.assertNotIn("Authorization", headers)


if __name__ == "__main__":
    unittest.main()
