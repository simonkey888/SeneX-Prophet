import datetime,hashlib,json,os,time,urllib.error,urllib.request
from pathlib import Path
API='https://api.northflank.com/v1'; P='seneciobot'; S='senecio-h011'
TB='feat/order-070-runtime-truth-hardening'; TS='202f154a71915eb4b1e3cdf0e1eec8005760a028'; TT='f037c7621135305fd3ec5f37aa029dfc8a28aa4b'; BD='sha256:1806ad0bc71c45264695c1c8973a497a39f9903f867ece2d56fdbc12f44e4892'
TOKEN=os.environ['NORTHFLANK_API_TOKEN']; H={'Authorization':f'Bearer {TOKEN}','Accept':'application/json','Content-Type':'application/json','User-Agent':'senex-order070-deploy/6'}; OUT=Path('order070-origin-evidence'); OUT.mkdir(exist_ok=True)
rec={'order':'ORDER-070-R3','target_sha':TS,'target_tree':TT,'target_branch':TB,'build_digest':BD,'secret_values_exported':False,'real_order_count':0,'real_capital_movement':0,'runtime017_mutation':0,'production_learning_mutations':0,'requests':[],'mutations':[],'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
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
 x=call('GET',f'/projects/{P}/services?per_page=100','list_services'); a=x.get('services') if isinstance(x,dict) else x; return next(v for v in a if v.get('id')==S)
def svc(label): return call('GET',f'/projects/{P}/services/{S}',label=label)
def dep(label): return call('GET',f'/projects/{P}/services/{S}/deployment',label=label)
e0=entry(); s0=svc('service_before'); d0=dep('deployment_before'); vcs=s0.get('vcsData') or {}; ob=vcs.get('projectBranch')
if s0.get('serviceType')!='combined' or ob!='main' or e0.get('disabledCI') is not True: raise RuntimeError('PREFLIGHT_MISMATCH')
p={k:vcs[k] for k in ('accountLogin','vcsLinkId','selfHostedVcsId') if vcs.get(k)}; p.update({'projectUrl':vcs['projectUrl'],'projectType':vcs['projectType'],'projectBranch':TB})
rec['preflight']={'branch':ob,'disabled_ci':e0.get('disabledCI'),'disabled_cd':e0.get('disabledCD'),'deployed_sha':(d0.get('internal') or {}).get('deployedSHA')}; save(); switched=False
try:
 call('PATCH',f'/projects/{P}/services/combined/{S}',{'disabledCI':True,'buildSource':'git','vcsData':p},'patch_target_branch'); switched=True; rec['mutations'].append('PATCH_TARGET_BRANCH'); save()
 if (svc('verify_target').get('vcsData') or {}).get('projectBranch')!=TB or entry().get('disabledCI') is not True: raise RuntimeError('TARGET_BRANCH_NOT_VERIFIED')
 b=call('POST',f'/projects/{P}/services/{S}/build',{'sha':TS,'overrides':{'buildArguments':{'SENEX_SOURCE_COMMIT':TS,'SENEX_SOURCE_TREE':TT,'SENEX_BUILD_DIGEST':BD}}},'start_exact_build'); bid=b.get('id'); end=time.time()+3600; final=None
 while time.time()<end:
  final=call('GET',f'/projects/{P}/services/{S}/build/{bid}',label='poll_build')
  if final.get('concluded'): break
  time.sleep(15)
 if not final or not final.get('success') or final.get('sha')!=TS: raise RuntimeError('EXACT_BUILD_FAILED')
 reg=final.get('registry') or {}; rec['build']={'id':bid,'sha':final.get('sha'),'branch':final.get('branch'),'registry_uri':reg.get('uri'),'registry_digest':reg.get('digest')}; save()
 cur=dep('post_build_deploy'); ii=cur.get('internal') or {}
 if ii.get('deployedSHA')!=TS:
  call('POST',f'/projects/{P}/services/{S}/deployment',{'internal':{'id':ii.get('id') or S,'branch':TB,'buildSHA':TS,'buildId':bid}},'deploy_exact'); rec['mutations'].append('POST_EXACT_DEPLOY'); save()
 end=time.time()+1800
 while time.time()<end:
  d=dep('poll_deploy'); e=entry(); ii=d.get('internal') or {}; st=((e.get('status') or {}).get('deployment') or {}).get('status')
  if ii.get('deployedSHA')==TS and st=='COMPLETED': break
  if st=='FAILED': raise RuntimeError('EXACT_DEPLOY_FAILED')
  time.sleep(10)
 else: raise RuntimeError('DEPLOY_TIMEOUT')
 rec['deployment']={'build_id':ii.get('buildId'),'build_sha':ii.get('buildSHA'),'deployed_sha':ii.get('deployedSHA'),'branch':ii.get('branch'),'disabled_cd':e.get('disabledCD')}; save()
finally:
 if switched:
  r=dict(p); r['projectBranch']=ob; call('PATCH',f'/projects/{P}/services/combined/{S}',{'disabledCI':True,'buildSource':'git','vcsData':r},'restore_main'); rec['mutations'].append('RESTORE_MAIN'); rec['source_restored']=True; save()
f=svc('final_service'); fe=entry(); fd=dep('final_deployment'); fi=fd.get('internal') or {}
if (f.get('vcsData') or {}).get('projectBranch')!='main' or fe.get('disabledCI') is not True or fi.get('deployedSHA')!=TS: raise RuntimeError('FINAL_CONTROL_MISMATCH')
rec['final']={'source_branch':'main','disabled_ci':True,'disabled_cd':fe.get('disabledCD'),'deployed_sha':fi.get('deployedSHA'),'build_sha':fi.get('buildSHA'),'build_id':fi.get('buildId')}; rec['origin_exact_head_deploy']='PASS'; rec['completed_at']=datetime.datetime.now(datetime.timezone.utc).isoformat(); save()
print('ORIGIN_EXACT_HEAD_DEPLOY=PASS'); print('BUILD_ID='+str(fi.get('buildId'))); print('DEPLOYED_SHA='+str(fi.get('deployedSHA'))); print('SOURCE_RESTORED=YES'); print('DISABLED_CD='+str(fe.get('disabledCD')))
