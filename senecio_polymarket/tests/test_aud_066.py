import unittest
from backend.research.aud066_liquidation import timestamp_gate,normalize_liquidation,feature_at,make_samples,canonical_hash

class Aud066PointInTimeTests(unittest.TestCase):
    def test_unknown_timestamp_fails_closed(self):
        self.assertIsNone(timestamp_gate({'timestamp':'1'}))
        self.assertIsNone(timestamp_gate({'local_timestamp':'1'}))

    def test_negative_or_large_delivery_lag_fails_closed(self):
        self.assertIsNone(timestamp_gate({'timestamp':'2000000','local_timestamp':'1000000'}))
        self.assertIsNone(timestamp_gate({'timestamp':'1000000','local_timestamp':'40000001'}))

    def test_liquidation_side_semantics_and_notional(self):
        x=normalize_liquidation({'symbol':'BTCUSDT','timestamp':'1000000','local_timestamp':'1001000','side':'sell','price':'50000','amount':'2'})
        self.assertEqual(x['liquidated_side'],'LONG'); self.assertEqual(x['notional_usd'],100000)
        y=normalize_liquidation({'symbol':'BTCUSDT','timestamp':'1000000','local_timestamp':'1001000','side':'buy','price':'50000','amount':'1'})
        self.assertEqual(y['liquidated_side'],'SHORT')

    def _mins(self):
        out={}
        for m in range(80,110):
            px=100+(m-80)*0.1
            out[m]={'open':px,'close':px,'high':px*1.001,'low':px*.999,'volume':10+m%3,'buy_volume':6,'sell_volume':4,
                    'quote':(m*60_000_000+1,px-.05,px+.05,5,4),
                    'ticker':(m*60_000_000+2,1000+m,0.0001)}
        return out

    def test_future_liquidation_never_enters_features(self):
        mins=self._mins(); t=(100+1)*60_000_000-1
        past={'exchange_ts_us':t-1_000_000,'known_at_us':t-900_000,'liquidated_side':'LONG','price':100,'amount':10,'notional_usd':1000}
        future={'exchange_ts_us':t-100_000,'known_at_us':t+1,'liquidated_side':'SHORT','price':100,'amount':999,'notional_usd':99900}
        a=feature_at(t,mins,[past]); b=feature_at(t,mins,[past,future])
        self.assertEqual(a,b)
        self.assertEqual(a['long_liq_usd_1m'],1000); self.assertEqual(a['short_liq_usd_1m'],0)

    def test_receipt_clock_not_exchange_clock_controls_eligibility(self):
        mins=self._mins(); t=(100+1)*60_000_000-1
        delayed={'exchange_ts_us':t-10_000,'known_at_us':t+10_000,'liquidated_side':'LONG','price':100,'amount':10,'notional_usd':1000}
        f=feature_at(t,mins,[delayed]); self.assertEqual(f['long_liq_usd_1m'],0)

    def test_label_is_strictly_future_and_not_in_feature_hash(self):
        mins=self._mins(); samples,_=make_samples('fixture',mins,[])
        self.assertTrue(samples)
        for r in samples:
            self.assertGreater(r['label_ts_min_us'],r['decision_ts_us'])
        r=samples[0]; h=canonical_hash(r['features']); r['y']=1-r['y']; self.assertEqual(h,canonical_hash(r['features']))

    def test_estimated_clusters_not_present_in_realized_feature_builder(self):
        mins=self._mins(); t=(100+1)*60_000_000-1; f=feature_at(t,mins,[])
        self.assertTrue(f)
        self.assertFalse(any('cluster' in k for k in f))

if __name__=='__main__': unittest.main()
