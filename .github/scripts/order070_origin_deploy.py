import hashlib,json,os,re,urllib.error,urllib.parse,urllib.request
from pathlib import Path
API='https://api.northflank.com/v1'; P='seneciobot'; S='senecio-h011'; BID='next-muscle-6785'; TS='202f154a71915eb4b1e3cdf0e1eec8005760a028'
TOKEN=os.environ['NORTHFLANK_API_TOKEN']; H={'Authorization':f'Bearer {TOKEN}','Accept':'application/json','User-Agent':'senex-order070-digest/1'}; OUT=Path('order070-origin-evidence'); OUT.mkdir(exist_ok=True)
pat=re.compile(r'sha256:[0-9a-fA-F]{64}')
def get(path,query=None):
 u=API+path
 if query: u+='?'+urllib.parse.urlencode(query)
 req=urllib.request.Request(u,headers=H,method='GET')
 try:
  with urllib.request.urlopen(req,timeout=60) as r: raw=r.read(); status=r.status
 except urllib.error.HTTPError as e: raw=e.read(); status=e.code
 if not 200<=status<300: raise RuntimeError(f'GET {path} HTTP {status}')
 return json.loads(raw.decode() or '{}')
def collect_keyed(node,path=''):
 out=[]
 if isinstance(node,dict):
  for k,v in node.items():
   kp=(path+'.'+str(k)).strip('.')
   if re.search(r'(digest|image|registry)',str(k),re.I) and isinstance(v,(str,int,float,bool,type(None))): out.append((kp,str(v)))
   out.extend(collect_keyed(v,kp))
 elif isinstance(node,list):
  for i,v in enumerate(node): out.extend(collect_keyed(v,f'{path}[{i}]'))
 return out
build=get(f'/projects/{P}/services/{S}/build/{BID}').get('data',{})
if build.get('sha')!=TS or not build.get('success'): raise RuntimeError('BUILD_IDENTITY_MISMATCH')
keyed=collect_keyed(build)
logs=get(f'/projects/{P}/services/{S}/build-logs',{'buildId':BID,'queryType':'range','duration':86400,'lineLimit':1000,'direction':'backward','regexIncludes':'sha256:[0-9a-fA-F]{64}'}).get('data',[])
log_hits=[]
for row in logs if isinstance(logs,list) else []:
 text=str(row.get('log',''))
 digs=pat.findall(text)
 if digs:
  # retain only digest values and small semantic markers, never raw log content
  lower=text.lower(); marker=next((m for m in ('digest','manifest','pushing','exporting','image','config') if m in lower),'sha256')
  for d in digs: log_hits.append({'digest':d.lower(),'marker':marker,'ts':row.get('ts')})
unique=[]
for d in [x['digest'] for x in log_hits]+[m.lower() for _,v in keyed for m in pat.findall(v)]:
 if d not in unique: unique.append(d)
rec={'build_id':BID,'target_sha':TS,'keyed_metadata':keyed,'log_digest_hits':log_hits,'unique_digests':unique,'secret_values_exported':False}
raw=(json.dumps(rec,sort_keys=True,indent=2)+'\n').encode(); (OUT/'image-digest-probe.json').write_bytes(raw); (OUT/'SHA256SUMS').write_text(hashlib.sha256(raw).hexdigest()+'  image-digest-probe.json\n')
print('BUILD_ID='+BID); print('TARGET_SHA='+TS); print('KEYED_METADATA='+json.dumps(keyed)); print('DIGEST_HITS='+json.dumps(log_hits)); print('UNIQUE_DIGESTS='+json.dumps(unique))
