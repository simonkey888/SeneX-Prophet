"""AUD-066 isolated liquidation-pressure research primitives.

No production imports, writes, credentials, wallet, signer, order path or runtime hooks.
All feature eligibility is based on local_timestamp (receipt time), never future labels.
"""
from __future__ import annotations

import hashlib, json, math, random
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

MAX_DELIVERY_LAG_US = 30_000_000
EPS = 1e-12

BASE_FEATURES = (
    "price_momentum_1m", "price_momentum_5m", "volume_delta_1m",
    "taker_imbalance_1m", "bidask_imbalance", "funding_rate",
    "oi_delta_5m", "spread_pct", "volatility_5m",
)
REALIZED_FEATURES = (
    "long_liq_usd_30s", "short_liq_usd_30s",
    "long_liq_usd_1m", "short_liq_usd_1m",
    "long_liq_usd_5m", "short_liq_usd_5m",
    "net_forced_flow_1m", "net_forced_flow_5m",
    "liq_imbalance_1m", "liq_imbalance_5m",
    "liq_acceleration", "liq_burst_zscore",
)
NORMALIZED_FEATURES = (
    "liq_to_volume_ratio", "liq_to_oi_ratio",
    "depth_normalized_liq_pressure", "spread_normalized_liq_pressure",
    "oi_delta_1m", "oi_delta_5m",
)


def _f(v, default=None):
    try:
        x=float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _i(v):
    try: return int(v)
    except (TypeError, ValueError): return None


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def timestamp_gate(row: dict) -> tuple[int,int] | None:
    """Return (exchange_ts_us, known_at_us) or fail closed.

    Tardis normalized CSV guarantees UTC microseconds. local_timestamp is the
    collector arrival timestamp and is therefore the causal availability clock.
    """
    exch=_i(row.get("timestamp")); local=_i(row.get("local_timestamp"))
    if exch is None or local is None or exch <= 0 or local <= 0: return None
    lag=local-exch
    if lag < 0 or lag > MAX_DELIVERY_LAG_US: return None
    return exch,local


def normalize_liquidation(row: dict) -> dict | None:
    gate=timestamp_gate(row)
    if gate is None or str(row.get("symbol") or "").upper()!="BTCUSDT": return None
    price=_f(row.get("price")); amount=_f(row.get("amount")); side=str(row.get("side") or "").lower()
    if price is None or amount is None or price <= 0 or amount <= 0 or side not in {"buy","sell"}: return None
    # Tardis liquidation semantics: buy = short position liquidated; sell = long position liquidated.
    liquidated_side="SHORT" if side=="buy" else "LONG"
    return {"exchange_ts_us":gate[0],"known_at_us":gate[1],"liquidated_side":liquidated_side,
            "price":price,"amount":amount,"notional_usd":price*amount}


def build_minute_market(trades: Iterable[dict], quotes: Iterable[dict], tickers: Iterable[dict]) -> dict[int,dict]:
    """Build receipt-time one-minute market state from normalized Tardis rows."""
    mins=defaultdict(lambda:{"volume":0.0,"buy_volume":0.0,"sell_volume":0.0,"prices":[],"last_known_at":0})
    for r in trades:
        gate=timestamp_gate(r)
        if gate is None or str(r.get("symbol") or "").upper()!="BTCUSDT": continue
        p=_f(r.get("price")); a=_f(r.get("amount")); side=str(r.get("side") or "").lower()
        if p is None or a is None or p<=0 or a<=0: continue
        m=gate[1]//60_000_000; x=mins[m]; x["prices"].append((gate[1],p)); x["volume"]+=a
        if side=="buy": x["buy_volume"]+=a
        elif side=="sell": x["sell_volume"]+=a
        x["last_known_at"]=max(x["last_known_at"],gate[1])
    for r in quotes:
        gate=timestamp_gate(r)
        if gate is None or str(r.get("symbol") or "").upper()!="BTCUSDT": continue
        bp=_f(r.get("bid_price")); ap=_f(r.get("ask_price")); ba=_f(r.get("bid_amount")); aa=_f(r.get("ask_amount"))
        if None in (bp,ap,ba,aa) or bp<=0 or ap<=0 or ba<0 or aa<0 or ap<bp: continue
        m=gate[1]//60_000_000; x=mins[m]
        prev=x.get("quote")
        if prev is None or gate[1]>=prev[0]: x["quote"]=(gate[1],bp,ap,ba,aa)
    for r in tickers:
        gate=timestamp_gate(r)
        if gate is None or str(r.get("symbol") or "").upper()!="BTCUSDT": continue
        oi=_f(r.get("open_interest")); funding=_f(r.get("funding_rate"))
        m=gate[1]//60_000_000; x=mins[m]; prev=x.get("ticker")
        if prev is None or gate[1]>=prev[0]: x["ticker"]=(gate[1],oi,funding)
    for m,x in mins.items():
        if x["prices"]:
            ps=sorted(x["prices"]); x["open"]=ps[0][1]; x["close"]=ps[-1][1]
            x["high"]=max(v for _,v in ps); x["low"]=min(v for _,v in ps)
        x.pop("prices",None)
    return dict(mins)


def _last_state(mins:dict[int,dict], m:int, key:str, lookback:int=10):
    for k in range(m,m-lookback-1,-1):
        v=mins.get(k,{}).get(key)
        if v is not None: return v
    return None


def _close(mins,m):
    v=mins.get(m,{}).get("close")
    return _f(v)


def _sum_liq(liqs, t_us, window_us, side=None):
    lo=t_us-window_us
    return sum(e["notional_usd"] for e in liqs if lo < e["known_at_us"] <= t_us and (side is None or e["liquidated_side"]==side))


def feature_at(t_us:int, mins:dict[int,dict], liqs:list[dict]) -> dict | None:
    """Build features using only rows known at or before t_us."""
    m=t_us//60_000_000
    c0=_close(mins,m); c1=_close(mins,m-1); c5=_close(mins,m-5)
    if None in (c0,c1,c5) or min(c0,c1,c5)<=0: return None
    v0=_f(mins.get(m,{}).get("volume"),0.0); v1=_f(mins.get(m-1,{}).get("volume"),0.0)
    q=_last_state(mins,m,"quote",3); tk=_last_state(mins,m,"ticker",10); tk1=_last_state(mins,m-1,"ticker",10); tk5=_last_state(mins,m-5,"ticker",10)
    if q is None or tk is None: return None
    _,bp,ap,ba,aa=q; mid=(bp+ap)/2; spread=(ap-bp)/mid if mid>0 else None
    if spread is None: return None
    depth_usd=(ba+aa)*mid; bidask=(ba-aa)/(ba+aa) if ba+aa>0 else 0.0
    buy=_f(mins.get(m,{}).get("buy_volume"),0); sell=_f(mins.get(m,{}).get("sell_volume"),0)
    taker=(buy-sell)/(buy+sell) if buy+sell>0 else 0.0
    oi=_f(tk[1]); oi1=_f(tk1[1]) if tk1 else None; oi5=_f(tk5[1]) if tk5 else None; funding=_f(tk[2],0.0)
    oi_d1=(oi-oi1)/oi1 if oi is not None and oi1 not in (None,0) else 0.0
    oi_d5=(oi-oi5)/oi5 if oi is not None and oi5 not in (None,0) else 0.0
    l30=_sum_liq(liqs,t_us,30_000_000,"LONG"); s30=_sum_liq(liqs,t_us,30_000_000,"SHORT")
    l1=_sum_liq(liqs,t_us,60_000_000,"LONG"); s1=_sum_liq(liqs,t_us,60_000_000,"SHORT")
    l5=_sum_liq(liqs,t_us,300_000_000,"LONG"); s5=_sum_liq(liqs,t_us,300_000_000,"SHORT")
    total1=l1+s1; total5=l5+s5; net1=s1-l1; net5=s5-l5
    prev1=_sum_liq(liqs,t_us-60_000_000,60_000_000)
    hist=[]
    for j in range(1,61): hist.append(_sum_liq(liqs,t_us-j*60_000_000,60_000_000))
    mu=sum(hist)/len(hist); sd=(sum((x-mu)**2 for x in hist)/len(hist))**0.5
    z=(total1-mu)/sd if sd>0 else 0.0
    # volume is BTC; convert current 5m volume to USD at mid
    vol5=sum(_f(mins.get(m-j,{}).get("volume"),0.0) for j in range(5))*mid
    oi_usd=(oi or 0.0)*mid
    vals={
      "price_momentum_1m":(c0-c1)/c1,"price_momentum_5m":(c0-c5)/c5,
      "volume_delta_1m":(v0-v1)/v1 if v1>0 else 0.0,"taker_imbalance_1m":taker,
      "bidask_imbalance":bidask,"funding_rate":funding,"oi_delta_5m":oi_d5,"spread_pct":spread,
      "volatility_5m":max((_f(mins.get(m-j,{}).get("high"),c0)-_f(mins.get(m-j,{}).get("low"),c0))/max(_f(mins.get(m-j,{}).get("close"),c0),EPS) for j in range(5)),
      "long_liq_usd_30s":l30,"short_liq_usd_30s":s30,"long_liq_usd_1m":l1,"short_liq_usd_1m":s1,
      "long_liq_usd_5m":l5,"short_liq_usd_5m":s5,"net_forced_flow_1m":net1,"net_forced_flow_5m":net5,
      "liq_imbalance_1m":net1/max(total1,EPS),"liq_imbalance_5m":net5/max(total5,EPS),
      "liq_acceleration":total1-prev1,"liq_burst_zscore":z,
      "liq_to_volume_ratio":total5/max(vol5,EPS),"liq_to_oi_ratio":total5/max(oi_usd,EPS),
      "depth_normalized_liq_pressure":net1/max(depth_usd,EPS),
      "spread_normalized_liq_pressure":net1/max(spread*mid,EPS),"oi_delta_1m":oi_d1,
    }
    return vals


def make_samples(day:str, mins:dict[int,dict], liqs:list[dict]) -> tuple[list[dict],dict]:
    """Decision grid every 5m; label strictly after t from receipt-time closes."""
    if not mins: return [],{"NO_MARKET_MINUTES":1}
    lo=min(mins); hi=max(mins); out=[]; excluded=defaultdict(int)
    start=((lo+14)//5)*5
    for m in range(start,hi-5,5):
        t=(m+1)*60_000_000-1  # end of minute m, all features <= t
        f=feature_at(t,mins,liqs)
        p0=_close(mins,m); p5=_close(mins,m+5)
        if f is None: excluded["MISSING_CAUSAL_CONTEXT"]+=1; continue
        if p0 is None or p5 is None or p0<=0: excluded["MISSING_FUTURE_LABEL_PRICE"]+=1; continue
        y=1 if p5>p0 else 0
        out.append({"day":day,"decision_ts_us":t,"label_ts_min_us":(m+5)*60_000_000,"y":y,"features":f})
    return out,dict(excluded)


def _standardize(train, names):
    mus={}; sds={}
    for n in names:
        xs=[math.asinh(_f(r["features"].get(n),0.0)) for r in train]
        mu=sum(xs)/len(xs); sd=(sum((x-mu)**2 for x in xs)/len(xs))**0.5
        mus[n]=mu; sds[n]=sd if sd>1e-9 else 1.0
    return mus,sds


def _vec(r,names,mus,sds): return [1.0]+[(math.asinh(_f(r["features"].get(n),0.0))-mus[n])/sds[n] for n in names]

def _sig(z):
    z=max(-35.0,min(35.0,z)); return 1/(1+math.exp(-z))


def fit_logit(train,names,steps=500,lr=0.03,l2=0.02):
    mus,sds=_standardize(train,names); w=[0.0]*(len(names)+1); n=max(1,len(train))
    for _ in range(steps):
        g=[0.0]*len(w)
        for r in train:
            x=_vec(r,names,mus,sds); p=_sig(sum(a*b for a,b in zip(w,x))); e=p-r["y"]
            for j,v in enumerate(x): g[j]+=e*v
        for j in range(len(w)):
            reg=0 if j==0 else l2*w[j]; w[j]-=lr*(g[j]/n+reg)
    return {"w":w,"mus":mus,"sds":sds,"names":list(names)}


def predict(model,rows):
    return [_sig(sum(a*b for a,b in zip(model["w"],_vec(r,model["names"],model["mus"],model["sds"])))) for r in rows]


def metrics(y,p):
    n=len(y)
    if n==0: return {"n":0}
    p=[max(1e-9,min(1-1e-9,float(x))) for x in p]
    brier=sum((a-b)**2 for a,b in zip(p,y))/n
    logloss=-sum(t*math.log(q)+(1-t)*math.log(1-q) for t,q in zip(y,p))/n
    pred=[1 if q>=.5 else 0 for q in p]; acc=sum(a==b for a,b in zip(pred,y))/n
    pos=sum(y); neg=n-pos; tpr=sum(a==b==1 for a,b in zip(pred,y))/pos if pos else 0; tnr=sum(a==b==0 for a,b in zip(pred,y))/neg if neg else 0
    ece=0.0
    for k in range(10):
        ids=[i for i,q in enumerate(p) if (k/10)<=q<((k+1)/10) or (k==9 and q==1)]
        if ids: ece+=len(ids)/n*abs(sum(p[i] for i in ids)/len(ids)-sum(y[i] for i in ids)/len(ids))
    return {"n":n,"brier":brier,"log_loss":logloss,"ece_10":ece,"accuracy":acc,"balanced_accuracy":(tpr+tnr)/2}


def walk_forward(samples:list[dict]):
    days=sorted({r["day"] for r in samples})
    if len(days)<7: return {"status":"INCONCLUSIVE","reason":"INSUFFICIENT_INDEPENDENT_DAYS","days":days}
    arms={"A":BASE_FEATURES,"B":BASE_FEATURES+REALIZED_FEATURES,"C":BASE_FEATURES+REALIZED_FEATURES+NORMALIZED_FEATURES}
    # Three chronological test blocks, with immediately prior day as validation; all earlier days train.
    test_days=days[-3:]; results=[]
    for td in test_days:
        i=days.index(td); vd=days[i-1]; train_days=days[:i-1]
        tr=[r for r in samples if r["day"] in train_days]; va=[r for r in samples if r["day"]==vd]; te=[r for r in samples if r["day"]==td]
        if min(len(tr),len(va),len(te))<30: continue
        fitted={a:fit_logit(tr,nms) for a,nms in arms.items()}
        vm={a:metrics([r["y"] for r in va],predict(m,va)) for a,m in fitted.items()}
        # Frozen before terminal test: choose lowest validation Brier, tie by simpler arm.
        chosen=min(arms,key=lambda a:(vm[a]["brier"],len(arms[a])))
        tm={a:metrics([r["y"] for r in te],predict(m,te)) for a,m in fitted.items()}
        results.append({"train_days":train_days,"validation_day":vd,"test_day":td,"validation":vm,"test":tm,"chosen_E":chosen})
    if len(results)<2: return {"status":"INCONCLUSIVE","reason":"INSUFFICIENT_VALID_WALK_FORWARD_BLOCKS","days":days,"blocks":results}
    def agg(arm,key):
        vals=[b["test"][arm][key] for b in results]; return sum(vals)/len(vals)
    summary={a:{k:agg(a,k) for k in ("brier","log_loss","ece_10","accuracy","balanced_accuracy")} for a in arms}
    # net-new realized value requires both proper scores improve over A in >=2 independent blocks and aggregate.
    betterB=sum(b["test"]["B"]["brier"]<b["test"]["A"]["brier"] and b["test"]["B"]["log_loss"]<b["test"]["A"]["log_loss"] for b in results)
    betterC=sum(b["test"]["C"]["brier"]<b["test"]["A"]["brier"] and b["test"]["C"]["log_loss"]<b["test"]["A"]["log_loss"] for b in results)
    best=min(("B","C"),key=lambda a:summary[a]["brier"])
    candidate_yes=(max(betterB,betterC)>=2 and summary[best]["brier"]<summary["A"]["brier"] and summary[best]["log_loss"]<summary["A"]["log_loss"])
    return {"status":"COMPLETE","days":days,"blocks":results,"summary":summary,"better_blocks":{"B":betterB,"C":betterC},"best_realized_arm":best,"realized_value":"YES" if candidate_yes else "NO"}
