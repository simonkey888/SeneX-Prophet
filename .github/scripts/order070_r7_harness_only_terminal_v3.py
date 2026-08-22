from __future__ import annotations
from pathlib import Path

SOURCE=Path(__file__).with_name('order070_r7_harness_only_terminal.py')
s=SOURCE.read_text()

def one(old:str,new:str,label:str)->None:
    global s
    n=s.count(old)
    if n!=1: raise RuntimeError(f'{label}: expected 1 match got {n}')
    s=s.replace(old,new,1)

one(
"def pub(base,path,method='GET',timeout=45): return request_json(method,base.rstrip('/')+path,None,None,timeout)",
r'''def pub(base,path,method='GET',timeout=45):
    url=base.rstrip('/')+path
    if '.workers.dev' in base and method=='GET':
        with tempfile.TemporaryDirectory() as td:
            hp=Path(td)/'h'; bp=Path(td)/'b'
            cp=subprocess.run(['curl','-sS','--max-time',str(int(timeout)),'-D',str(hp),'-o',str(bp),'-w','%{http_code}',url],text=True,capture_output=True,timeout=timeout+5,env=cf_env())
            status=int(cp.stdout.strip()) if cp.returncode==0 and cp.stdout.strip().isdigit() else 0
            raw=bp.read_bytes() if bp.exists() else b''; headers={}
            if hp.exists():
                for line in hp.read_text(errors='replace').splitlines():
                    if ':' in line:
                        k,v=line.split(':',1); headers[k.strip().lower()]=v.strip()
            try: body=json.loads(raw.decode())
            except Exception: body={'_non_json':True,'bytes':len(raw),'sha256':h256(raw),'text':raw.decode(errors='replace')[:300]}
            return {'http':status,'body':body,'headers':headers,'sha256':h256(raw) if raw else None,'curl_exit':cp.returncode}
    return request_json(method,url,None,None,timeout)''',
'edge curl transport')

one(
"if not all(x['http']==200 for x in (h,r,p)): raise RuntimeError(f'LIVE_HTTP:{base}:{h[\"http\"]}:{r[\"http\"]}:{p[\"http\"]}')",
"if not all(x['http']==200 for x in (h,r,p)): raise RuntimeError(f'LIVE_HTTP:{base}:{h[\"http\"]}:{r[\"http\"]}:{p[\"http\"]}:ready_decision={r[\"headers\"].get(\"x-senex-edge-decision\")}:prov_decision={p[\"headers\"].get(\"x-senex-edge-decision\")}:ready_body={str(r[\"body\"])[:400]}:prov_body={str(p[\"body\"])[:220]}')",
'live diagnostic')

one(
"oh,orr,op=assert_exact_live(ORIGIN)\nwrite('ORIGIN_EXACT_READBACK.json'",
r'''readiness_attempts=[]; oh=orr=op=None
for attempt in range(1,76):
    oh=pub(ORIGIN,'/healthz'); orr=pub(ORIGIN,'/readyz?symbol=BTCUSDT'); op=pub(ORIGIN,'/api/runtime/provenance')
    body=orr.get('body') if isinstance(orr,dict) else {}
    readiness_attempts.append({
        'attempt':attempt,'observed_at':iso(),'health_http':oh.get('http'),'ready_http':orr.get('http'),'provenance_http':op.get('http'),
        'ready_status':body.get('status') if isinstance(body,dict) else None,
        'checks':body.get('checks') if isinstance(body,dict) else None,
        'last_refresh_error':body.get('last_refresh_error') if isinstance(body,dict) else None,
        'snapshot_stale':body.get('snapshot_stale') if isinstance(body,dict) else None,
        'snapshot_age_s':body.get('snapshot_age_s') if isinstance(body,dict) else None,
        'generation':body.get('generation') if isinstance(body,dict) else None,
    })
    if oh.get('http')==200 and orr.get('http')==200 and op.get('http')==200:
        break
    time.sleep(5)
else:
    log_evidence=[]
    for pat in ['authority snapshot refresh failed','AUTHORITY_HISTORY','EXACT_COUNT','Process terminated with exit code','uvicorn exited','OOMKilled','Connection refused']:
        try:
            rows,_=nf(f'/projects/{PROJECT}/services/{SERVICE}/logs',{'queryType':'range','duration':1800,'type':'runtime','lineLimit':1000,'direction':'backward','regexIncludes':pat})
            if isinstance(rows,list) and rows: log_evidence.append({'pattern':pat,'rows':rows})
        except Exception as exc: log_evidence.append({'pattern':pat,'query_error':type(exc).__name__})
    write('ORIGIN_READINESS_WAIT.json',{'observed_at':iso(),'result':'FAIL','attempts':readiness_attempts,'runtime_log_evidence':log_evidence})
    raise RuntimeError(f'ORIGIN_READINESS_DID_NOT_RECOVER:{readiness_attempts[-1]}')
assert_exact_live(ORIGIN)
write('ORIGIN_READINESS_WAIT.json',{'observed_at':iso(),'result':'PASS','attempts':readiness_attempts,'recovered_on_attempt':readiness_attempts[-1]['attempt']})
write('ORIGIN_EXACT_READBACK.json' ''',
'passive readiness wait')

code=compile(s,str(SOURCE)+'[R7_PASSIVE_READINESS_DIRECT_CURL]','exec')
exec(code,{'__name__':'__main__','__file__':str(SOURCE)})
