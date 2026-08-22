#!/usr/bin/env python3
from __future__ import annotations
import hashlib, json, subprocess
from datetime import datetime, timezone
from pathlib import Path
root=Path(__file__).resolve().parents[2]
head=subprocess.check_output(["git","rev-parse","HEAD"],cwd=root,text=True).strip()
tree=subprocess.check_output(["git","rev-parse","HEAD^{tree}"],cwd=root,text=True).strip()
tracked=subprocess.check_output(["git","ls-files","-z"],cwd=root).split(b"\0")
files={}
for raw in tracked:
    if not raw: continue
    rel=raw.decode(); p=root/rel
    if p.is_file(): files[rel]=hashlib.sha256(p.read_bytes()).hexdigest()
payload={"contract":"senex-order070-sealed-v1","head":head,"tree":tree,"sealed_at":datetime.now(timezone.utc).isoformat(),"files":files}
canonical=json.dumps(payload,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
payload["artifact_sha256"]=hashlib.sha256(canonical).hexdigest()
out=root/'senecio_polymarket'/'artifacts'/'order070-sealed.json'; out.parent.mkdir(exist_ok=True); out.write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n")
print(payload["artifact_sha256"])
