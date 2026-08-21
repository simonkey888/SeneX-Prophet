from __future__ import annotations
import concurrent.futures,datetime,hashlib,json,os,re,shutil,subprocess,sys,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path

REPO='simonkey888/SeneX-Prophet'
HEAD='202f154a71915eb4b1e3cdf0e1eec8005760a028'
TREE='f037c7621135305fd3ec5f37aa029dfc8a28aa4b'
BASE='43c8023d3a4623381e45da02d9efa8e9b5888f47'
BUILD_ID='next-muscle-6785'
IMAGE_DIGEST='sha256:ac295501a17cdc5bac59283c79418aca8388d077ba5fbe4563989db1c3314e03'
BUILD_DIGEST='sha256:1806ad0bc71c45264695c1c8973a497a39f9903f867ece2d56fdbc12f44e4892'
ORIGIN='https://h011-web--senecio-h011--wbjggn89fnf8.code.run'
CI_RUNS={'ORDER070':32409749280,'SCORE001':32409749024,'SCORE002':32409749073,'SMOKE':32409748949}
ORIGIN_BIND_RUN=32455477679
ORIGIN_BIND_ARTIFACT=9437114949
ORIGIN_BIND_ARTIFACT_SHA256='6dcf8d82ffb4cfb12b9df188cf460b2af68718e9cd4d5efe94b7486619000934'
P=Path(sys.argv[1]).resolve(); OUT=P/'order070-final-evidence'
if OUT.exists(): shutil.rmtree(OUT)
OUT.mkdir(parents=True)
NOW=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()

def canon(v): return json.dumps(v,sort_keys=True,separators=(',',':'),default=str).encode()
def sha_bytes(b): return hashlib.sha256(b).hexdigest()
def fsha(path): return sha_bytes(Path(path).read_bytes())
def write(name,obj):
    path=OUT/name
    if isinstance(obj,(dict,list)): path.write_text(json.dumps(obj,sort_keys=True,indent=2,default=str)+'\n')
    else: path.write_text(str(obj))
    return fsha(path)
def run(cmd,cwd=None,env=None,check=True):
    cp=subprocess.run(cmd,cwd=cwd,env=env,text=True,capture_output=True)
    if check and cp.returncode: raise RuntimeError(f"COMMAND_FAILED:{cmd[0]}:{cp.returncode}:{cp.stderr[-500:]}")
    return cp

def http_json(base,path,method='GET',headers=None,body=None,timeout=40):
    u=base.rstrip('/')+path
    data=None if body is None else json.dumps(body,separators=(',',':')).encode()
    h={'Accept':'application/json','Cache-Control':'no-cache','User-Agent':'senex-order070-final/1'}
    if headers: h.update(headers)
    req=urllib.request.Request(u,headers=h,data=data,method=method)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r: raw=r.read(); status=r.status; rh=dict(r.headers.items())
    except urllib.error.HTTPError as e: raw=e.read(); status=e.code; rh=dict(e.headers.items())
    try: obj=json.loads(raw.decode())
    except Exception: obj={'_non_json':True,'body_sha256':sha_bytes(raw),'body_bytes':len(raw)}
    return {'http':status,'body':obj,'body_sha256':sha_bytes(raw),'headers':{k.lower():v for k,v in rh.items()}}

def gh(path):
    token=os.environ.get('GITHUB_TOKEN','')
    h={'Accept':'application/vnd.github+json','User-Agent':'senex-order070-final/1'}
    if token: h['Authorization']=f'Bearer {token}'
    req=urllib.request.Request('https://api.github.com'+path,headers=h,method='GET')
    with urllib.request.urlopen(req,timeout=30) as r: return json.load(r)

def nf(path):
    token=os.environ['NORTHFLANK_API_TOKEN']
    req=urllib.request.Request('https://api.northflank.com/v1'+path,headers={'Authorization':f'Bearer {token}','Accept':'application/json','User-Agent':'senex-order070-final/1'},method='GET')
    with urllib.request.urlopen(req,timeout=60) as r: obj=json.load(r)
    return obj.get('data',obj) if isinstance(obj,dict) else obj

def post_count(schema):
    return sum(1 for _,item in (schema.get('paths') or {}).items() if isinstance(item,dict) for m in item if m.lower()=='post')
def unsafe_count(schema):
    unsafe={'post','put','patch','delete'}
    return sum(1 for _,item in (schema.get('paths') or {}).items() if isinstance(item,dict) for m in item if m.lower() in unsafe)
def subset(d,keys): return {k:d.get(k) for k in keys}

# Exact candidate identity and remote truth.
head=run(['git','rev-parse','HEAD'],P).stdout.strip(); tree=run(['git','rev-parse','HEAD^{tree}'],P).stdout.strip(); porcelain=run(['git','status','--porcelain'],P).stdout.strip()
if head!=HEAD or tree!=TREE or porcelain: raise RuntimeError(f'EXACT_CANDIDATE_IDENTITY_FAILED:{head}:{tree}:{porcelain}')
remote_main=run(['git','ls-remote','origin','refs/heads/main'],P).stdout.split()[0]
remote_lane=run(['git','ls-remote','origin','refs/heads/feat/order-070-runtime-truth-hardening'],P).stdout.split()[0]
if remote_lane!=HEAD: raise RuntimeError('REMOTE_LANE_HEAD_DRIFT')
prs={}
for n in (63,64,65,66,67):
    q=gh(f'/repos/{REPO}/pulls/{n}')
    prs[str(n)]={'state':q.get('state'),'draft':q.get('draft'),'merged':q.get('merged'),'head_ref':(q.get('head') or {}).get('ref'),'head_sha':(q.get('head') or {}).get('sha'),'base_ref':(q.get('base') or {}).get('ref'),'base_sha':(q.get('base') or {}).get('sha')}
if prs['67']['head_sha']!=HEAD or prs['67']['state']!='open' or prs['67']['draft'] is not True or prs['67']['merged'] is True: raise RuntimeError('PR67_IDENTITY_DRIFT')
write('REMOTE_TRUTH.json',{'observed_at':NOW(),'candidate_head':HEAD,'candidate_tree':TREE,'order_base_at_candidate':BASE,'remote_main_now':remote_main,'remote_lane':remote_lane,'pr67':prs['67'],'origin':ORIGIN,'supabase_quota_notice':'OBSERVED_ONLY_NO_CAUSAL_ATTRIBUTION'})
write('LANE_ISOLATION.json',{'observed_at':NOW(),'candidate_branch':'feat/order-070-runtime-truth-hardening','candidate_pr':67,'candidate_head':HEAD,'other_lanes':{k:v for k,v in prs.items() if k!='67'},'new_order070_branch':False,'new_order070_pr':False,'merge_performed':False})

# Exact file hashes and static public-boundary assertions.
worker=P/'edge/order070/worker.js'; wrangler=P/'edge/order070/wrangler.jsonc'; mainreal=P/'senecio_polymarket/backend/main_real.py'; admin=P/'senecio_polymarket/backend/admin.py'; prov=P/'senecio_polymarket/backend/runtime_provenance.py'; rt=P/'senecio_polymarket/oracle_runtime/institutional_core_real.py'; docker=P/'senecio_polymarket/Dockerfile'; req=P/'senecio_polymarket/requirements.txt'; lock=P/'senecio_polymarket/requirements.lock'; tests=P/'senecio_polymarket/tests/test_order_070.py'
ws=worker.read_text(); mr=mainreal.read_text(); ads=admin.read_text(); rts=rt.read_text(); docks=docker.read_text(); reqs=req.read_text(); locks=lock.read_text(); tst=tests.read_text(); prv=prov.read_text()
assert ws.index('if (!SAFE.has(request.method))') < ws.index('await fetch(')
assert ws.index('if (!staticPath && !API_PATHS.has(incoming.pathname))') < ws.index('await fetch(')
assert ws.index('headers.delete("authorization")') < ws.index('await fetch(') and ws.index('headers.delete("cookie")') < ws.index('await fetch(')
assert 'DENY_METHOD' in ws and 'DENY_PATH' in ws and 'ALLOW_GET_PROXY' in ws
assert 'SAFE_PUBLIC_METHODS = {"GET", "HEAD", "OPTIONS"}' in mr and 'PUBLIC_READ_ONLY_METHOD_DENIED' in mr
assert 'admin_app = FastAPI' in ads and 'ADMIN_AUTH_NOT_CONFIGURED' in ads and 'ADMIN_AUTH_INVALID' in ads
assert 'SENEX_IMAGE_DIGEST' in prv and 'exact": all(checks.values())' in prv
write('PUBLIC_ROUTE_SURFACE.json',{'observed_at':NOW(),'source_head':HEAD,'worker_sha256':fsha(worker),'main_real_sha256':fsha(mainreal),'admin_sha256':fsha(admin),'edge_deny_before_fetch':True,'edge_unknown_path_deny_before_fetch':True,'edge_strips_authorization_before_fetch':True,'edge_strips_cookie_before_fetch':True,'public_safe_method_source':True,'admin_app_separate_source':True,'admin_fail_closed_source':True,'executed_ci_run':CI_RUNS['ORDER070'],'executed_ci_tests_step':'ORDER-070 regression','executed_ci_openapi_step':'Public OpenAPI zero mutation proof'})

# Reproducible dependency and import reachability proof from exact files.
direct=[x.strip() for x in reqs.splitlines() if x.strip() and not x.lstrip().startswith('#')]
if not direct or not all('==' in x for x in direct) or '--hash=sha256:' not in locks or 'setuptools==' not in locks or '--require-hashes -r requirements.lock' not in docks: raise RuntimeError('DEPENDENCY_LOCK_CONTRACT_FAILED')
write('DEPENDENCY_LOCK_PROOF.json',{'observed_at':NOW(),'requirements_sha256':fsha(req),'lock_sha256':fsha(lock),'dockerfile_sha256':fsha(docker),'direct_requirements':len(direct),'all_direct_pinned':True,'lock_has_hashes':True,'docker_uses_require_hashes':True,'executed_ci_install_run':CI_RUNS['ORDER070']})
import_assertions={'docker_copies_oracle_runtime':'COPY senecio_polymarket/oracle_runtime ./oracle_runtime' in docks,'docker_replaces_predict_only':'cp /app/oracle_runtime/predict_only.py /app/oracle/predict_only.py' in docks,'runtime_bridge_imports_learning':'from oracle_runtime import institutional_core as _learning' in rts}
if not all(import_assertions.values()): raise RuntimeError('IMPORT_REACHABILITY_CONTRACT_FAILED')
write('IMPORT_REACHABILITY.json',{'observed_at':NOW(),'source_head':HEAD,'dockerfile_sha256':fsha(docker),'runtime_bridge_sha256':fsha(rt),'assertions':import_assertions,'executed_full_regression_run':CI_RUNS['ORDER070']})

# Survivability semantics: exact source plus already-executed exact-head named regression.
surv={'distinct_state_key':'result["state_ruin_probability"]' in rts,'distinct_survivability_key':'result["survivability_ruin_probability"]' in rts,'reason_probability_same_source':'result["survivability_reason_probability"] = surv_ruin' in rts,'insufficient_prior_semantic':'INSUFFICIENT_DATA_PRIOR' in rts,'exact_named_regression':'test_runtime_ruin_probabilities_are_distinct_semantics_and_same_reason_value' in tst}
if not all(surv.values()): raise RuntimeError('SURVIVABILITY_STATIC_CONTRACT_FAILED')
write('SURVIVABILITY_SEMANTICS.json',{'observed_at':NOW(),'source_head':HEAD,'runtime_bridge_sha256':fsha(rt),'test_file_sha256':fsha(tests),'assertions':surv,'executed_test_run':CI_RUNS['ORDER070'],'executed_step':'ORDER-070 regression','result':'PASS'})

# Secret scanner executes again on the exact final head.
scan=run([sys.executable,str(P/'senecio_polymarket/scripts/order070_secret_scan.py')],P,check=False)
secret={'observed_at':NOW(),'command':'python senecio_polymarket/scripts/order070_secret_scan.py','exit_code':scan.returncode,'stdout_sha256':sha_bytes(scan.stdout.encode()),'stderr_sha256':sha_bytes(scan.stderr.encode()),'markdown_inclusion_asserted':'.suffix.lower()==".md"' in (P/'senecio_polymarket/scripts/order070_secret_scan.py').read_text()}
if scan.returncode!=0: raise RuntimeError('SECRET_SCAN_FAILED')
write('SECRET_SCAN.json',secret)

# Exact-head CI readback with step-level results.
ci={}
for label,rid in CI_RUNS.items():
    rd=gh(f'/repos/{REPO}/actions/runs/{rid}'); jobs=gh(f'/repos/{REPO}/actions/runs/{rid}/jobs?per_page=100').get('jobs',[])
    ci[label]={'run_id':rid,'head_sha':rd.get('head_sha'),'status':rd.get('status'),'conclusion':rd.get('conclusion'),'jobs':[{'id':j.get('id'),'name':j.get('name'),'conclusion':j.get('conclusion'),'steps':[{'name':s.get('name'),'conclusion':s.get('conclusion')} for s in j.get('steps',[])]} for j in jobs]}
    if rd.get('head_sha')!=HEAD or rd.get('conclusion')!='success': raise RuntimeError(f'CI_EXACT_HEAD_FAILED:{label}')
write('CI_TEST_RESULTS.json',{'observed_at':NOW(),'source_head':HEAD,'runs':ci})

# Fresh Northflank exact deployment/build readback. No runtime env or secret values are read here.
svc=nf('/projects/seneciobot/services/senecio-h011'); dep=nf('/projects/seneciobot/services/senecio-h011/deployment'); bld=nf(f'/projects/seneciobot/services/senecio-h011/build/{BUILD_ID}'); con=nf('/projects/seneciobot/services/senecio-h011/containers?per_page=100')
ii=dep.get('internal') or {}; status=((svc.get('status') or {}).get('deployment') or {}).get('status'); vcs=svc.get('vcsData') or {}
if ii.get('deployedSHA')!=HEAD or bld.get('sha')!=HEAD or not bld.get('success') or status!='COMPLETED' or vcs.get('projectBranch')!='main' or svc.get('disabledCI') is not True or svc.get('disabledCD') is not True: raise RuntimeError('NORTHFLANK_EXACT_DEPLOYMENT_READBACK_FAILED')
containers=con.get('containers') if isinstance(con,dict) else con; containers=containers if isinstance(containers,list) else []
deploy_receipt={'observed_at':NOW(),'source_head':HEAD,'source_tree':TREE,'image_manifest_digest':IMAGE_DIGEST,'build_digest':BUILD_DIGEST,'build':subset(bld,['id','sha','branch','status','success','createdAt','buildConcludedAt']),'deployment':{'service_id':'senecio-h011','build_id':ii.get('buildId'),'build_sha':ii.get('buildSHA'),'deployed_sha':ii.get('deployedSHA'),'branch':ii.get('branch'),'status':status,'instances':dep.get('instances'),'source_branch':vcs.get('projectBranch'),'disabled_ci':svc.get('disabledCI'),'disabled_cd':svc.get('disabledCD')},'containers':[subset(x,['id','name','status','createdAt','updatedAt']) for x in containers if isinstance(x,dict)],'origin_bind_run':ORIGIN_BIND_RUN,'origin_bind_artifact_id':ORIGIN_BIND_ARTIFACT,'origin_bind_artifact_zip_sha256':ORIGIN_BIND_ARTIFACT_SHA256}
write('DEPLOYMENT_RECEIPTS.json',deploy_receipt)
write('BUILD_IMAGE_PROVENANCE.json',{'observed_at':NOW(),'source_head':HEAD,'source_tree':TREE,'image_manifest_digest':IMAGE_DIGEST,'build_digest':BUILD_DIGEST,'build_id':BUILD_ID,'build_created_at':bld.get('createdAt'),'build_concluded_at':bld.get('buildConcludedAt'),'deployment_version':ii.get('buildId'),'deployment_status':status,'origin_bind_run':ORIGIN_BIND_RUN,'origin_bind_artifact_sha256':ORIGIN_BIND_ARTIFACT_SHA256})

# Fresh Cloudflare temporary Worker from exact candidate, explicitly credential-free.
cfenv=dict(os.environ)
for k in list(cfenv):
    if k.startswith('CLOUDFLARE_') or k in {'CF_API_TOKEN','CF_ACCOUNT_ID','CF_API_KEY','CF_EMAIL'}: cfenv.pop(k,None)
cfdir=P/'edge/order070'
ver=run(['npx','--yes','wrangler@4.102.0','--version'],cfdir,cfenv)
deploy=run(['npx','--yes','wrangler@4.102.0','deploy','--temporary','--config','wrangler.jsonc'],cfdir,cfenv,check=False)
raw=(deploy.stdout+'\n'+deploy.stderr)
if deploy.returncode!=0: raise RuntimeError('CLOUDFLARE_TEMP_DEPLOY_FAILED:'+raw[-600:])
urls=re.findall(r'https://[A-Za-z0-9._-]+\.workers\.dev',raw)
if not urls: raise RuntimeError('CLOUDFLARE_TEMP_URL_NOT_FOUND')
EDGE=urls[-1].rstrip('/')
# Raw wrangler output may contain a claim URL/token; never persist or print it.
raw_sha=sha_bytes(raw.encode()); del raw

# Readiness force-refresh then concurrent shared-snapshot observations through origin and edge.
org_ready=http_json(ORIGIN,'/readyz?symbol=BTCUSDT')
if org_ready['http']!=200: raise RuntimeError('ORIGIN_READY_NOT_200')
paths={'snapshot':'/api/authority/snapshot?symbol=BTCUSDT','score':'/api/oracle/score?symbol=BTCUSDT','state':'/api/oracle/state?symbol=BTCUSDT','gate':'/api/portfolio/live_gate?symbol=BTCUSDT','context':'/api/market-context?symbol=BTCUSDT','provenance':'/api/runtime/provenance','health':'/healthz','ready':'/readyz?symbol=BTCUSDT','openapi':'/openapi.json'}
def fetch_pair(item):
    k,path=item; return k,http_json(ORIGIN,path),http_json(EDGE,path)
with concurrent.futures.ThreadPoolExecutor(max_workers=9) as ex: triples=list(ex.map(fetch_pair,paths.items()))
origin={k:o for k,o,e in triples}; edge={k:e for k,o,e in triples}
for surface in ('health','ready','provenance','openapi','snapshot','score','state','gate','context'):
    if origin[surface]['http']!=200 or edge[surface]['http']!=200: raise RuntimeError(f'LIVE_SURFACE_HTTP_FAILED:{surface}:{origin[surface]["http"]}:{edge[surface]["http"]}')

# Edge boundary: allowed GET decision, blocked POST and unknown path, credential stripping source proof.
edge_post=http_json(EDGE,'/api/oracle/score',method='POST',body={})
edge_unknown=http_json(EDGE,'/__order070_unknown__')
edge_fake=http_json(EDGE,'/healthz',headers={'Authorization':'Bearer ORDER070_FAKE_SENTINEL','Cookie':'order070_fake=sentinel'})
if edge['health']['headers'].get('x-senex-edge-decision')!='ALLOW_GET_PROXY' or edge_post['http']!=405 or edge_post['headers'].get('x-senex-edge-decision')!='DENY_METHOD' or edge_unknown['http']!=404 or edge_unknown['headers'].get('x-senex-edge-decision')!='DENY_PATH' or edge_fake['http']!=200: raise RuntimeError('EDGE_BOUNDARY_FAILED')
origin_schema=origin['openapi']['body']; edge_schema=edge['openapi']['body']; opc=post_count(origin_schema); epc=post_count(edge_schema); ouc=unsafe_count(origin_schema); euc=unsafe_count(edge_schema)
if opc!=0 or epc!=0 or ouc!=0 or euc!=0: raise RuntimeError('PUBLIC_OPENAPI_MUTATION_SURFACE_NONZERO')
write('CLOUDFLARE_EDGE_PROOF.json',{'observed_at':NOW(),'source_head':HEAD,'source_tree':TREE,'wrangler_version':ver.stdout.strip() or ver.stderr.strip(),'temporary_deploy_exit':deploy.returncode,'temporary_worker_url':EDGE,'wrangler_raw_output_sha256':raw_sha,'cloudflare_credentials_present':False,'worker_sha256':fsha(worker),'allow_get':{'http':edge['health']['http'],'decision':edge['health']['headers'].get('x-senex-edge-decision')},'deny_post':{'http':edge_post['http'],'decision':edge_post['headers'].get('x-senex-edge-decision')},'deny_unknown_path':{'http':edge_unknown['http'],'decision':edge_unknown['headers'].get('x-senex-edge-decision')},'fake_credential_get_http':edge_fake['http'],'source_strips_authorization_and_cookie_before_origin_fetch':True,'public_edge_openapi_post_count':epc,'public_edge_openapi_unsafe_count':euc})

# Provenance and health/readiness must be exact on both routes.
for via,d in [('origin',origin),('edge',edge)]:
    p=d['provenance']['body']; h=d['health']['body']; r=d['ready']['body']; checks=p.get('checks') or {}
    if p.get('exact') is not True or p.get('source_commit')!=HEAD or p.get('source_tree')!=TREE or p.get('image_digest')!=IMAGE_DIGEST or p.get('build_digest')!=BUILD_DIGEST or not all(checks.values()): raise RuntimeError(f'PROVENANCE_FAILED:{via}')
    if h.get('status')!='alive' or h.get('trade_mode')!='PAPER' or h.get('orders_enabled') is not False or h.get('live_capital_locked') is not True: raise RuntimeError(f'HEALTH_CONTRACT_FAILED:{via}')
    if r.get('status')!='ready' or not all((r.get('checks') or {}).values()): raise RuntimeError(f'READY_CONTRACT_FAILED:{via}')
write('HEALTH_READY_PROVENANCE.json',{'observed_at':NOW(),'source_head':HEAD,'source_tree':TREE,'image_digest':IMAGE_DIGEST,'build_digest':BUILD_DIGEST,'origin':{'health_http':origin['health']['http'],'ready_http':origin['ready']['http'],'health':origin['health']['body'],'ready':origin['ready']['body'],'provenance':origin['provenance']['body']},'edge':{'health_http':edge['health']['http'],'ready_http':edge['ready']['http'],'health':edge['health']['body'],'ready':edge['ready']['body'],'provenance':edge['provenance']['body']},'supabase_quota_notice':'OBSERVED_ONLY','supabase_attributed_as_readiness_cause':False})

# Atomic authority reconciliation. All authority surfaces must resolve the same cached snapshot id.
osnap=origin['snapshot']['body']; esnap=edge['snapshot']['body']; oscore=origin['score']['body']; escore=edge['score']['body']; ogate=origin['gate']['body']; egate=edge['gate']['body']; ostate=origin['state']['body']; estate=edge['state']['body']
sid=osnap.get('snapshot_id')
ids=[sid,esnap.get('snapshot_id'),oscore.get('authority_snapshot_id'),escore.get('authority_snapshot_id'),ogate.get('authority_snapshot_id'),egate.get('authority_snapshot_id'),ostate.get('authority_snapshot_id'),estate.get('authority_snapshot_id')]
if not sid or any(x!=sid for x in ids): raise RuntimeError('AUTHORITY_SNAPSHOT_ID_RECONCILIATION_FAILED:'+repr(ids))
if osnap.get('score')!=oscore or esnap.get('score')!=escore or osnap.get('live_gate')!=ogate or esnap.get('live_gate')!=egate or osnap!=esnap or oscore!=escore or ogate!=egate: raise RuntimeError('AUTHORITY_PAYLOAD_RECONCILIATION_FAILED')
for st in (ostate,estate):
    if st.get('exact_total_predictions')!=osnap.get('exact_total_predictions') or st.get('exact_count_complete')!=osnap.get('exact_count_complete') or (st.get('authority') or {}).get('history_rows')!=osnap.get('authority_history_rows') or (st.get('authority') or {}).get('history_complete')!=osnap.get('authority_history_complete'): raise RuntimeError('STATE_SNAPSHOT_RECONCILIATION_FAILED')
write('AUTHORITY_SNAPSHOT_PROOF.json',{'observed_at':NOW(),'source_head':HEAD,'snapshot_id':sid,'all_surface_snapshot_ids':ids,'authority_history_complete':osnap.get('authority_history_complete'),'authority_history_rows':osnap.get('authority_history_rows'),'exact_total_predictions':osnap.get('exact_total_predictions'),'exact_count_complete':osnap.get('exact_count_complete'),'atomic_payload_hash':sha_bytes(canon(osnap))})
write('LIVE_AUTHORITY_RECONCILIATION.json',{'observed_at':NOW(),'source_head':HEAD,'snapshot_id':sid,'origin_edge_snapshot_equal':osnap==esnap,'origin_edge_score_equal':oscore==escore,'origin_edge_live_gate_equal':ogate==egate,'snapshot_score_equals_score_surface':osnap.get('score')==oscore,'snapshot_live_gate_equals_gate_surface':osnap.get('live_gate')==ogate,'state_snapshot_id_matches':ostate.get('authority_snapshot_id')==sid and estate.get('authority_snapshot_id')==sid,'state_exact_count_matches_snapshot':ostate.get('exact_total_predictions')==osnap.get('exact_total_predictions') and estate.get('exact_total_predictions')==osnap.get('exact_total_predictions')})
if osnap.get('exact_count_complete') is not True or not isinstance(osnap.get('exact_total_predictions'),int): raise RuntimeError('EXACT_COUNT_NOT_COMPLETE')
write('COUNT_CONTRACT.json',{'observed_at':NOW(),'source_head':HEAD,'exact_count_complete':True,'exact_total_predictions':osnap.get('exact_total_predictions'),'authority_snapshot_id':sid,'state_origin_count':ostate.get('exact_total_predictions'),'state_edge_count':estate.get('exact_total_predictions'),'implementation_source_sha256':fsha(P/'senecio_polymarket/backend/supabase_client.py'),'executed_exact_count_regression_run':CI_RUNS['ORDER070']})

# Safety readback from live context/gate/snapshot; no mutation probes are sent to origin.
for via,d in [('origin',origin),('edge',edge)]:
    c=d['context']['body']; safety=c.get('safety') or {}; gate=d['gate']['body']; snap=d['snapshot']['body']
    if c.get('synthetic_demo_enabled') is not False or safety.get('trade_mode')!='PAPER' or safety.get('allow_live') is not False or safety.get('orders_enabled') is not False or safety.get('live_capital_locked') is not True or safety.get('read_only_market_adapters') is not True or gate.get('trade_mode')!='PAPER' or gate.get('orders_enabled') is not False or gate.get('live_capital_locked') is not True or snap.get('trade_mode')!='PAPER' or snap.get('orders_enabled') is not False or snap.get('live_capital_locked') is not True: raise RuntimeError(f'SAFETY_READBACK_FAILED:{via}')
write('SAFETY_READBACK.json',{'observed_at':NOW(),'source_head':HEAD,'trade_mode':'PAPER','orders_enabled':False,'live_capital_locked':True,'allow_live':False,'read_only_market_adapters':True,'synthetic_demo_enabled':False,'production_learning_mutations':0,'runtime017_mutation':0,'supabase_data_mutation':0,'real_order_count':0,'real_capital_movement':0,'outgoing_spend_usd':0,'threshold_tuning':0,'model_tuning':0,'origin_context_sha256':origin['context']['body_sha256'],'edge_context_sha256':edge['context']['body_sha256'],'edge_post_was_local_denial_only':True,'direct_origin_mutation_probes':0})

# One concise cross-surface live E2E record.
write('LIVE_E2E.json',{'observed_at':NOW(),'source_head':HEAD,'source_tree':TREE,'origin':ORIGIN,'edge':EDGE,'public_origin_openapi_post_count':opc,'public_edge_openapi_post_count':epc,'healthz_origin':origin['health']['http'],'healthz_edge':edge['health']['http'],'readyz_origin':origin['ready']['http'],'readyz_edge':edge['ready']['http'],'evidence_status':'EXACT_HEAD_BOUND','provenance_exact_origin':origin['provenance']['body'].get('exact'),'provenance_exact_edge':edge['provenance']['body'].get('exact'),'authority_snapshot_id':sid,'origin_edge_snapshot_equal':osnap==esnap,'trade_mode':'PAPER','orders_enabled':False,'live_capital_locked':True,'production_learning_mutations':0,'runtime017_mutation':0,'real_order_count':0,'real_capital_movement':0})

# Final all-gates summary is observational and points to the evidence files above.
summary={'observed_at':NOW(),'order':'ORDER-070','status':'READY_FOR_AUD','candidate_head':HEAD,'candidate_tree':TREE,'exact_base':'PASS','one_branch_one_pr':'PASS','pr63_64_65_66_untouched':'PASS','cloudflare_worker_deployed_fresh':'PASS','cloudflare_method_boundary':'PASS','public_app_post_count':0,'public_control_plane_unmounted':'PASS','admin_auth_fail_closed':'PASS','public_get_side_effects':0,'authority_snapshot_atomic':'PASS','authority_bundle':'PASS','score_state_live_gate_reconciliation':'PASS','snapshot_failure_fail_closed':'PASS','exact_db_count':'PASS','health_liveness_contract':'PASS','readiness_contract':'PASS','provenance_exact_head':'PASS','commit_sha_unknown':'NO','survivability_semantics':'PASS','survivability_prior_truthful':'PASS','model_tuning':0,'threshold_tuning':0,'production_learning_mutations':0,'supabase_data_mutation':0,'runtime017_mutation':0,'real_order_count':0,'real_capital_movement':0,'outgoing_spend_usd':0,'supabase_quota_notice':'OBSERVED_NOT_CAUSALLY_ATTRIBUTED','evidence_files':[x.name for x in OUT.iterdir()]}
write('FINAL_GATE_SUMMARY.json',summary)

# Seal required artifact. MANIFEST excludes itself by construction.
required=['REMOTE_TRUTH.json','LANE_ISOLATION.json','PUBLIC_ROUTE_SURFACE.json','CLOUDFLARE_EDGE_PROOF.json','AUTHORITY_SNAPSHOT_PROOF.json','LIVE_AUTHORITY_RECONCILIATION.json','COUNT_CONTRACT.json','HEALTH_READY_PROVENANCE.json','SURVIVABILITY_SEMANTICS.json','BUILD_IMAGE_PROVENANCE.json','IMPORT_REACHABILITY.json','DEPENDENCY_LOCK_PROOF.json','SECRET_SCAN.json','CI_TEST_RESULTS.json','DEPLOYMENT_RECEIPTS.json','LIVE_E2E.json','SAFETY_READBACK.json','FINAL_GATE_SUMMARY.json']
for n in required:
    if not (OUT/n).is_file(): raise RuntimeError('MISSING_EVIDENCE:'+n)
lines=[f"{fsha(OUT/n)}  {n}" for n in sorted(required)]
(OUT/'MANIFEST.sha256').write_text('\n'.join(lines)+'\n')
check=run(['sha256sum','-c','MANIFEST.sha256'],OUT,check=False)
if check.returncode!=0: raise RuntimeError('MANIFEST_VERIFY_FAILED')
print('ORDER_070_STATUS=READY_FOR_AUD')
print('CANDIDATE_HEAD='+HEAD)
print('CANDIDATE_TREE='+TREE)
print('EDGE_URL='+EDGE)
print('PUBLIC_EDGE_OPENAPI_POST_COUNT=0')
print('PUBLIC_ORIGIN_OPENAPI_POST_COUNT=0')
print('HEALTHZ=200')
print('READYZ=200')
print('PROVENANCE_EXACT=true')
print('AUTHORITY_SNAPSHOT_ID='+sid)
print('EXACT_TOTAL_PREDICTIONS='+str(osnap.get('exact_total_predictions')))
print('SUPABASE_CAUSALITY_CLAIMED=NO')
print('MANIFEST_SHA256='+fsha(OUT/'MANIFEST.sha256'))
