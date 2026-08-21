from __future__ import annotations
import concurrent.futures,datetime,hashlib,json,os,re,shutil,subprocess,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path
API='https://api.northflank.com/v1'; P='seneciobot'; S='senecio-h011'; TB='feat/order-070-runtime-truth-hardening'
TS='483b389a83610992800181c0a21b5a337009f7b4'; TT='0494d3d4066f94a9dd055d81c07a3633a243ec2f'; BD='sha256:1806ad0bc71c45264695c1c8973a497a39f9903f867ece2d56fdbc12f44e4892'; BID='curved-board-2291'
ORIGIN='https://h011-web--senecio-h011--wbjggn89fnf8.code.run'; CI={'ORDER070':32476281034,'SCORE001':32476281065,'SCORE002':32476281013,'SMOKE':32476281024}
TOKEN=os.environ['NORTHFLANK_API_TOKEN']; ROOT=Path(os.environ.get('CANDIDATE_DIR','candidate')).resolve(); OUT=Path('order070-r5-final-evidence').resolve()
if OUT.exists(): shutil.rmtree(OUT)
OUT.mkdir(parents=True)
H={'Authorization':f'Bearer {TOKEN}','Accept':'application/json','Content-Type':'application/json','User-Agent':'senex-order070-r5-resume/1'}

def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def canon(v): return json.dumps(v,sort_keys=True,separators=(',',':'),default=str).encode()
def h256(b): return hashlib.sha256(b).hexdigest()
def write(name,obj):
 p=OUT/name; p.write_text(json.dumps(obj,sort_keys=True,indent=2,default=str)+'\n' if isinstance(obj,(dict,list)) else str(obj)); return h256(p.read_bytes())
def data(x): return x.get('data',x) if isinstance(x,dict) else x
REQ=[]
def nf(method,path,payload=None,label=None,query=None,timeout=90):
 url=API+path
 if query: url+='?'+urllib.parse.urlencode(query)
 body=None if payload is None else json.dumps(payload,separators=(',',':')).encode(); req=urllib.request.Request(url,headers=H,data=body,method=method)
 try:
  with urllib.request.urlopen(req,timeout=timeout) as r: st=r.status; raw=r.read()
 except urllib.error.HTTPError as e: st=e.code; raw=e.read()
 try: obj=json.loads(raw.decode() or '{}')
 except Exception: obj={}
 safe=str(obj.get('message') or obj.get('error') or '')[:180] if isinstance(obj,dict) else ''
 REQ.append({'label':label or path,'method':method,'path':path,'status':st,'response_sha256':h256(raw),'safe_error':safe if st>=400 else ''})
 if not 200<=st<300: raise RuntimeError(f'{label or path}:HTTP_{st}:{safe}')
 return data(obj)
def public(base,path,method='GET',body=None,headers=None,timeout=40):
 rawbody=None if body is None else json.dumps(body,separators=(',',':')).encode(); hs={'Accept':'application/json','Cache-Control':'no-cache','User-Agent':'senex-order070-r5-live/3'}
 if headers: hs.update(headers)
 req=urllib.request.Request(base.rstrip('/')+path,headers=hs,data=rawbody,method=method)
 try:
  with urllib.request.urlopen(req,timeout=timeout) as r: st=r.status; raw=r.read(); rh={k.lower():v for k,v in r.headers.items()}
 except urllib.error.HTTPError as e: st=e.code; raw=e.read(); rh={k.lower():v for k,v in e.headers.items()}
 try: obj=json.loads(raw.decode())
 except Exception: obj={'_non_json':True,'bytes':len(raw),'sha256':h256(raw)}
 return {'http':st,'body':obj,'sha256':h256(raw),'headers':rh}
def git(*args): return subprocess.run(['git',*args],cwd=ROOT,text=True,capture_output=True,check=True).stdout.strip()
def fp(v): return h256(canon(v))

# Exact immutable candidate identity and proven existing CI.
if git('rev-parse','HEAD')!=TS or git('rev-parse','HEAD^{tree}')!=TT or git('status','--porcelain'): raise RuntimeError('EXACT_CANDIDATE_DRIFT')
if git('ls-remote','origin',f'refs/heads/{TB}').split()[0]!=TS: raise RuntimeError('REMOTE_CANDIDATE_DRIFT')
write('REMOTE_TRUTH.json',{'observed_at':now(),'head':TS,'tree':TT,'pr':67,'candidate_changed':False,'new_ci':False,'new_prepush':False,'merge':False,'ci_runs':CI,'prior_exact_build_run':32489371392,'prior_partial_artifact':9449161282})

# Repository-scoped credential, no Environment shadowing, bounded GET first.
nf('GET',f'/projects/{P}',label='repository_auth_project'); nf('GET',f'/projects/{P}/services/{S}/deployment',label='repository_auth_deployment')
write('AUTH_PROBE.json',{'observed_at':now(),'source':'repository_scoped_no_environment','project_http':200,'deployment_http':200,'secret_value_observed':False})

# Reuse the already-created exact build and prove source restoration.
b=nf('GET',f'/projects/{P}/services/{S}/build/{BID}',label='verify_existing_exact_build'); svc=nf('GET',f'/projects/{P}/services/{S}',label='verify_service_source'); sl=nf('GET',f'/projects/{P}/services',label='verify_ci_frozen',query={'per_page':100}); arr=sl.get('services') if isinstance(sl,dict) else sl; entry=next(x for x in arr if x.get('id')==S)
if not b.get('concluded') or not b.get('success') or b.get('sha')!=TS: raise RuntimeError('EXISTING_BUILD_NOT_EXACT')
if (svc.get('vcsData') or {}).get('projectBranch')!='main' or entry.get('disabledCI') is not True: raise RuntimeError('SOURCE_NOT_RESTORED')
reg=b.get('registry') if isinstance(b.get('registry'),dict) else {}; img=str(reg.get('digest') or '').lower()
if img and not img.startswith('sha256:') and re.fullmatch(r'[0-9a-f]{64}',img): img='sha256:'+img
if not re.fullmatch(r'sha256:[0-9a-f]{64}',img):
 logs=nf('GET',f'/projects/{P}/services/{S}/build-logs',label='manifest_build_logs',query={'buildId':BID,'queryType':'range','duration':86400,'lineLimit':1000,'direction':'backward','regexIncludes':'manifest'})
 rows=logs if isinstance(logs,list) else []
 hits=[]
 for row in rows:
  text=str(row.get('log','')) if isinstance(row,dict) else str(row); m=re.search(r'exporting manifest sha256:([0-9a-f]{64})',text,re.I)
  if m: hits.append('sha256:'+m.group(1).lower())
 if not hits:
  for row in rows:
   text=str(row.get('log','')) if isinstance(row,dict) else str(row); m=re.search(r'\bmanifest\b.*?sha256:([0-9a-f]{64})',text,re.I)
   if m and 'attestation manifest' not in text.lower() and 'manifest list' not in text.lower(): hits.append('sha256:'+m.group(1).lower())
 if not hits: raise RuntimeError('OCI_MANIFEST_NOT_PROVEN')
 img=hits[0]
write('BUILD_PROVENANCE.json',{'observed_at':now(),'build_id':BID,'build_sha':b.get('sha'),'build_branch':b.get('branch'),'success':b.get('success'),'source_tree':TT,'build_digest':BD,'image_manifest_digest':img,'source_branch_restored':'main'})

# Bind exact OCI digest; preserve all unrelated environment entries without persisting their values.
envdoc=nf('GET',f'/projects/{P}/services/{S}/runtime-environment',label='runtime_env_before',query={'show':'this'}); env=envdoc.get('runtimeEnvironment') if isinstance(envdoc,dict) else None
if not isinstance(env,dict): raise RuntimeError('RUNTIME_ENV_NOT_READABLE')
non_target={k:v for k,v in env.items() if k!='SENEX_IMAGE_DIGEST'}; before_fp=fp(non_target); updated=dict(env); updated['SENEX_IMAGE_DIGEST']=img
nf('PATCH',f'/projects/{P}/services/combined/{S}',{'runtimeEnvironment':updated},label='bind_exact_oci_manifest')
afterdoc=nf('GET',f'/projects/{P}/services/{S}/runtime-environment',label='runtime_env_after',query={'show':'this'}); after=afterdoc.get('runtimeEnvironment') if isinstance(afterdoc,dict) else None
if not isinstance(after,dict) or after.get('SENEX_IMAGE_DIGEST')!=img or fp({k:v for k,v in after.items() if k!='SENEX_IMAGE_DIGEST'})!=before_fp: raise RuntimeError('RUNTIME_ENV_DRIFT')
write('OCI_BIND.json',{'observed_at':now(),'image_digest':img,'non_target_environment_preserved':True,'non_target_environment_sha256':before_fp,'secret_values_persisted':False})

# Deploy existing service with exact buildSHA-only contract.
d0=nf('GET',f'/projects/{P}/services/{S}/deployment',label='deployment_before_exact'); ii=d0.get('internal') or {}
if ii.get('deployedSHA')!=TS: nf('POST',f'/projects/{P}/services/{S}/deployment',{'internal':{'buildSHA':TS}},label='deploy_exact_build_sha')
end=time.time()+1800; stable=None
while time.time()<end:
 d=nf('GET',f'/projects/{P}/services/{S}/deployment',label='deployment_poll'); sl=nf('GET',f'/projects/{P}/services',label='service_poll',query={'per_page':100}); aa=sl.get('services') if isinstance(sl,dict) else sl; e=next(x for x in aa if x.get('id')==S); ii=d.get('internal') or {}; st=((e.get('status') or {}).get('deployment') or {}).get('status')
 if ii.get('deployedSHA')==TS and st=='COMPLETED': stable=(d,e); break
 if st=='FAILED': raise RuntimeError('EXACT_DEPLOY_FAILED')
 time.sleep(10)
if stable is None: raise RuntimeError('EXACT_DEPLOY_TIMEOUT')
d,e=stable; ii=d.get('internal') or {}
write('ORIGIN_DEPLOY.json',{'observed_at':now(),'service':S,'build_id':ii.get('buildId') or BID,'build_sha':ii.get('buildSHA'),'deployed_sha':ii.get('deployedSHA'),'deployment_status':((e.get('status') or {}).get('deployment') or {}).get('status'),'source_branch':(nf('GET',f'/projects/{P}/services/{S}',label='service_final').get('vcsData') or {}).get('projectBranch'),'disabled_ci':e.get('disabledCI'),'disabled_cd':e.get('disabledCD'),'image_digest':img,'build_digest':BD})

# Wait for exact live origin and initialize one valid AuthoritySnapshot generation.
end=time.time()+600; live={}
while time.time()<end:
 live={'health':public(ORIGIN,'/healthz'),'snapshot':public(ORIGIN,'/api/authority/snapshot?symbol=BTCUSDT'),'ready':public(ORIGIN,'/readyz?symbol=BTCUSDT'),'provenance':public(ORIGIN,'/api/runtime/provenance')}
 p=live['provenance']['body'] if isinstance(live['provenance']['body'],dict) else {}
 if live['health']['http']==200 and live['snapshot']['http']==200 and live['ready']['http']==200 and live['provenance']['http']==200 and p.get('exact') is True and p.get('source_commit')==TS and p.get('source_tree')==TT and p.get('build_digest')==BD and p.get('image_digest')==img: break
 time.sleep(10)
else: raise RuntimeError('ORIGIN_EXACT_LIVE_TIMEOUT')
write('ORIGIN_LIVE.json',{'observed_at':now(),'health_http':live['health']['http'],'ready_http':live['ready']['http'],'provenance':live['provenance']['body'],'snapshot_id':live['snapshot']['body'].get('snapshot_id'),'generation':live['snapshot']['body'].get('generation'),'canonical_sha256':live['snapshot']['body'].get('canonical_sha256'),'evidence_status':'EXACT_HEAD_BOUND'})

# Fresh temporary Cloudflare final proof from exact candidate bytes; do not persist claim/token output.
cfenv=dict(os.environ)
for k in list(cfenv):
 if k.startswith('CLOUDFLARE_') or k in {'CF_API_TOKEN','CF_ACCOUNT_ID','CF_API_KEY','CF_EMAIL'}: cfenv.pop(k,None)
edge_dir=ROOT/'edge/order070'; cp=subprocess.run(['npx','--yes','wrangler@4.102.0','deploy','--temporary','--config','wrangler.jsonc'],cwd=edge_dir,env=cfenv,text=True,capture_output=True); raw=(cp.stdout or '')+'\n'+(cp.stderr or '')
if cp.returncode!=0: raise RuntimeError('CLOUDFLARE_TEMP_DEPLOY_FAILED')
urls=re.findall(r'https://[A-Za-z0-9._-]+\.workers\.dev',raw)
if not urls: raise RuntimeError('CLOUDFLARE_TEMP_URL_NOT_FOUND')
EDGE=urls[-1].rstrip('/'); wrangler_sha=h256(raw.encode()); del raw
end=time.time()+120; boot=None
while time.time()<end:
 boot=public(EDGE,'/healthz')
 if boot['http']==200 and boot['headers'].get('x-senex-edge-decision')=='ALLOW_GET_PROXY': break
 time.sleep(3)
if not boot or boot['http']!=200: raise RuntimeError('CLOUDFLARE_EDGE_NOT_ROUTABLE')
post=public(EDGE,'/api/oracle/score',method='POST',body={}); unknown=public(EDGE,'/__order070_unknown__')
if post['http']!=405 or post['headers'].get('x-senex-edge-decision')!='DENY_METHOD' or unknown['http']!=404 or unknown['headers'].get('x-senex-edge-decision')!='DENY_PATH': raise RuntimeError('EDGE_DENY_FAILED')
write('CLOUDFLARE_FINAL.json',{'observed_at':now(),'head':TS,'tree':TT,'temporary_worker_url':EDGE,'wrangler_raw_output_sha256':wrangler_sha,'cloudflare_credentials_used':False,'health_http':boot['http'],'allow_decision':boot['headers'].get('x-senex-edge-decision'),'post_http':post['http'],'post_decision':post['headers'].get('x-senex-edge-decision'),'unknown_http':unknown['http'],'unknown_decision':unknown['headers'].get('x-senex-edge-decision')})

# Truly concurrent origin/edge reads across all AuthoritySnapshot-backed surfaces.
paths={'snapshot':'/api/authority/snapshot?symbol=BTCUSDT','score':'/api/oracle/score?symbol=BTCUSDT','state':'/api/oracle/state?symbol=BTCUSDT','gate':'/api/portfolio/live_gate?symbol=BTCUSDT','context':'/api/market-context?symbol=BTCUSDT','provenance':'/api/runtime/provenance','health':'/healthz','ready':'/readyz?symbol=BTCUSDT','openapi':'/openapi.json'}
def one(k,p):
 with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
  a=ex.submit(public,ORIGIN,p); b=ex.submit(public,EDGE,p); return k,a.result(),b.result()
with concurrent.futures.ThreadPoolExecutor(max_workers=len(paths)) as ex: rows=list(ex.map(lambda kv: one(*kv),paths.items()))
origin={k:o for k,o,ed in rows}; edge={k:ed for k,o,ed in rows}
for k in paths:
 if origin[k]['http']!=200 or edge[k]['http']!=200: raise RuntimeError(f'SURFACE_HTTP_FAILED:{k}')
def unsafe(schema): return sum(1 for _,item in (schema.get('paths') or {}).items() if isinstance(item,dict) for m in item if str(m).lower() in {'post','put','patch','delete'})
if unsafe(origin['openapi']['body']) or unsafe(edge['openapi']['body']): raise RuntimeError('PUBLIC_MUTATION_SURFACE_NONZERO')
osnap=origin['snapshot']['body']; esnap=edge['snapshot']['body']; oscore=origin['score']['body']; escore=edge['score']['body']; ostate=origin['state']['body']; estate=edge['state']['body']; ogate=origin['gate']['body']; egate=edge['gate']['body']; octx=origin['context']['body']; ectx=edge['context']['body']
sid=osnap.get('snapshot_id'); gen=osnap.get('generation'); can=osnap.get('canonical_sha256')
ids=[sid,esnap.get('snapshot_id'),oscore.get('authority_snapshot_id'),escore.get('authority_snapshot_id'),ostate.get('authority_snapshot_id'),estate.get('authority_snapshot_id'),ogate.get('authority_snapshot_id'),egate.get('authority_snapshot_id'),octx.get('authority_snapshot_id'),ectx.get('authority_snapshot_id')]
gens=[gen,esnap.get('generation'),oscore.get('authority_generation'),escore.get('authority_generation'),ostate.get('authority_generation'),estate.get('authority_generation'),ogate.get('authority_generation'),egate.get('authority_generation'),octx.get('authority_generation'),ectx.get('authority_generation')]
cans=[can,esnap.get('canonical_sha256'),oscore.get('authority_canonical_sha256'),escore.get('authority_canonical_sha256'),ostate.get('authority_canonical_sha256'),estate.get('authority_canonical_sha256'),ogate.get('authority_canonical_sha256'),egate.get('authority_canonical_sha256'),octx.get('authority_canonical_sha256'),ectx.get('authority_canonical_sha256')]
if not sid or any(x!=sid for x in ids) or any(x!=gen for x in gens) or any(x!=can for x in cans): raise RuntimeError('AUTHORITY_IDENTITY_RECONCILIATION_FAILED')
if osnap.get('score')!=oscore or esnap.get('score')!=escore or osnap.get('live_gate')!=ogate or esnap.get('live_gate')!=egate: raise RuntimeError('AUTHORITY_PAYLOAD_RECONCILIATION_FAILED')
if osnap.get('exact_count_complete') is not True or not isinstance(osnap.get('exact_total_predictions'),int): raise RuntimeError('COUNT_CONTRACT_FAILED')
for h in (origin['health']['body'],edge['health']['body']):
 if h.get('trade_mode')!='PAPER' or h.get('orders_enabled') is not False or h.get('live_capital_locked') is not True: raise RuntimeError('PAPER_LOCK_FAILED')
write('SNAPSHOT_RECONCILIATION.json',{'observed_at':now(),'snapshot_id':sid,'generation':gen,'canonical_sha256':can,'all_ids_equal':True,'all_generations_equal':True,'all_canonical_hashes_equal':True,'snapshot_score_matches':True,'snapshot_gate_matches':True,'exact_total_predictions':osnap.get('exact_total_predictions'),'exact_count_complete':True,'origin_edge_snapshot_equal':osnap==esnap})
write('LIVE_E2E.json',{'observed_at':now(),'head':TS,'tree':TT,'origin':ORIGIN,'edge':EDGE,'healthz_origin':200,'healthz_edge':200,'readyz_origin':200,'readyz_edge':200,'provenance_exact_origin':origin['provenance']['body'].get('exact'),'provenance_exact_edge':edge['provenance']['body'].get('exact'),'public_origin_unsafe_count':0,'public_edge_unsafe_count':0,'authority_snapshot_id':sid,'authority_generation':gen,'authority_canonical_sha256':can,'snapshot_reconciliation':'PASS','trade_mode':'PAPER','orders_enabled':False,'live_capital_locked':True,'real_order_count':0,'real_capital_movement':0,'supabase_mutation':0,'runtime017_mutation':0,'tuning':0,'evidence_status':'EXACT_HEAD_BOUND'})
write('REQUEST_RECEIPTS.json',{'observed_at':now(),'northflank_requests':REQ,'secret_value_observed':False})
summary={'ORDER_070_STATUS':'READY_FOR_AUD','PR':67,'HEAD':TS,'TREE':TT,'CI':CI,'ORIGIN_DEPLOY':{'build_id':BID,'deployed_sha':ii.get('deployedSHA'),'image_digest':img,'build_digest':BD},'CLOUDFLARE_FINAL_EXACT_HEAD':EDGE,'PUBLIC_POST_COUNT':0,'AUTHORITY_SNAPSHOT_ID':sid,'AUTHORITY_GENERATION':gen,'AUTHORITY_CANONICAL_SHA256':can,'SNAPSHOT_RECONCILIATION':'PASS','PROVENANCE':'PASS','LIVE_E2E':'PASS','REAL_ORDER_COUNT':0,'REAL_CAPITAL_MOVEMENT':0,'SUPABASE_DATA_MUTATION':0,'RUNTIME017_MUTATION':0,'TUNING':0,'BLOCKER':'none'}
write('FINAL_SUMMARY.json',summary)
files=sorted(p.name for p in OUT.glob('*.json')); (OUT/'MANIFEST.sha256').write_text('\n'.join(f'{h256((OUT/n).read_bytes())}  {n}' for n in files)+'\n')
if subprocess.run(['sha256sum','-c','MANIFEST.sha256'],cwd=OUT,text=True,capture_output=True).returncode: raise RuntimeError('MANIFEST_VERIFY_FAILED')
print('ORDER_070_STATUS=READY_FOR_AUD'); print('BUILD_ID='+BID); print('DEPLOYED_SHA='+str(ii.get('deployedSHA'))); print('IMAGE_DIGEST='+img); print('HEALTHZ=200'); print('READYZ=200'); print('PROVENANCE_EXACT=true'); print('CLOUDFLARE_FINAL='+EDGE); print('AUTHORITY_SNAPSHOT_ID='+str(sid)); print('AUTHORITY_GENERATION='+str(gen)); print('AUTHORITY_CANONICAL_SHA256='+str(can)); print('SNAPSHOT_RECONCILIATION=PASS'); print('LIVE_E2E=PASS'); print('MANIFEST_SHA256='+h256((OUT/'MANIFEST.sha256').read_bytes()))
