from __future__ import annotations
import concurrent.futures, datetime, hashlib, json, os, re, shutil, subprocess, tempfile, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

API='https://api.northflank.com/v1'; P='seneciobot'; S='senecio-h011'; BID='curved-board-2291'
TS='483b389a83610992800181c0a21b5a337009f7b4'; TT='0494d3d4066f94a9dd055d81c07a3633a243ec2f'
BD='sha256:1806ad0bc71c45264695c1c8973a497a39f9903f867ece2d56fdbc12f44e4892'
IMG='sha256:7efb105084053fecf45bac799728424675eb66606735a60f80c0d3c5ff4ba7f8'
ORIGIN='https://h011-web--senecio-h011--wbjggn89fnf8.code.run'
CI={'ORDER070':32476281034,'SCORE001':32476281065,'SCORE002':32476281013,'SMOKE':32476281024}
TOKEN=os.environ['NORTHFLANK_API_TOKEN']; ROOT=Path(os.environ.get('CANDIDATE_DIR','candidate')).resolve(); OUT=Path('order070-r5-final-evidence').resolve()
if OUT.exists(): shutil.rmtree(OUT)
OUT.mkdir(parents=True)
H={'Authorization':f'Bearer {TOKEN}','Accept':'application/json','User-Agent':'senex-order070-r5-final/1'}

def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def canon(v): return json.dumps(v,sort_keys=True,separators=(',',':'),default=str).encode()
def h256(b): return hashlib.sha256(b).hexdigest()
def write(name,obj):
    p=OUT/name; p.write_text(json.dumps(obj,sort_keys=True,indent=2,default=str)+'\n'); return h256(p.read_bytes())
def data(x): return x.get('data',x) if isinstance(x,dict) else x

def nf(path,query=None):
    url=API+path
    if query: url+='?'+urllib.parse.urlencode(query)
    req=urllib.request.Request(url,headers=H,method='GET')
    try:
        with urllib.request.urlopen(req,timeout=45) as r: st=r.status; raw=r.read()
    except urllib.error.HTTPError as e: st=e.code; raw=e.read()
    try: obj=json.loads(raw.decode() or '{}')
    except Exception: obj={}
    if not 200<=st<300: raise RuntimeError(f'NORTHFLANK_GET_FAILED:{path}:HTTP_{st}')
    return data(obj),st,h256(raw)

def public(base,path,timeout=40):
    req=urllib.request.Request(base.rstrip('/')+path,headers={'Accept':'application/json','Cache-Control':'no-cache','User-Agent':'senex-order070-r5-final/1'},method='GET')
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r: st=r.status; raw=r.read(); hdr={k.lower():v for k,v in r.headers.items()}
    except urllib.error.HTTPError as e: st=e.code; raw=e.read(); hdr={k.lower():v for k,v in e.headers.items()}
    try: obj=json.loads(raw.decode())
    except Exception: obj={'_non_json':True,'bytes':len(raw),'sha256':h256(raw)}
    return {'http':st,'body':obj,'headers':hdr,'sha256':h256(raw)}

def git(*args): return subprocess.run(['git',*args],cwd=ROOT,text=True,capture_output=True,check=True).stdout.strip()
def unsafe_count(schema): return sum(1 for _,item in (schema.get('paths') or {}).items() if isinstance(item,dict) for m in item if str(m).lower() in {'post','put','patch','delete'})

# Remote/exactness truth, no candidate mutation and no CI rerun.
if git('rev-parse','HEAD')!=TS or git('rev-parse','HEAD^{tree}')!=TT or git('status','--porcelain'): raise RuntimeError('EXACT_CANDIDATE_DRIFT')
remote=git('ls-remote','origin','refs/heads/feat/order-070-runtime-truth-hardening').split()[0]
if remote!=TS: raise RuntimeError('REMOTE_CANDIDATE_DRIFT')
write('REMOTE_TRUTH.json',{'observed_at':now(),'pr':67,'head':TS,'tree':TT,'remote_head':remote,'candidate_changed':False,'new_ci':False,'merge':False,'ci_runs':CI})

# Repository-scoped Northflank token: bounded GET auth probe, no Environment scope.
project,pst,ph=nf(f'/projects/{P}'); dep,dst,dh=nf(f'/projects/{P}/services/{S}/deployment')
write('AUTH_PROBE.json',{'observed_at':now(),'source':'repository_scoped_no_environment','project_http':pst,'deployment_http':dst,'secret_value_observed':False,'project_response_sha256':ph,'deployment_response_sha256':dh})

# Exact build/deployment readback only; no Northflank mutation in this closing run.
b,_,_=nf(f'/projects/{P}/services/{S}/build/{BID}'); svc,_,_=nf(f'/projects/{P}/services/{S}'); services,_,_=nf(f'/projects/{P}/services',{'per_page':100})
arr=services.get('services') if isinstance(services,dict) else services; entry=next(x for x in arr if x.get('id')==S); ii=dep.get('internal') or {}
if not b.get('concluded') or not b.get('success') or b.get('sha')!=TS: raise RuntimeError('EXACT_BUILD_READBACK_FAILED')
if ii.get('deployedSHA')!=TS or ((entry.get('status') or {}).get('deployment') or {}).get('status')!='COMPLETED': raise RuntimeError('EXACT_DEPLOY_READBACK_FAILED')
if (svc.get('vcsData') or {}).get('projectBranch')!='main' or entry.get('disabledCI') is not True: raise RuntimeError('SOURCE_CONTROL_READBACK_FAILED')
write('BUILD_DEPLOY_PROVENANCE.json',{'observed_at':now(),'build_id':BID,'build_sha':b.get('sha'),'build_branch':b.get('branch'),'build_success':b.get('success'),'target_tree':TT,'build_digest':BD,'image_manifest_digest':IMG,'deployed_sha':ii.get('deployedSHA'),'deployment_build_sha':ii.get('buildSHA'),'deployment_build_id':ii.get('buildId'),'deployment_status':((entry.get('status') or {}).get('deployment') or {}).get('status'),'source_branch':(svc.get('vcsData') or {}).get('projectBranch'),'disabled_ci':entry.get('disabledCI'),'disabled_cd':entry.get('disabledCD'),'northflank_mutations_this_run':0})

# Exact origin live gate.
health=public(ORIGIN,'/healthz'); ready=public(ORIGIN,'/readyz?symbol=BTCUSDT'); prov=public(ORIGIN,'/api/runtime/provenance'); snap0=public(ORIGIN,'/api/authority/snapshot?symbol=BTCUSDT')
if health['http']!=200 or ready['http']!=200 or prov['http']!=200 or snap0['http']!=200: raise RuntimeError('ORIGIN_LIVE_HTTP_FAILED')
p=prov['body']; r=ready['body']; s0=snap0['body']
if p.get('exact') is not True or p.get('source_commit')!=TS or p.get('source_tree')!=TT or p.get('build_digest')!=BD or p.get('image_digest')!=IMG: raise RuntimeError('ORIGIN_PROVENANCE_NOT_EXACT')
if r.get('status')!='ready' or not all((r.get('checks') or {}).values()): raise RuntimeError('ORIGIN_NOT_READY')
if health['body'].get('trade_mode')!='PAPER' or health['body'].get('orders_enabled') is not False or health['body'].get('live_capital_locked') is not True: raise RuntimeError('ORIGIN_PAPER_LOCK_FAILED')
write('ORIGIN_LIVE.json',{'observed_at':now(),'health_http':health['http'],'ready_http':ready['http'],'provenance_http':prov['http'],'provenance':p,'authority_snapshot_id':s0.get('snapshot_id'),'authority_generation':s0.get('generation'),'authority_canonical_sha256':s0.get('canonical_sha256'),'evidence_status':'EXACT_HEAD_BOUND'})

# Fresh Cloudflare final proof from the exact candidate. Claim/token output is never persisted or printed.
cfenv=dict(os.environ)
for k in list(cfenv):
    if k.startswith('CLOUDFLARE_') or k in {'CF_API_TOKEN','CF_ACCOUNT_ID','CF_API_KEY','CF_EMAIL'}: cfenv.pop(k,None)
edge_dir=ROOT/'edge/order070'
cp=subprocess.run(['npx','--yes','wrangler@4.102.0','deploy','--temporary','--config','wrangler.jsonc'],cwd=edge_dir,env=cfenv,text=True,capture_output=True)
raw=(cp.stdout or '')+'\n'+(cp.stderr or '')
if cp.returncode!=0: raise RuntimeError('CLOUDFLARE_TEMP_DEPLOY_FAILED')
urls=re.findall(r'https://[A-Za-z0-9._-]+\.workers\.dev',raw)
if not urls: raise RuntimeError('CLOUDFLARE_TEMP_URL_NOT_FOUND')
EDGE=urls[-1].rstrip('/'); deploy_output_sha=h256(raw.encode()); del raw

def curl_probe(method,path):
    with tempfile.TemporaryDirectory() as td:
        hp=Path(td)/'headers'; bp=Path(td)/'body'
        cmd=['curl','-sS','-D',str(hp),'-o',str(bp),'-w','%{http_code}','-X',method,EDGE+path]
        cp2=subprocess.run(cmd,text=True,capture_output=True,timeout=40)
        if cp2.returncode: return {'http':0,'decision':None,'curl_exit':cp2.returncode}
        try: code=int(cp2.stdout.strip())
        except Exception: code=0
        decision=None
        for line in hp.read_text(errors='replace').splitlines():
            if line.lower().startswith('x-senex-edge-decision:'): decision=line.split(':',1)[1].strip()
        return {'http':code,'decision':decision,'body_sha256':h256(bp.read_bytes()),'curl_exit':0}

boot=None
for _ in range(40):
    boot=curl_probe('GET','/healthz')
    if boot['http']==200 and boot['decision']=='ALLOW_GET_PROXY': break
    time.sleep(2)
if not boot or boot['http']!=200 or boot['decision']!='ALLOW_GET_PROXY': raise RuntimeError(f'EDGE_BOOT_FAILED:{boot}')
post=curl_probe('POST','/api/oracle/score'); unknown=curl_probe('GET','/__order070_unknown__')
write('CLOUDFLARE_FINAL.json',{'observed_at':now(),'head':TS,'tree':TT,'temporary_worker_url':EDGE,'wrangler_output_sha256':deploy_output_sha,'cloudflare_credentials_used':False,'allow_get':boot,'deny_post':post,'deny_unknown_path':unknown})
if post['http']!=405 or post['decision']!='DENY_METHOD' or unknown['http']!=404 or unknown['decision']!='DENY_PATH': raise RuntimeError(f'EDGE_DENY_FAILED:post={post}:unknown={unknown}')

# 8 truly concurrent reconciliation rounds. Compare immutable authority generation, not time-varying observability metadata.
authority_paths={'snapshot':'/api/authority/snapshot?symbol=BTCUSDT','score':'/api/oracle/score?symbol=BTCUSDT','state':'/api/oracle/state?symbol=BTCUSDT','gate':'/api/portfolio/live_gate?symbol=BTCUSDT','ready':'/readyz?symbol=BTCUSDT'}
def surface_id(kind,body):
    if kind=='snapshot': return (body.get('snapshot_id'),body.get('generation'),body.get('canonical_sha256'))
    if kind=='ready': return (body.get('authority_snapshot_id'),body.get('generation'),body.get('canonical_sha256'))
    return (body.get('authority_snapshot_id'),body.get('authority_generation'),body.get('authority_canonical_sha256'))
def fetch_job(side,base,kind,path): return side,kind,public(base,path)
rounds=[]
for n in range(1,9):
    jobs=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        for side,base in (('origin',ORIGIN),('edge',EDGE)):
            for kind,path in authority_paths.items(): jobs.append(ex.submit(fetch_job,side,base,kind,path))
        rows=[f.result() for f in jobs]
    got={(side,kind):res for side,kind,res in rows}
    if any(got[(side,kind)]['http']!=200 for side in ('origin','edge') for kind in authority_paths): raise RuntimeError(f'ROUND_HTTP_FAILED:{n}')
    identities=[surface_id(kind,got[(side,kind)]['body']) for side in ('origin','edge') for kind in authority_paths]
    if any(x[0] is None or x[1] is None or x[2] is None for x in identities): raise RuntimeError(f'ROUND_IDENTITY_MISSING:{n}:{identities}')
    if len(set(identities))!=1: raise RuntimeError(f'ROUND_IDENTITY_MISMATCH:{n}:{identities}')
    osnap=got[('origin','snapshot')]['body']; esnap=got[('edge','snapshot')]['body']; oscore=got[('origin','score')]['body']; escore=got[('edge','score')]['body']; ogate=got[('origin','gate')]['body']; egate=got[('edge','gate')]['body']; ostate=got[('origin','state')]['body']; estate=got[('edge','state')]['body']
    core_keys=['snapshot_id','generation','canonical_sha256','symbol','authority_history_complete','authority_history_rows','exact_total_predictions','exact_count_complete','last_cursor_or_equivalent','score','live_gate','provenance']
    snap_core_equal=all(osnap.get(k)==esnap.get(k) for k in core_keys)
    if not snap_core_equal or oscore!=escore or ogate!=egate: raise RuntimeError(f'ROUND_PAYLOAD_RECONCILIATION_FAILED:{n}')
    if ostate.get('exact_total_predictions')!=osnap.get('exact_total_predictions') or estate.get('exact_total_predictions')!=osnap.get('exact_total_predictions'): raise RuntimeError(f'ROUND_COUNT_RECONCILIATION_FAILED:{n}')
    identity=identities[0]
    rounds.append({'round':n,'snapshot_id':identity[0],'generation':identity[1],'canonical_sha256':identity[2],'all_10_surface_identities_equal':True,'origin_edge_snapshot_core_equal':snap_core_equal,'origin_edge_score_equal':True,'origin_edge_live_gate_equal':True,'exact_total_predictions':osnap.get('exact_total_predictions'),'exact_count_complete':osnap.get('exact_count_complete')})
write('CONCURRENT_RECONCILIATION.json',{'observed_at':now(),'round_count':len(rounds),'rounds':rounds,'result':'PASS'})

# Final cross-surface E2E, public mutation surface, admin unmounted and PAPER locks.
paths={'snapshot':'/api/authority/snapshot?symbol=BTCUSDT','score':'/api/oracle/score?symbol=BTCUSDT','state':'/api/oracle/state?symbol=BTCUSDT','gate':'/api/portfolio/live_gate?symbol=BTCUSDT','context':'/api/market-context?symbol=BTCUSDT','provenance':'/api/runtime/provenance','health':'/healthz','ready':'/readyz?symbol=BTCUSDT','openapi':'/openapi.json'}
final={k:{'origin':public(ORIGIN,path),'edge':public(EDGE,path)} for k,path in paths.items()}
for k in paths:
    if final[k]['origin']['http']!=200 or final[k]['edge']['http']!=200: raise RuntimeError(f'FINAL_SURFACE_HTTP_FAILED:{k}')
for side in ('origin','edge'):
    pv=final['provenance'][side]['body']; rd=final['ready'][side]['body']; hh=final['health'][side]['body']; ctx=final['context'][side]['body']; saf=ctx.get('safety') or {}
    if pv.get('exact') is not True or pv.get('source_commit')!=TS or pv.get('source_tree')!=TT or pv.get('build_digest')!=BD or pv.get('image_digest')!=IMG: raise RuntimeError(f'FINAL_PROVENANCE_FAILED:{side}')
    if rd.get('status')!='ready' or not all((rd.get('checks') or {}).values()): raise RuntimeError(f'FINAL_READY_FAILED:{side}')
    if hh.get('trade_mode')!='PAPER' or hh.get('orders_enabled') is not False or hh.get('live_capital_locked') is not True: raise RuntimeError(f'FINAL_HEALTH_LOCK_FAILED:{side}')
    if saf.get('trade_mode')!='PAPER' or saf.get('orders_enabled') is not False or saf.get('live_capital_locked') is not True or saf.get('allow_live') is not False: raise RuntimeError(f'FINAL_CONTEXT_LOCK_FAILED:{side}')
    if unsafe_count(final['openapi'][side]['body'])!=0: raise RuntimeError(f'PUBLIC_MUTATION_SURFACE_NONZERO:{side}')
admin_root=public(ORIGIN,'/admin'); admin_api=public(ORIGIN,'/api/admin')
if admin_root['http']!=404 or admin_api['http']!=404: raise RuntimeError('ADMIN_PUBLICLY_MOUNTED')
sid=rounds[-1]['snapshot_id']
write('LIVE_E2E.json',{'observed_at':now(),'head':TS,'tree':TT,'build_id':BID,'build_digest':BD,'image_digest':IMG,'origin':ORIGIN,'edge':EDGE,'healthz_origin':200,'healthz_edge':200,'readyz_origin':200,'readyz_edge':200,'provenance_exact_origin':True,'provenance_exact_edge':True,'evidence_status':'EXACT_HEAD_BOUND','authority_snapshot_id':sid,'concurrent_reconciliation_rounds':8,'public_origin_unsafe_count':0,'public_edge_unsafe_count':0,'admin_publicly_unmounted':True,'trade_mode':'PAPER','orders_enabled':False,'live_capital_locked':True,'real_order_count':0,'real_capital_movement':0,'supabase_data_mutation':0,'runtime017_mutation':0,'tuning':0})

# Seal artifact.
summary={'observed_at':now(),'order':'ORDER-070-R5','status':'READY_FOR_AUD','pr':67,'head':TS,'tree':TT,'repository_scoped_auth':'PASS','exact_build':'PASS','exact_origin_deploy':'PASS','oci_source_provenance':'PASS','health_ready':'PASS','cloudflare_final':'PASS','concurrent_origin_edge_reconciliation':'PASS','live_e2e':'PASS','authority_snapshot_id':sid,'public_post_count':0,'admin_publicly_unmounted':'PASS','candidate_changed':False,'new_ci':False,'merge':False,'tuning':0,'runtime017_mutation':0,'supabase_data_mutation':0,'real_order_count':0,'real_capital_movement':0}
write('FINAL_GATE_SUMMARY.json',summary)
required=['REMOTE_TRUTH.json','AUTH_PROBE.json','BUILD_DEPLOY_PROVENANCE.json','ORIGIN_LIVE.json','CLOUDFLARE_FINAL.json','CONCURRENT_RECONCILIATION.json','LIVE_E2E.json','FINAL_GATE_SUMMARY.json']
lines=[]
for name in sorted(required): lines.append(f'{h256((OUT/name).read_bytes())}  {name}')
(OUT/'MANIFEST.sha256').write_text('\n'.join(lines)+'\n')
check=subprocess.run(['sha256sum','-c','MANIFEST.sha256'],cwd=OUT,text=True,capture_output=True)
if check.returncode!=0: raise RuntimeError('MANIFEST_VERIFY_FAILED')
print('ORDER_070_R5_STATUS=READY_FOR_AUD')
print('HEAD='+TS); print('TREE='+TT); print('BUILD_ID='+BID); print('IMAGE_DIGEST='+IMG); print('HEALTHZ=200'); print('READYZ=200'); print('CLOUDFLARE_FINAL=PASS'); print('CONCURRENT_RECONCILIATION=PASS'); print('AUTHORITY_SNAPSHOT_ID='+sid); print('LIVE_E2E=PASS'); print('MANIFEST_SHA256='+h256((OUT/'MANIFEST.sha256').read_bytes()))
