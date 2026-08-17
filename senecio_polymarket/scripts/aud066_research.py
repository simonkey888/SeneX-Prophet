#!/usr/bin/env python3
"""AUD-066 zero-cost historical replay over Tardis first-of-month sample CSVs."""
from __future__ import annotations
import csv,gzip,hashlib,json,sys,tempfile,urllib.request,urllib.error
from pathlib import Path
RESEARCH_DIR=Path(__file__).resolve().parents[1]/'backend'/'research'; sys.path.insert(0,str(RESEARCH_DIR))
from aud066_liquidation import normalize_liquidation,build_minute_market,make_samples,canonical_hash
from aud066_analysis import walk_forward_extended
BASE_SHA='43c8023d3a4623381e45da02d9efa8e9b5888f47'; BASE_TREE='20ec5775ea37a7288e8cd8748ea304843d9b0866'
DATES=['2023-01-01','2023-04-01','2023-07-01','2023-10-01','2024-01-01','2024-04-01','2024-07-01','2024-10-01','2025-01-01','2025-04-01','2025-07-01','2025-10-01']
MAX_BYTES=180_000_000; ROOT=Path(__file__).resolve().parents[1]/'docs'/'evidence'/'aud-066'; DATASETS=('trades','quotes','derivative_ticker','liquidations')
def url(dtype,date):
 y,m,d=date.split('-'); s='PERPETUALS' if dtype=='liquidations' else 'BTCUSDT'; return f'https://datasets.tardis.dev/v1/binance-futures/{dtype}/{y}/{m}/{d}/{s}.csv.gz'
def download_bounded(u,path):
 req=urllib.request.Request(u,headers={'User-Agent':'SENEX-AUD066-research/1','Accept':'application/gzip,*/*;q=.1'}); h=hashlib.sha256(); total=0
 try:
  with urllib.request.urlopen(req,timeout=45) as r, open(path,'wb') as f:
   final=r.geturl()
   if not final.startswith('https://datasets.tardis.dev/'): raise RuntimeError('REDIRECT_OUTSIDE_ALLOWLIST:'+final)
   cl=r.headers.get('Content-Length')
   if cl and int(cl)>MAX_BYTES: raise RuntimeError('PAYLOAD_TOO_LARGE_HEADER:'+cl)
   while True:
    b=r.read(1024*1024)
    if not b: break
    total+=len(b)
    if total>MAX_BYTES: raise RuntimeError('PAYLOAD_TOO_LARGE_STREAM:'+str(total))
    h.update(b); f.write(b)
  return {'url':u,'resolved_url':final,'bytes':total,'sha256':h.hexdigest(),'status':'OK'}
 except (urllib.error.HTTPError,urllib.error.URLError,TimeoutError,RuntimeError) as e:
  path.unlink(missing_ok=True); return {'url':u,'bytes':total,'sha256':None,'status':'ERROR','error':type(e).__name__+':'+str(e)[:240]}
def rows(path):
 with gzip.open(path,'rt',encoding='utf-8',newline='') as f: yield from csv.DictReader(f)
def main():
 ROOT.mkdir(parents=True,exist_ok=True); all_samples=[]; provenance=[]; excluded={}; date_stats=[]
 with tempfile.TemporaryDirectory(prefix='aud066-') as td:
  td=Path(td)
  for date in DATES:
   files={}; manifests=[]; failed=False
   for dtype in DATASETS:
    p=td/f'{date}-{dtype}.csv.gz'; rec=download_bounded(url(dtype,date),p); rec.update({'date':date,'data_type':dtype}); manifests.append(rec)
    if rec['status']!='OK': failed=True
    else: files[dtype]=p
   provenance.extend(manifests)
   if failed:
    excluded['DATASET_DOWNLOAD_OR_AVAILABILITY_FAILURE']=excluded.get('DATASET_DOWNLOAD_OR_AVAILABILITY_FAILURE',0)+1; date_stats.append({'date':date,'status':'EXCLUDED_SOURCE_FAILURE','manifests':manifests}); continue
   liqs=[]; bad=0
   for r in rows(files['liquidations']):
    if str(r.get('symbol') or '').upper()!='BTCUSDT': continue
    x=normalize_liquidation(r)
    if x is None: bad+=1
    else: liqs.append(x)
   liqs.sort(key=lambda x:x['known_at_us'])
   trades=[r for r in rows(files['trades']) if str(r.get('symbol') or '').upper()=='BTCUSDT']; quotes=[r for r in rows(files['quotes']) if str(r.get('symbol') or '').upper()=='BTCUSDT']; tickers=[r for r in rows(files['derivative_ticker']) if str(r.get('symbol') or '').upper()=='BTCUSDT']
   mins=build_minute_market(trades,quotes,tickers); samples,exc=make_samples(date,mins,liqs); all_samples.extend(samples)
   for k,v in exc.items(): excluded[k]=excluded.get(k,0)+v
   excluded['LIQ_ROWS_BAD_TIMESTAMP_OR_SCHEMA']=excluded.get('LIQ_ROWS_BAD_TIMESTAMP_OR_SCHEMA',0)+bad
   date_stats.append({'date':date,'status':'USED','liquidations':len(liqs),'trades':len(trades),'quotes':len(quotes),'ticker_rows':len(tickers),'samples':len(samples),'excluded':exc})
 result=walk_forward_extended(all_samples)
 manifest={'order':'AUD-066','base_sha':BASE_SHA,'base_tree':BASE_TREE,'source':'Tardis normalized first-of-month sample datasets exported from exchange real-time feeds','zero_cost':True,'api_key_required':False,'dates_requested':DATES,'date_stats':date_stats,'sample_count':len(all_samples),'excluded':excluded,'provenance':provenance,'provenance_hash':canonical_hash([{k:v for k,v in r.items() if k!='error'} for r in provenance]),'point_in_time_clock':'local_timestamp(receipt time)','label_rule':'BTC price at t+5m strictly after decision; never used in feature construction','cost_usd':0}
 (ROOT/'data-manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n'); (ROOT/'oos-results.json').write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
 lines=['arm,status,test_blocks,mean_brier,mean_log_loss,mean_ece_10,calibration_intercept,calibration_slope,mean_accuracy,mean_balanced_accuracy,delta_brier_vs_A,delta_logloss_vs_A']
 if result.get('status')=='COMPLETE':
  sm=result['summary']; base=sm['A']
  for arm in ('A','B','C'):
   x=sm[arm]; lines.append(','.join(str(v) for v in [arm,'TESTED',len(result['blocks']),x['brier'],x['log_loss'],x['ece_10'],x['calibration_intercept'],x['calibration_slope'],x['accuracy'],x['balanced_accuracy'],x['brier']-base['brier'],x['log_loss']-base['log_loss']]))
 else:
  for arm in ('A','B','C'): lines.append(f'{arm},INCONCLUSIVE,0,,,,,,,,,')
 lines+=['D,NOT_TESTABLE_AT_ZERO_COST,0,,,,,,,,,','E,VALIDATION_SELECTED_PER_BLOCK,,,,,,,,,,']; (ROOT/'ablation-results.csv').write_text('\n'.join(lines)+'\n')
 print('AUD066_SAMPLE_COUNT='+str(len(all_samples))); print('AUD066_RESULT='+json.dumps(result,sort_keys=True)); print('AUD066_MANIFEST_HASH='+canonical_hash(manifest))
if __name__=='__main__': main()
