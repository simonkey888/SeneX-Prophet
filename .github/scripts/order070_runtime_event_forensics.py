from __future__ import annotations
import datetime as dt, json, os, re, urllib.error, urllib.parse, urllib.request
from pathlib import Path

API='https://api.northflank.com/v1'
PROJECT='seneciobot'
SERVICE='senecio-h011'
BUILD='curved-board-2291'
TARGET_HEAD='483b389a83610992800181c0a21b5a337009f7b4'
TARGET_TREE='0494d3d4066f94a9dd055d81c07a3633a243ec2f'
ORIGIN='https://h011-web--senecio-h011--wbjggn89fnf8.code.run'
TOKEN=os.environ['NORTHFLANK_API_TOKEN']
OUT=Path('order070-runtime-forensics'); OUT.mkdir(exist_ok=True)
H={'Authorization':f'Bearer {TOKEN}','Accept':'application/json','User-Agent':'senex-order070-runtime-forensics/1'}

def utcnow(): return dt.datetime.now(dt.timezone.utc)
def iso(v):
    if v is None: return None
    if isinstance(v,(int,float)):
        # Northflank container timestamps are unix seconds; tolerate ms.
        if v>10_000_000_000: v=v/1000
        return dt.datetime.fromtimestamp(v,dt.timezone.utc).isoformat()
    return str(v)
def req(path, params=None):
    q=''
    if params:
        q='?'+urllib.parse.urlencode(params,doseq=True)
    r=urllib.request.Request(API+path+q,headers=H,method='GET')
    try:
        with urllib.request.urlopen(r,timeout=60) as x: st=x.status; raw=x.read()
    except urllib.error.HTTPError as e: st=e.code; raw=e.read()
    try:o=json.loads(raw.decode() or '{}')
    except Exception:o={'_raw':raw.decode(errors='replace')[:1000]}
    if not 200<=st<300: raise RuntimeError(f'GET {path} -> {st}: {str(o)[:500]}')
    return o.get('data',o) if isinstance(o,dict) else o

def public(path):
    r=urllib.request.Request(ORIGIN+path,headers={'Accept':'application/json','Cache-Control':'no-cache','User-Agent':'senex-order070-runtime-forensics/1'},method='GET')
    try:
        with urllib.request.urlopen(r,timeout=45) as x: st=x.status; raw=x.read()
    except urllib.error.HTTPError as e: st=e.code; raw=e.read()
    try:o=json.loads(raw.decode())
    except Exception:o={'_raw':raw.decode(errors='replace')[:500]}
    return st,o

def write(name,obj): (OUT/name).write_text(json.dumps(obj,indent=2,sort_keys=True,default=str)+'\n')

service=req(f'/projects/{PROJECT}/services/{SERVICE}')
deployment=req(f'/projects/{PROJECT}/services/{SERVICE}/deployment')
build=req(f'/projects/{PROJECT}/services/{SERVICE}/build/{BUILD}')
containers=req(f'/projects/{PROJECT}/services/{SERVICE}/containers',{'per_page':100})
if isinstance(containers,dict): containers_list=containers.get('containers') or []
else: containers_list=containers or []

now=utcnow(); start=(now-dt.timedelta(hours=30)).isoformat().replace('+00:00','Z'); end=now.isoformat().replace('+00:00','Z')
logs_by_type={}
for typ in ('runtime','ingress','mesh'):
    try:
        logs_by_type[typ]=req(f'/projects/{PROJECT}/services/{SERVICE}/logs',{
            'queryType':'range','startTime':start,'endTime':end,'type':typ,'lineLimit':20000,'direction':'forward'
        })
    except Exception as e:
        logs_by_type[typ]={'_error':type(e).__name__+':'+str(e)}

metrics={}
try:
    metrics=req(f'/projects/{PROJECT}/services/{SERVICE}/metrics',[
        ('queryType','range'),('startTime',start),('endTime',end),
        ('metricTypes','cpu'),('metricTypes','memory'),('metricTypes','networkIngress'),('metricTypes','networkEgress'),
        ('metricTypes','tcpConnectionsOpen'),('metricTypes','requests'),('metricTypes','http4xxResponses'),('metricTypes','http5xxResponses')
    ])
except Exception as e:
    metrics={'_error':type(e).__name__+':'+str(e)}

write('service.json',service); write('deployment.json',deployment); write('build.json',build); write('containers.json',containers_list); write('logs.json',logs_by_type); write('metrics.json',metrics)

patterns={
 'connection_refused':re.compile(r'connection refused|connect error|upstream connect error|disconnect/reset before headers|remote connection failure',re.I),
 'oom':re.compile(r'oom|out of memory|oomkilled|killed process|memory cgroup',re.I),
 'process_exit':re.compile(r'exit code|exited with|process exited|container exited|terminated with',re.I),
 'shutdown':re.compile(r'shutting down|shutdown complete|application shutdown|received signal|sigterm|terminating',re.I),
 'startup':re.compile(r'started server process|application startup complete|server running|uvicorn running|starting worker',re.I),
 'health_fail':re.compile(r'health.?check.*fail|liveness.*fail|unhealthy',re.I),
 'ready_fail':re.compile(r'ready|readiness',re.I),
}

def lines_for(t):
    x=logs_by_type.get(t,[])
    return x if isinstance(x,list) else []
allrows=[]
for typ in ('runtime','ingress','mesh'):
    for r in lines_for(typ):
        allrows.append({'type':typ,'ts':r.get('ts'),'containerId':r.get('containerId'),'log':str(r.get('log') or '')})
allrows.sort(key=lambda r:str(r.get('ts') or ''))
matches={k:[] for k in patterns}
for row in allrows:
    for k,p in patterns.items():
        if p.search(row['log']): matches[k].append(row)

# Use explicit readiness text only when it contains a failure marker.
ready_fail=[r for r in matches['ready_fail'] if re.search(r'fail|unhealthy|not ready|503|timeout|error',r['log'],re.I)]

# Current exact runtime truth.
current={}
for name,path in [('health','/healthz'),('ready','/readyz?symbol=BTCUSDT'),('provenance','/api/runtime/provenance'),('snapshot','/api/authority/snapshot?symbol=BTCUSDT'),('context','/api/market-context?symbol=BTCUSDT')]:
    current[name]={'http':public(path)[0],'body':public(path)[1]}
write('current_public.json',current)

status=service.get('status') or {}; dep_status=status.get('deployment') or {}
transition=dep_status.get('lastTransitionTime')
internal=deployment.get('internal') or {}
normalized=[]
for c in containers_list:
    normalized.append({'name':c.get('name'),'status':c.get('status'),'createdAt':iso(c.get('createdAt')),'updatedAt':iso(c.get('updatedAt'))})
normalized.sort(key=lambda c:c.get('createdAt') or '')
current_running=[c for c in normalized if c.get('status')=='TASK_RUNNING']
terminated=[c for c in normalized if c.get('status') in {'TASK_KILLED','TASK_FAILED','TASK_FINISHED'}]

# Classify connection-refused clusters and relationship to deployment transition when timestamps allow.
def parse_ts(x):
    if not x:return None
    try:return dt.datetime.fromisoformat(str(x).replace('Z','+00:00'))
    except Exception:return None
dep_t=parse_ts(transition)
conn=matches['connection_refused']
conn_deltas=[]
for r in conn:
    t=parse_ts(r.get('ts'))
    if dep_t and t: conn_deltas.append((t-dep_t).total_seconds())
near_deploy=[d for d in conn_deltas if abs(d)<=180]
expected_deploy_transition=bool(conn) and len(near_deploy)==len(conn_deltas) and len(conn_deltas)>0

# If the list contains one running container and no post-deploy terminated containers, there is no evidence of an unexpected restart.
post_deploy_terminated=[]
if dep_t:
    for c in terminated:
        ct=parse_ts(c.get('createdAt')); ut=parse_ts(c.get('updatedAt'))
        if (ct and ct>=dep_t) or (ut and ut>=dep_t): post_deploy_terminated.append(c)

summary={
 'observed_at':now.isoformat(),
 'target_head':TARGET_HEAD,'target_tree':TARGET_TREE,
 'deployment':{
   'deployedSHA':internal.get('deployedSHA'),'buildSHA':internal.get('buildSHA'),'buildId':internal.get('buildId'),
   'status':dep_status.get('status'),'reason':dep_status.get('reason'),'lastTransitionTime':transition,
 },
 'build':{'id':build.get('id'),'sha':build.get('sha'),'status':build.get('status'),'success':build.get('success'),'createdAt':build.get('createdAt'),'buildConcludedAt':iso(build.get('buildConcludedAt'))},
 'containers':normalized,
 'running_count':len(current_running),'terminated_count':len(terminated),'post_deploy_terminated_count':len(post_deploy_terminated),
 'connection_refused_count':len(conn),'connection_refused_matches':conn[-50:],'connection_refused_seconds_from_deploy':conn_deltas,
 'oom_match_count':len(matches['oom']),'oom_matches':matches['oom'][-50:],
 'process_exit_match_count':len(matches['process_exit']),'process_exit_matches':matches['process_exit'][-50:],
 'shutdown_match_count':len(matches['shutdown']),'shutdown_matches':matches['shutdown'][-50:],
 'startup_match_count':len(matches['startup']),'startup_matches':matches['startup'][-50:],
 'health_failure_match_count':len(matches['health_fail']),'health_failure_matches':matches['health_fail'][-50:],
 'readiness_failure_match_count':len(ready_fail),'readiness_failure_matches':ready_fail[-50:],
 'expected_deploy_transition_by_ingress_timestamp':expected_deploy_transition,
 'current_http':{k:v['http'] for k,v in current.items()},
 'current_provenance':current.get('provenance',{}).get('body'),
 'current_snapshot_identity':{k:current.get('snapshot',{}).get('body',{}).get(k) for k in ('snapshot_id','generation','canonical_sha256','exact_total_predictions','exact_count_complete')},
 'current_safety':current.get('context',{}).get('body',{}).get('safety'),
}
write('RUNTIME_EVENT_SUMMARY.json',summary)

print('RUNTIME_EVENT_FORENSICS=COMPLETE')
print('DEPLOYMENT_STATUS='+str(dep_status.get('status')))
print('DEPLOYMENT_REASON='+str(dep_status.get('reason')))
print('DEPLOYMENT_LAST_TRANSITION='+str(transition))
print('DEPLOYED_SHA='+str(internal.get('deployedSHA')))
print('RUNNING_CONTAINERS='+str(len(current_running)))
print('TERMINATED_CONTAINERS='+str(len(terminated)))
print('POST_DEPLOY_TERMINATED='+str(len(post_deploy_terminated)))
print('CONNECTION_REFUSED_MATCHES='+str(len(conn)))
print('CONNECTION_REFUSED_DELTAS_SEC='+json.dumps(conn_deltas))
print('OOM_MATCHES='+str(len(matches['oom'])))
print('PROCESS_EXIT_MATCHES='+str(len(matches['process_exit'])))
print('HEALTH_FAIL_MATCHES='+str(len(matches['health_fail'])))
print('READY_FAIL_MATCHES='+str(len(ready_fail)))
print('EXPECTED_DEPLOY_TRANSITION_BY_LOGS='+('YES' if expected_deploy_transition else 'NO'))
for c in normalized: print('CONTAINER='+json.dumps(c,sort_keys=True))
for r in conn[-20:]: print('CONNECTION_REFUSED='+json.dumps(r,sort_keys=True))
for r in matches['oom'][-20:]: print('OOM='+json.dumps(r,sort_keys=True))
for r in matches['process_exit'][-20:]: print('PROCESS_EXIT='+json.dumps(r,sort_keys=True))
