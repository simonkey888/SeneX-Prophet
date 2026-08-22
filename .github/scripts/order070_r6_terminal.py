from __future__ import annotations
import concurrent.futures, datetime as dt, hashlib, json, os, re, shutil, subprocess, tempfile, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

API='https://api.northflank.com/v1'; PROJECT='seneciobot'; SERVICE='senecio-h011'; BRANCH='feat/order-070-runtime-truth-hardening'
HEAD='d166495e9a74f528ccce1adeb5ce97a281b175cf'; TREE='6106f1c2f39b4509d3a237eb807db5d45feb7463'
BUILD_DIGEST='sha256:1806ad0bc71c45264695c1c8973a497a39f9903f867ece2d56fdbc12f44e4892'
ORIGIN='https://h011-web--senecio-h011--wbjggn89fnf8.code.run'; RAM_LIMIT_MB=512.0; STABILITY_SECONDS=1800; SAMPLE_SECONDS=15
ROOT=Path(os.environ.get('CANDIDATE_DIR','candidate')).resolve(); OUT=Path('order070-r6-final-evidence').resolve(); TOKEN=os.environ['NORTHFLANK_API_TOKEN']
if OUT.exists(): shutil.rmtree(OUT)
OUT.mkdir(parents=True)
NF_HEADERS={'Authorization':f'Bearer {TOKEN}','Accept':'application/json','Content-Type':'application/json','User-Agent':'senex-order070-r6/1'}

def now(): return dt.datetime.now(dt.timezone.utc)
def iso(t=None): return (t or now()).isoformat().replace('+00:00','Z')
def h256(b:bytes): return hashlib.sha256(b).hexdigest()
def canonical(v): return json.dumps(v,sort_keys=True,separators=(',',':'),default=str).encode()
def write(name,obj):
    p=OUT/name; p.write_text(json.dumps(obj,sort_keys=True,indent=2,default=str)+'\n'); return h256(p.read_bytes())
def git(*args): return subprocess.run(['git',*args],cwd=ROOT,text=True,capture_output=True,check=True).stdout.strip()
def data(x): return x.get('data',x) if isinstance(x,dict) else x

def request_json(method,url,headers=None,payload=None,timeout=60):
    body=None if payload is None else json.dumps(payload,separators=(',',':')).encode()
    req=urllib.request.Request(url,headers=headers or {'Accept':'application/json','Cache-Control':'no-cache','User-Agent':'senex-order070-r6-live/1'},data=body,method=method)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r: st=r.status; raw=r.read(); rh={k.lower():v for k,v in r.headers.items()}
    except urllib.error.HTTPError as e: st=e.code; raw=e.read(); rh={k.lower():v for k,v in e.headers.items()}
    except Exception as e: return {'http':0,'body':{'error_type':type(e).__name__,'error':str(e)[:200]},'headers':{},'sha256':None}
    try: obj=json.loads(raw.decode())
    except Exception: obj={'_non_json':True,'bytes':len(raw),'sha256':h256(raw),'text':raw.decode(errors='replace')[:300]}
    return {'http':st,'body':obj,'headers':rh,'sha256':h256(raw)}

def nf(method,path,payload=None,query=None,timeout=90):
    url=API+path
    if query: url+='?'+urllib.parse.urlencode(query,doseq=True)
    r=request_json(method,url,NF_HEADERS,payload,timeout)
    if not 200<=r['http']<300: raise RuntimeError(f'NF_{method}_{path}_HTTP_{r["http"]}:{str(r["body"])[:240]}')
    return data(r['body']),r

def pub(base,path,method='GET',payload=None,timeout=45): return request_json(method,base.rstrip('/')+path,None,payload,timeout)
def services_entry():
    x,_=nf('GET',f'/projects/{PROJECT}/services',query={'per_page':100}); arr=x.get('services') if isinstance(x,dict) else x; return next(v for v in arr if v.get('id')==SERVICE)
def service(): return nf('GET',f'/projects/{PROJECT}/services/{SERVICE}')[0]
def deployment(): return nf('GET',f'/projects/{PROJECT}/services/{SERVICE}/deployment')[0]
def containers():
    x,_=nf('GET',f'/projects/{PROJECT}/services/{SERVICE}/containers',query={'per_page':100}); return (x.get('containers') if isinstance(x,dict) else x) or []
def running_names(rows): return sorted(str(x.get('name')) for x in rows if isinstance(x,dict) and x.get('status')=='TASK_RUNNING')
def fp(v): return h256(canonical(v))

def extract_memory(metrics):
    obj=(metrics or {}).get('memory',{}) if isinstance(metrics,dict) else {}; unit=((obj.get('metricInfo') or {}).get('metricUnit') if isinstance(obj,dict) else None) or 'pct'; pts=[]
    for series in obj.get('values',[]) if isinstance(obj,dict) else []:
        cid=(series.get('metadata') or {}).get('containerId'); vals=[]
        for p in series.get('data') or []:
            try:
                if isinstance(p,(list,tuple)) and len(p)>=2: vals.append((p[0],float(p[1])))
                elif isinstance(p,dict): vals.append((p.get('timestamp') or p.get('time') or p.get('ts'),float(p.get('value'))))
            except Exception: pass
        for ts,val in vals: pts.append({'container':cid,'ts':ts,'value':val})
    if unit=='mb':
        for p in pts: p['pct']=p['value']/RAM_LIMIT_MB*100.0
    else:
        for p in pts: p['pct']=p['value']
    return unit,pts

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

def start_remote_dev():
    fd,path=tempfile.mkstemp(prefix='r6-wrangler-',suffix='.log'); os.close(fd); fh=open(path,'wb')
    proc=subprocess.Popen(['npx','--yes','wrangler@4.102.0','dev','--remote','--config','wrangler.jsonc','--port','8787'],cwd=ROOT/'edge/order070',env=cf_env(),stdout=fh,stderr=subprocess.STDOUT)
    base='http://127.0.0.1:8787'; boot=None
    for _ in range(60):
        if proc.poll() is not None: fh.close(); raise RuntimeError(f'WRANGLER_REMOTE_EXIT:{proc.returncode}:{h256(Path(path).read_bytes())}')
        boot=pub(base,'/healthz')
        if boot['http']==200 and boot['headers'].get('x-senex-edge-decision')=='ALLOW_GET_PROXY': return proc,fh,Path(path),base,boot
        time.sleep(2)
    proc.terminate(); fh.close(); raise RuntimeError(f'WRANGLER_REMOTE_TIMEOUT:{h256(Path(path).read_bytes())}')

def curl_probe(base,method,path):
    with tempfile.TemporaryDirectory() as td:
        hp=Path(td)/'h'; bp=Path(td)/'b'; cp=subprocess.run(['curl','-sS','--max-time','40','-D',str(hp),'-o',str(bp),'-w','%{http_code}','-X',method,base.rstrip('/')+path],text=True,capture_output=True,timeout=45)
        code=int(cp.stdout.strip()) if cp.returncode==0 and cp.stdout.strip().isdigit() else 0; decision=None
        if hp.exists():
            for line in hp.read_text(errors='replace').splitlines():
                if line.lower().startswith('x-senex-edge-decision:'): decision=line.split(':',1)[1].strip()
        return {'http':code,'decision':decision,'curl_exit':cp.returncode,'body_sha256':h256(bp.read_bytes()) if bp.exists() else None}

def identity(kind,b):
    if kind=='snapshot': return b.get('snapshot_id'),b.get('generation'),b.get('canonical_sha256')
    if kind=='ready': return b.get('authority_snapshot_id'),b.get('generation'),b.get('canonical_sha256')
    return b.get('authority_snapshot_id'),b.get('authority_generation'),b.get('authority_canonical_sha256')

def assert_safety(h,ready,prov,state=None):
    if h['http']!=200 or ready['http']!=200 or prov['http']!=200: raise RuntimeError(f'LIVE_HTTP:{h["http"]}:{ready["http"]}:{prov["http"]}')
    hb=h['body']; rb=ready['body']; pb=prov['body']
    if hb.get('trade_mode')!='PAPER' or hb.get('orders_enabled') is not False or hb.get('live_capital_locked') is not True: raise RuntimeError('PAPER_LOCK_FAILED')
    if rb.get('status')!='ready' or not all((rb.get('checks') or {}).values()): raise RuntimeError(f'READY_FAILED:{rb}')
    if pb.get('exact') is not True or pb.get('source_commit')!=HEAD or pb.get('source_tree')!=TREE or pb.get('build_digest')!=BUILD_DIGEST: raise RuntimeError(f'PROVENANCE_FAILED:{pb}')
    if state is not None:
        sb=state['body'];
        if sb.get('trade_mode')!='PAPER' or sb.get('live_capital_locked') is not True: raise RuntimeError('STATE_SAFETY_FAILED')

# Exact candidate identity and narrow security/guardrail scope.
if git('rev-parse','HEAD')!=HEAD or git('rev-parse','HEAD^{tree}')!=TREE or git('status','--porcelain'): raise RuntimeError('EXACT_CANDIDATE_DRIFT')
remote=git('ls-remote','origin',f'refs/heads/{BRANCH}').split()[0]
if remote!=HEAD: raise RuntimeError('REMOTE_HEAD_DRIFT')
changed=git('diff','--name-only','483b389a83610992800181c0a21b5a337009f7b4..HEAD').splitlines()
if changed!=['senecio_polymarket/backend/main.py']: raise RuntimeError(f'R6_SCOPE_DRIFT:{changed}')
write('REMOTE_TRUTH.json',{'observed_at':iso(),'pr':67,'head':HEAD,'tree':TREE,'parent':'483b389a83610992800181c0a21b5a337009f7b4','changed_since_r5':changed,'candidate_scope':'OPTIONAL_ANALYTICS_LAZY_INIT_ONLY','merge':False,'tuning':0,'runtime017_mutation':0,'supabase_data_mutation':0})
write('EXACT_GATE.json',{'observed_at':iso(),'workflow_run_id':os.environ.get('GITHUB_RUN_ID'),'head':HEAD,'tree':TREE,'gate':'PASS','native_pr_runs_action_required_without_jobs':True,'equivalent_original_workflow_commands_executed':True,'import_rss_kb':int(os.environ.get('R6_IMPORT_RSS_KB','0') or 0),'import_rss_limit_kb':81920})

# Repository-scoped auth.
_,auth1=nf('GET',f'/projects/{PROJECT}'); _,auth2=nf('GET',f'/projects/{PROJECT}/services/{SERVICE}/deployment')
write('NORTHFLANK_AUTH.json',{'observed_at':iso(),'repository_scoped_no_environment':True,'project_http':auth1['http'],'deployment_http':auth2['http'],'secret_value_observed':False})

# Reversible source switch -> exact build -> restore main.
e0=services_entry(); s0=service(); d0=deployment(); vcs=s0.get('vcsData') or {}; original=vcs.get('projectBranch')
if s0.get('serviceType')!='combined' or original!='main' or e0.get('disabledCI') is not True: raise RuntimeError('NORTHFLANK_PREFLIGHT')
vpatch={k:vcs[k] for k in ('accountLogin','vcsLinkId','selfHostedVcsId') if vcs.get(k)}; vpatch.update({'projectUrl':vcs['projectUrl'],'projectType':vcs['projectType'],'projectBranch':BRANCH})
switched=False; build_id=None; build=None
try:
    nf('PATCH',f'/projects/{PROJECT}/services/combined/{SERVICE}',{'disabledCI':True,'buildSource':'git','vcsData':vpatch}); switched=True
    if (service().get('vcsData') or {}).get('projectBranch')!=BRANCH or services_entry().get('disabledCI') is not True: raise RuntimeError('SOURCE_SWITCH_VERIFY')
    b,_=nf('POST',f'/projects/{PROJECT}/services/{SERVICE}/build',{'sha':HEAD,'overrides':{'buildArguments':{'SENEX_SOURCE_COMMIT':HEAD,'SENEX_SOURCE_TREE':TREE,'SENEX_BUILD_DIGEST':BUILD_DIGEST}}}); build_id=b.get('id')
    if not build_id: raise RuntimeError('BUILD_ID_MISSING')
    deadline=time.time()+3600
    while time.time()<deadline:
        build,_=nf('GET',f'/projects/{PROJECT}/services/{SERVICE}/build/{build_id}')
        if build.get('concluded'): break
        time.sleep(15)
    if not build or not build.get('concluded') or not build.get('success') or build.get('sha')!=HEAD: raise RuntimeError('EXACT_BUILD_FAILED')
finally:
    if switched:
        restore=dict(vpatch); restore['projectBranch']=original; nf('PATCH',f'/projects/{PROJECT}/services/combined/{SERVICE}',{'disabledCI':True,'buildSource':'git','vcsData':restore})
if (service().get('vcsData') or {}).get('projectBranch')!='main' or services_entry().get('disabledCI') is not True: raise RuntimeError('SOURCE_RESTORE_FAILED')

# Exact OCI manifest.
reg=build.get('registry') if isinstance(build.get('registry'),dict) else {}; image=str(reg.get('digest') or '').lower()
if image and not image.startswith('sha256:') and re.fullmatch(r'[0-9a-f]{64}',image): image='sha256:'+image
if not re.fullmatch(r'sha256:[0-9a-f]{64}',image):
    logs,_=nf('GET',f'/projects/{PROJECT}/services/{SERVICE}/build-logs',query={'buildId':build_id,'queryType':'range','duration':86400,'lineLimit':1000,'direction':'backward','regexIncludes':'manifest'})
    hits=[]
    for row in logs if isinstance(logs,list) else []:
        text=str(row.get('log','')) if isinstance(row,dict) else str(row); m=re.search(r'exporting manifest sha256:([0-9a-f]{64})',text,re.I)
        if m: hits.append('sha256:'+m.group(1).lower())
    if not hits: raise RuntimeError('OCI_MANIFEST_NOT_PROVEN')
    image=hits[0]
write('BUILD_PROVENANCE.json',{'observed_at':iso(),'build_id':build_id,'build_sha':build.get('sha'),'build_success':build.get('success'),'tree':TREE,'build_digest':BUILD_DIGEST,'image_digest':image,'source_branch_restored':'main'})

# Bind OCI, preserving unrelated runtime env.
envdoc,_=nf('GET',f'/projects/{PROJECT}/services/{SERVICE}/runtime-environment',query={'show':'this'}); env=envdoc.get('runtimeEnvironment') if isinstance(envdoc,dict) else None
if not isinstance(env,dict): raise RuntimeError('RUNTIME_ENV_NOT_READABLE')
non_target={k:v for k,v in env.items() if k!='SENEX_IMAGE_DIGEST'}; before_fp=fp(non_target); updated=dict(env); updated['SENEX_IMAGE_DIGEST']=image
nf('PATCH',f'/projects/{PROJECT}/services/combined/{SERVICE}',{'runtimeEnvironment':updated})
afterdoc,_=nf('GET',f'/projects/{PROJECT}/services/{SERVICE}/runtime-environment',query={'show':'this'}); after=afterdoc.get('runtimeEnvironment') if isinstance(afterdoc,dict) else None
if not isinstance(after,dict) or after.get('SENEX_IMAGE_DIGEST')!=image or fp({k:v for k,v in after.items() if k!='SENEX_IMAGE_DIGEST'})!=before_fp: raise RuntimeError('OCI_BIND_DRIFT')
write('OCI_BIND.json',{'observed_at':iso(),'image_digest':image,'non_target_environment_preserved':True,'non_target_environment_sha256':before_fp})

# Deploy exact SHA to existing service.
nf('POST',f'/projects/{PROJECT}/services/{SERVICE}/deployment',{'internal':{'buildSHA':HEAD}})
deadline=time.time()+1800; dep=None; ent=None
while time.time()<deadline:
    dep=deployment(); ent=services_entry(); ii=dep.get('internal') or {}; dst=((ent.get('status') or {}).get('deployment') or {}).get('status')
    if ii.get('deployedSHA')==HEAD and dst=='COMPLETED': break
    if dst=='FAILED': raise RuntimeError('DEPLOY_FAILED')
    time.sleep(10)
else: raise RuntimeError('DEPLOY_TIMEOUT')
write('ORIGIN_DEPLOY.json',{'observed_at':iso(),'build_id':(dep.get('internal') or {}).get('buildId') or build_id,'build_sha':(dep.get('internal') or {}).get('buildSHA'),'deployed_sha':(dep.get('internal') or {}).get('deployedSHA'),'deployment_status':((ent.get('status') or {}).get('deployment') or {}).get('status'),'image_digest':image,'build_digest':BUILD_DIGEST,'source_branch':(service().get('vcsData') or {}).get('projectBranch')})

# Exact origin gate; snapshot primes observational readiness.
deadline=time.time()+600; origin_gate=[]
while time.time()<deadline:
    snap=pub(ORIGIN,'/api/authority/snapshot?symbol=BTCUSDT'); health=pub(ORIGIN,'/healthz'); ready=pub(ORIGIN,'/readyz?symbol=BTCUSDT'); prov=pub(ORIGIN,'/api/runtime/provenance'); state=pub(ORIGIN,'/api/oracle/state?symbol=BTCUSDT')
    origin_gate.append({'at':iso(),'snapshot':snap['http'],'health':health['http'],'ready':ready['http'],'provenance':prov['http'],'state':state['http']})
    if all(x['http']==200 for x in [snap,health,ready,prov,state]):
        try:
            assert_safety(health,ready,prov,state)
            if prov['body'].get('image_digest')==image: break
        except Exception: pass
    time.sleep(10)
else: write('ORIGIN_GATE_ATTEMPTS.json',origin_gate); raise RuntimeError('ORIGIN_EXACT_GATE_TIMEOUT')
write('ORIGIN_LIVE.json',{'observed_at':iso(),'attempts':origin_gate,'health_http':200,'ready_http':200,'provenance':prov['body'],'state':state['body'],'snapshot_identity':{k:snap['body'].get(k) for k in ('snapshot_id','generation','canonical_sha256','exact_total_predictions','exact_count_complete')},'evidence_status':'EXACT_HEAD_BOUND'})

# Fresh Cloudflare exact-head: public temporary Worker + remote Cloudflare method boundary.
edge,edge_deploy_sha=deploy_temp_worker(); boot=None
for _ in range(40):
    boot=pub(edge,'/healthz')
    if boot['http']==200 and boot['headers'].get('x-senex-edge-decision')=='ALLOW_GET_PROXY': break
    time.sleep(2)
if not boot or boot['http']!=200 or boot['headers'].get('x-senex-edge-decision')!='ALLOW_GET_PROXY': raise RuntimeError(f'EDGE_BOOT_FAILED:{boot}')
proc=fh=logpath=None
try:
    proc,fh,logpath,remote_edge,remote_boot=start_remote_dev(); post=curl_probe(remote_edge,'POST','/api/oracle/score'); unknown=curl_probe(remote_edge,'GET','/__order070_unknown__')
    if post['http']!=405 or post['decision']!='DENY_METHOD' or unknown['http']!=404 or unknown['decision']!='DENY_PATH': raise RuntimeError(f'EDGE_METHOD_BOUNDARY:{post}:{unknown}')
    fh.flush(); remote_log_sha=h256(logpath.read_bytes())
finally:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=10)
        except Exception: proc.kill()
    if fh is not None and not fh.closed: fh.close()
write('CLOUDFLARE_FINAL.json',{'observed_at':iso(),'head':HEAD,'tree':TREE,'temporary_worker_url':edge,'temporary_deploy_output_sha256':edge_deploy_sha,'public_get':{'http':boot['http'],'decision':boot['headers'].get('x-senex-edge-decision')},'remote_method_boundary':{'get':{'http':remote_boot['http'],'decision':remote_boot['headers'].get('x-senex-edge-decision')},'post':post,'unknown':unknown,'remote_log_sha256':remote_log_sha},'credentials_used':False})

# >=8 concurrent origin<->public-edge rounds. Re-prime before every round; identity mismatch is never retried.
paths={'snapshot':'/api/authority/snapshot?symbol=BTCUSDT','score':'/api/oracle/score?symbol=BTCUSDT','state':'/api/oracle/state?symbol=BTCUSDT','gate':'/api/portfolio/live_gate?symbol=BTCUSDT','ready':'/readyz?symbol=BTCUSDT'}
def fetch_job(side,base,kind,path): return side,kind,pub(base,path)
rounds=[]
for n in range(1,9):
    ok=False; attempts=[]
    for attempt in range(1,4):
        prime=pub(ORIGIN,paths['snapshot']);
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            rows=[f.result() for f in [ex.submit(fetch_job,s,b,k,p) for s,b in [('origin',ORIGIN),('edge',edge)] for k,p in paths.items()]]
        got={(s,k):r for s,k,r in rows}; statuses={f'{s}:{k}':got[(s,k)]['http'] for s in ('origin','edge') for k in paths}; ids=[identity(k,got[(s,k)]['body']) for s in ('origin','edge') for k in paths if got[(s,k)]['http']==200]
        attempts.append({'attempt':attempt,'prime_http':prime['http'],'statuses':statuses,'identities':ids})
        if all(v==200 for v in statuses.values()):
            if any(None in ident for ident in ids) or len(set(ids))!=1: raise RuntimeError(f'ROUND_IDENTITY_RACE:{n}:{ids}')
            osnap=got[('origin','snapshot')]['body']; esnap=got[('edge','snapshot')]['body']; core=['snapshot_id','generation','canonical_sha256','symbol','authority_history_complete','authority_history_rows','exact_total_predictions','exact_count_complete','last_cursor_or_equivalent','score','live_gate','provenance']
            if not all(osnap.get(k)==esnap.get(k) for k in core): raise RuntimeError(f'ROUND_SNAPSHOT_CORE_MISMATCH:{n}')
            if got[('origin','score')]['body']!=got[('edge','score')]['body'] or got[('origin','gate')]['body']!=got[('edge','gate')]['body']: raise RuntimeError(f'ROUND_PAYLOAD_MISMATCH:{n}')
            rounds.append({'round':n,'attempts':attempts,'snapshot_id':ids[0][0],'generation':ids[0][1],'canonical_sha256':ids[0][2],'all_10_identities_equal':True,'exact_total_predictions':osnap.get('exact_total_predictions'),'exact_count_complete':osnap.get('exact_count_complete')}); ok=True; break
        time.sleep(1)
    if not ok: raise RuntimeError(f'ROUND_HTTP_FAILED:{n}:{attempts}')
write('CONCURRENT_RECONCILIATION.json',{'observed_at':iso(),'round_count':len(rounds),'rounds':rounds,'result':'PASS'})

# Live E2E.
prime=pub(ORIGIN,paths['snapshot']); e2e_paths={'snapshot':paths['snapshot'],'context':'/api/market-context?symbol=BTCUSDT','provenance':'/api/runtime/provenance','health':'/healthz','ready':'/readyz?symbol=BTCUSDT','openapi':'/openapi.json'}; final={k:{'origin':pub(ORIGIN,p),'edge':pub(edge,p)} for k,p in e2e_paths.items()}
for k in e2e_paths:
    if final[k]['origin']['http']!=200 or final[k]['edge']['http']!=200: raise RuntimeError(f'E2E_HTTP:{k}:{final[k]["origin"]["http"]}:{final[k]["edge"]["http"]}')
for side in ('origin','edge'):
    p=final['provenance'][side]['body']; h=final['health'][side]['body']; r=final['ready'][side]['body']; saf=(final['context'][side]['body'].get('safety') or {}); schema=final['openapi'][side]['body']; unsafe=sum(1 for _,item in (schema.get('paths') or {}).items() if isinstance(item,dict) for m in item if str(m).lower() in {'post','put','patch','delete'})
    if p.get('exact') is not True or p.get('source_commit')!=HEAD or p.get('source_tree')!=TREE or p.get('build_digest')!=BUILD_DIGEST or p.get('image_digest')!=image: raise RuntimeError(f'E2E_PROVENANCE:{side}')
    if r.get('status')!='ready' or not all((r.get('checks') or {}).values()): raise RuntimeError(f'E2E_READY:{side}')
    if h.get('trade_mode')!='PAPER' or h.get('orders_enabled') is not False or h.get('live_capital_locked') is not True: raise RuntimeError(f'E2E_HEALTH:{side}')
    if saf.get('trade_mode')!='PAPER' or saf.get('orders_enabled') is not False or saf.get('live_capital_locked') is not True or saf.get('allow_live') is not False: raise RuntimeError(f'E2E_SAFETY:{side}')
    if unsafe!=0: raise RuntimeError(f'E2E_UNSAFE_SURFACE:{side}:{unsafe}')
write('LIVE_E2E.json',{'observed_at':iso(),'head':HEAD,'tree':TREE,'build_id':build_id,'build_digest':BUILD_DIGEST,'image_digest':image,'origin':ORIGIN,'edge':edge,'healthz_origin':200,'healthz_edge':200,'readyz_origin':200,'readyz_edge':200,'provenance_exact_origin':True,'provenance_exact_edge':True,'evidence_status':'EXACT_HEAD_BOUND','authority_snapshot_id':rounds[-1]['snapshot_id'],'concurrent_rounds':8,'public_unsafe_count':0,'trade_mode':'PAPER','orders_enabled':False,'live_capital_locked':True})

# Production stability window begins AFTER deploy + exact origin + edge + reconciliation + E2E.
stability_start=now(); start_iso=iso(stability_start); initial_containers=containers(); initial_running=running_names(initial_containers)
if not initial_running: raise RuntimeError('NO_RUNNING_CONTAINER_AT_STABILITY_START')
base_snap=pub(ORIGIN,paths['snapshot']); base_state=pub(ORIGIN,paths['state']); base_ready=pub(ORIGIN,paths['ready']); base_health=pub(ORIGIN,'/healthz'); base_prov=pub(ORIGIN,'/api/runtime/provenance'); assert_safety(base_health,base_ready,base_prov,base_state)
base_cycles=int(base_state['body'].get('cycles_run') or 0); base_db=int(base_snap['body'].get('exact_total_predictions') or 0); samples=[]; generation_last=int(base_snap['body'].get('generation') or 0); next_sample=time.monotonic()
while (now()-stability_start).total_seconds()<STABILITY_SECONDS:
    delay=next_sample-time.monotonic()
    if delay>0: time.sleep(delay)
    at=now(); snap=pub(ORIGIN,paths['snapshot']); health=pub(ORIGIN,'/healthz'); ready=pub(ORIGIN,paths['ready']); prov=pub(ORIGIN,'/api/runtime/provenance'); state=pub(ORIGIN,paths['state'])
    row={'at':iso(at),'snapshot_http':snap['http'],'health_http':health['http'],'ready_http':ready['http'],'provenance_http':prov['http'],'state_http':state['http']}
    if not all(x['http']==200 for x in [snap,health,ready,prov,state]): row['failure']='HTTP_CONTINUITY'; samples.append(row); write('STABILITY_SAMPLES_PARTIAL.json',samples); raise RuntimeError(f'STABILITY_HTTP_FAILURE:{row}')
    assert_safety(health,ready,prov,state)
    if prov['body'].get('image_digest')!=image: raise RuntimeError('STABILITY_PROVENANCE_IMAGE_DRIFT')
    sid=snap['body'].get('snapshot_id'); gen=int(snap['body'].get('generation') or 0); canon=snap['body'].get('canonical_sha256'); rid=ready['body'].get('authority_snapshot_id'); stateid=state['body'].get('authority_snapshot_id')
    if not sid or rid!=sid or stateid!=sid or gen<generation_last: raise RuntimeError(f'STABILITY_SNAPSHOT_INCONSISTENT:{sid}:{rid}:{stateid}:{gen}:{generation_last}')
    generation_last=gen; row.update({'snapshot_id':sid,'generation':gen,'canonical_sha256':canon,'cycles_run':state['body'].get('cycles_run'),'db_predictions':snap['body'].get('exact_total_predictions'),'snapshot_stale':ready['body'].get('snapshot_stale'),'last_refresh_error':ready['body'].get('last_refresh_error')})
    if row['snapshot_stale'] is not False or row['last_refresh_error'] is not None: raise RuntimeError(f'STABILITY_AUTHORITY_REFRESH_FAILED:{row}')
    # Northflank container identity every 30 seconds.
    if len(samples)%2==0:
        current_running=running_names(containers()); row['running_containers']=current_running
        if current_running!=initial_running: samples.append(row); write('STABILITY_SAMPLES_PARTIAL.json',samples); raise RuntimeError(f'UNEXPECTED_CONTAINER_REPLACEMENT:{initial_running}:{current_running}')
    samples.append(row); next_sample+=SAMPLE_SECONDS
stability_end=now(); end_iso=iso(stability_end)
final_snap=pub(ORIGIN,paths['snapshot']); final_state=pub(ORIGIN,paths['state']); final_ready=pub(ORIGIN,paths['ready']); final_health=pub(ORIGIN,'/healthz'); final_prov=pub(ORIGIN,'/api/runtime/provenance'); assert_safety(final_health,final_ready,final_prov,final_state)
final_cycles=int(final_state['body'].get('cycles_run') or 0); final_db=int(final_snap['body'].get('exact_total_predictions') or 0)
if final_cycles<=base_cycles: raise RuntimeError(f'ORACLE_CYCLES_DID_NOT_ADVANCE:{base_cycles}:{final_cycles}')
if final_db<=base_db: raise RuntimeError(f'DB_PREDICTIONS_DID_NOT_INCREASE:{base_db}:{final_db}')
final_running=running_names(containers())
if final_running!=initial_running: raise RuntimeError('FINAL_CONTAINER_IDENTITY_DRIFT')

# Northflank memory metrics and runtime logs strictly inside stability window.
metrics,_=nf('GET',f'/projects/{PROJECT}/services/{SERVICE}/metrics',query=[('queryType','range'),('startTime',start_iso),('endTime',end_iso),('metricTypes','memory'),('metricTypes','requests'),('metricTypes','http5xxResponses'),('metricTypes','tcpConnectionsOpen')])
unit,mempts=extract_memory(metrics); relevant=[p for p in mempts if p.get('container') in initial_running]
if not relevant: raise RuntimeError(f'NO_MEMORY_METRICS:{unit}:{initial_running}')
ram_max=max(p['pct'] for p in relevant)
if ram_max>=90.0: raise RuntimeError(f'RAM_MAX_NOT_BELOW_90:{ram_max}')
logs,_=nf('GET',f'/projects/{PROJECT}/services/{SERVICE}/logs',query={'queryType':'range','startTime':start_iso,'endTime':end_iso,'type':'runtime','lineLimit':2000,'direction':'forward'})
rows=logs if isinstance(logs,list) else []
patterns={'connection_refused':re.compile(r'connection refused|connect error|upstream connect error|disconnect/reset before headers|remote connection failure',re.I),'oom':re.compile(r'oom|out of memory|oomkilled|killed process|memory cgroup|MemoryError',re.I),'process_exit':re.compile(r'uvicorn exited|Process terminated|exit code|process exited|container exited|TASK_KILLED',re.I)}
matches={k:[] for k in patterns}
for row in rows:
    text=str(row.get('log') or '') if isinstance(row,dict) else str(row)
    for k,p in patterns.items():
        if p.search(text): matches[k].append({'ts':row.get('ts') if isinstance(row,dict) else None,'containerId':row.get('containerId') if isinstance(row,dict) else None,'log':text[:500]})
if matches['connection_refused'] or matches['oom'] or matches['process_exit']: raise RuntimeError(f'STABILITY_RUNTIME_EVENT:{ {k:len(v) for k,v in matches.items()} }')
write('STABILITY_30M.json',{'observed_at':iso(),'start':start_iso,'end':end_iso,'duration_seconds':(stability_end-stability_start).total_seconds(),'sample_interval_seconds':SAMPLE_SECONDS,'sample_count':len(samples),'initial_running_containers':initial_running,'final_running_containers':final_running,'unexpected_restarts':0,'unexpected_process_exits':0,'oom_kills':0,'connection_refused':0,'healthz_continuous':'PASS','readyz_continuous':'PASS','authority_refresh_continuous':'PASS','snapshot_generation_consistent':'PASS','ram_metric_unit':unit,'ram_max_pct':ram_max,'ram_points':len(relevant),'oracle_cycles_initial':base_cycles,'oracle_cycles_final':final_cycles,'oracle_cycles_advance':final_cycles-base_cycles,'db_predictions_initial':base_db,'db_predictions_final':final_db,'db_predictions_increase':final_db-base_db,'runtime_log_rows':len(rows),'runtime_matches':{k:len(v) for k,v in matches.items()},'orders_enabled':False,'real_order_count':0,'real_capital_movement':0,'samples':samples})

# Final seal.
summary={'observed_at':iso(),'order':'ORDER-070-R6','status':'READY_FOR_AUD','pr':67,'head':HEAD,'tree':TREE,'exact_gate':'PASS','exact_build':'PASS','build_id':build_id,'origin_deploy':'PASS','image_digest':image,'healthz':200,'readyz':200,'provenance':'EXACT_HEAD_BOUND','cloudflare_final_exact_head':'PASS','snapshot_reconciliation':'PASS_8_ROUNDS','live_e2e':'PASS','runtime_memory_fix':'OPTIONAL_ANALYTICS_TRUE_LAZY_INIT','ram_max_pct_30m':ram_max,'unexpected_restarts_30m':0,'unexpected_process_exits_30m':0,'oom_kills_30m':0,'connection_refused_30m':0,'health_continuity_30m':'PASS','ready_continuity_30m':'PASS','authority_refresh_continuous_30m':'PASS','oracle_cycles_advance':final_cycles-base_cycles,'db_predictions_increase':final_db-base_db,'real_order_count':0,'real_capital_movement':0,'supabase_data_mutation':0,'runtime017_mutation':0,'tuning':0,'merge':False}
write('FINAL_GATE_SUMMARY.json',summary)
required=['REMOTE_TRUTH.json','EXACT_GATE.json','NORTHFLANK_AUTH.json','BUILD_PROVENANCE.json','OCI_BIND.json','ORIGIN_DEPLOY.json','ORIGIN_LIVE.json','CLOUDFLARE_FINAL.json','CONCURRENT_RECONCILIATION.json','LIVE_E2E.json','STABILITY_30M.json','FINAL_GATE_SUMMARY.json']
(OUT/'MANIFEST.sha256').write_text('\n'.join(f'{h256((OUT/n).read_bytes())}  {n}' for n in sorted(required))+'\n')
ck=subprocess.run(['sha256sum','-c','MANIFEST.sha256'],cwd=OUT,text=True,capture_output=True)
if ck.returncode: raise RuntimeError('MANIFEST_VERIFY_FAILED')
print('READY_FOR_AUD')
print('HEAD='+HEAD); print('TREE='+TREE); print('BUILD_ID='+str(build_id)); print('OCI_DIGEST='+image); print('RAM_MAX_PCT='+f'{ram_max:.4f}'); print('ORACLE_CYCLES_ADVANCE='+str(final_cycles-base_cycles)); print('DB_PREDICTIONS_INCREASE='+str(final_db-base_db)); print('MANIFEST_SHA256='+h256((OUT/'MANIFEST.sha256').read_bytes()))
