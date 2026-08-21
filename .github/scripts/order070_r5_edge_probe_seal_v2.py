from __future__ import annotations
import concurrent.futures,datetime,hashlib,json,os,re,shutil,subprocess,tempfile,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path
API='https://api.northflank.com/v1'; P='seneciobot'; S='senecio-h011'; BID='curved-board-2291'
TS='483b389a83610992800181c0a21b5a337009f7b4'; TT='0494d3d4066f94a9dd055d81c07a3633a243ec2f'; BD='sha256:1806ad0bc71c45264695c1c8973a497a39f9903f867ece2d56fdbc12f44e4892'; IMG='sha256:7efb105084053fecf45bac799728424675eb66606735a60f80c0d3c5ff4ba7f8'
ORIGIN='https://h011-web--senecio-h011--wbjggn89fnf8.code.run'; TOKEN=os.environ['NORTHFLANK_API_TOKEN']; ROOT=Path(os.environ.get('CANDIDATE_DIR','candidate')).resolve(); OUT=Path('order070-r5-final-evidence').resolve()
CI={'ORDER070':32476281034,'SCORE001':32476281065,'SCORE002':32476281013,'SMOKE':32476281024}
if OUT.exists(): shutil.rmtree(OUT)
OUT.mkdir(parents=True)

def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def h256(b): return hashlib.sha256(b).hexdigest()
def write(n,o): p=OUT/n; p.write_text(json.dumps(o,sort_keys=True,indent=2,default=str)+'\n'); return h256(p.read_bytes())
def git(*a): return subprocess.run(['git',*a],cwd=ROOT,text=True,capture_output=True,check=True).stdout.strip()
def jget(url,headers=None,timeout=45):
 req=urllib.request.Request(url,headers=headers or {'Accept':'application/json','Cache-Control':'no-cache','User-Agent':'senex-r5-v2/1'},method='GET')
 try:
  with urllib.request.urlopen(req,timeout=timeout) as r: st=r.status; raw=r.read(); rh={k.lower():v for k,v in r.headers.items()}
 except urllib.error.HTTPError as e: st=e.code; raw=e.read(); rh={k.lower():v for k,v in e.headers.items()}
 try: obj=json.loads(raw.decode())
 except Exception: obj={'_non_json':True,'sha256':h256(raw),'bytes':len(raw)}
 return {'http':st,'body':obj,'headers':rh,'sha256':h256(raw)}
def pub(base,path): return jget(base.rstrip('/')+path)
def nf(path,query=None):
 url=API+path+(('?'+urllib.parse.urlencode(query)) if query else '')
 r=jget(url,{'Authorization':f'Bearer {TOKEN}','Accept':'application/json','User-Agent':'senex-r5-v2-nf/1'})
 if not 200<=r['http']<300: raise RuntimeError(f'NF_GET_FAILED:{path}:{r["http"]}')
 x=r['body']; return x.get('data',x) if isinstance(x,dict) else x,r

def deploy_temp(dirpath,config='wrangler.jsonc'):
 env=dict(os.environ)
 for k in list(env):
  if k.startswith('CLOUDFLARE_') or k in {'CF_API_TOKEN','CF_ACCOUNT_ID','CF_API_KEY','CF_EMAIL'}: env.pop(k,None)
 cp=subprocess.run(['npx','--yes','wrangler@4.102.0','deploy','--temporary','--config',config],cwd=dirpath,env=env,text=True,capture_output=True,timeout=180)
 raw=(cp.stdout or '')+'\n'+(cp.stderr or '')
 if cp.returncode: raise RuntimeError('TEMP_DEPLOY_FAILED')
 urls=re.findall(r'https://[A-Za-z0-9._-]+\.workers\.dev',raw)
 if not urls: raise RuntimeError('TEMP_URL_MISSING')
 return urls[-1].rstrip('/'),h256(raw.encode())

def curl_get_json(url):
 cp=subprocess.run(['curl','-sS','--max-time','40','-w','\n%{http_code}',url],text=True,capture_output=True,timeout=45)
 if cp.returncode: return {'http':0,'body':{},'curl_exit':cp.returncode}
 body,code=cp.stdout.rsplit('\n',1)
 try:o=json.loads(body)
 except Exception:o={'_raw_sha256':h256(body.encode())}
 return {'http':int(code),'body':o,'curl_exit':0}

def unsafe(schema): return sum(1 for _,item in (schema.get('paths') or {}).items() if isinstance(item,dict) for m in item if str(m).lower() in {'post','put','patch','delete'})

# exact immutable remote truth
if git('rev-parse','HEAD')!=TS or git('rev-parse','HEAD^{tree}')!=TT or git('status','--porcelain'): raise RuntimeError('CANDIDATE_DRIFT')
if git('ls-remote','origin','refs/heads/feat/order-070-runtime-truth-hardening').split()[0]!=TS: raise RuntimeError('REMOTE_DRIFT')
write('REMOTE_TRUTH.json',{'observed_at':now(),'pr':67,'head':TS,'tree':TT,'candidate_changed':False,'new_ci':False,'merge':False,'ci_runs':CI})

# repository-scoped auth + exact build/deploy readback, GET-only
proj,rp=nf(f'/projects/{P}'); dep,rd=nf(f'/projects/{P}/services/{S}/deployment'); build,rb=nf(f'/projects/{P}/services/{S}/build/{BID}'); svc,rs=nf(f'/projects/{P}/services/{S}'); services,_=nf(f'/projects/{P}/services',{'per_page':100})
arr=services.get('services') if isinstance(services,dict) else services; entry=next(x for x in arr if x.get('id')==S); ii=dep.get('internal') or {}; dst=((entry.get('status') or {}).get('deployment') or {}).get('status')
if not build.get('concluded') or not build.get('success') or build.get('sha')!=TS or ii.get('deployedSHA')!=TS or dst!='COMPLETED': raise RuntimeError('EXACT_ORIGIN_READBACK_FAILED')
if (svc.get('vcsData') or {}).get('projectBranch')!='main' or entry.get('disabledCI') is not True: raise RuntimeError('SOURCE_CONTROL_DRIFT')
write('AUTH_AND_DEPLOY_READBACK.json',{'observed_at':now(),'repository_scoped_auth':{'project_http':rp['http'],'deployment_http':rd['http'],'secret_value_observed':False},'build_id':BID,'build_sha':build.get('sha'),'build_success':build.get('success'),'deployed_sha':ii.get('deployedSHA'),'deployment_status':dst,'source_branch':(svc.get('vcsData') or {}).get('projectBranch'),'disabled_ci':entry.get('disabledCI'),'disabled_cd':entry.get('disabledCD'),'target_tree':TT,'build_digest':BD,'image_digest':IMG,'northflank_mutations_this_run':0})

# prime current generation then exact live origin
prime=pub(ORIGIN,'/api/authority/snapshot?symbol=BTCUSDT'); health=pub(ORIGIN,'/healthz'); ready=pub(ORIGIN,'/readyz?symbol=BTCUSDT'); prov=pub(ORIGIN,'/api/runtime/provenance')
if prime['http']!=200 or health['http']!=200 or ready['http']!=200 or prov['http']!=200: raise RuntimeError('ORIGIN_HTTP_FAILED')
pv=prov['body']; rdj=ready['body']
if pv.get('exact') is not True or pv.get('source_commit')!=TS or pv.get('source_tree')!=TT or pv.get('build_digest')!=BD or pv.get('image_digest')!=IMG: raise RuntimeError('PROVENANCE_FAILED')
if rdj.get('status')!='ready' or not all((rdj.get('checks') or {}).values()): raise RuntimeError('READY_FAILED')
write('ORIGIN_LIVE.json',{'observed_at':now(),'health_http':200,'ready_http':200,'provenance':pv,'snapshot_id':prime['body'].get('snapshot_id'),'generation':prime['body'].get('generation'),'canonical_sha256':prime['body'].get('canonical_sha256'),'evidence_status':'EXACT_HEAD_BOUND'})

# target exact worker
EDGE,edge_deploy_sha=deploy_temp(ROOT/'edge/order070')
boot=None
for _ in range(40):
 boot=pub(EDGE,'/healthz')
 if boot['http']==200 and boot['headers'].get('x-senex-edge-decision')=='ALLOW_GET_PROXY': break
 time.sleep(2)
if not boot or boot['http']!=200 or boot['headers'].get('x-senex-edge-decision')!='ALLOW_GET_PROXY': raise RuntimeError('EDGE_BOOT_FAILED')

# Cloudflare-internal probe avoids GitHub/Azure preview transport filtering for POST/unknown-path proof.
with tempfile.TemporaryDirectory() as td:
 t=Path(td); target=json.dumps(EDGE)
 (t/'worker.js').write_text(f'''const TARGET={target};\nexport default {{async fetch(req){{const u=new URL(req.url);if(u.pathname!=="/probe")return new Response("not found",{{status:404}});const [g,p,x]=await Promise.all([fetch(TARGET+"/healthz"),fetch(TARGET+"/api/oracle/score",{{method:"POST",headers:{{"content-type":"application/json"}},body:"{{}}"}}),fetch(TARGET+"/__order070_unknown__")]);const one=r=>({{status:r.status,decision:r.headers.get("x-senex-edge-decision")}});return Response.json({{get:one(g),post:one(p),unknown:one(x)}});}}}};\n''')
 (t/'wrangler.jsonc').write_text('{"name":"senex-order070-r5-internal-probe","main":"worker.js","compatibility_date":"2026-08-21"}\n')
 PROBE,probe_deploy_sha=deploy_temp(t)
 pr=None
 for _ in range(40):
  pr=curl_get_json(PROBE+'/probe')
  if pr['http']==200 and isinstance(pr['body'],dict) and 'post' in pr['body']: break
  time.sleep(2)
 if not pr or pr['http']!=200: raise RuntimeError('INTERNAL_PROBE_NOT_ROUTABLE')
 decisions=pr['body']
if decisions.get('get')!={'status':200,'decision':'ALLOW_GET_PROXY'} or decisions.get('post')!={'status':405,'decision':'DENY_METHOD'} or decisions.get('unknown')!={'status':404,'decision':'DENY_PATH'}: raise RuntimeError(f'EDGE_INTERNAL_DECISION_FAILED:{decisions}')
write('CLOUDFLARE_FINAL.json',{'observed_at':now(),'head':TS,'tree':TT,'target_worker_url':EDGE,'target_deploy_output_sha256':edge_deploy_sha,'probe_worker_url':PROBE,'probe_deploy_output_sha256':probe_deploy_sha,'cloudflare_credentials_used':False,'target_direct_get':{'http':boot['http'],'decision':boot['headers'].get('x-senex-edge-decision')},'in_network_decisions':decisions,'result':'PASS'})

# eight concurrent rounds across five authority-backed surfaces
aps={'snapshot':'/api/authority/snapshot?symbol=BTCUSDT','score':'/api/oracle/score?symbol=BTCUSDT','state':'/api/oracle/state?symbol=BTCUSDT','gate':'/api/portfolio/live_gate?symbol=BTCUSDT','ready':'/readyz?symbol=BTCUSDT'}
def ident(kind,b):
 if kind=='snapshot': return b.get('snapshot_id'),b.get('generation'),b.get('canonical_sha256')
 if kind=='ready': return b.get('authority_snapshot_id'),b.get('generation'),b.get('canonical_sha256')
 return b.get('authority_snapshot_id'),b.get('authority_generation'),b.get('authority_canonical_sha256')
def fetch(side,base,k,path): return side,k,pub(base,path)
rounds=[]
for n in range(1,9):
 with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
  fs=[ex.submit(fetch,side,base,k,path) for side,base in [('origin',ORIGIN),('edge',EDGE)] for k,path in aps.items()]
  rows=[f.result() for f in fs]
 got={(s,k):r for s,k,r in rows}
 if any(got[(s,k)]['http']!=200 for s in ('origin','edge') for k in aps): raise RuntimeError(f'ROUND_HTTP:{n}')
 ids=[ident(k,got[(s,k)]['body']) for s in ('origin','edge') for k in aps]
 if any(None in x for x in ids) or len(set(ids))!=1: raise RuntimeError(f'ROUND_ID_RACE:{n}:{ids}')
 o=got[('origin','snapshot')]['body']; e=got[('edge','snapshot')]['body']; core=['snapshot_id','generation','canonical_sha256','symbol','authority_history_complete','authority_history_rows','exact_total_predictions','exact_count_complete','last_cursor_or_equivalent','score','live_gate','provenance']
 if not all(o.get(k)==e.get(k) for k in core): raise RuntimeError(f'ROUND_SNAPSHOT_CORE:{n}')
 if got[('origin','score')]['body']!=got[('edge','score')]['body'] or got[('origin','gate')]['body']!=got[('edge','gate')]['body']: raise RuntimeError(f'ROUND_PAYLOAD:{n}')
 if o.get('exact_count_complete') is not True or not isinstance(o.get('exact_total_predictions'),int): raise RuntimeError(f'ROUND_COUNT:{n}')
 rounds.append({'round':n,'snapshot_id':ids[0][0],'generation':ids[0][1],'canonical_sha256':ids[0][2],'all_10_identities_equal':True,'snapshot_core_equal':True,'score_equal':True,'live_gate_equal':True,'exact_total_predictions':o.get('exact_total_predictions'),'exact_count_complete':True})
write('CONCURRENT_RECONCILIATION.json',{'observed_at':now(),'rounds':rounds,'round_count':8,'result':'PASS'})

# final E2E surfaces and safety
paths={'snapshot':'/api/authority/snapshot?symbol=BTCUSDT','context':'/api/market-context?symbol=BTCUSDT','provenance':'/api/runtime/provenance','health':'/healthz','ready':'/readyz?symbol=BTCUSDT','openapi':'/openapi.json'}
final={k:{'origin':pub(ORIGIN,p),'edge':pub(EDGE,p)} for k,p in paths.items()}
for k in paths:
 if final[k]['origin']['http']!=200 or final[k]['edge']['http']!=200: raise RuntimeError(f'E2E_HTTP:{k}')
for side in ('origin','edge'):
 p=final['provenance'][side]['body']; h=final['health'][side]['body']; r=final['ready'][side]['body']; saf=(final['context'][side]['body'].get('safety') or {}); schema=final['openapi'][side]['body']
 if p.get('exact') is not True or p.get('source_commit')!=TS or p.get('source_tree')!=TT or p.get('build_digest')!=BD or p.get('image_digest')!=IMG: raise RuntimeError(f'E2E_PROV:{side}')
 if r.get('status')!='ready' or not all((r.get('checks') or {}).values()): raise RuntimeError(f'E2E_READY:{side}')
 if h.get('trade_mode')!='PAPER' or h.get('orders_enabled') is not False or h.get('live_capital_locked') is not True: raise RuntimeError(f'E2E_HEALTH:{side}')
 if saf.get('trade_mode')!='PAPER' or saf.get('orders_enabled') is not False or saf.get('live_capital_locked') is not True or saf.get('allow_live') is not False: raise RuntimeError(f'E2E_SAFETY:{side}')
 if unsafe(schema)!=0: raise RuntimeError(f'E2E_UNSAFE:{side}')
admin1=pub(ORIGIN,'/admin'); admin2=pub(ORIGIN,'/api/admin')
if admin1['http']!=404 or admin2['http']!=404: raise RuntimeError('ADMIN_MOUNTED')
sid=rounds[-1]['snapshot_id']
write('LIVE_E2E.json',{'observed_at':now(),'head':TS,'tree':TT,'build_id':BID,'build_digest':BD,'image_digest':IMG,'origin':ORIGIN,'edge':EDGE,'healthz_origin':200,'healthz_edge':200,'readyz_origin':200,'readyz_edge':200,'provenance_exact_origin':True,'provenance_exact_edge':True,'evidence_status':'EXACT_HEAD_BOUND','authority_snapshot_id':sid,'concurrent_rounds':8,'public_origin_unsafe_count':0,'public_edge_unsafe_count':0,'admin_publicly_unmounted':True,'trade_mode':'PAPER','orders_enabled':False,'live_capital_locked':True,'real_order_count':0,'real_capital_movement':0,'supabase_data_mutation':0,'runtime017_mutation':0,'tuning':0})
write('FINAL_GATE_SUMMARY.json',{'observed_at':now(),'order':'ORDER-070-R5','status':'READY_FOR_AUD','pr':67,'head':TS,'tree':TT,'repository_scoped_auth':'PASS','exact_build':'PASS','exact_origin_deploy':'PASS','oci_source_provenance':'PASS','health_ready':'PASS','cloudflare_final':'PASS','concurrent_origin_edge_reconciliation':'PASS','live_e2e':'PASS','authority_snapshot_id':sid,'public_post_count':0,'admin_publicly_unmounted':'PASS','candidate_changed':False,'new_ci':False,'merge':False,'tuning':0,'runtime017_mutation':0,'supabase_data_mutation':0,'real_order_count':0,'real_capital_movement':0})
required=['REMOTE_TRUTH.json','AUTH_AND_DEPLOY_READBACK.json','ORIGIN_LIVE.json','CLOUDFLARE_FINAL.json','CONCURRENT_RECONCILIATION.json','LIVE_E2E.json','FINAL_GATE_SUMMARY.json']
(OUT/'MANIFEST.sha256').write_text('\n'.join(f'{h256((OUT/n).read_bytes())}  {n}' for n in sorted(required))+'\n')
ck=subprocess.run(['sha256sum','-c','MANIFEST.sha256'],cwd=OUT,text=True,capture_output=True)
if ck.returncode: raise RuntimeError('MANIFEST_FAILED')
print('ORDER_070_R5_STATUS=READY_FOR_AUD'); print('HEAD='+TS); print('TREE='+TT); print('BUILD_ID='+BID); print('IMAGE_DIGEST='+IMG); print('HEALTHZ=200'); print('READYZ=200'); print('CLOUDFLARE_FINAL=PASS'); print('CONCURRENT_RECONCILIATION=PASS'); print('AUTHORITY_SNAPSHOT_ID='+sid); print('LIVE_E2E=PASS'); print('MANIFEST_SHA256='+h256((OUT/'MANIFEST.sha256').read_bytes()))
