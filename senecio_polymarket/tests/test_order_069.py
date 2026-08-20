from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "backend" / "portfolio" / "binance_usdm_shadow.py"
spec = importlib.util.spec_from_file_location("order069_shadow", MODULE)
m = importlib.util.module_from_spec(spec)
assert spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._body = json.dumps(payload, separators=(",", ":")).encode()
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def getcode(self): return self.status
    def read(self, _limit=None): return self._body


class RecordingOpener:
    def __init__(self, payload=None):
        self.calls=[]
        self.payload=payload or {"serverTime": 1}
    def __call__(self, req, timeout=None):
        self.calls.append((req.get_method(), req.full_url, timeout))
        return FakeResponse(self.payload)


class Order069Tests(unittest.TestCase):
    def setUp(self):
        self.capture=m.fixture_capture()

    def route(self, signal):
        decision={"schema_version":"order069.decision.v1","symbol":"BTCUSDT","signal":signal,"confidence":"0.6","source":"fixture"}
        return decision,m.route_shadow(decision,self.capture)

    def test_offline_long_buy_fill_and_ledger(self):
        _,r=self.route("LONG")
        self.assertEqual(r["intent"]["side"],"BUY")
        self.assertEqual(r["outcome"]["status"],"SHADOW_FILL")
        self.assertEqual(r["ledger"]["REAL_ORDER_COUNT"],0)
        self.assertEqual(r["ledger"]["REAL_CAPITAL_MOVEMENT"],0)
        self.assertLessEqual(m.D(r["intent"]["post_round_mark_notional_usdt"]),m.TARGET_NOTIONAL_CAP)

    def test_offline_short_sell_fill_and_ledger(self):
        _,r=self.route("SHORT")
        self.assertEqual(r["intent"]["side"],"SELL")
        self.assertEqual(r["outcome"]["status"],"SHADOW_FILL")
        self.assertEqual(r["ledger"]["synthetic_position"]["side"],"SELL")

    def test_offline_flat_is_no_order(self):
        _,r=self.route("FLAT")
        self.assertEqual(r["intent"]["status"],"NO_ORDER_FLAT")
        self.assertEqual(r["outcome"]["status"],"NO_ORDER")
        self.assertEqual(r["ledger"]["synthetic_position"]["quantity"],"0")

    def test_replay_byte_stable(self):
        decision={"schema_version":"order069.decision.v1","symbol":"BTCUSDT","signal":"LONG","confidence":"0.6","source":"fixture"}
        a=m.route_shadow(decision,copy.deepcopy(self.capture))
        b=m.route_shadow(copy.deepcopy(decision),copy.deepcopy(self.capture))
        self.assertEqual(m.canonical_bytes(a),m.canonical_bytes(b))
        self.assertEqual(m.sha256_json(a),m.sha256_json(b))

    def test_non_get_and_unknown_path_fail_before_transport(self):
        opener=RecordingOpener(); t=m.PublicGetTransport(opener=opener,retries=0)
        with self.assertRaises(m.ShadowBoundaryError): t.assert_allowed("POST","/fapi/v1/time")
        with self.assertRaises(m.ShadowBoundaryError): t.get_json("/fapi/v1/order")
        self.assertEqual(opener.calls,[])

    def test_forbidden_operation_surface_absent(self):
        names=set(dir(m.BinanceUsdMShadowProvider))|set(dir(m.PublicGetTransport))
        self.assertTrue(m.FORBIDDEN_OPERATION_NAMES.isdisjoint(names))

    def test_api_key_environment_not_referenced(self):
        source=MODULE.read_text(encoding="utf-8")
        for token in ("API_KEY","API_SECRET","BINANCE_API","os.environ","getenv"):
            self.assertNotIn(token,source)

    def test_timeout_fail_closed(self):
        class Boom:
            def __call__(self,*a,**k): raise TimeoutError("x")
        t=m.PublicGetTransport(opener=Boom(),retries=0)
        with self.assertRaises(m.ShadowDataError): t.get_json("/fapi/v1/time")

    def test_malformed_json_fail_closed(self):
        class Bad(FakeResponse):
            def __init__(self): self.status=200; self._body=b"{"
        t=m.PublicGetTransport(opener=lambda *a,**k: Bad(),retries=0)
        with self.assertRaises(m.ShadowDataError): t.get_json("/fapi/v1/time")

    def test_empty_and_crossed_book_fail_closed(self):
        for mutate in ("empty","crossed"):
            c=copy.deepcopy(self.capture)
            if mutate=="empty": c["depth"]["asks"]=[]
            else: c["depth"]["bids"][0][0]="70000"
            with self.assertRaises(m.ShadowDataError): m.validate_capture(c)

    def test_stale_book_fail_closed(self):
        c=copy.deepcopy(self.capture); c["server_time"]["serverTime"] += m.MAX_BOOK_AGE_MS+1
        with self.assertRaises(m.ShadowDataError): m.validate_capture(c)

    def test_wrong_contract_identity_fail_closed(self):
        cases=[("status","BREAK"),("contractType","CURRENT_QUARTER"),("quoteAsset","USDC"),("marginAsset","USDC")]
        for key,val in cases:
            c=copy.deepcopy(self.capture); c["exchange_info"]["symbols"][0][key]=val
            with self.assertRaises(m.ShadowDataError,msg=key): m.validate_capture(c)

    def test_missing_or_invalid_lot_filter_fail_closed(self):
        c=copy.deepcopy(self.capture); c["exchange_info"]["symbols"][0]["filters"]=[{"filterType":"MIN_NOTIONAL","notional":"5"}]
        with self.assertRaises(m.ShadowDataError): m.validate_capture(c)
        with self.assertRaises(m.ShadowDataError): m.fixture_capture(step="0")

    def test_filter_cap_and_rounding_fail_closed_to_no_order(self):
        c=m.fixture_capture(min_notional="30"); d={"signal":"LONG","symbol":"BTCUSDT"}
        i=m.build_intent(d,c)
        self.assertEqual(i["status"],"SHADOW_FILTER_INCOMPATIBLE")
        self.assertEqual(m.route_shadow(d,c)["outcome"]["status"],"NO_ORDER")

    def test_qty_below_min_after_rounding_no_order(self):
        c=m.fixture_capture(min_qty="0.001",step="0.001",min_notional="5"); d={"signal":"LONG","symbol":"BTCUSDT"}
        self.assertEqual(m.build_intent(d,c)["status"],"SHADOW_FILTER_INCOMPATIBLE")

    def test_insufficient_depth_no_fill(self):
        c=copy.deepcopy(self.capture); c["depth"]["asks"]=[["60010","0.00001"]]
        r=m.route_shadow({"signal":"LONG","symbol":"BTCUSDT"},c)
        self.assertEqual(r["outcome"]["status"],"NO_ORDER")
        self.assertEqual(r["outcome"]["reason"],"NO_SHADOW_FILL_INSUFFICIENT_DEPTH")

    def test_duplicate_replay_idempotence(self):
        d={"signal":"SHORT","symbol":"BTCUSDT","decision_id":"same"}
        one=m.route_shadow(d,self.capture); two=m.route_shadow(d,self.capture)
        self.assertEqual(one["intent"]["intent_sha256"],two["intent"]["intent_sha256"])
        self.assertEqual(one["ledger"]["ledger_sha256"],two["ledger"]["ledger_sha256"])

    def test_shadow_live_provider_injection_is_backward_compatible_surface(self):
        shadow_path=MODULE.parent/"shadow_live.py"
        sspec=importlib.util.spec_from_file_location("order069_shadow_live",shadow_path)
        sm=importlib.util.module_from_spec(sspec); sys.modules[sspec.name]=sm; sspec.loader.exec_module(sm)
        class Provider:
            def shadow_live_book(self,symbol):
                return {"bid":1.0,"ask":2.0,"mid":1.5,"spread_bps":1.0,"depth_usd":10.0,"fetch_latency_ms":0,"ts":"x"}
        obj=sm.ShadowLive(config={"fetch_real_book":True,"output_path":"/tmp/order069-shadow.jsonl","report_path":"/tmp/order069-shadow-report.json"},book_provider=Provider())
        got=asyncio.run(obj._fetch_real_book("BTC/USDT:USDT"))
        self.assertEqual(got["mid"],1.5)
        self.assertIsNotNone(obj.book_provider)

    def test_capture_rejects_wrong_symbol(self):
        c=copy.deepcopy(self.capture); c["symbol"]="ETHUSDT"
        with self.assertRaises(m.ShadowDataError): m.validate_capture(c)


if __name__ == "__main__": unittest.main()
