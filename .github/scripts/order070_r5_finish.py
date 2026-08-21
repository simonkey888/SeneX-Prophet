from __future__ import annotations
import concurrent.futures,datetime,hashlib,json,os,re,shutil,subprocess,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path

API='https://api.northflank.com/v1'
P='seneciobot'; S='senecio-h011'; TB='feat/order-070-runtime-truth-hardening'
TS='483b389a83610992800181c0a21b5a337009f7b4'
TT='0494d3d4066f94a9dd055d81c07a3633a243ec2f'
BD='sha256:1806ad0bc71c45264695c1c8973a497a39f9903f867ece2d56fdbc12f44e4892'
ORIGIN='https://h011-web--senecio-h011--wbjggn89fnf8.code.run'
CI={'ORDER070':32476281034,'SCORE001':32476281065,'SCORE002':32476281013,'SMOKE':32476281024}
TOKEN=os.environ['NORTHFLANK_API_TOKEN']
ROOT=Path(os.environ.get('CANDIDATE_DIR','candidate')).resolve()
OUT=Path('order070-r5-final-evidence').resolve()
if OUT.exists(): shutil.rmtree(OUT)
OUT.mkdir(parents=True)
H={'Authorization':f'Bearer {TOKEN}','Accept':'application/json','Content-Type':'application/json','User-Agent':'senex-order070-r5/2'}
rec={'order':'ORDER-070-R5','target_sha':TS,'target_tree':TT,'build_digest':BD,'candidate_changed':False,'new_ci':False,'new_prepush':False,'merge':False,'secret_value_observed':False,'runtime017_mutation':0,'supabase_mutation':0,'real_order_count':0,'real_capital_movement':0,'threshold_tuning':0,'model_tuning':0,'requests':[],'mutations':[],'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}

def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def canon(v): return json.dumps(v,sort_keys=True,separators=(',',':'),default=str).encode()
def h256(b:bytes): return hashlib.sha256(b).hexdigest()
def write(name,obj):
 p=OUT/name
 if isinstance(obj,(dict,list)): p.write_text(json.dumps(obj,sort_keys=True,indent=2,default=str)+'\n')
 else: p.write_text(str(obj))
 return h256(p.read_bytes())
def save_partial(): write('SESSION.json',rec)
def data(x): return x.get('data',x) if isinstance(x,dict) else x

def nf(method,path,payload=None,label=None,query=None,timeout=90):
 url=API+path
 if query: url+='?'+urllib.parse.urlencode(query)
 body=None if payload is None else json.dumps(payload,separators=(',',':')).encode()
 req=urllib.request.Request(url,headers=H,data=body,method=method)
 try:
  with urllib.request.urlopen(req,timeout=timeout) as r: status=r.status; raw=r.read()
 except urllib.error.HTTPError as e: status=e.code; raw=e.read()
 try: obj=json.loads(raw.decode() or '{}')
 except Exception: obj={}
 safe=str(obj.get('message') or obj.get('error') or '')[:200] if isinstance(obj,dict) else ''
 rec['requests'].append({'label':label or path,'method':method,'path':path,'status':status,'response_sha256':h256(raw),'safe_error':safe if status>=400 else ''}); save_partial()
 if not 200<=status<300: raise RuntimeError(f'{label or path}:HTTP_{status}:{safe}')
 return data(obj)

def service(label): return nf('GET',f'/projects/{P}/services/{S}',label=label)
def services(label):
 x=nf('GET',f'/projects/{P}/services',label=label,query={'per_page':100}); a=x.get('services') if isinstance(x,dict) else x
 return next(v for v in a if v.get('id')==S)
def deployment(label): return nf('GET',f'/projects/{P}/services/{S}/deployment',label=label)

def public(base,path,method='GET',body=None,headers=None,timeout=40):
 rawbody=None if body is None else json.dumps(body,separators=(',',':')).encode()
 hs={'Accept':'application/json','Cache-Control':'no-cache','User-Agent':'senex-order070-r5-live/2'}
 if headers: hs.update(headers)
 req=urllib.request.Request(base.rstrip('/')+path,headers=hs,data=rawbody,method=method)
 try:
  with urllib.request.urlopen(req,timeout=timeout) as r: st=r.status; raw=r.read(); rh={k.lower():v for k,v in r.headers.items()}
 except urllib.error.HTTPError as e: st=e.code; raw=e.read(); rh={k.lower():v for k,v in e.headers.items()}
 try: obj=json.loads(raw.decode())
 except Exception: obj={'_non_json':True,'bytes':len(raw),'sha256':h256(raw)}
 return {'http':st,'body':obj,'sha256':h256(raw),'headers':rh}

def fp(v): return h256(canon(v))

def git(*args):
 cp=subprocess.run(['git',*args],cwd=ROOT,text=True,capture_output=True,check=True)
 return cp.stdout.strip()

# Exact candidate and remote lane identity. No candidate writes.
if git('rev-parse','HEAD')!=TS or git('rev-parse','HEAD^{tree}')!=TT: raise RuntimeError('LOCAL_EXACT_IDENTITY_FAILED')
if git('status','--porcelain'): raise RuntimeError('CANDIDATE_WORKTREE_DIRTY')
remote=git('ls-remote','origin',f'refs/heads/{TB}').split()[0]
if remote!=TS: raise RuntimeError('REMOTE_HEAD_DRIFT')
write('REMOTE_TRUTH.json',{'observed_at':now(),'head':TS,'tree':TT,'remote_lane':remote,'ci_runs':CI,'candidate_changed':False,'merge':False})

# Bounded repository-scoped auth probe before mutation.
auth_project=nf('GET',f'/projects/{P}',label='repository_token_auth_project')
auth_dep=deployment('repository_token_auth_deployment')
write('AUTH_PROBE.json',{'observed_at':now(),'secret_source':'repository_scoped_no_environment','project_http':200,'deployment_http':200,'secret_value_observed':False})

# Reversible source branch switch solely to permit exact-SHA build on combined service.
e0=services('services_before'); s0=service('service_before'); d0=deployment('deployment_before'); vcs=s0.get('vcsData') or {}; original_branch=vcs.get('projectBranch')
if s0.get('serviceType')!='combined' or original_branch!='main' or e0.get('disabledCI') is not True: raise RuntimeError('NORTHFLANK_PREFLIGHT_MISMATCH')
vcs_patch={k:vcs[k] for k in ('accountLogin','vcsLinkId','selfHostedVcsId') if vcs.get(k)}
vcs_patch.update({'projectUrl':vcs['projectUrl'],'projectType':vcs['projectType'],'projectBranch':TB})
rec['preflight']={'source_branch':original_branch,'disabled_ci':e0.get('disabledCI'),'disabled_cd':e0.get('disabledCD'),'deployed_sha':(d0.get('internal') or {}).get('deployedSHA')}; save_partial()
switched=False; bid=None; build_final=None
try:
 nf('PATCH',f'/projects/{P}/services/combined/{S}',{'disabledCI':True,'buildSource':'git','vcsData':vcs_patch},label='switch_source_to_exact_branch'); switched=True; rec['mutations'].append('REVERSIBLE_SOURCE_BRANCH_SWITCH'); save_partial()
 if (service('verify_source_branch').get('vcsData') or {}).get('projectBranch')!=TB or services('verify_disabled_ci').get('disabledCI') is not True: raise RuntimeError('SOURCE_BRANCH_SWITCH_NOT_VERIFIED')
 b=nf('POST',f'/projects/{P}/services/{S}/build',{'sha':TS,'overrides':{'buildArguments':{'SENEX_SOURCE_COMMIT':TS,'SENEX_SOURCE_TREE':TT,'SENEX_BUILD_DIGEST':BD}}},label='start_exact_build'); bid=b.get('id')
 if not bid: raise RuntimeError('BUILD_ID_MISSING')
 end=time.time()+3600
 while time.time()<end:
  build_final=nf('GET',f'/projects/{P}/services/{S}/build/{bid}',label='poll_exact_build')
  if build_final.get('concluded'): break
  time.sleep(15)
 if not build_final or not build_final.get('concluded') or not build_final.get('success') or build_final.get('sha')!=TS: raise RuntimeError('EXACT_BUILD_FAILED')
finally:
 if switched:
  restore=dict(vcs_patch); restore['projectBranch']=original_branch
  nf('PATCH',f'/projects/{P}/services/combined/{S}',{'disabledCI':True,'buildSource':'git','vcsData':restore},label='restore_source_main'); rec['mutations'].append('RESTORE_SOURCE_MAIN'); save_partial()
if not build_final: raise RuntimeError('NO_BUILD_RESULT')
if (service('verify_source_restored').get('vcsData') or {}).get('projectBranch')!='main' or services('verify_ci_frozen').get('disabledCI') is not True: raise RuntimeError('SOURCE_RESTORE_FAILED')

# Prove exact OCI manifest from build result or BuildKit logs.
reg=build_final.get('registry') if isinstance(build_final.get('registry'),dict) else {}
img=str(reg.get('digest') or '').lower()
if img and not img.startswith('sha256:') and re.fullmatch(r'[0-9a-f]{64}',img): img='sha256:'+img
if not re.fullmatch(r'sha256:[0-9a-f]{64}',img):
 logs=nf('GET',f'/projects/{P}/services/{S}/build-logs',label='manifest_build_logs',query={'buildId':bid,'queryType':'range','duration':86400,'lineLimit':2000,'direction':'backward','regexIncludes':'manifest'})
 rows=logs if isinstance(logs,list) else []
 hits=[]
 for row in rows:
  text=str(row.get('log','')) if isinstance(row,dict) else str(row)
  m=re.search(r'exporting manifest sha256:([0-9a-f]{64})',text,re.I)
  if m: hits.append('sha256:'+m.group(1).lower())
 if not hits:
  for row in rows:
   text=str(row.get('log','')) if isinstance(row,dict) else str(row)
   m=re.search(r'\bmanifest\b.*?sha256:([0-9a-f]{64})',text,re.I)
   if m and 'attestation manifest' not in text.lower() and 'manifest list' not in text.lower(): hits.append('sha256:'+m.group(1).lower())
 if not hits: raise RuntimeError('OCI_MANIFEST_NOT_PROVEN')
 img=hits[0]
write('BUILD_PROVENANCE.json',{'observed_at':now(),'build_id':bid,'build_sha':build_final.get('sha'),'build_branch':build_final.get('branch'),'success':build_final.get('success'),'source_tree':TT,'build_digest':BD,'image_manifest_digest':img,'source_restored':'main'})

# Bind OCI digest preserving every non-target runtime environment value in memory only.
envdoc=nf('GET',f'/projects/{P}/services/{S}/runtime-environment',label='runtime_env_before',query={'show':'this'}); env=envdoc.get('runtimeEnvironment') if isinstance(envdoc,dict) else None
if not isinstance(env,dict): raise RuntimeError('RUNTIME_ENV_NOT_READABLE')
non_target={k:v for k,v in env.items() if k!='SENEX_IMAGE_DIGEST'}; before_fp=fp(non_target); updated=dict(env); updated['SENEX_IMAGE_DIGEST']=img
nf('PATCH',f'/projects/{P}/services/combined/{S}',{'runtimeEnvironment':updated},label='bind_exact_oci_manifest'); rec['mutations'].append('OCI_PROVENANCE_BIND'); save_partial()
afterdoc=nf('GET',f'/projects/{P}/services/{S}/runtime-environment',label='runtime_env_after',query={'show':'this'}); after=afterdoc.get('runtimeEnvironment') if isinstance(afterdoc,dict) else None
if not isinstance(after,dict) or after.get('SENEX_IMAGE_DIGEST')!=img or fp({k:v for k,v in after.items() if k!='SENEX_IMAGE_DIGEST'})!=before_fp: raise RuntimeError('RUNTIME_ENV_DRIFT')
write('OCI_BIND.json',{'observed_at':now(),'image_digest':img,'non_target_environment_preserved':True,'non_target_environment_sha256':before_fp,'secret_values_persisted':False})

# Exact deployment using previously proven buildSHA-only combined-service contract.
cur=deployment('deployment_pre_exact'); ii=cur.get('internal') or {}
if ii.get('deployedSHA')!=TS:
 nf('POST',f'/projects/{P}/services/{S}/deployment',{'internal':{'buildSHA':TS}},label='deploy_exact_build_sha'); rec['mutations'].append('EXACT_DEPLOY_EXISTING_SERVICE'); save_partial()
end=time.time()+1800; stable=None
while time.time()<end:
 d=deployment('deployment_poll'); e=services('service_poll'); ii=d.get('internal') or {}; st=((e.get('status') or {}).get('deployment') or {}).get('status')
 if ii.get('deployedSHA')==TS and st=='COMPLETED': stable=(d,e); break
 if st=='FAILED': raise RuntimeError('EXACT_DEPLOYMENT_FAILED')
 time.sleep(10)
if stable is None: raise RuntimeError('EXACT_DEPLOYMENT_TIMEOUT')
d,e=stable; ii=d.get('internal') or {}
write('ORIGIN_DEPLOY.json',{'observed_at':now(),'service':S,'build_id':ii.get('buildId') or bid,'build_sha':ii.get('buildSHA'),'deployed_sha':ii.get('deployedSHA'),'deployment_status':((e.get('status') or {}).get('deployment') or {}).get('status'),'source_branch':(service('service_final').get('vcsData') or {}).get('projectBranch'),'disabled_ci':e.get('disabledCI'),'disabled_cd':e.get('disabledCD'),'image_digest':img,'build_digest':BD})

# Live origin readiness and exact provenance.
end=time.time()+600; live_origin={}
while time.time()<end:
 live_origin={'health':public(ORIGIN,'/healthz'),'ready':public(ORIGIN,'/readyz?symbol=BTCUSDT'),'provenance':public(ORIGIN,'/api/runtime/provenance'),'snapshot':public(ORIGIN,'/api/authority/snapshot?symbol=BTCUSDT')}
 p=live_origin['provenance']['body'] if isinstance(live_origin['provenance']['body'],dict) else {}
 if live_origin['health']['http']==200 and live_origin['ready']['http']==200 and live_origin['provenance']['http']==200 and live_origin['snapshot']['http']==200 and p.get('exact') is True and p.get('source_commit')==TS and p.get('source_tree')==TT and p.get('build_digest')==BD and p.get('image_digest')==img: break
 time.sleep(10)
else: raise RuntimeError('ORIGIN_LIVE_EXACT_TIMEOUT')
write('ORIGIN_LIVE.json',{'observed_at':now(),'health_http':live_origin['health']['http'],'ready_http':live_origin['ready']['http'],'provenance':live_origin['provenance']['body'],'snapshot_id':live_origin['snapshot']['body'].get('snapshot_id'),'generation':live_origin['snapshot']['body'].get('generation'),'canonical_sha256':live_origin['snapshot']['body'].get('canonical_sha256'),'evidence_status':'EXACT_HEAD_BOUND'})

# Fresh unauthenticated temporary Cloudflare deployment from the exact candidate bytes.
cfenv=dict(os.environ)
for k in list(cfenv):
 if k.startswith('CLOUDFLARE_') or k in {'CF_API_TOKEN','CF_ACCOUNT_ID','CF_API_KEY','CF_EMAIL'}: cfenv.pop(k,None)
edge_dir=ROOT/'edge/order070'
cp=subprocess.run(['npx','--yes','wrangler@4.102.0','deploy','--temporary','--config','wrangler.jsonc'],cwd=edge_dir,env=cfenv,text=True,capture_output=True)
raw=(cp.stdout or '')+'\n'+(cp.stderr or '')
if cp.returncode!=0: raise RuntimeError('CLOUDFLARE_TEMP_DEPLOY_FAILED')
urls=re.findall(r'https://[A-Za-z0-9._-]+\.workers\.dev',raw)
if not urls: raise RuntimeError('CLOUDFLARE_TEMP_URL_NOT_FOUND')
EDGE=urls[-1].rstrip('/'); raw_sha=h256(raw.encode()); del raw
end=time.time()+120; boot=None
while time.time()<end:
 boot=public(EDGE,'/healthz')
 if boot['http']==200 and boot['headers'].get('x-senex-edge-decision')=='ALLOW_GET_PROXY': break
 time.sleep(3)
if not boot or boot['http']!=200: raise RuntimeError('CLOUDFLARE_EDGE_NOT_ROUTABLE')
post=public(EDGE,'/api/oracle/score',method='POST',body={}); unknown=public(EDGE,'/__order070_unknown__')
if post['http']!=405 or post['headers'].get('x-senex-edge-decision')!='DENY_METHOD' or unknown['http']!=404 or unknown['headers'].get('x-senex-edge-decision')!='DENY_PATH': raise RuntimeError('EDGE_DENY_CONTRACT_FAILED')
write('CLOUDFLARE_FINAL.json',{'observed_at':now(),'head':TS,'tree':TT,'temporary_worker_url':EDGE,'wrangler_raw_output_sha256':raw_sha,'cloudflare_credentials_used':False,'health_http':boot['http'],'allow_decision':boot['headers'].get('x-senex-edge-decision'),'post_http':post['http'],'post_decision':post['headers'].get('x-senex-edge-decision'),'unknown_http':unknown['http'],'unknown_decision':unknown['headers'].get('x-senex-edge-decision')})

# Concurrent origin<->edge cross-surface reconciliation over the corrected AuthoritySnapshot generation.
paths={'snapshot':'/api/authority/snapshot?symbol=BTCUSDT','score':'/api/oracle/score?symbol=BTCUSDT','state':'/api/oracle/state?symbol=BTCUSDT','gate':'/api/portfolio/live_gate?symbol=BTCUSDT','context':'/api/market-context?symbol=BTCUSDT','provenance':'/api/runtime/provenance','health':'/healthz','ready':'/readyz?symbol=BTCUSDT','openapi':'/openapi.json'}
def pair(item):
 k,p=item
 with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
  fo=ex.submit(public,ORIGIN,p); fe=ex.submit(public,EDGE,p); return k,fo.result(),fe.result()
with concurrent.futures.ThreadPoolExecutor(max_workers=len(paths)) as ex: rows=list(ex.map(pair,paths.items()))
origin={k:o for k,o,ed in rows}; edge={k:ed for k,o,ed in rows}
for k in paths:
 if origin[k]['http']!=200 or edge[k]['http']!=200: raise RuntimeError(f'LIVE_SURFACE_HTTP_FAILED:{k}:{origin[k]["http"]}:{edge[k]["http"]}')

def unsafe(schema):
 return sum(1 for _,item in (schema.get('paths') or {}).items() if isinstance(item,dict) for m in item if str(m).lower() in {'post','put','patch','delete'})
if unsafe(origin['openapi']['body'])!=0 or unsafe(edge['openapi']['body'])!=0: raise RuntimeError('PUBLIC_MUTATION_SURFACE_NONZERO')
osnap=origin['snapshot']['body']; esnap=edge['snapshot']['body']; oscore=origin['score']['body']; escore=edge['score']['body']; ostate=origin['state']['body']; estate=edge['state']['body']; ogate=origin['gate']['body']; egate=edge['gate']['body']; octx=origin['context']['body']; ectx=edge['context']['body']
sid=osnap.get('snapshot_id'); generation=osnap.get('generation'); canonical=osnap.get('canonical_sha256')
ids=[sid,esnap.get('snapshot_id'),oscore.get('authority_snapshot_id'),escore.get('authority_snapshot_id'),ostate.get('authority_snapshot_id'),estate.get('authority_snapshot_id'),ogate.get('authority_snapshot_id'),egate.get('authority_snapshot_id'),octx.get('authority_snapshot_id'),ectx.get('authority_snapshot_id')]
gens=[generation,esnap.get('generation'),oscore.get('authority_generation'),escore.get('authority_generation'),ostate.get('authority_generation'),estate.get('authority_generation'),ogate.get('authority_generation'),egate.get('authority_generation'),octx.get('authority_generation'),ectx.get('authority_generation')]
canons=[canonical,esnap.get('canonical_sha256'),oscore.get('authority_canonical_sha256'),escore.get('authority_canonical_sha256'),ostate.get('authority_canonical_sha256'),estate.get('authority_canonical_sha256'),ogate.get('authority_canonical_sha256'),egate.get('authority_canonical_sha256'),octx.get('authority_canonical_sha256'),ectx.get('authority_canonical_sha256')]
if not sid or any(x!=sid for x in ids): raise RuntimeError('SNAPSHOT_ID_RECONCILIATION_FAILED')
if any(x!=generation for x in gens) or any(x!=canonical for x in canons): raise RuntimeError('SNAPSHOT_GENERATION_HASH_RECONCILIATION_FAILED')
if osnap.get('score')!=oscore or esnap.get('score')!=escore or osnap.get('live_gate')!=ogate or esnap.get('live_gate')!=egate: raise RuntimeError('SNAPSHOT_PAYLOAD_RECONCILIATION_FAILED')
if osnap.get('exact_count_complete') is not True or not isinstance(osnap.get('exact_total_predictions'),int): raise RuntimeError('EXACT_COUNT_CONTRACT_FAILED')
for d in (origin['health']['body'],edge['health']['body']):
 if d.get('trade_mode')!='PAPER' or d.get('orders_enabled') is not False or d.get('live_capital_locked') is not True: raise RuntimeError('PAPER_LOCK_HEALTH_FAILED')
for d in (octx,ectx):
 s=d.get('safety') or {}
 if s.get('trade_mode')!='PAPER' or s.get('allow_live') is not False or s.get('orders_enabled') is not False or s.get('live_capital_locked') is not True: raise RuntimeError('PAPER_LOCK_CONTEXT_FAILED')
write('SNAPSHOT_RECONCILIATION.json',{'observed_at':now(),'snapshot_id':sid,'generation':generation,'canonical_sha256':canonical,'all_ids_equal':True,'all_generations_equal':True,'all_canonical_hashes_equal':True,'snapshot_score_matches':osnap.get('score')==oscore and esnap.get('score')==escore,'snapshot_gate_matches':osnap.get('live_gate')==ogate and esnap.get('live_gate')==egate,'exact_total_predictions':osnap.get('exact_total_predictions'),'exact_count_complete':osnap.get('exact_count_complete'),'origin_edge_snapshot_equal':osnap==esnap})
write('LIVE_E2E.json',{'observed_at':now(),'head':TS,'tree':TT,'origin':ORIGIN,'edge':EDGE,'healthz_origin':origin['health']['http'],'healthz_edge':edge['health']['http'],'readyz_origin':origin['ready']['http'],'readyz_edge':edge['ready']['http'],'provenance_exact_origin':origin['provenance']['body'].get('exact'),'provenance_exact_edge':edge['provenance']['body'].get('exact'),'public_origin_unsafe_count':unsafe(origin['openapi']['body']),'public_edge_unsafe_count':unsafe(edge['openapi']['body']),'authority_snapshot_id':sid,'authority_generation':generation,'authority_canonical_sha256':canonical,'snapshot_reconciliation':'PASS','trade_mode':'PAPER','orders_enabled':False,'live_capital_locked':True,'real_order_count':0,'real_capital_movement':0,'supabase_mutation':0,'runtime017_mutation':0,'tuning':0,'evidence_status':'EXACT_HEAD_BOUND'})

rec['build_id']=bid; rec['image_digest']=img; rec['deployed_sha']=ii.get('deployedSHA'); rec['edge_url']=EDGE; rec['authority_snapshot_id']=sid; rec['authority_generation']=generation; rec['authority_canonical_sha256']=canonical; rec['status']='READY_FOR_AUD'; rec['completed_at']=now(); save_partial()
summary={'ORDER_070_STATUS':'READY_FOR_AUD','PR':67,'HEAD':TS,'TREE':TT,'CI':CI,'ORIGIN_DEPLOY':{'build_id':bid,'deployed_sha':ii.get('deployedSHA'),'image_digest':img,'build_digest':BD},'CLOUDFLARE_FINAL_EXACT_HEAD':EDGE,'PUBLIC_POST_COUNT':0,'AUTHORITY_SNAPSHOT_ID':sid,'AUTHORITY_GENERATION':generation,'AUTHORITY_CANONICAL_SHA256':canonical,'SNAPSHOT_RECONCILIATION':'PASS','PROVENANCE':'PASS','LIVE_E2E':'PASS','REAL_ORDER_COUNT':0,'REAL_CAPITAL_MOVEMENT':0,'SUPABASE_DATA_MUTATION':0,'RUNTIME017_MUTATION':0,'TUNING':0,'BLOCKER':'none'}
write('FINAL_SUMMARY.json',summary)
files=sorted(p.name for p in OUT.glob('*.json'))
lines=[]
for name in files: lines.append(f'{h256((OUT/name).read_bytes())}  {name}')
(OUT/'MANIFEST.sha256').write_text('\n'.join(lines)+'\n')
cp=subprocess.run(['sha256sum','-c','MANIFEST.sha256'],cwd=OUT,text=True,capture_output=True)
if cp.returncode!=0: raise RuntimeError('FINAL_MANIFEST_VERIFY_FAILED')
print('ORDER_070_STATUS=READY_FOR_AUD')
print('BUILD_ID='+str(bid))
print('DEPLOYED_SHA='+str(ii.get('deployedSHA')))
print('IMAGE_DIGEST='+img)
print('HEALTHZ=200')
print('READYZ=200')
print('PROVENANCE_EXACT=true')
print('CLOUDFLARE_FINAL='+EDGE)
print('AUTHORITY_SNAPSHOT_ID='+str(sid))
print('AUTHORITY_GENERATION='+str(generation))
print('AUTHORITY_CANONICAL_SHA256='+str(canonical))
print('SNAPSHOT_RECONCILIATION=PASS')
print('LIVE_E2E=PASS')
print('MANIFEST_SHA256='+h256((OUT/'MANIFEST.sha256').read_bytes()))
