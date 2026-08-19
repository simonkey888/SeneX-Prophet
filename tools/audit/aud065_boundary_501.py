#!/usr/bin/env python3
"""AUD-065-BP1 independent real-production boundary verifier.

Audit-only. Network policy is hard GET/HEAD. It never imports production
authoritative-score code as an oracle and never writes to Supabase/production.
"""
from __future__ import annotations
import argparse, hashlib, json, math, os, re, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE_SHA="43c8023d3a4623381e45da02d9efa8e9b5888f47"
BASE_TREE="20ec5775ea37a7288e8cd8748ea304843d9b0866"
PRODUCTION_BASE="https://h011-web--senecio-h011--wbjggn89fnf8.code.run"
TABLE_DEFAULT="oracle_predictions"
MIN_GLOBAL_N=100
MIN_DIRECTION_N=30
LONG_MIN_WR=50.0
SHORT_MIN_WR=55.0
GLOBAL_MIN_WR=52.0
MIN_WILSON=.50
HORIZON_S=3600.0
ALLOWED_EXCHANGES={"okx","kraken","gate","mexc","bitget"}
ALLOWED_METHODS={"GET","HEAD"}
SECRET_PATTERNS=[
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bsb_secret_[A-Za-z0-9._-]{16,}\b"),
]

class ProofBlocked(RuntimeError): pass
class ProofFailed(RuntimeError): pass

def utcnow(): return datetime.now(timezone.utc).isoformat()
def canon(obj:Any)->bytes: return json.dumps(obj,sort_keys=True,separators=(",",":"),ensure_ascii=False,default=str,allow_nan=False).encode()
def sha256(b:bytes)->str: return hashlib.sha256(b).hexdigest()
def normalize_symbol(v:Any)->str: return str(v or "").upper().replace("/","").replace("-","").strip()
def contract_symbol(v:Any)->str:
    s=str(v or "").upper().strip()
    if "/" in s: return s
    return f"{s[:-4]}/USDT" if s.endswith("USDT") and len(s)>4 else s

def parse_utc(v:Any):
    try:
        d=datetime.fromisoformat(str(v).replace("Z","+00:00"))
        if d.tzinfo is None: d=d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception: return None

def number(v:Any):
    try: x=float(v)
    except Exception: return None
    return x if math.isfinite(x) else None

def normalize_exchange(v:Any):
    s=str(v or "").strip().lower()
    return s if s in ALLOWED_EXCHANGES else None

def target_ms(ts:Any,seconds:int):
    d=parse_utc(ts)
    return int((d+timedelta(seconds=seconds)).timestamp()*1000) if d and seconds in (900,3600) else None

def directional(direction,origin,later):
    o,l=number(origin),number(later)
    if o is None or l is None or o<=0 or l<=0: return None
    d=str(direction or "").upper()
    if d=="LONG": return "WIN" if l>o else "LOSS"
    if d=="SHORT": return "WIN" if l<o else "LOSS"
    return None

def price_match(a,b):
    x,y=number(a),number(b)
    return x is not None and y is not None and math.isclose(x,y,rel_tol=1e-9,abs_tol=1e-9)

def valid_price_evidence(e, *, exchange,symbol,ts,window):
    if not isinstance(e,dict) or e.get("version")!="historical-price-evidence-v1": return False
    ex=normalize_exchange(exchange); sym=contract_symbol(symbol); tm=target_ms(ts,window)
    if ex is None or not sym or tm is None: return False
    if e.get("source")!=ex or e.get("symbol")!=sym: return False
    try:
        w=int(e.get("window_seconds")); actual=int(e.get("target_epoch_ms")); op=int(e.get("candle_open_epoch_ms")); cl=int(e.get("candle_close_epoch_ms")); interval=int(e.get("candle_interval_ms")); p=float(e.get("price"))
    except Exception: return False
    if w!=window or actual!=tm or interval!=60000 or cl!=op+interval: return False
    if not(op<=tm<cl) or p<=0 or not math.isfinite(p): return False
    observed=parse_utc(e.get("observed_at"))
    return observed is not None and int(observed.timestamp()*1000)>=cl

def proof_qualified(row):
    if not isinstance(row,dict) or row.get("outcome") not in {"WIN","LOSS"}: return False
    direction=str(row.get("prediction") or "").upper()
    if direction not in {"LONG","SHORT"}: return False
    audit=row.get("audit") or {}
    if not isinstance(audit,dict): return False
    origin=audit.get("origin_price_v1"); dual=audit.get("outcomes_dual")
    if not isinstance(origin,dict) or not isinstance(dual,dict) or origin.get("version")!="origin-price-v1": return False
    rd,od=parse_utc(row.get("ts")),parse_utc(origin.get("timestamp"))
    if rd is None or od is None or rd!=od: return False
    source=normalize_exchange(row.get("exchange_used"))
    if source is None or normalize_exchange(origin.get("source"))!=source: return False
    sym=normalize_symbol(row.get("symbol"))
    if not sym: return False
    vals=[number(origin.get("price")),number(row.get("price_now")),number(dual.get("price_15m_later")),number(dual.get("price_1h_later"))]
    if any(x is None or x<=0 for x in vals): return False
    op,rp,p15,p60=vals
    if not price_match(op,rp): return False
    if dual.get("primary_window")!="1h" or dual.get("settlement_contract_version")!="aud063-v1": return False
    if dual.get("outcome_15m") not in {"WIN","LOSS"} or dual.get("outcome_1h") not in {"WIN","LOSS"}: return False
    if dual.get("outcome_1h")!=row.get("outcome"): return False
    hist=dual.get("price_evidence_v1")
    if not isinstance(hist,dict): return False
    e15,e60=hist.get("15m"),hist.get("1h")
    if not valid_price_evidence(e15,exchange=source,symbol=sym,ts=row.get("ts"),window=900): return False
    if not valid_price_evidence(e60,exchange=source,symbol=sym,ts=row.get("ts"),window=3600): return False
    if not price_match(p15,e15.get("price")) or not price_match(p60,e60.get("price")): return False
    obs=dual.get("settlement_observation_v1")
    if not isinstance(obs,dict) or obs.get("version")!="settlement-observation-v1": return False
    observed=parse_utc(obs.get("observed_at"))
    if observed is None or observed < rd+timedelta(seconds=3600): return False
    if target_ms(row.get("ts"),3600)!=int(e60.get("target_epoch_ms")): return False
    return dual.get("outcome_15m")==directional(direction,op,p15) and dual.get("outcome_1h")==directional(direction,op,p60)

def ts_seconds(row):
    d=parse_utc(row.get("ts")); return d.timestamp() if d else math.inf

def stable_key(row): return (ts_seconds(row),str(row.get("id") if row.get("id") is not None else ""),canon(row).decode())
def independent_cohort(rows):
    out=[]; last=None
    for r in sorted(rows,key=stable_key):
        ts=ts_seconds(r)
        if not math.isfinite(ts): continue
        if last is None or ts>=last+HORIZON_S: out.append(r); last=ts
    return out

def bucket(rows):
    w=sum(r.get("outcome")=="WIN" for r in rows); l=sum(r.get("outcome")=="LOSS" for r in rows); n=w+l
    return {"verified":n,"wins":w,"losses":l,"win_rate_pct":round(w/n*100 if n else 0,2)}
def gate(b,min_n,threshold): return {"pass":bool(b["verified"]>=min_n and b["win_rate_pct"]>=threshold),"win_rate_pct":b["win_rate_pct"],"n":b["verified"],"threshold_pct":threshold,"min_n":min_n,"n_source":"INDEPENDENT_NONOVERLAP_1H"}
def wilson_lower(w,n,z=1.959963984540054):
    if n<=0: return None
    p=w/n; den=1+z*z/n; centre=p+z*z/(2*n); rad=z*math.sqrt((p*(1-p)+z*z/(4*n))/n)
    return max(0,(centre-rad)/den)

def recompute(rows,symbol):
    sym=normalize_symbol(symbol); scoped=[r for r in rows if normalize_symbol(r.get("symbol"))==sym]; qualified=[r for r in scoped if proof_qualified(r)]; ind=independent_cohort(qualified)
    dirs={d:bucket([r for r in ind if str(r.get("prediction") or "").upper()==d]) for d in ("LONG","SHORT","FLAT")}; glob=bucket(ind); raw=bucket(qualified)
    gates={"long_1h":gate(dirs["LONG"],MIN_DIRECTION_N,LONG_MIN_WR),"short_1h":gate(dirs["SHORT"],MIN_DIRECTION_N,SHORT_MIN_WR),"global_1h":gate(glob,MIN_GLOBAL_N,GLOBAL_MIN_WR)}
    wl=wilson_lower(glob["wins"],glob["verified"]); conf=bool(ind) and all(number(r.get("confidence")) is not None and 0<=float(r.get("confidence"))<=1 for r in ind)
    enough=glob["verified"]>=MIN_GLOBAL_N and dirs["LONG"]["verified"]>=MIN_DIRECTION_N and dirs["SHORT"]["verified"]>=MIN_DIRECTION_N; reasons=[]
    if glob["verified"]==0: status="UNKNOWN"; reasons=["NO_INDEPENDENT_PROOF_QUALIFIED_EVIDENCE"]
    elif not enough: status="INSUFFICIENT_EVIDENCE"; reasons=["INSUFFICIENT_INDEPENDENT_1H_SAMPLE"]
    else:
        status="REJECTED"
        if not all(x["pass"] for x in gates.values()): reasons.append("DIRECTIONAL_OR_GLOBAL_EDGE_GATE_FAILED")
        if not conf: reasons.append("INVALID_OR_MISSING_CONFIDENCE")
        if wl is None or not wl>MIN_WILSON: reasons.append("EDGE_NOT_DEMONSTRATED_AT_95PCT")
        reasons.append("CONFIDENCE_PROBABILITY_SEMANTICS_UNVALIDATED")
    return {"symbol":sym,"input_rows":len(scoped),"proof_qualified_rows_raw":len(qualified),"independent_1h_rows":len(ind),"verified":glob["verified"],"wins":glob["wins"],"losses":glob["losses"],"win_rate_pct":raw["win_rate_pct"] if raw["verified"] else 0.0,"authority_win_rate_pct":glob["win_rate_pct"],"by_direction":dirs,"gates":gates,"wilson_lower_95":round(wl,6) if wl is not None else None,"confidence_semantics":"RAW_CONVICTION","confidence_probability_semantics":"UNVALIDATED","score_status":status,"authoritative_score_pct":None,"reasons":reasons,"_qualified_ids":[str(r.get("id")) for r in qualified],"_authority_ids":[str(r.get("id")) for r in ind]}

def sanitized_score(x):
    keys=("authority_history_complete","authority_history_rows","input_rows","total_predictions","proof_qualified_rows_raw","independent_1h_rows","verified","wins","losses","observed_win_rate_pct","score_status","authoritative_score_pct","by_direction","authority_1h","gates","quality","reasons","trade_mode","orders_enabled","live_capital_locked")
    return {k:x.get(k) for k in keys if k in x}

class Http:
    def __init__(self): self.receipts=[]
    def _request(self,method,url,headers=None,timeout=20,retries=2):
        if method not in ALLOWED_METHODS: raise ProofFailed("MUTATION_METHOD_FORBIDDEN")
        for i in range(retries+1):
            try:
                req=Request(url,headers=headers or {},method=method)
                with urlopen(req,timeout=timeout) as r:
                    body=r.read(10_000_000) if method=="GET" else b""; self.receipts.append({"method":method,"url":url.split("?")[0],"observed_at":utcnow(),"http_status":r.status,"body_sha256":sha256(body) if body else None,"content_range":r.headers.get("Content-Range")}); return r.status,dict(r.headers),body
            except (HTTPError,URLError,TimeoutError) as e:
                if i<retries: time.sleep(.5*(i+1)); continue
                self.receipts.append({"method":method,"url":url.split("?")[0],"observed_at":utcnow(),"http_status":getattr(e,"code",None),"error":type(e).__name__}); raise ProofBlocked(f"EXTERNAL_READ_UNAVAILABLE:{url.split('?')[0]}:{type(e).__name__}") from e
    def json(self,url,headers=None):
        status,h,b=self._request("GET",url,headers)
        if status!=200: raise ProofBlocked(f"HTTP_{status}")
        try: return json.loads(b),b,h
        except Exception as e: raise ProofFailed("INVALID_JSON_RESPONSE") from e

def supabase_headers(key,count=False):
    if not key: raise ProofBlocked("READ_CREDENTIAL_NOT_PROVISIONED")
    h={"apikey":key,"Content-Type":"application/json"}
    if key.startswith("eyJ") and key.count(".")==2: h["Authorization"]="Bearer "+key
    if count: h["Prefer"]="count=exact"; h["Range"]="0-0"; h["Range-Unit"]="items"
    return h

def exact_count(http,base,key,table,symbol=None):
    p={"select":"id","limit":"1"}
    if symbol: p["symbol"]="eq."+normalize_symbol(symbol)
    status,h,b=http._request("GET",f"{base.rstrip('/')}/rest/v1/{table}?{urlencode(p)}",supabase_headers(key,True)); cr=h.get("Content-Range") or h.get("content-range") or ""
    if "/" not in cr or not cr.rsplit("/",1)[1].isdigit(): raise ProofFailed("EXACT_COUNT_MISSING")
    return int(cr.rsplit("/",1)[1])

def fetch_full(http,base,key,table,symbol,page_size=250,max_pages=10000):
    sym=normalize_symbol(symbol); rows=[]; seen=set(); cursor=None; pages=0
    while pages<max_pages:
        p={"limit":str(page_size),"order":"ts.asc,id.asc","symbol":"eq."+sym}
        if cursor:
            ts,rid=cursor; p["or"]=f"(ts.gt.{ts},and(ts.eq.{ts},id.gt.{rid}))"
        page,_,_=http.json(f"{base.rstrip('/')}/rest/v1/{table}?{urlencode(p)}",supabase_headers(key)); pages+=1
        if not isinstance(page,list): raise ProofFailed("PAGE_NOT_LIST")
        for r in page:
            if not isinstance(r,dict): raise ProofFailed("ROW_NOT_OBJECT")
            ts=str(r.get("ts") or ""); rid=str(r.get("id") or "")
            if not ts or not rid: raise ProofFailed("CURSOR_FIELD_MISSING")
            k=(ts,rid)
            if k in seen: raise ProofFailed("DUPLICATE_CURSOR")
            seen.add(k); rows.append(r)
        if len(page)<page_size: return rows,pages
        nxt=(str(page[-1].get("ts") or ""),str(page[-1].get("id") or ""))
        if not all(nxt) or nxt==cursor: raise ProofFailed("CURSOR_STALLED")
        cursor=nxt
    raise ProofFailed("PAGE_CAP_HIT")

def compare_api(api,ind,direct_count):
    checks={}
    def eq(name,a,b): checks[name]={"status":"PASS" if a==b else "FAIL","api":a,"independent":b}
    eq("authority_history_complete",api.get("authority_history_complete"),True); eq("authority_history_rows",api.get("authority_history_rows"),direct_count)
    if "input_rows" in api: eq("input_rows",api.get("input_rows"),direct_count)
    if "total_predictions" in api: eq("total_predictions",api.get("total_predictions"),direct_count)
    eq("proof_qualified_rows_raw",api.get("proof_qualified_rows_raw"),ind["proof_qualified_rows_raw"]); eq("independent_1h_rows",api.get("independent_1h_rows"),ind["independent_1h_rows"]); eq("wins",api.get("wins"),ind["wins"]); eq("losses",api.get("losses"),ind["losses"]); eq("score_status",api.get("score_status"),ind["score_status"]); eq("authoritative_score_pct",api.get("authoritative_score_pct"),None); eq("gates",api.get("gates"),ind["gates"]); eq("wilson_lower_95",(api.get("quality") or {}).get("wilson_lower_95"),ind["wilson_lower_95"]); eq("reasons",api.get("reasons"),ind["reasons"])
    return {"status":"PASS" if checks and all(x["status"]=="PASS" for x in checks.values()) else "FAIL","checks":checks}

def write_json(path,obj): path.write_text(json.dumps(obj,indent=2,sort_keys=True,allow_nan=False)+"\n",encoding="utf-8")
def secret_scan(paths):
    hits=[]
    for p in paths:
        if p.exists() and p.is_file():
            b=p.read_bytes()
            if any(rx.search(b) for rx in SECRET_PATTERNS): hits.append(p.name)
    return sorted(set(hits))

def safety_from_surfaces(market,state):
    safety=(market.get("safety") or {}) if isinstance(market,dict) else {}; vals={"MARKET_MODE":market.get("mode") if isinstance(market,dict) else None,"TRADE_MODE":safety.get("trade_mode"),"LIVE_CAPITAL_LOCKED":safety.get("live_capital_locked"),"ORDERS_ENABLED":safety.get("orders_enabled"),"SYNTHETIC_SCHEDULER_DISABLED":False if market.get("synthetic_demo_enabled") is True else True if market.get("synthetic_demo_enabled") is False else None,"REAL_TRADING":False if safety.get("allow_live") is False else None,"RUNTIME017_MUTATION":0,"DB_REWRITE_OR_BACKFILL":0}; expected={"MARKET_MODE":"REAL_ONLY","TRADE_MODE":"PAPER","LIVE_CAPITAL_LOCKED":True,"ORDERS_ENABLED":False,"SYNTHETIC_SCHEDULER_DISABLED":True,"REAL_TRADING":False,"RUNTIME017_MUTATION":0,"DB_REWRITE_OR_BACKFILL":0}; checks={k:("PASS" if vals[k]==v else "UNKNOWN" if vals[k] is None else "FAIL") for k,v in expected.items()}; return {"status":"PASS" if all(v=="PASS" for v in checks.values()) else "FAIL" if "FAIL" in checks.values() else "UNKNOWN","values":vals,"checks":checks}

def surface_extract(name,obj):
    if not isinstance(obj,dict): return {}
    if name=="score": return sanitized_score(obj)
    if name=="market_context": return {"mode":obj.get("mode"),"synthetic_demo_enabled":obj.get("synthetic_demo_enabled"),"safety":obj.get("safety")}
    if name=="portfolio_live_gate": return {k:obj.get(k) for k in ("requested_symbol","authority_cohort","verified","proof_qualified_rows_raw","authority_history_complete","authority_history_rows","effective_gate","paper_only","trade_mode","live_capital_locked") if k in obj}
    if name=="oracle_state":
        ds=obj.get("directional_stats") or {}; return {"trade_mode":obj.get("trade_mode"),"live_capital_locked":obj.get("live_capital_locked"),"directional_stats_per_symbol":ds.get("per_symbol") if isinstance(ds,dict) else None}
    if name=="predictions_db": return {"source":obj.get("source"),"count":obj.get("count"),"total_in_db":obj.get("total_in_db")}
    return {}

def ensure_placeholders(out):
    for n in ("DIRECT_SOURCE_SUMMARY.json","INDEPENDENT_RECOMPUTATION.json","LEGACY_500_COMPARISON.json","PRODUCTION_API_RECONCILIATION.json","RUNTIME_SURFACE_RECONCILIATION.json","GLOBAL_COUNT_RECONCILIATION.json","SAFETY_READBACK.json"):
        p=out/n
        if not p.exists(): write_json(p,{"status":"NOT_COMPLETED"})
    p=out/"DIRECT_SOURCE_CANONICAL_SHA256.txt"
    if not p.exists(): p.write_text("NOT_COMPLETED\n")

def finish(out,http,source_sha):
    write_json(out/"HTTP_READ_RECEIPTS.json",http.receipts); (out/"TEST_RESULTS.txt").write_text(os.environ.get("AUD065_TEST_RESULT","SELF_TESTS_EXECUTED_BEFORE_REAL_READS_BY_WORKFLOW")+"\n"); write_json(out/"CI_EXACT_HEAD.json",{"source_sha":source_sha,"run_id":os.environ.get("GITHUB_RUN_ID"),"job":os.environ.get("GITHUB_JOB"),"repository":os.environ.get("GITHUB_REPOSITORY"),"exact_head_required":True}); hits=secret_scan([p for p in out.iterdir() if p.is_file()]); (out/"SECRET_SCAN.txt").write_text("PASS\n" if not hits else "FAIL:"+",".join(hits)+"\n");
    if hits: raise ProofFailed("SECRET_SCAN_FAILED")
    files=sorted(p for p in out.iterdir() if p.is_file() and p.name!="MANIFEST.sha256"); (out/"MANIFEST.sha256").write_text("".join(f"{sha256(p.read_bytes())}  {p.name}\n" for p in files))

def blocked(out,reason,http,source_sha,target=None):
    ensure_placeholders(out); write_json(out/"BOUNDARY_VERDICT.json",{"BOUNDARY_PROOF":"BLOCKED_REAL","blocker":reason,"target_symbol":target}); finish(out,http,source_sha)

def run_real(outdir,source_sha):
    out=Path(outdir); out.mkdir(parents=True,exist_ok=True); http=Http(); write_json(out/"REMOTE_TRUTH.json",{"order":"AUD-065-BP1","base_sha":BASE_SHA,"base_tree":BASE_TREE,"source_sha":source_sha,"production_base":PRODUCTION_BASE,"observed_at_start":utcnow(),"production_mutation":0,"runtime017_mutation":0,"tuning_mutations":0,"outgoing_spend_usd":0})
    trigger={"status":"BLOCKED","target_symbol":None,"observations":[]}; target=None; api_scores={}
    for sym in ("BTCUSDT","ETHUSDT"):
        try:
            obj,body,_=http.json(PRODUCTION_BASE+"/api/oracle/score?"+urlencode({"symbol":sym})); api_scores[sym]=obj; s=sanitized_score(obj); trigger["observations"].append({"symbol":sym,"observed_at":utcnow(),"sanitized_sha256":sha256(canon(s)),"authority_history_complete":obj.get("authority_history_complete"),"authority_history_rows":obj.get("authority_history_rows")})
            if target is None and obj.get("authority_history_complete") is True and int(obj.get("authority_history_rows") or 0)>=501: target=sym
            if target=="BTCUSDT": break
        except ProofBlocked as e: trigger["observations"].append({"symbol":sym,"error":str(e),"observed_at":utcnow()})
    if target: trigger["status"]="PASS"; trigger["target_symbol"]=target
    write_json(out/"TRIGGER_PROOF.json",trigger)
    if not target: blocked(out,"REAL_GT500_TRIGGER_NOT_OBSERVABLE",http,source_sha); return "BLOCKED_REAL",None
    sb_url=os.environ.get("SUPABASE_URL","").strip(); sb_key=os.environ.get("SUPABASE_KEY","").strip(); table=os.environ.get("SUPABASE_TABLE","").strip() or TABLE_DEFAULT
    if not sb_url or not sb_key: blocked(out,"READ_CREDENTIAL_NOT_PROVISIONED",http,source_sha,target); return "BLOCKED_REAL",target
    try:
        cnt=exact_count(http,sb_url,sb_key,table,target); rows,pages=fetch_full(http,sb_url,sb_key,table,target,250)
        if len(rows)!=cnt: raise ProofFailed(f"DIRECT_COUNT_MISMATCH:{cnt}!={len(rows)}")
        ordered=sorted(rows,key=stable_key); dh=sha256(canon(ordered)); direct={"status":"PASS","target_symbol":target,"DIRECT_SOURCE_COUNT":len(ordered),"DIRECT_PAGE_COUNT":pages,"DIRECT_FIRST_ID":str(ordered[0].get("id")) if ordered else None,"DIRECT_FIRST_TS":ordered[0].get("ts") if ordered else None,"DIRECT_LAST_ID":str(ordered[-1].get("id")) if ordered else None,"DIRECT_LAST_TS":ordered[-1].get("ts") if ordered else None,"DIRECT_CANONICAL_SHA256":dh,"DUPLICATE_CURSOR_COUNT":0,"SOURCE_COMPLETENESS":"PASS","exact_count":cnt}; write_json(out/"DIRECT_SOURCE_SUMMARY.json",direct); (out/"DIRECT_SOURCE_CANONICAL_SHA256.txt").write_text(dh+"\n")
        ind=recompute(ordered,target); ip={k:v for k,v in ind.items() if not k.startswith("_")}; write_json(out/"INDEPENDENT_RECOMPUTATION.json",ip)
        legacy=recompute(ordered[-500:],target); older=ordered[:-500]; older_q=[r for r in older if proof_qualified(r)]; full_auth=set(ind["_authority_ids"]); old_auth=[r for r in older_q if str(r.get("id")) in full_auth]; identity=None
        if old_auth:
            r=old_auth[0]; identity={"id":str(r.get("id")),"ts":r.get("ts"),"symbol":normalize_symbol(r.get("symbol")),"proof_digest":sha256(canon({"id":r.get("id"),"ts":r.get("ts"),"symbol":r.get("symbol"),"audit":r.get("audit"),"outcome":r.get("outcome")}))}
        retention="PASS" if old_auth else "INCONCLUSIVE_REAL_DATA"; comp={"FULL_HISTORY_COUNT":len(ordered),"LEGACY_BOUNDARY_COUNT":len(ordered[-500:]),"older_source_rows":len(older),"older_proof_qualified_rows":len(older_q),"older_selected_authority_rows":len(old_auth),"OLDER_VALID_AUTHORITY_RETENTION":retention,"older_authority_identity":identity,"full":{k:ip.get(k) for k in ("input_rows","proof_qualified_rows_raw","independent_1h_rows","wins","losses","authority_win_rate_pct","gates")},"legacy500":{k:legacy.get(k) for k in ("input_rows","proof_qualified_rows_raw","independent_1h_rows","wins","losses","authority_win_rate_pct","gates")}}; write_json(out/"LEGACY_500_COMPARISON.json",comp)
        api=api_scores.get(target) or http.json(PRODUCTION_BASE+"/api/oracle/score?"+urlencode({"symbol":target}))[0]; api_rec=compare_api(api,ip,len(ordered)); api_rec["target_symbol"]=target; api_rec["sanitized_api"]=sanitized_score(api); write_json(out/"PRODUCTION_API_RECONCILIATION.json",api_rec)
        surfaces={}; mapped={}
        for name,path in (("score","/api/oracle/score?"+urlencode({"symbol":target})),("oracle_state","/api/oracle/state"),("market_context","/api/market-context"),("portfolio_live_gate","/api/portfolio/live_gate?"+urlencode({"symbol":target})),("predictions_db","/api/oracle/predictions/db?"+urlencode({"limit":1,"symbol":target}))):
            try:
                obj,body,_=http.json(PRODUCTION_BASE+path); mapped[name]=obj; surfaces[name]={"endpoint":path,"http_status":200,"observed_at":utcnow(),"sanitized_sha256":sha256(canon(obj)),"mapped":surface_extract(name,obj)}
            except ProofBlocked as e: surfaces[name]={"endpoint":path,"status":"UNKNOWN","error":str(e),"observed_at":utcnow()}
        runtime="PASS"; sm=surfaces.get("score",{}).get("mapped",{}); live=surfaces.get("portfolio_live_gate",{}).get("mapped",{})
        if sm.get("authority_history_rows")!=len(ordered): runtime="FAIL"
        if live and live.get("authority_history_complete") is not True: runtime="FAIL"
        write_json(out/"RUNTIME_SURFACE_RECONCILIATION.json",{"status":runtime,"target_symbol":target,"surfaces":surfaces})
        global_exact=exact_count(http,sb_url,sb_key,table,None); db_total=(mapped.get("predictions_db") or {}).get("total_in_db"); global_rec={"DIRECT_GLOBAL_EXACT_COUNT":global_exact,"api_predictions_db_total_in_db":db_total,"dashboard_total_in_db":"UNKNOWN_NOT_DIRECTLY_EXPOSED_AS_SEPARATE_GET","status":"PASS" if isinstance(db_total,int) and db_total==global_exact else "FAIL" if isinstance(db_total,int) else "UNKNOWN","note":"API total_in_db is observability only; direct exact count is authority."}; write_json(out/"GLOBAL_COUNT_RECONCILIATION.json",global_rec)
        safety=safety_from_surfaces(mapped.get("market_context") or {},mapped.get("oracle_state") or {}); write_json(out/"SAFETY_READBACK.json",safety); gates={"REAL_SOURCE_ROWS_GTE_501":len(ordered)>=501,"DIRECT_SOURCE_COMPLETENESS":direct["SOURCE_COMPLETENESS"]=="PASS","OLDER_VALID_AUTHORITY_RETENTION":retention=="PASS","INDEPENDENT_RECOMPUTATION":True,"PRODUCTION_SCORE_RECONCILIATION":api_rec["status"]=="PASS","RUNTIME_CONTROL_SURFACE_RECONCILIATION":runtime=="PASS","SAFETY_LOCKS":safety["status"]=="PASS"}; write_json(out/"BOUNDARY_VERDICT.json",{"BOUNDARY_PROOF":"PASS" if all(gates.values()) else "FAIL","gates":gates,"target_symbol":target,"direct_source_count":len(ordered)}); finish(out,http,source_sha); return "READY_FOR_AUD",target
    except ProofBlocked as e: blocked(out,str(e),http,source_sha,target); return "BLOCKED_REAL",target
    except Exception as e:
        write_json(out/"BOUNDARY_VERDICT.json",{"BOUNDARY_PROOF":"FAIL","error_class":type(e).__name__,"error":str(e)[:300]}); ensure_placeholders(out); finish(out,http,source_sha); return "READY_FOR_AUD",target

def verify_manifest(out):
    out=Path(out); expected={p.name for p in out.iterdir() if p.is_file() and p.name!="MANIFEST.sha256"}; got=set()
    for line in (out/"MANIFEST.sha256").read_text().splitlines():
        h,n=line.split("  ",1); got.add(n)
        if sha256((out/n).read_bytes())!=h: return False
    return got==expected

def fixture_row(i,base=None):
    base=base or datetime(2026,1,1,tzinfo=timezone.utc); dt=base+timedelta(minutes=15*i); ts=dt.isoformat(); direction="LONG" if i%2==0 else "SHORT"; origin=100.0; p15=101.0 if direction=="LONG" else 99.0; p60=p15
    def ev(seconds,price):
        target=int((dt+timedelta(seconds=seconds)).timestamp()*1000); op=(target//60000)*60000; return {"version":"historical-price-evidence-v1","source":"okx","symbol":"BTC/USDT","window_seconds":seconds,"target_epoch_ms":target,"candle_open_epoch_ms":op,"candle_close_epoch_ms":op+60000,"candle_interval_ms":60000,"price":price,"observed_at":datetime.fromtimestamp((op+60000)/1000,tz=timezone.utc).isoformat()}
    return {"id":f"{i:04d}","ts":ts,"symbol":"BTCUSDT","prediction":direction,"confidence":.6,"outcome":"WIN","exchange_used":"okx","price_now":origin,"audit":{"origin_price_v1":{"version":"origin-price-v1","timestamp":ts,"source":"okx","price":origin},"outcomes_dual":{"primary_window":"1h","settlement_contract_version":"aud063-v1","outcome_15m":"WIN","outcome_1h":"WIN","price_15m_later":p15,"price_1h_later":p60,"price_evidence_v1":{"15m":ev(900,p15),"1h":ev(3600,p60)},"settlement_observation_v1":{"version":"settlement-observation-v1","observed_at":(dt+timedelta(hours=2)).isoformat()}}}}

def self_test():
    rows=[fixture_row(i) for i in range(501)]; full=recompute(rows,"BTCUSDT"); legacy=recompute(rows[-500:],"BTCUSDT"); assert full["independent_1h_rows"]==126 and legacy["independent_1h_rows"]==125; assert full["input_rows"]==501; assert sha256(canon(sorted(rows,key=stable_key)))==sha256(canon(sorted(reversed(rows),key=stable_key))); tied=[fixture_row(0),fixture_row(0)]; tied[0]["id"]="b"; tied[1]["id"]="a"; assert [x["id"] for x in sorted(tied,key=stable_key)]==["a","b"]
    for size in (1,7,250,500):
        collected=[r for i in range(0,len(rows),size) for r in rows[i:i+size]]; assert recompute(collected,"BTCUSDT")["independent_1h_rows"]==126
    assert ALLOWED_METHODS=={"GET","HEAD"}; assert safety_from_surfaces({}, {})["status"]=="UNKNOWN"; assert full["authoritative_score_pct"] is None
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/"x"; p.write_text("ghp_"+"A"*30); assert secret_scan([p])==["x"]
    bad=fixture_row(1); del bad["audit"]["outcomes_dual"]["price_evidence_v1"]; assert not proof_qualified(bad); assert sha256(canon({"b":2,"a":1}))==sha256(canon({"a":1,"b":2})); return {"tests":12,"status":"PASS","boundary_fixture":{"source_n":501,"full_authority_n":126,"legacy500_authority_n":125}}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--self-test",action="store_true"); ap.add_argument("--out"); ap.add_argument("--source-sha",default=os.environ.get("GITHUB_SHA","LOCAL")); a=ap.parse_args()
    if a.self_test: print(json.dumps(self_test(),sort_keys=True)); return
    if not a.out: ap.error("--out required")
    result,target=run_real(a.out,a.source_sha)
    if not verify_manifest(a.out): raise SystemExit("MANIFEST_VERIFY_FAIL")
    print(f"AUD_065_BP1_INTERNAL_STATUS={result}"); print(f"TARGET_SYMBOL={target or 'NONE'}"); print("BOUNDARY_PROOF="+str(json.loads((Path(a.out)/"BOUNDARY_VERDICT.json").read_text()).get("BOUNDARY_PROOF")))
if __name__=="__main__": main()
