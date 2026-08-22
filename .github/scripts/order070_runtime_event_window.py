from __future__ import annotations
import json, os, urllib.parse, urllib.request, urllib.error
from pathlib import Path
API='https://api.northflank.com/v1'; P='seneciobot'; S='senecio-h011'; TOKEN=os.environ['NORTHFLANK_API_TOKEN']
OLD='senecio-h011-bd588cbd7-wjgzx'; NEW='senecio-h011-bd588cbd7-w2pnf'
START='2026-08-22T06:35:00Z'; END='2026-08-22T06:55:00Z'
OUT=Path('order070-runtime-event-window'); OUT.mkdir(exist_ok=True)
H={'Authorization':f'Bearer {TOKEN}','Accept':'application/json','User-Agent':'senex-order070-event-window/1'}
def get(path,params=None):
 q='?'+urllib.parse.urlencode(params,doseq=True) if params else ''
 req=urllib.request.Request(API+path+q,headers=H,method='GET')
 try:
  with urllib.request.urlopen(req,timeout=60) as r: st=r.status; raw=r.read()
 except urllib.error.HTTPError as e: st=e.code; raw=e.read()
 try:o=json.loads(raw.decode() or '{}')
 except Exception:o={'raw':raw.decode(errors='replace')[:2000]}
 if not 200<=st<300: return {'_http':st,'_error':o}
 return o.get('data',o) if isinstance(o,dict) else o
def write(n,o): (OUT/n).write_text(json.dumps(o,indent=2,sort_keys=True,default=str)+'\n')
logs={}
for typ in ('runtime','ingress','mesh'):
 for cname in (OLD,NEW,None):
  key=typ+(':'+cname if cname else ':all')
  p={'queryType':'range','startTime':START,'endTime':END,'type':typ,'lineLimit':1000,'direction':'forward'}
  if cname: p['containerName']=cname
  logs[key]=get(f'/projects/{P}/services/{S}/logs',p)
write('logs.json',logs)
metrics=get(f'/projects/{P}/services/{S}/metrics',[
 ('queryType','range'),('startTime',START),('endTime',END),
 ('metricTypes','cpu'),('metricTypes','memory'),('metricTypes','networkIngress'),('metricTypes','networkEgress'),
 ('metricTypes','tcpConnectionsOpen'),('metricTypes','requests'),('metricTypes','http4xxResponses'),('metricTypes','http5xxResponses')
])
write('metrics.json',metrics)
health=get(f'/projects/{P}/services/{S}/health-checks')
write('health_checks.json',health)
containers=get(f'/projects/{P}/services/{S}/containers',{'per_page':100})
write('containers.json',containers)
print('EVENT_WINDOW='+START+'..'+END)
print('HEALTH_CHECKS='+json.dumps(health,sort_keys=True))
for key,rows in logs.items():
 if isinstance(rows,list):
  print('LOGSET='+key+' COUNT='+str(len(rows)))
  for r in rows: print('LOG='+key+' '+json.dumps(r,sort_keys=True))
 else: print('LOGSET='+key+' ERROR='+json.dumps(rows,sort_keys=True))
for metric,obj in metrics.items() if isinstance(metrics,dict) else []:
 print('METRIC='+metric)
 for series in obj.get('values',[]):
  cid=(series.get('metadata') or {}).get('containerId')
  if cid in {OLD,NEW}:
   print('SERIES='+metric+' '+str(cid)+' '+json.dumps(series.get('data') or [],sort_keys=True))
