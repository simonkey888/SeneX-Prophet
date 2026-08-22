from __future__ import annotations
import json, os, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

API='https://api.northflank.com/v1'
PROJECT='seneciobot'
SERVICE='senecio-h011'
OLD='senecio-h011-bd588cbd7-wjgzx'
NEW='senecio-h011-bd588cbd7-w2pnf'
TOKEN=os.environ['NORTHFLANK_API_TOKEN']
OUT=Path('order070-runtime-root-cause-r2'); OUT.mkdir(exist_ok=True)
H={'Authorization':f'Bearer {TOKEN}','Accept':'application/json','User-Agent':'senex-order070-root-cause-r2/1'}

def get(path, params=None):
    q='?'+urllib.parse.urlencode(params,doseq=True) if params else ''
    req=urllib.request.Request(API+path+q,headers=H,method='GET')
    try:
        with urllib.request.urlopen(req,timeout=60) as r: st=r.status; raw=r.read()
    except urllib.error.HTTPError as e: st=e.code; raw=e.read()
    try: obj=json.loads(raw.decode() or '{}')
    except Exception: obj={'raw':raw.decode(errors='replace')[:4000]}
    if not 200 <= st < 300: return {'_http':st,'_error':obj}
    return obj.get('data',obj) if isinstance(obj,dict) else obj

def write(name,obj):
    (OUT/name).write_text(json.dumps(obj,indent=2,sort_keys=True,default=str)+'\n')

def pct_series(metrics, metric='memory'):
    out=[]
    obj=(metrics or {}).get(metric,{}) if isinstance(metrics,dict) else {}
    for series in obj.get('values',[]) if isinstance(obj,dict) else []:
        cid=(series.get('metadata') or {}).get('containerId')
        data=series.get('data') or []
        vals=[]
        for p in data:
            if isinstance(p,(list,tuple)) and len(p)>=2:
                try: vals.append((p[0],float(p[1])))
                except Exception: pass
            elif isinstance(p,dict):
                try: vals.append((p.get('timestamp') or p.get('time'),float(p.get('value'))))
                except Exception: pass
        if vals: out.append((cid,vals))
    return out

service=get(f'/projects/{PROJECT}/services/{SERVICE}')
plans=get('/plans')
containers=get(f'/projects/{PROJECT}/services/{SERVICE}/containers',{'per_page':100})
health=get(f'/projects/{PROJECT}/services/{SERVICE}/health-checks')
now=datetime.now(timezone.utc)
recent_start=(now-timedelta(hours=8)).isoformat().replace('+00:00','Z')
recent_end=now.isoformat().replace('+00:00','Z')
metrics=get(f'/projects/{PROJECT}/services/{SERVICE}/metrics',[
    ('queryType','range'),('startTime',recent_start),('endTime',recent_end),
    ('metricTypes','cpu'),('metricTypes','memory'),('metricTypes','tcpConnectionsOpen'),
    ('metricTypes','requests'),('metricTypes','http5xxResponses')])
logs=get(f'/projects/{PROJECT}/services/{SERVICE}/logs',{
    'queryType':'range','startTime':recent_start,'endTime':recent_end,
    'type':'runtime','lineLimit':2000,'direction':'forward'})
for n,o in [('service.json',service),('plans.json',plans),('containers.json',containers),('health_checks.json',health),('metrics_recent.json',metrics),('runtime_logs_recent.json',logs)]: write(n,o)

billing=service.get('billing',{}) if isinstance(service,dict) else {}
plan_id=billing.get('deploymentPlan')
plan_list=(plans.get('plans') if isinstance(plans,dict) else None) or []
plan=next((p for p in plan_list if p.get('id')==plan_id),None)
print('PROBE_AT='+recent_end)
print('DEPLOYMENT_PLAN='+str(plan_id))
print('PLAN_RAM_MB='+str((plan or {}).get('ramResource')))
print('PLAN_CPU_VCPU='+str((plan or {}).get('cpuResource')))
print('PLAN_USD_HOUR='+str((plan or {}).get('amountPerHour')))
print('HEALTH_CHECKS='+json.dumps(health,sort_keys=True))
status=service.get('status',{}) if isinstance(service,dict) else {}
print('SERVICE_STATUS='+json.dumps(status,sort_keys=True))

actions=('uvicorn exited','Process terminated','OOM','oom','out of memory','MemoryError','Killed','Terminated','Traceback','FATAL')
if isinstance(logs,list):
    matches=[]
    for row in logs:
        s=json.dumps(row,sort_keys=True)
        if any(x in s for x in actions): matches.append(row)
    print('RUNTIME_LOG_ROWS='+str(len(logs)))
    print('RUNTIME_SUSPICIOUS_ROWS='+str(len(matches)))
    for row in matches: print('RUNTIME_SUSPICIOUS='+json.dumps(row,sort_keys=True))
else:
    print('RUNTIME_LOG_ERROR='+json.dumps(logs,sort_keys=True))

for cid,vals in pct_series(metrics):
    if cid in {OLD,NEW}:
        peak=max(v for _,v in vals); last=vals[-1]
        above90=sum(1 for _,v in vals if v>=90.0)
        print(f'MEMORY_CONTAINER={cid} POINTS={len(vals)} PEAK_PCT={peak:.4f} ABOVE90={above90} LAST={last[0]}:{last[1]:.4f}')
        if plan and plan.get('ramResource'):
            print(f'MEMORY_PEAK_MB_EST={cid}:{peak*float(plan["ramResource"])/100.0:.2f}')
