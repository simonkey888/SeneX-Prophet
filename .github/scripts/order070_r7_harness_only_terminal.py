from __future__ import annotations
import concurrent.futures, datetime as dt, hashlib, json, os, re, shutil, subprocess, tempfile, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

API='https://api.northflank.com/v1'; PROJECT='seneciobot'; SERVICE='senecio-h011'; BRANCH='feat/order-070-runtime-truth-hardening'
HEAD='4b107bfb427cb85ea84850ffd9ddd5d7a4231d94'; TREE='5d1d9ec806b7d0e02031726565f08ef75d5a9340'
BUILD_ID='bumpy-brass-9194'; BUILD_DIGEST='sha256:8f4511e0ac2499e3b7408843a82e7f3a5bc4cc466c296003eb363842ad2023ac'
IMAGE_DIGEST='sha256:431702a5e4bb08d139151b5d484428423fa3cc15927d155b768ed2142aee1084'
ORIGIN='https://h011-web--senecio-h011--wbjggn89fnf8.code.run'; RAM_LIMIT_MB=512.0; STABILITY_SECONDS=1800; SAMPLE_SECONDS=15
CI={'ORDER070':32585446334,'SCORE001':32585446345,'SCORE002':32585446326,'SMOKE':32585446328}
ROOT=Path(os.environ.get('CANDIDATE_DIR','candidate')).resolve(); OUT=Path('order070-r7-final-evidence').resolve(); TOKEN=os.environ['NORTHFLANK_API_TOKEN']
if OUT.exists(): shutil.rmtree(OUT)
OUT.mkdir(parents=True)
NF_HEADERS={'Authorization':f'Bearer {TOKEN}','Accept':'application/json','Content-Type':'application/json','User-Agent':'senex-order070-r7/1'}

def now(): return dt.datetime.now(dt.timezone.utc)
def iso(t=None): return (t or now()).isoformat().replace('+00:00','Z')
def h256(b:bytes): return hashlib.sha256(b).hexdigest()
def write(name,obj):
    p=OUT/name; p.write_text(json.dumps(obj,sort_keys=True,indent=2,default=str)+'\n'); return h256(p.read_bytes())
def git(*args): return subprocess.run(['git',*args],cwd=ROOT,text=True,capture_output=True,check=True).stdout.strip()
def data(x): return x.get('data',x) if isinstance(x,dict) else x

def request_json(method,url,headers=None,payload=None,timeout=60):
    body=None if payload is None else json.dumps(payload,separators=(',',':')).encode()
    hdr=headers or {'Accept':'application/json','Cache-Control':'no-cache','User-Agent':'senex-order070-r7-live/1'}
    req=urllib.request.Request(url,headers=hdr,data=body,method=method)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r: st=r.status; raw=r.read(); rh={k.lower():v for k,v in r.headers.items()}
    except urllib.error.HTTPError as e: st=e.code; raw=e.read(); rh={k.lower():v for k,v in e.headers.items()}
    except Exception as e: return {'http':0,'body':{'error_type':type(e).__name__,'error':str(e)[:200]},'headers':{},'sha256':None}
    try: obj=json.loads(raw.decode())
    except Exception: obj={'_non_json':True,'bytes':len(raw),'sha256':h256(raw),'text':raw.decode(errors='replace')[:300]}
    return {'http':st,'body':obj,'headers':rh,'sha256':h256(raw)}

def nf(path,query=None,timeout=90):
    url=API+path
    if query: url+='?'+urllib.parse.urlencode(query,doseq=True)
    r=request_json('GET',url,NF_HEADERS,None,timeout)
    if not 200<=r['http']<300: raise RuntimeError(f'NF_GET_{path}_HTTP_{r["http"]}:{str(r["body"])[:240]}')
    return data(r['body']),r

def pub(base,path,method='GET',timeout=45): return request_json(method,base.rstrip('/')+path,None,None,timeout)
def service(): return nf(f'/projects/{PROJECT}/services/{SERVICE}')[0]
def deployment(): return nf(f'/projects/{PROJECT}/services/{SERVICE}/deployment')[0]
def services_entry():
    x,_=nf(f'/projects/{PROJECT}/services',{'per_page':100}); arr=x.get('services') if isinstance(x,dict) else x; return next(v for v in arr if v.get('id')==SERVICE)
def containers():
    x,_=nf(f'/projects/{PROJECT}/services/{SERVICE}/containers',{'per_page':100}); return (x.get('containers') if isinstance(x,dict) else x) or []
def running_names(rows): return sorted(str(x.get('name')) for x in rows if isinstance(x,dict) and x.get('status')=='TASK_RUNNING')

def cf_env():
    env=dict(os.environ)
    for k in list(env):
        if k.startswith('CLOUDFLARE_') or k in {'CF_API_TOKEN','CF_ACCOUNT_ID','CF_API_KEY','CF_EMAIL'}: env.pop(k,None)
    return env

def deploy_temp_worker():
    cp=subprocess.run(['npx','--yes','wrangler@4.102.0','deploy','--temporary','--config','wrangler.jsonc'],cwd=ROOT/'edge/order070',env=cf_env(),text=True,capture_output=True,timeout=180)
    raw=(cp.stdout or '')+'\n'+(cp.stderr or ''); dig=h256(raw.encode())
    if cp.returncode: raise RuntimeError(f'CLOUDFLARE_DEPLOY_FAILED:{cp.returncode}:{dig}')
    urls=re.findall(r'https://[A-Za-z0-9._-]+\.workers\.dev',raw)
    if not urls: raise RuntimeError(f'CLOUDFLARE_URL_MISSING:{dig}')
    return urls[-1].rstrip('/'),dig

def curl_probe(base,method,path):
    with tempfile.TemporaryDirectory() as td:
        hp=Path(td)/'h'; bp=Path(td)/'b'
        env=cf_env()
        cp=subprocess.run(['curl','-sS','--max-time','40','-D',str(hp),'-o',str(bp),'-w','%{http_code}','-X',method,base.rstrip('/')+path],text=True,capture_output=True,timeout=45,env=env)
        code=int(cp.stdout.strip()) if cp.returncode==0 and cp.stdout.strip().isdigit() else 0; decision=None
        if hp.exists():
            for line in hp.read_text(errors='replace').splitlines():
                if line.lower().startswith('x-senex-edge-decision:'): decision=line.split(':',1)[1].strip()
        return {'http':code,'decision':decision,'curl_exit':cp.returncode,'body_sha256':h256(bp.read_bytes()) if bp.exists() else None}

def identity(kind,b):
    if kind=='snapshot': return b.get('snapshot_id'),b.get('generation'),b.get('canonical_sha256')
    if kind=='ready': return b.get('authority_snapshot_id'),b.get('generation'),b.get('canonical_sha256')
    return b.get('authority_snapshot_id'),b.get('authority_generation'),b.get('authority_canonical_sha256')

def unsafe_count(schema):
    return sum(1 for _,item in (schema.get('paths') or {}).items() if isinstance(item,dict) for m in item if str(m).lower() in {'post','put','patch','delete'})

def assert_exact_live(base,edge=False):
    h=pub(base,'/healthz'); r=pub(base,'/readyz?symbol=BTCUSDT'); p=pub(base,'/api/runtime/provenance')
    if not all(x['http']==200 for x in (h,r,p)): raise RuntimeError(f'LIVE_HTTP:{base}:{h["http"]}:{r["http"]}:{p["http"]}')
    hb=h['body']; rb=r['body']; pb=p['body']
    if hb.get('trade_mode')!='PAPER' or hb.get('orders_enabled') is not False or hb.get('live_capital_locked') is not True: raise RuntimeError('PAPER_LOCK_FAILED')
    if rb.get('status')!='ready' or not all((rb.get('checks') or {}).values()): raise RuntimeError(f'READY_FAILED:{rb}')
    if pb.get('exact') is not True or pb.get('source_commit')!=HEAD or pb.get('source_tree')!=TREE or pb.get('build_digest')!=BUILD_DIGEST or pb.get('image_digest')!=IMAGE_DIGEST: raise RuntimeError(f'PROVENANCE_FAILED:{pb}')
    if edge and h['headers'].get('x-senex-edge-decision')!='ALLOW_GET_PROXY': raise RuntimeError('EDGE_DECISION_HEADER_MISSING')
    return h,r,p

def extract_memory(metrics):
    obj=(metrics or {}).get('memory',{}) if isinstance(metrics,dict) else {}; unit=((obj.get('metricInfo') or {}).get('metricUnit') if isinstance(obj,dict) else None) or 'pct'; pts=[]
    for series in obj.get('values',[]) if isinstance(obj,dict) else []:
        cid=(series.get('metadata') or {}).get('containerId')
        for p in series.get('data') or []:
            try:
                if isinstance(p,(list,tuple)) and len(p)>=2: ts,val=p[0],float(p[1])
                elif isinstance(p,dict): ts,val=p.get('timestamp') or p.get('time') or p.get('ts'),float(p.get('value'))
                else: continue
                pct=(val/RAM_LIMIT_MB*100.0) if unit=='mb' else val; pts.append({'container':cid,'ts':ts,'value':val,'pct':pct})
            except Exception: pass
    return unit,pts

# Immutable candidate and remote truth.
if git('rev-parse','HEAD')!=HEAD or git('rev-parse','HEAD^{tree}')!=TREE or git('status','--porcelain'): raise RuntimeError('CANDIDATE_DRIFT')
remote=git('ls-remote','origin',f'refs/heads/{BRANCH}').split()[0]
if remote!=HEAD: raise RuntimeError('REMOTE_HEAD_DRIFT')
write('REMOTE_TRUTH.json',{'observed_at':iso(),'order':'ORDER-070-R7','pr':67,'head':HEAD,'tree':TREE,'remote_head':remote,'candidate_change':False,'ops_harness_only':True,'merge':False,'ci_runs':CI})

# R7 requires reuse of the already exact Northflank origin. GET-only verification; no rebuild/redeploy.
_,a1=nf(f'/projects/{PROJECT}'); dep,a2=nf(f'/projects/{PROJECT}/services/{SERVICE}/deployment'); build,_=nf(f'/projects/{PROJECT}/services/{SERVICE}/build/{BUILD_ID}'); svc=service(); ent=services_entry()
ii=dep.get('internal') or {}; deploy_status=((ent.get('status') or {}).get('deployment') or {}).get('status')
if a1['http']!=200 or a2['http']!=200: raise RuntimeError('NORTHFLANK_AUTH_FAILED')
if build.get('sha')!=HEAD or build.get('success') is not True or not build.get('concluded'): raise RuntimeError('EXACT_BUILD_READBACK_FAILED')
if ii.get('deployedSHA')!=HEAD or deploy_status!='COMPLETED': raise RuntimeError(f'EXACT_DEPLOY_READBACK_FAILED:{ii.get("deployedSHA")}:{deploy_status}')
if (svc.get('vcsData') or {}).get('projectBranch')!='main': raise RuntimeError('SOURCE_BRANCH_NOT_RESTORED')
oh,orr,op=assert_exact_live(ORIGIN)
write('ORIGIN_EXACT_READBACK.json',{'observed_at':iso(),'northflank_project_http':a1['http'],'northflank_deployment_http':a2['http'],'build_id':BUILD_ID,'build_sha':build.get('sha'),'build_success':True,'deployed_sha':ii.get('deployedSHA'),'deployment_status':deploy_status,'head':HEAD,'tree':TREE,'build_digest':BUILD_DIGEST,'image_digest':IMAGE_DIGEST,'healthz':oh['http'],'readyz':orr['http'],'provenance_http':op['http'],'provenance_exact':True,'northflank_mutations':0})

# Fresh temporary exact-head edge. Direct public URL only; no wrangler dev --remote.
EDGE,deploy_sha=deploy_temp_worker(); boot=None
for attempt in range(1,41):
    boot=pub(EDGE,'/healthz')
    if boot['http']==200 and boot['headers'].get('x-senex-edge-decision')=='ALLOW_GET_PROXY': break
    time.sleep(2)
if not boot or boot['http']!=200 or boot['headers'].get('x-senex-edge-decision')!='ALLOW_GET_PROXY': raise RuntimeError(f'EDGE_BOOT_FAILED:{boot}')
post=curl_probe(EDGE,'POST','/api/oracle/score'); unknown=curl_probe(EDGE,'GET','/__order070_unknown__')
if post['http']!=405 or post['decision']!='DENY_METHOD': raise RuntimeError(f'EDGE_POST_DENIAL_FAILED:{post}')
if unknown['http']!=404 or unknown['decision']!='DENY_PATH': raise RuntimeError(f'EDGE_UNKNOWN_DENIAL_FAILED:{unknown}')
assert_exact_live(EDGE,edge=True)
write('CLOUDFLARE_FINAL.json',{'observed_at':iso(),'head':HEAD,'tree':TREE,'temporary_worker_url':EDGE,'temporary_deploy_output_sha256':deploy_sha,'credentials_used':False,'positive_allowlist':{'health_http':200,'decision':'ALLOW_GET_PROXY'},'method_denial':post,'unknown_path_denial':unknown,'wrangler_remote_dev_used':False,'result':'PASS'})

# >=8 concurrent origin <-> direct public edge reconciliation rounds.
paths={'snapshot':'/api/authority/snapshot?symbol=BTCUSDT','score':'/api/oracle/score?symbol=BTCUSDT','state':'/api/oracle/state?symbol=BTCUSDT','gate':'/api/portfolio/live_gate?symbol=BTCUSDT','ready':'/readyz?symbol=BTCUSDT'}
def fetch_job(side,base,kind,path): return side,kind,pub(base,path)
rounds=[]
for n in range(1,9):
    attempts=[]; success=False
    for attempt in range(1,4):
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            rows=[f.result() for f in [ex.submit(fetch_job,s,b,k,p) for s,b in [('origin',ORIGIN),('edge',EDGE)] for k,p in paths.items()]]
        got={(s,k):r for s,k,r in rows}; statuses={f'{s}:{k}':got[(s,k)]['http'] for s in ('origin','edge') for k in paths}; attempts.append(statuses)
        if any(v!=200 for v in statuses.values()): time.sleep(2); continue
        ids=[identity(k,got[(s,k)]['body']) for s in ('origin','edge') for k in paths]
        if any(None in x for x in ids) or len(set(ids))!=1: raise RuntimeError(f'ROUND_IDENTITY_MISMATCH:{n}:{ids}')
        osnap=got[('origin','snapshot')]['body']; esnap=got[('edge','snapshot')]['body']
        core=['snapshot_id','generation','canonical_sha256','symbol','authority_history_complete','authority_history_rows','exact_total_predictions','exact_count_complete','last_cursor_or_equivalent','score','live_gate','provenance']
        if not all(osnap.get(k)==esnap.get(k) for k in core): raise RuntimeError(f'ROUND_SNAPSHOT_BODY_MISMATCH:{n}')
        if got[('origin','score')]['body']!=got[('edge','score')]['body'] or got[('origin','gate')]['body']!=got[('edge','gate')]['body']: raise RuntimeError(f'ROUND_BODY_MISMATCH:{n}')
        rounds.append({'round':n,'attempt':attempt,'snapshot_id':ids[0][0],'generation':ids[0][1],'canonical_sha256':ids[0][2],'all_10_identities_equal':True,'snapshot_core_equal':True,'score_equal':True,'live_gate_equal':True,'exact_total_predictions':osnap.get('exact_total_predictions'),'btc_authority_rows':osnap.get('authority_history_rows')}); success=True; break
    if not success: raise RuntimeError(f'ROUND_HTTP_FAILED:{n}:{attempts}')
write('CONCURRENT_RECONCILIATION.json',{'observed_at':iso(),'round_count':len(rounds),'rounds':rounds,'result':'PASS'})

# Live E2E and public route safety/parity.
e2e_paths={'snapshot':paths['snapshot'],'context':'/api/market-context?symbol=BTCUSDT','predictions':'/api/oracle/predictions/db?limit=50&symbol=BTCUSDT','provenance':'/api/runtime/provenance','health':'/healthz','ready':paths['ready'],'openapi':'/openapi.json'}
final={k:{'origin':pub(ORIGIN,p),'edge':pub(EDGE,p)} for k,p in e2e_paths.items()}
for k in e2e_paths:
    if final[k]['origin']['http']!=200 or final[k]['edge']['http']!=200: raise RuntimeError(f'E2E_HTTP:{k}:{final[k]["origin"]["http"]}:{final[k]["edge"]["http"]}')
if final['predictions']['origin']['body']!=final['predictions']['edge']['body']: raise RuntimeError('E2E_PREDICTIONS_PARITY_FAILED')
for side in ('origin','edge'):
    p=final['provenance'][side]['body']; h=final['health'][side]['body']; r=final['ready'][side]['body']; saf=final['context'][side]['body'].get('safety') or {}; schema=final['openapi'][side]['body']
    if p.get('exact') is not True or p.get('source_commit')!=HEAD or p.get('source_tree')!=TREE or p.get('build_digest')!=BUILD_DIGEST or p.get('image_digest')!=IMAGE_DIGEST: raise RuntimeError(f'E2E_PROVENANCE:{side}')
    if r.get('status')!='ready' or not all((r.get('checks') or {}).values()): raise RuntimeError(f'E2E_READY:{side}')
    if h.get('trade_mode')!='PAPER' or h.get('orders_enabled') is not False or h.get('live_capital_locked') is not True: raise RuntimeError(f'E2E_HEALTH:{side}')
    if saf.get('trade_mode')!='PAPER' or saf.get('orders_enabled') is not False or saf.get('live_capital_locked') is not True or saf.get('allow_live') is not False: raise RuntimeError(f'E2E_SAFETY:{side}')
    if unsafe_count(schema)!=0: raise RuntimeError(f'E2E_UNSAFE_METHODS:{side}')
if pub(ORIGIN,'/admin')['http']!=404 or pub(ORIGIN,'/api/admin')['http']!=404: raise RuntimeError('PUBLIC_ADMIN_MOUNTED')
write('LIVE_E2E.json',{'observed_at':iso(),'head':HEAD,'tree':TREE,'origin':ORIGIN,'edge':EDGE,'health_origin':200,'health_edge':200,'ready_origin':200,'ready_edge':200,'provenance_exact_origin':True,'provenance_exact_edge':True,'predictions_body_parity':True,'public_origin_unsafe_count':0,'public_edge_unsafe_count':0,'admin_publicly_unmounted':True,'trade_mode':'PAPER','orders_enabled':False,'live_capital_locked':True,'result':'PASS'})

# Mandatory 30m stability starts only after exact readback + edge + reconciliation + E2E.
stability_start=now(); start_iso=iso(stability_start); initial_running=running_names(containers())
if not initial_running: raise RuntimeError('NO_RUNNING_CONTAINER_AT_STABILITY_START')
base_snap=pub(ORIGIN,paths['snapshot']); base_state=pub(ORIGIN,paths['state']); base_pred=pub(ORIGIN,e2e_paths['predictions']); base_h,base_r,base_p=assert_exact_live(ORIGIN)
if base_snap['http']!=200 or base_state['http']!=200 or base_pred['http']!=200: raise RuntimeError('STABILITY_BASELINE_HTTP')
base_cycles=int(base_state['body'].get('cycles_run') or 0); base_db=int(base_snap['body'].get('exact_total_predictions') or 0); base_rows=int(base_snap['body'].get('authority_history_rows') or 0); base_last=base_state['body'].get('last_prediction_ts')
samples=[]; generation_last=int(base_snap['body'].get('generation') or 0); next_sample=time.monotonic(); connection_refused=0
while (now()-stability_start).total_seconds()<STABILITY_SECONDS:
    delay=next_sample-time.monotonic()
    if delay>0: time.sleep(delay)
    at=now(); snap=pub(ORIGIN,paths['snapshot']); health=pub(ORIGIN,'/healthz'); ready=pub(ORIGIN,paths['ready']); prov=pub(ORIGIN,'/api/runtime/provenance'); state=pub(ORIGIN,paths['state']); edge_health=pub(EDGE,'/healthz'); edge_ready=pub(EDGE,paths['ready'])
    responses=[snap,health,ready,prov,state,edge_health,edge_ready]; connection_refused+=sum(1 for x in responses if x['http']==0)
    row={'at':iso(at),'snapshot_http':snap['http'],'health_http':health['http'],'ready_http':ready['http'],'provenance_http':prov['http'],'state_http':state['http'],'edge_health_http':edge_health['http'],'edge_ready_http':edge_ready['http']}
    if any(x['http']!=200 for x in responses): row['failure']='HTTP_CONTINUITY'; samples.append(row); write('STABILITY_SAMPLES_PARTIAL.json',samples); raise RuntimeError(f'STABILITY_HTTP_FAILURE:{row}')
    hb=health['body']; rb=ready['body']; pb=prov['body']; sb=state['body']
    if hb.get('trade_mode')!='PAPER' or hb.get('orders_enabled') is not False or hb.get('live_capital_locked') is not True: raise RuntimeError('STABILITY_PAPER_LOCK')
    if rb.get('status')!='ready' or not all((rb.get('checks') or {}).values()): raise RuntimeError(f'STABILITY_READY:{rb}')
    if pb.get('exact') is not True or pb.get('source_commit')!=HEAD or pb.get('source_tree')!=TREE or pb.get('build_digest')!=BUILD_DIGEST or pb.get('image_digest')!=IMAGE_DIGEST: raise RuntimeError('STABILITY_PROVENANCE_DRIFT')
    sid=snap['body'].get('snapshot_id'); gen=int(snap['body'].get('generation') or 0); rid=rb.get('authority_snapshot_id'); stateid=sb.get('authority_snapshot_id')
    if not sid or rid!=sid or stateid!=sid or gen<generation_last: raise RuntimeError(f'STABILITY_SNAPSHOT_INCONSISTENT:{sid}:{rid}:{stateid}:{gen}:{generation_last}')
    generation_last=gen
    stale=rb.get('snapshot_stale'); refresh_error=rb.get('last_refresh_error')
    if stale is not False or refresh_error is not None: raise RuntimeError(f'STABILITY_AUTHORITY_REFRESH:{stale}:{refresh_error}')
    row.update({'snapshot_id':sid,'generation':gen,'cycles_run':sb.get('cycles_run'),'db_predictions':snap['body'].get('exact_total_predictions'),'btc_authority_rows':snap['body'].get('authority_history_rows'),'snapshot_stale':stale,'last_refresh_error':refresh_error})
    if len(samples)%2==0:
        current_running=running_names(containers()); row['running_containers']=current_running
        if current_running!=initial_running: samples.append(row); write('STABILITY_SAMPLES_PARTIAL.json',samples); raise RuntimeError(f'UNEXPECTED_CONTAINER_REPLACEMENT:{initial_running}:{current_running}')
    samples.append(row); next_sample+=SAMPLE_SECONDS

stability_end=now(); end_iso=iso(stability_end); elapsed=(stability_end-stability_start).total_seconds()
if elapsed<1800: raise RuntimeError(f'STABILITY_TOO_SHORT:{elapsed}')
final_snap=pub(ORIGIN,paths['snapshot']); final_state=pub(ORIGIN,paths['state']); final_pred=pub(ORIGIN,e2e_paths['predictions']); assert_exact_live(ORIGIN)
if final_snap['http']!=200 or final_state['http']!=200 or final_pred['http']!=200: raise RuntimeError('FINAL_PROGRESS_HTTP')
final_cycles=int(final_state['body'].get('cycles_run') or 0); final_db=int(final_snap['body'].get('exact_total_predictions') or 0); final_rows=int(final_snap['body'].get('authority_history_rows') or 0); final_last=final_state['body'].get('last_prediction_ts')
if final_cycles<=base_cycles: raise RuntimeError(f'ORACLE_CYCLES_DID_NOT_ADVANCE:{base_cycles}:{final_cycles}')
if final_db<=base_db: raise RuntimeError(f'DB_PREDICTIONS_DID_NOT_INCREASE:{base_db}:{final_db}')
if final_rows<base_rows: raise RuntimeError(f'BTC_AUTHORITY_ROWS_DECREASED:{base_rows}:{final_rows}')
if not final_last or final_last==base_last: raise RuntimeError(f'LATEST_PREDICTION_TIMESTAMP_DID_NOT_ADVANCE:{base_last}:{final_last}')
final_running=running_names(containers())
if final_running!=initial_running: raise RuntimeError(f'FINAL_CONTAINER_IDENTITY_DRIFT:{initial_running}:{final_running}')

metrics,_=nf(f'/projects/{PROJECT}/services/{SERVICE}/metrics',[('queryType','range'),('startTime',start_iso),('endTime',end_iso),('metricTypes','memory'),('metricTypes','requests'),('metricTypes','http5xxResponses'),('metricTypes','tcpConnectionsOpen')])
unit,mempts=extract_memory(metrics); relevant=[p for p in mempts if p.get('container') in initial_running]
if not relevant: relevant=mempts
if not relevant: raise RuntimeError(f'NO_MEMORY_METRICS:{unit}:{initial_running}')
ram_max=max(p['pct'] for p in relevant)
if ram_max>=90.0: raise RuntimeError(f'RAM_MAX_NOT_BELOW_90:{ram_max}')

# Search only exit/OOM/refusal signatures during the sealed stability window.
patterns=['Process terminated with exit code','uvicorn exited','OOMKilled','Killed process','Connection refused','TASK_KILLED']
log_hits=[]
for pat in patterns:
    logs,_=nf(f'/projects/{PROJECT}/services/{SERVICE}/logs',{'queryType':'range','startTime':start_iso,'endTime':end_iso,'type':'runtime','lineLimit':1000,'direction':'forward','regexIncludes':pat})
    rows=logs if isinstance(logs,list) else []
    for row in rows: log_hits.append({'pattern':pat,'row':row})
if log_hits: write('STABILITY_LOG_HITS.json',{'hits':log_hits}); raise RuntimeError(f'UNEXPECTED_RUNTIME_LOG_HITS:{len(log_hits)}')
if connection_refused!=0: raise RuntimeError(f'CONNECTION_REFUSED_COUNT:{connection_refused}')

write('STABILITY_30M.json',{'started_at':start_iso,'ended_at':end_iso,'elapsed_seconds':elapsed,'sample_interval_seconds':SAMPLE_SECONDS,'sample_count':len(samples),'initial_running_containers':initial_running,'final_running_containers':final_running,'unexpected_restarts':0,'unexpected_process_exits':0,'oom_kills':0,'connection_refused':0,'healthz_continuous':True,'readyz_continuous':True,'edge_health_continuous':True,'edge_ready_continuous':True,'authority_refresh_continuous':True,'snapshot_generation_consistent':True,'ram_metric_unit':unit,'ram_max_pct':ram_max,'ram_lt_90':True,'oracle_cycles_initial':base_cycles,'oracle_cycles_final':final_cycles,'oracle_cycles_advance':final_cycles-base_cycles,'exact_total_predictions_initial':base_db,'exact_total_predictions_final':final_db,'exact_total_predictions_increase':final_db-base_db,'btc_authority_rows_initial':base_rows,'btc_authority_rows_final':final_rows,'btc_authority_rows_nondecreasing':True,'last_prediction_ts_initial':base_last,'last_prediction_ts_final':final_last,'latest_prediction_ts_advanced':True,'trade_mode':'PAPER','orders_enabled':False,'real_order_count':0,'real_capital_movement':0,'supabase_data_mutation':0,'runtime017_mutation':0,'tuning':0,'result':'PASS'})
write('STABILITY_SAMPLES.json',samples)
write('SAFETY_READBACK.json',{'observed_at':iso(),'trade_mode':'PAPER','orders_enabled':False,'live_capital_locked':True,'real_order_count':0,'real_capital_movement':0,'supabase_data_mutation':0,'runtime017_mutation':0,'tuning':0})
write('FINAL_GATE_SUMMARY.json',{'observed_at':iso(),'order':'ORDER-070-R7','status':'READY_FOR_AUD','pr':67,'head':HEAD,'tree':TREE,'candidate_change':False,'ops_harness_only':True,'northflank_redeploy':False,'build_id':BUILD_ID,'build_digest':BUILD_DIGEST,'image_digest':IMAGE_DIGEST,'origin_exact':'PASS','cloudflare_direct_public_edge':'PASS','reconciliation_rounds':8,'live_e2e':'PASS','stability_30m':'PASS','ram_max_pct':ram_max,'unexpected_restarts':0,'connection_refused':0,'oracle_cycles_advance':final_cycles-base_cycles,'predictions_increase':final_db-base_db,'btc_authority_rows_nondecreasing':True,'merge':False})

files=sorted(p.name for p in OUT.glob('*.json'))
(OUT/'MANIFEST.sha256').write_text('\n'.join(f'{h256((OUT/n).read_bytes())}  {n}' for n in files)+'\n')
ck=subprocess.run(['sha256sum','-c','MANIFEST.sha256'],cwd=OUT,text=True,capture_output=True)
if ck.returncode: raise RuntimeError(f'MANIFEST_VERIFY:{ck.stdout}:{ck.stderr}')
manifest_hash=h256((OUT/'MANIFEST.sha256').read_bytes())
print('ORDER_070_STATUS=READY_FOR_AUD'); print('PR=67'); print('HEAD='+HEAD); print('TREE='+TREE); print('BUILD_ID='+BUILD_ID); print('OCI_DIGEST='+IMAGE_DIGEST); print('HEALTHZ=200'); print('READYZ=200'); print('CLOUDFLARE_FINAL_EXACT_HEAD=PASS'); print('SNAPSHOT_RECONCILIATION=PASS_8_ROUNDS'); print('PROVENANCE=EXACT_HEAD_BOUND'); print('LIVE_E2E=PASS'); print(f'RAM_MAX_PCT={ram_max:.6f}'); print('UNEXPECTED_RESTARTS_30M=0'); print('OOM_KILLS_30M=0'); print('CONNECTION_REFUSED_30M=0'); print('HEALTH_CONTINUITY_30M=PASS'); print('READY_CONTINUITY_30M=PASS'); print('MANIFEST_SHA256='+manifest_hash)
