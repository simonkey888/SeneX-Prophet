import datetime,hashlib,json,os,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path
API='https://api.northflank.com/v1'; P='seneciobot'; S='senecio-h011'; BID='next-muscle-6785'
TS='202f154a71915eb4b1e3cdf0e1eec8005760a028'; TT='f037c7621135305fd3ec5f37aa029dfc8a28aa4b'; BD='sha256:1806ad0bc71c45264695c1c8973a497a39f9903f867ece2d56fdbc12f44e4892'; IMG='sha256:ac295501a17cdc5bac59283c79418aca8388d077ba5fbe4563989db1c3314e03'; ORIGIN='https://h011-web--senecio-h011--wbjggn89fnf8.code.run'
TOKEN=os.environ['NORTHFLANK_API_TOKEN']; H={'Authorization':f'Bearer {TOKEN}','Accept':'application/json','Content-Type':'application/json','User-Agent':'senex-order070-bind/1'}; OUT=Path('order070-origin-evidence'); OUT.mkdir(exist_ok=True)
rec={'order':'ORDER-070-R3','phase':'OCI_BIND_AND_READY','target_sha':TS,'target_tree':TT,'build_id':BID,'build_digest':BD,'image_digest':IMG,'secret_values_exported':False,'real_order_count':0,'real_capital_movement':0,'runtime017_mutation':0,'production_learning_mutations':0,'supabase_quota_notice_observed':True,'supabase_causality_claimed':False,'requests':[],'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
def save():
 raw=(json.dumps(rec,sort_keys=True,indent=2)+'\n').encode(); (OUT/'oci-bind-receipt.json').write_bytes(raw); (OUT/'SHA256SUMS').write_text(hashlib.sha256(raw).hexdigest()+'  oci-bind-receipt.json\n')
def data(x): return x.get('data',x) if isinstance(x,dict) else x
def call(method,path,payload=None,label=None,query=None):
 u=API+path
 if query: u+='?'+urllib.parse.urlencode(query)
 body=None if payload is None else json.dumps(payload,separators=(',',':')).encode(); req=urllib.request.Request(u,headers=H,data=body,method=method); raw=b''
 try:
  with urllib.request.urlopen(req,timeout=90) as r: status=r.status; raw=r.read()
 except urllib.error.HTTPError as e: status=e.code; raw=e.read()
 dig=hashlib.sha256(raw).hexdigest(); safe=''
 try: obj=json.loads(raw.decode() or '{}'); safe=str(obj.get('message') or obj.get('error') or '')[:240] if isinstance(obj,dict) else ''
 except Exception: obj={}
 rec['requests'].append({'label':label or path,'method':method,'path':path,'status':status,'response_sha256':dig,'request_body_sha256':hashlib.sha256(body).hexdigest() if body else None,'safe_error':safe if status>=400 else ''}); save()
 if not 200<=status<300: raise RuntimeError(f'{label or path}:HTTP_{status}:{safe}')
 return data(obj)
def entry():
 x=call('GET',f'/projects/{P}/services',label='list_services',query={'per_page':100}); a=x.get('services') if isinstance(x,dict) else x; return next(v for v in a if v.get('id')==S)
def dep(label): return call('GET',f'/projects/{P}/services/{S}/deployment',label=label)
def public(path):
 req=urllib.request.Request(ORIGIN+path,headers={'Accept':'application/json','Cache-Control':'no-cache','User-Agent':'senex-order070-live/10'},method='GET')
 try:
  with urllib.request.urlopen(req,timeout=40) as r: status=r.status; raw=r.read()
 except urllib.error.HTTPError as e: status=e.code; raw=e.read()
 try: obj=json.loads(raw.decode())
 except Exception: obj={'non_json':True}
 return status,obj,hashlib.sha256(raw).hexdigest()
def fp(v): return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()
# Prove the selected OCI value is the manifest emitted by the exact successful build.
b=call('GET',f'/projects/{P}/services/{S}/build/{BID}',label='verify_exact_build')
if not b.get('concluded') or not b.get('success') or b.get('sha')!=TS: raise RuntimeError('EXACT_BUILD_IDENTITY_MISMATCH')
logs=call('GET',f'/projects/{P}/services/{S}/build-logs',label='verify_manifest_log',query={'buildId':BID,'queryType':'range','duration':86400,'lineLimit':1000,'direction':'backward','regexIncludes':IMG})
rows=logs if isinstance(logs,list) else []
manifest_hits=sum(1 for r in rows if IMG in str(r.get('log','')).lower() and 'manifest' in str(r.get('log','')).lower())
if manifest_hits<1: raise RuntimeError('OCI_MANIFEST_NOT_PROVEN')
rec['oci_identity']={'build_id':BID,'build_sha':TS,'manifest_digest':IMG,'manifest_log_hits':manifest_hits}; save()
# Read service-local runtime env in-process only; never persist or print values.
envdoc=call('GET',f'/projects/{P}/services/{S}/runtime-environment',label='runtime_env_before',query={'show':'this'})
env=envdoc.get('runtimeEnvironment') if isinstance(envdoc,dict) else None
if not isinstance(env,dict): raise RuntimeError('RUNTIME_ENV_NOT_READABLE')
before_keys=sorted(env); before_fp=fp(env); previous=env.get('SENEX_IMAGE_DIGEST'); previous_state='ABSENT' if previous is None else ('EXACT' if previous==IMG else ('UNKNOWN' if str(previous).lower()=='unknown' else 'OTHER'))
updated=dict(env); updated['SENEX_IMAGE_DIGEST']=IMG
rec['runtime_env_before']={'key_count':len(before_keys),'keys_sha256':fp(before_keys),'environment_sha256':before_fp,'senex_image_digest_state':previous_state}; save()
call('PATCH',f'/projects/{P}/services/combined/{S}',{'runtimeEnvironment':updated},label='bind_runtime_image_digest')
afterdoc=call('GET',f'/projects/{P}/services/{S}/runtime-environment',label='runtime_env_after',query={'show':'this'}); after=afterdoc.get('runtimeEnvironment') if isinstance(afterdoc,dict) else None
if not isinstance(after,dict) or after.get('SENEX_IMAGE_DIGEST')!=IMG: raise RuntimeError('RUNTIME_DIGEST_BIND_NOT_VERIFIED')
if {k:v for k,v in after.items() if k!='SENEX_IMAGE_DIGEST'}!={k:v for k,v in env.items() if k!='SENEX_IMAGE_DIGEST'}: raise RuntimeError('RUNTIME_ENV_DRIFT')
rec['runtime_env_after']={'key_count':len(after),'keys_sha256':fp(sorted(after)),'environment_sha256':fp(after),'non_target_environment_preserved':True}; save()
# Force a rollout of the already-built exact SHA; no rebuild and no HEAD movement.
d0=dep('deployment_before_rollout'); e0=entry()
if (d0.get('internal') or {}).get('deployedSHA')!=TS or e0.get('disabledCI') is not True or e0.get('disabledCD') is not True: raise RuntimeError('PRE_ROLLOUT_CONTROL_DRIFT')
call('POST',f'/projects/{P}/services/{S}/deployment',{'internal':{'buildSHA':TS}},label='rollout_same_exact_sha')
end=time.time()+1800; stable=None
while time.time()<end:
 d=dep('deployment_poll'); e=entry(); ii=d.get('internal') or {}; st=((e.get('status') or {}).get('deployment') or {}).get('status')
 if ii.get('deployedSHA')==TS and st=='COMPLETED': stable=(d,e); break
 if st=='FAILED': raise RuntimeError('ROLLOUT_FAILED')
 time.sleep(10)
if not stable: raise RuntimeError('ROLLOUT_TIMEOUT')
# Readiness/provenance gate: no Supabase causal attribution; report actual checks only.
end=time.time()+600; live={}
while time.time()<end:
 live={}
 for p,k in [('/healthz','healthz'),('/readyz','readyz'),('/api/runtime/provenance','provenance'),('/api/authority/snapshot','authority')]:
  st,obj,dig=public(p); live[k]={'http':st,'body':obj,'sha256':dig}
 prov=live['provenance']['body'] if isinstance(live['provenance']['body'],dict) else {}; checks=prov.get('checks') or {}
 if live['healthz']['http']==200 and live['readyz']['http']==200 and live['provenance']['http']==200 and prov.get('exact') is True and prov.get('source_commit')==TS and prov.get('source_tree')==TT and prov.get('build_digest')==BD and prov.get('image_digest')==IMG and all(checks.values()): break
 time.sleep(10)
else: raise RuntimeError('LIVE_EXACT_READINESS_TIMEOUT')
d,e=stable; ii=d.get('internal') or {}; rec['deployment']={'deployed_sha':ii.get('deployedSHA'),'build_sha':ii.get('buildSHA'),'build_id':ii.get('buildId'),'source_branch':(call('GET',f'/projects/{P}/services/{S}',label='service_final').get('vcsData') or {}).get('projectBranch'),'disabled_ci':e.get('disabledCI'),'disabled_cd':e.get('disabledCD'),'status':((e.get('status') or {}).get('deployment') or {}).get('status')}; rec['live']=live; rec['origin_exact_ready']='PASS'; rec['completed_at']=datetime.datetime.now(datetime.timezone.utc).isoformat(); save()
print('OCI_MANIFEST='+IMG); print('RUNTIME_ENV_NON_TARGET_PRESERVED=YES'); print('DEPLOYED_SHA='+str(ii.get('deployedSHA'))); print('HEALTHZ='+str(live['healthz']['http'])); print('READYZ='+str(live['readyz']['http'])); print('PROVENANCE_EXACT='+str(live['provenance']['body'].get('exact')).lower()); print('SUPABASE_CAUSALITY_CLAIMED=NO')
