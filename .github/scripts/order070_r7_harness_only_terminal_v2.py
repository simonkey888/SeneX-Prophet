from __future__ import annotations
from pathlib import Path

SOURCE=Path(__file__).with_name('order070_r7_harness_only_terminal.py')
s=SOURCE.read_text()
old="def pub(base,path,method='GET',timeout=45): return request_json(method,base.rstrip('/')+path,None,None,timeout)"
new=r'''def pub(base,path,method='GET',timeout=45):
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
    return request_json(method,url,None,None,timeout)'''
if s.count(old)!=1: raise RuntimeError(f'R7_EDGE_TRANSPORT_PATCH_MATCHES={s.count(old)}')
s=s.replace(old,new,1)
# Add explicit response details if public exact-live fails.
old2="if not all(x['http']==200 for x in (h,r,p)): raise RuntimeError(f'LIVE_HTTP:{base}:{h[\"http\"]}:{r[\"http\"]}:{p[\"http\"]}')"
new2="if not all(x['http']==200 for x in (h,r,p)): raise RuntimeError(f'LIVE_HTTP:{base}:{h[\"http\"]}:{r[\"http\"]}:{p[\"http\"]}:ready_decision={r[\"headers\"].get(\"x-senex-edge-decision\")}:prov_decision={p[\"headers\"].get(\"x-senex-edge-decision\")}:ready_body={str(r[\"body\"])[:180]}:prov_body={str(p[\"body\"])[:180]}')"
if s.count(old2)!=1: raise RuntimeError(f'R7_DIAGNOSTIC_PATCH_MATCHES={s.count(old2)}')
s=s.replace(old2,new2,1)
code=compile(s,str(SOURCE)+'[R7_DIRECT_CURL_EDGE]','exec')
exec(code,{'__name__':'__main__','__file__':str(SOURCE)})
