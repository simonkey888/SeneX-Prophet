import datetime,hashlib,json,os,time,urllib.error,urllib.request
from pathlib import Path
API='https://api.northflank.com/v1'; P='seneciobot'; S='senecio-h011'; BID='next-muscle-6785'
TB='feat/order-070-runtime-truth-hardening'; TS='202f154a71915eb4b1e3cdf0e1eec8005760a028'; TT='f037c7621135305fd3ec5f37aa029dfc8a28aa4b'; BD='sha256:1806ad0bc71c45264695c1c8973a497a39f9903f867ece2d56fdbc12f44e4892'; ORIGIN='https://h011-web--senecio-h011--wbjggn89fnf8.code.run'
TOKEN=os.environ['NORTHFLANK_API_TOKEN']; H={'Authorization':f'Bearer {TOKEN}','Accept':'application/json','Content-Type':'application/json','User-Agent':'senex-order070-deploy/8'}; OUT=Path('order070-origin-evidence'); OUT.mkdir(exist_ok=True)
rec={'order':'ORDER-070-R3','target_sha':TS,'target_tree':TT,'build_id':BID,'build_digest':BD,'secret_values_exported':False,'real_order_count':0,'real_capital_movement':0,'runtime017_mutation':0,'production_learning_mutations':0,'requests':[],'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
def save():
 raw=(json.dumps(rec,sort_keys=True,indent=2)+'\n').encode(); (OUT/'origin-deploy-receipt.json').write_bytes(raw); (OUT/'SHA256SUMS').write_text(hashlib.sha256(raw).hexdigest()+'  origin-deploy-receipt.json\n')
def data(x): return x.get('data',x) if isinstance(x,dict) else x
def call(method,path,payload=None,label=None):
 body=None if payload is None else json.dumps(payload,separators=(',',':')).encode(); req=urllib.request.Request(API+path,headers=H,data=body,method=method); raw=b''
 try:
  with urllib.request.urlopen(req,timeout=90) as r: status=r.status; raw=r.read()
 except urllib.error.HTTPError as e: status=e.code; raw=e.read()
 dig=hashlib.sha256(raw).hexdigest(); safe=''
 try: obj=json.loads(raw.decode() or '{}'); safe=str(obj.get('message') or obj.get('error') or '')[:240] if isinstance(obj,dict) else ''
 except Exception: obj={}
 rec['requests'].append({'label':label or path,'method':method,'path':path,'status':status,'body_sha256':dig,'safe_error':safe if status>=400 else ''}); save()
 if not 200<=status<300: raise RuntimeError(f'{label or path}:HTTP_{status}:{safe}')
 return data(obj)
def entry():
 x=call('GET',f'/projects/{P}/services?per_page=100',label='list_services'); a=x.get('services') if isinstance(x,dict) else x; return next(v for v in a if v.get('id')==S)
def dep(label): return call('GET',f'/projects/{P}/services/{S}/deployment',label=label)
def public(path):
 req=urllib.request.Request(ORIGIN+path,headers={'Accept':'application/json','Cache-Control':'no-cache','User-Agent':'senex-order070-live/8'},method='GET')
 try:
  with urllib.request.urlopen(req,timeout=40) as r: status=r.status; raw=r.read()
 except urllib.error.HTTPError as e: status=e.code; raw=e.read()
 dig=hashlib.sha256(raw).hexdigest()
 try: obj=json.loads(raw.decode())
 except Exception: obj={'non_json':True}
 return status,obj,dig
b=call('GET',f'/projects/{P}/services/{S}/build/{BID}',label='verify_exact_build')
if not b.get('concluded') or not b.get('success') or b.get('sha')!=TS: raise RuntimeError('EXACT_BUILD_IDENTITY_MISMATCH')
e=entry(); d=dep('deployment_before'); ii=d.get('internal') or {}
if e.get('disabledCI') is not True or e.get('disabledCD') is not True: raise RuntimeError('CI_CD_NOT_FROZEN')
if ii.get('deployedSHA')!=TS:
 call('POST',f'/projects/{P}/services/{S}/deployment',{'internal':{'id':ii.get('id') or S,'branch':TB,'buildSHA':TS}},label='deploy_exact_build_sha')
 rec['deployment_request']='POST_BUILD_SHA_ONLY'; save()
end=time.time()+1800; stable=None
while time.time()<end:
 d=dep('deployment_poll'); e=entry(); ii=d.get('internal') or {}; st=((e.get('status') or {}).get('deployment') or {}).get('status')
 if ii.get('deployedSHA')==TS and st=='COMPLETED': stable=(d,e); break
 if st=='FAILED': raise RuntimeError('EXACT_DEPLOYMENT_FAILED')
 time.sleep(10)
if not stable: raise RuntimeError('DEPLOYMENT_TIMEOUT')
d,e=stable; ii=d.get('internal') or {}
c=call('GET',f'/projects/{P}/services/{S}/containers?per_page=100',label='containers'); arr=c.get('containers') if isinstance(c,dict) else c; arr=arr if isinstance(arr,list) else []
rec['deployment']={'build_id':ii.get('buildId'),'build_sha':ii.get('buildSHA'),'deployed_sha':ii.get('deployedSHA'),'branch':ii.get('branch'),'instances':d.get('instances'),'disabled_ci':e.get('disabledCI'),'disabled_cd':e.get('disabledCD'),'status':((e.get('status') or {}).get('deployment') or {}).get('status')}
rec['containers']=[{k:x.get(k) for k in ('id','name','status','createdAt','updatedAt','image','imageDigest','deploymentId','revision') if x.get(k) is not None} for x in arr if isinstance(x,dict)]
live={}
for p,k in [('/healthz','healthz'),('/readyz','readyz'),('/api/runtime/provenance','provenance')]:
 st,obj,dig=public(p); live[k]={'http':st,'body':obj,'sha256':dig}
rec['live_bootstrap']=live; rec['completed_at']=datetime.datetime.now(datetime.timezone.utc).isoformat(); rec['origin_exact_head_deploy']='PASS'; save()
print('ORIGIN_EXACT_HEAD_DEPLOY=PASS'); print('DEPLOYED_SHA='+str(ii.get('deployedSHA'))); print('BUILD_ID='+str(ii.get('buildId'))); print('HEALTHZ='+str(live['healthz']['http'])); print('READYZ='+str(live['readyz']['http'])); print('PROVENANCE='+json.dumps(live['provenance']['body'],sort_keys=True))
