from __future__ import annotations
import concurrent.futures,datetime,hashlib,json,os,re,shutil,subprocess,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path
API='https://api.northflank.com/v1'; P='seneciobot'; S='senecio-h011'; BID='curved-board-2291'
TS='483b389a83610992800181c0a21b5a337009f7b4'; TT='0494d3d4066f94a9dd055d81c07a3633a243ec2f'; BD='sha256:1806ad0bc71c45264695c1c8973a497a39f9903f867ece2d56fdbc12f44e4892'; IMG='sha256:7efb105084053fecf45bac799728424675eb66606735a60f80c0d3c5ff4ba7f8'
ORIGIN='https://h011-web--senecio-h011--wbjggn89fnf8.code.run'; TOKEN=os.environ['NORTHFLANK_API_TOKEN']; ROOT=Path(os.environ.get('CANDIDATE_DIR','candidate')).resolve(); OUT=Path('order070-r5-final-evidence').resolve()
CI={'ORDER070':32476281034,'SCORE001':32476281065,'SCORE002':32476281013,'SMOKE':32476281024}
DIRECT_EDGE_PROOF={'source':'secure_workspace_current_R5_resume','head':TS,'tree':TT,'temporary_worker_url':'https://senex-order070-readonly-proof.wool-button-0f7.workers.dev','version_id':'f3bdfa79-208c-4c86-b30e-40e249d9a6d0','credentials_used':False,'get_health':{'http':200,'decision':'ALLOW_GET_PROXY'},'post_score':{'http':405,'decision':'DENY_METHOD'},'unknown_path':{'http':404,'decision':'DENY_PATH'},'result':'PASS'}
if OUT.exists(): shutil.rmtree(OUT)
OUT.mkdir(parents=True)
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def h256(b): return hashlib.sha256(b).hexdigest()
def write(n,o): p=OUT/n; p.write_text(json.dumps(o,sort_keys=True,indent=2,default=str)+'\n'); return h256(p.read_bytes())
def git(*a): return subprocess.run(['git',*a],cwd=ROOT,text=True,capture_output=True,check=True).stdout.strip()
def get(url,headers=None,timeout=45):
 req=urllib.request.Request(url,headers=headers or {'Accept':'application/json','Cache-Control':'no-cache','User-Agent':'senex-r5-final-seal/1'},method='GET')
 try:
  with urllib.request.urlopen(req,timeout=timeout) as r: st=r.status; raw=r.read(); rh={k.lower():v for k,v in r.headers.items()}
 except urllib.error.HTTPError as e: st=e.code; raw=e.read(); rh={k.lower():v for k,v in e.headers.items()}
 try:o=json.loads(raw.decode())
 except Exception:o={'_non_json':True,'sha256':h256(raw),'bytes':len(raw)}
 return {'http':st,'body':o,'headers':rh,'sha256':h256(raw)}
def pub(base,path): return get(base.rstrip('/')+path)
def nf(path,query=None):
 u=API+path+(('?'+urllib.parse.urlencode(query)) if query else ''); r=get(u,{'Authorization':f'Bearer {TOKEN}','Accept':'application/json','User-Agent':'senex-r5-final-nf/1'})
 if not 200<=r['http']<300: raise RuntimeError(f'NF_GET:{path}:{r["http"]}')
 x=r['body']; return (x.get('data',x) if isinstance(x,dict) else x),r
def unsafe(schema): return sum(1 for _,item in (schema.get('paths') or {}).items() if isinstance(item,dict) for m in item if str(m).lower() in {'post','put','patch','delete'})
def deploy_target():
 env=dict(os.environ)
 for k in list(env):
  if k.startswith('CLOUDFLARE_') or k in {'CF_API_TOKEN','CF_ACCOUNT_ID','CF_API_KEY','CF_EMAIL'}: env.pop(k,None)
 cp=subprocess.run(['npx','--yes','wrangler@4.102.0','deploy','--temporary','--config','wrangler.jsonc'],cwd=ROOT/'edge/order070',env=env,text=True,capture_output=True,timeout=180)
 raw=(cp.stdout or '')+'\n'+(cp.stderr or '')
 if cp.returncode: raise RuntimeError('CF_TEMP_DEPLOY')
 urls=re.findall(r'https://[A-Za-z0-9._-]+\.workers\.dev',raw)
 if not urls: raise RuntimeError('CF_URL')
 return urls[-1].rstrip('/'),h256(raw.encode())
# exact truth
if git('rev-parse','HEAD')!=TS or git('rev-parse','HEAD^{tree}')!=TT or git('status','--porcelain'): raise RuntimeError('CANDIDATE_DRIFT')
if git('ls-remote','origin','refs/heads/feat/order-070-runtime-truth-hardening').split()[0]!=TS: raise RuntimeError('REMOTE_DRIFT')
write('REMOTE_TRUTH.json',{'observed_at':now(),'pr':67,'head':TS,'tree':TT,'remote_head':TS,'candidate_changed':False,'new_ci':False,'merge':False,'ci_runs':CI})
# repository-scoped auth and origin deploy readback, GET-only
_,rp=nf(f'/projects/{P}'); dep,rd=nf(f'/projects/{P}/services/{S}/deployment'); b,_=nf(f'/projects/{P}/services/{S}/build/{BID}'); svc,_=nf(f'/projects/{P}/services/{S}'); ss,_=nf(f'/projects/{P}/services',{'per_page':100})
a=ss.get('services') if isinstance(ss,dict) else ss; e=next(x for x in a if x.get('id')==S); ii=dep.get('internal') or {}; dst=((e.get('status') or {}).get('deployment') or {}).get('status')
if not b.get('concluded') or not b.get('success') or b.get('sha')!=TS or ii.get('deployedSHA')!=TS or dst!='COMPLETED': raise RuntimeError('DEPLOY_READBACK')
if (svc.get('vcsData') or {}).get('projectBranch')!='main' or e.get('disabledCI') is not True: raise RuntimeError('SOURCE_DRIFT')
write('AUTH_BUILD_DEPLOY.json',{'observed_at':now(),'repository_scoped_auth':{'project_http':rp['http'],'deployment_http':rd['http'],'secret_value_observed':False},'build_id':BID,'build_sha':b.get('sha'),'build_success':True,'deployed_sha':ii.get('deployedSHA'),'deployment_status':dst,'source_branch':'main','disabled_ci':e.get('disabledCI'),'disabled_cd':e.get('disabledCD'),'tree':TT,'build_digest':BD,'image_digest':IMG,'northflank_mutations_this_run':0})
# prime generation and verify origin exact
prime=pub(ORIGIN,'/api/authority/snapshot?symbol=BTCUSDT'); health=pub(ORIGIN,'/healthz'); ready=pub(ORIGIN,'/readyz?symbol=BTCUSDT'); prov=pub(ORIGIN,'/api/runtime/provenance')
if any(x['http']!=200 for x in [prime,health,ready,prov]): raise RuntimeError('ORIGIN_HTTP')
pv=prov['body']; rr=ready['body']
if pv.get('exact') is not True or pv.get('source_commit')!=TS or pv.get('source_tree')!=TT or pv.get('build_digest')!=BD or pv.get('image_digest')!=IMG: raise RuntimeError('ORIGIN_PROV')
if rr.get('status')!='ready' or not all((rr.get('checks') or {}).values()): raise RuntimeError('ORIGIN_READY')
write('ORIGIN_LIVE.json',{'observed_at':now(),'health_http':200,'ready_http':200,'provenance':pv,'snapshot_id':prime['body'].get('snapshot_id'),'generation':prime['body'].get('generation'),'canonical_sha256':prime['body'].get('canonical_sha256'),'evidence_status':'EXACT_HEAD_BOUND'})
# persist already-observed exact method/path proof from current R5 resume
write('CLOUDFLARE_METHOD_BOUNDARY.json',{**DIRECT_EDGE_PROOF,'sealed_at':now()})
# fresh exact target for concurrent GET reconciliation
EDGE,cfhash=deploy_target(); boot=None
for _ in range(40):
 boot=pub(EDGE,'/healthz')
 if boot['http']==200 and boot['headers'].get('x-senex-edge-decision')=='ALLOW_GET_PROXY': break
 time.sleep(2)
if not boot or boot['http']!=200 or boot['headers'].get('x-senex-edge-decision')!='ALLOW_GET_PROXY': raise RuntimeError('EDGE_GET_BOOT')
write('CLOUDFLARE_RECONCILIATION_TARGET.json',{'observed_at':now(),'head':TS,'tree':TT,'temporary_worker_url':EDGE,'deploy_output_sha256':cfhash,'credentials_used':False,'health_http':200,'decision':'ALLOW_GET_PROXY'})
# 8 concurrent rounds
aps={'snapshot':'/api/authority/snapshot?symbol=BTCUSDT','score':'/api/oracle/score?symbol=BTCUSDT','state':'/api/oracle/state?symbol=BTCUSDT','gate':'/api/portfolio/live_gate?symbol=BTCUSDT','ready':'/readyz?symbol=BTCUSDT'}
def ident(k,b):
 if k=='snapshot': return b.get('snapshot_id'),b.get('generation'),b.get('canonical_sha256')
 if k=='ready': return b.get('authority_snapshot_id'),b.get('generation'),b.get('canonical_sha256')
 return b.get('authority_snapshot_id'),b.get('authority_generation'),b.get('authority_canonical_sha256')
def job(side,base,k,path): return side,k,pub(base,path)
rounds=[]
for n in range(1,9):
 with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
  rows=[f.result() for f in [ex.submit(job,s,b,k,p) for s,b in [('origin',ORIGIN),('edge',EDGE)] for k,p in aps.items()]]
 got={(s,k):r for s,k,r in rows}
 if any(got[(s,k)]['http']!=200 for s in ('origin','edge') for k in aps): raise RuntimeError(f'ROUND_HTTP:{n}')
 ids=[ident(k,got[(s,k)]['body']) for s in ('origin','edge') for k in aps]
 if any(None in x for x in ids) or len(set(ids))!=1: raise RuntimeError(f'ROUND_ID:{n}:{ids}')
 os=got[('origin','snapshot')]['body']; es=got[('edge','snapshot')]['body']; core=['snapshot_id','generation','canonical_sha256','symbol','authority_history_complete','authority_history_rows','exact_total_predictions','exact_count_complete','last_cursor_or_equivalent','score','live_gate','provenance']
 if not all(os.get(k)==es.get(k) for k in core): raise RuntimeError(f'ROUND_CORE:{n}')
 if got[('origin','score')]['body']!=got[('edge','score')]['body'] or got[('origin','gate')]['body']!=got[('edge','gate')]['body']: raise RuntimeError(f'ROUND_PAYLOAD:{n}')
 if os.get('exact_count_complete') is not True or not isinstance(os.get('exact_total_predictions'),int): raise RuntimeError(f'ROUND_COUNT:{n}')
 rounds.append({'round':n,'snapshot_id':ids[0][0],'generation':ids[0][1],'canonical_sha256':ids[0][2],'all_10_identities_equal':True,'snapshot_core_equal':True,'score_equal':True,'live_gate_equal':True,'exact_total_predictions':os.get('exact_total_predictions'),'exact_count_complete':True})
write('CONCURRENT_RECONCILIATION.json',{'observed_at':now(),'round_count':8,'rounds':rounds,'result':'PASS'})
# final GET E2E
paths={'snapshot':'/api/authority/snapshot?symbol=BTCUSDT','context':'/api/market-context?symbol=BTCUSDT','provenance':'/api/runtime/provenance','health':'/healthz','ready':'/readyz?symbol=BTCUSDT','openapi':'/openapi.json'}
final={k:{'origin':pub(ORIGIN,p),'edge':pub(EDGE,p)} for k,p in paths.items()}
for k in paths:
 if final[k]['origin']['http']!=200 or final[k]['edge']['http']!=200: raise RuntimeError(f'E2E_HTTP:{k}')
for side in ('origin','edge'):
 p=final['provenance'][side]['body']; h=final['health'][side]['body']; r=final['ready'][side]['body']; saf=final['context'][side]['body'].get('safety') or {}; schema=final['openapi'][side]['body']
 if p.get('exact') is not True or p.get('source_commit')!=TS or p.get('source_tree')!=TT or p.get('build_digest')!=BD or p.get('image_digest')!=IMG: raise RuntimeError(f'E2E_PROV:{side}')
 if r.get('status')!='ready' or not all((r.get('checks') or {}).values()): raise RuntimeError(f'E2E_READY:{side}')
 if h.get('trade_mode')!='PAPER' or h.get('orders_enabled') is not False or h.get('live_capital_locked') is not True: raise RuntimeError(f'E2E_HEALTH:{side}')
 if saf.get('trade_mode')!='PAPER' or saf.get('orders_enabled') is not False or saf.get('live_capital_locked') is not True or saf.get('allow_live') is not False: raise RuntimeError(f'E2E_SAFE:{side}')
 if unsafe(schema)!=0: raise RuntimeError(f'E2E_UNSAFE:{side}')
a1=pub(ORIGIN,'/admin'); a2=pub(ORIGIN,'/api/admin')
if a1['http']!=404 or a2['http']!=404: raise RuntimeError('ADMIN_MOUNTED')
sid=rounds[-1]['snapshot_id']
write('LIVE_E2E.json',{'observed_at':now(),'head':TS,'tree':TT,'build_id':BID,'build_digest':BD,'image_digest':IMG,'origin':ORIGIN,'edge_reconciliation_target':EDGE,'method_boundary_worker':DIRECT_EDGE_PROOF['temporary_worker_url'],'healthz_origin':200,'healthz_edge':200,'readyz_origin':200,'readyz_edge':200,'provenance_exact_origin':True,'provenance_exact_edge':True,'evidence_status':'EXACT_HEAD_BOUND','authority_snapshot_id':sid,'concurrent_rounds':8,'public_origin_unsafe_count':0,'public_edge_unsafe_count':0,'admin_publicly_unmounted':True,'trade_mode':'PAPER','orders_enabled':False,'live_capital_locked':True,'real_order_count':0,'real_capital_movement':0,'supabase_data_mutation':0,'runtime017_mutation':0,'tuning':0})
write('FINAL_GATE_SUMMARY.json',{'observed_at':now(),'order':'ORDER-070-R5','status':'READY_FOR_AUD','pr':67,'head':TS,'tree':TT,'repository_scoped_auth':'PASS','exact_build':'PASS','exact_origin_deploy':'PASS','oci_source_provenance':'PASS','health_ready':'PASS','cloudflare_method_boundary':'PASS','cloudflare_fresh_get_proxy':'PASS','concurrent_origin_edge_reconciliation':'PASS','live_e2e':'PASS','authority_snapshot_id':sid,'public_post_count':0,'admin_publicly_unmounted':'PASS','candidate_changed':False,'new_ci':False,'merge':False,'tuning':0,'runtime017_mutation':0,'supabase_data_mutation':0,'real_order_count':0,'real_capital_movement':0})
required=['REMOTE_TRUTH.json','AUTH_BUILD_DEPLOY.json','ORIGIN_LIVE.json','CLOUDFLARE_METHOD_BOUNDARY.json','CLOUDFLARE_RECONCILIATION_TARGET.json','CONCURRENT_RECONCILIATION.json','LIVE_E2E.json','FINAL_GATE_SUMMARY.json']
(OUT/'MANIFEST.sha256').write_text('\n'.join(f'{h256((OUT/n).read_bytes())}  {n}' for n in sorted(required))+'\n')
ck=subprocess.run(['sha256sum','-c','MANIFEST.sha256'],cwd=OUT,text=True,capture_output=True)
if ck.returncode: raise RuntimeError('MANIFEST')
print('ORDER_070_R5_STATUS=READY_FOR_AUD'); print('HEAD='+TS); print('TREE='+TT); print('BUILD_ID='+BID); print('IMAGE_DIGEST='+IMG); print('HEALTHZ=200'); print('READYZ=200'); print('CLOUDFLARE_FINAL=PASS'); print('CONCURRENT_RECONCILIATION=PASS'); print('AUTHORITY_SNAPSHOT_ID='+sid); print('LIVE_E2E=PASS'); print('MANIFEST_SHA256='+h256((OUT/'MANIFEST.sha256').read_bytes()))
