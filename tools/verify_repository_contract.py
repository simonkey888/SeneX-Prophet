#!/usr/bin/env python3
"""SENEX executable repository contract verifier. Static and network-free."""
from __future__ import annotations
import argparse, ast, fnmatch, hashlib, json, re, subprocess, sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

GATE_IDS=("DO_NOT_TOUCH_HASH_CHANGED","FORBIDDEN_RUNTIME_OUTPUT_TRACKED","PRODUCTION_PATH_CHANGED","PRODUCTION_PATH_CHANGE_AUTHORIZED","UNCLASSIFIED_DUPLICATE","IMPORT_GRAPH_REGRESSION","AUTHORITATIVE_RAW_WRITER_COUNT","SAFETY_FLAGS_PRESENT","WALLET_OR_ORDER_CODE_INTRODUCED","RESEARCH_AUTHORITY_VIOLATION","TEMPORARY_WORKFLOW_REINTRODUCED")
REQUIRED_OVERRIDE_FIELDS={"mission_id","base_sha","head_sha","allowed_paths","issued_by","issued_at","expires_at","reason","old_hashes","new_hashes"}
DANGEROUS_CALLS={"sign_order","sign_transaction","submit_order","create_order","place_order","send_order","broadcast_transaction","send_transaction"}
DANGEROUS_MODULE_PARTS={"wallet","web3","eth_account","private_key","py_clob_client"}

@dataclass
class GateResult:
    gate_id:str; status:str; evidence:list[str]; affected_paths:list[str]; classification:str="VERIFIED_STATIC_ANALYSIS"; override_required:bool=False

def canonical_json_bytes(obj:Any)->bytes:
    return (json.dumps(obj,sort_keys=True,separators=(",",":"),ensure_ascii=False)+"\n").encode()
def sha256_bytes(payload:bytes)->str:return hashlib.sha256(payload).hexdigest()
def sha256_file(path:Path)->str:return sha256_bytes(path.read_bytes())
def matches(path:str,patterns:Iterable[str])->bool:
    path=path.replace("\\","/")
    for p in patterns:
        p=p.replace("\\","/")
        if p.endswith("/**") and (path==p[:-3] or path.startswith(p[:-2])):return True
        if fnmatch.fnmatchcase(path,p):return True
    return False

def load_structured(path:Path)->dict[str,Any]:
    text=path.read_text(encoding="utf-8")
    try:value=json.loads(text)
    except json.JSONDecodeError:
        try:import yaml
        except ImportError as exc:raise ValueError(f"{path} is not JSON-compatible YAML") from exc
        value=yaml.safe_load(text)
    if not isinstance(value,dict):raise ValueError(f"{path} root must be object")
    return value

def verify_sidecar(data_path:Path,sidecar_path:Path)->tuple[bool,str]:
    try:digest,name=sidecar_path.read_text().strip().split(None,1)
    except Exception as exc:return False,f"invalid sidecar: {exc}"
    if name.strip()!=data_path.name:return False,"sidecar filename mismatch"
    actual=sha256_file(data_path)
    return (digest==actual,actual if digest==actual else f"digest mismatch: {digest} != {actual}")

def _call_name(node:ast.AST)->str:
    parts=[]
    while isinstance(node,ast.Attribute):parts.append(node.attr);node=node.value
    if isinstance(node,ast.Name):parts.append(node.id)
    return ".".join(reversed(parts))
def python_imports(path:Path)->set[str]:
    tree=ast.parse(path.read_text(),filename=str(path)); found=set()
    for node in ast.walk(tree):
        if isinstance(node,ast.Import):found.update(a.name for a in node.names)
        elif isinstance(node,ast.ImportFrom) and node.module:found.add(node.module)
        elif isinstance(node,ast.Call) and _call_name(node.func) in {"importlib.import_module","__import__"} and node.args and isinstance(node.args[0],ast.Constant) and isinstance(node.args[0].value,str):found.add(node.args[0].value)
    return found
def capability_findings(path:Path)->list[str]:
    if path.suffix!=".py":return []
    try:tree=ast.parse(path.read_text(),filename=str(path))
    except Exception as exc:return [f"unparseable Python: {exc}"]
    findings=[]
    for name in python_imports(path):
        if any(x in name.lower() for x in DANGEROUS_MODULE_PARTS):findings.append(f"dangerous import capability: {name}")
    for node in ast.walk(tree):
        if not isinstance(node,ast.Call):continue
        call=_call_name(node.func); leaf=call.rsplit(".",1)[-1].lower()
        strings=" ".join(n.value for n in ast.walk(node) if isinstance(n,ast.Constant) and isinstance(n.value,str)).lower()
        if leaf in DANGEROUS_CALLS:findings.append(f"signing/order call capability: {call}")
        if call in {"subprocess.run","subprocess.call","subprocess.Popen","os.system"} and any(x in strings for x in ("submit-order","place-order","create-order","send-order","wallet","private-key")):findings.append("subprocess execution capability")
        if call in {"importlib.import_module","__import__"} and any(x in strings for x in DANGEROUS_MODULE_PARTS):findings.append(f"dynamic dangerous import capability: {strings}")
    return sorted(set(findings))

ACTION_RE=re.compile(r"^\s*-\s*uses:\s*([^#\s]+)",re.M); FULL_SHA_RE=re.compile(r"^[^@]+@[0-9a-fA-F]{40}$")
def workflow_findings(path:Path)->list[str]:
    text=path.read_text(); low=text.lower(); out=[]
    for action in ACTION_RE.findall(text):
        if not FULL_SHA_RE.fullmatch(action):out.append(f"unpinned action: {action}")
    if re.search(r"(?m)^\s*contents\s*:\s*write\s*$",low):out.append("contents:write permission")
    temporary="temporary" in path.name.lower() or "temporary" in low or "temp-" in path.name.lower()
    if temporary and "expires_at:" not in low:out.append("temporary workflow missing expiry")
    if "workflow_dispatch:" in low:out.append("workflow_dispatch present")
    for phrase in ("git push","git commit","northflank","pages deploy"):
        if phrase in low:out.append(f"dangerous workflow capability: {phrase}")
    return sorted(set(out))

def _utc(value:Any)->datetime:
    if not isinstance(value,str):raise ValueError("timestamp must be string")
    dt=datetime.fromisoformat(value.replace("Z","+00:00"))
    if dt.tzinfo is None or dt.utcoffset()!=timezone.utc.utcoffset(dt):raise ValueError("timestamp must be UTC")
    return dt
def validate_override(auth:dict[str,Any]|None,*,base_sha:str,head_sha:str,paths:Iterable[str],now:datetime|None=None)->list[str]:
    auth=auth or {}; errors=[]; missing=sorted(REQUIRED_OVERRIDE_FIELDS-set(auth))
    if missing:return [f"missing fields: {missing}"]
    if auth.get("base_sha")!=base_sha:errors.append("wrong base_sha")
    if auth.get("head_sha")!=head_sha:errors.append("wrong head_sha")
    if not isinstance(auth.get("allowed_paths"),list) or sorted(set(auth["allowed_paths"]))!=sorted(set(paths)):errors.append("allowed_paths must match exactly")
    try:issued,expiry=_utc(auth.get("issued_at")),_utc(auth.get("expires_at")); instant=now or datetime.now(timezone.utc)
    except Exception as exc:errors.append(f"invalid authorization timestamp: {exc}")
    else:
        if expiry<=instant:errors.append("authorization expired")
        if issued>instant:errors.append("authorization issued_at is in the future")
    if auth.get("issued_by")!="AUD":errors.append("issued_by must be AUD")
    if not isinstance(auth.get("old_hashes"),dict) or not isinstance(auth.get("new_hashes"),dict):errors.append("hash maps invalid")
    return errors

def _git(root:Path,*args:str)->str:
    p=subprocess.run(["git",*args],cwd=root,text=True,capture_output=True)
    if p.returncode:raise RuntimeError(p.stderr.strip())
    return p.stdout
def changed(root:Path,base:str,head:str,status:bool=False)->list[str]:
    args=("diff","--name-status",f"{base}...{head}") if status else ("diff","--name-only",f"{base}...{head}")
    lines=_git(root,*args).splitlines()
    return sorted((x.split("\t",1)[1] for x in lines if x.startswith("A\t")) if status else lines)
def ok(gate:str,*evidence:str)->GateResult:return GateResult(gate,"PASS",list(evidence),[])
def bad(gate:str,evidence:list[str],paths:list[str],override:bool=False)->GateResult:return GateResult(gate,"FAIL",evidence,sorted(set(paths)),override_required=override)

def evaluate(root:Path,c:dict[str,Any],base:str,head:str,auth:dict[str,Any]|None)->list[GateResult]:
    ch=changed(root,base,head); added=changed(root,base,head,True); r=[]
    mismatch=[]
    for rel,expected in c.get("do_not_touch_hashes",{}).items():
        p=root/rel; actual=sha256_file(p) if p.is_file() else "MISSING"
        if actual!=expected:mismatch.append(f"{rel}: {actual} != {expected}")
    r.append(bad(GATE_IDS[0],mismatch,[x.split(":",1)[0] for x in mismatch]) if mismatch else ok(GATE_IDS[0],"all locked hashes match"))
    new_runtime=[p for p in added if matches(p,c.get("generated_runtime_paths",[])) and not matches(p,["tests/governance/fixtures/**"])]
    r.append(bad(GATE_IDS[1],["new tracked runtime output"],new_runtime) if new_runtime else ok(GATE_IDS[1],"no new tracked runtime output"))
    prod=[p for p in ch if matches(p,c.get("production_paths",[]))]
    r.append(bad(GATE_IDS[2],["production paths changed"],prod,True) if prod else ok(GATE_IDS[2],"no production path changed"))
    errors=validate_override(auth,base_sha=base,head_sha=head,paths=prod) if prod else []
    r.append(bad(GATE_IDS[3],errors,prod,True) if errors else ok(GATE_IDS[3],"not required" if not prod else "exact authorization valid"))
    py=[p for p in added if p.endswith(".py") and not matches(p,["tests/governance/**","tools/verify_repository_contract.py"])]
    groups={}
    for p in py:groups.setdefault(Path(p).name,[]).append(p)
    dup=[p for g in groups.values() if len(g)>1 for p in g]
    r.append(bad(GATE_IDS[4],["new unclassified duplicate basename"],dup) if dup else ok(GATE_IDS[4],"no new unclassified duplicate family"))
    regress=[]
    for rel in ch:
        p=root/rel
        if rel.endswith(".py") and matches(rel,["polymarket/**"]) and p.is_file() and any(x=="senecio_polymarket" or x.startswith("senecio_polymarket.") for x in python_imports(p)):regress.append(rel)
    r.append(bad(GATE_IDS[5],["product imports legacy"],regress) if regress else ok(GATE_IDS[5],"no product-to-legacy import regression"))
    writers=c.get("authoritative_raw_writers",[]); we=[]; werr=[]
    for w in writers:
        p=root/str(w.get("path")); symbol=w.get("symbol")
        try:count=sum(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==symbol for n in ast.walk(ast.parse(p.read_text())))
        except Exception as exc:count=-1;werr.append(str(exc))
        we.append(f"{w.get('path')}::{symbol} count={count}")
        if count!=1:werr.append(we[-1])
    if len(writers)!=1:werr.append(f"declared primary writer count={len(writers)}")
    r.append(bad(GATE_IDS[6],werr,[str(w.get("path")) for w in writers]) if werr else ok(GATE_IDS[6],*we))
    safety=c.get("safety_invariants",{}); expected={"paper_only":True,"orders_enabled":False,"live_capital_locked":True}; serr=[]
    for k,v in expected.items():
        rec=safety.get(k)
        if not isinstance(rec,dict) or rec.get("value") is not v or rec.get("override") is not False:serr.append(k)
    r.append(bad(GATE_IDS[7],["invalid safety invariant"],serr) if serr else ok(GATE_IDS[7],"permanent safety invariants locked"))
    danger=[]; de=[]
    for rel in ch:
        p=root/rel
        if rel.endswith(".py") and not matches(rel,["tests/governance/**"]) and p.is_file():
            f=capability_findings(p)
            if f:danger.append(rel);de.extend(f"{rel}: {x}" for x in f)
    r.append(bad(GATE_IDS[8],de,danger) if danger else ok(GATE_IDS[8],"no executable wallet/order capability introduced"))
    research=[]; raw=str(c.get("authoritative_raw_root",""))
    for rel in ch:
        p=root/rel
        if rel.endswith(".py") and matches(rel,c.get("research_paths",[])) and p.is_file() and (raw in p.read_text() or "raw_chain_v1" in p.read_text()):research.append(rel)
    r.append(bad(GATE_IDS[9],["research references authoritative raw root"],research) if research else ok(GATE_IDS[9],"no research authority violation"))
    wf=[]; wfe=[]
    for rel in ch:
        p=root/rel
        if matches(rel,[".github/workflows/**"]) and p.is_file():
            f=workflow_findings(p)
            if f:wf.append(rel);wfe.extend(f"{rel}: {x}" for x in f)
    r.append(bad(GATE_IDS[10],wfe,wf) if wf else ok(GATE_IDS[10],"workflow pinned, read-only and non-dispatch"))
    return r

def main(argv:list[str]|None=None)->int:
    ap=argparse.ArgumentParser();ap.add_argument("--root",type=Path,default=Path.cwd());ap.add_argument("--contract",type=Path,default=Path("governance/repository_contract.yaml"));ap.add_argument("--base",required=True);ap.add_argument("--head",required=True);ap.add_argument("--authorization",type=Path);ap.add_argument("--report",type=Path);a=ap.parse_args(argv)
    root=a.root.resolve(); cp=a.contract if a.contract.is_absolute() else root/a.contract; c=load_structured(cp); auth=load_structured(a.authorization) if a.authorization else None
    gates=evaluate(root,c,a.base,a.head,auth); report={"schema_version":"senex-repository-contract-report-v1","repository":c.get("repository"),"base_sha":a.base,"head_sha":a.head,"gates":[asdict(x) for x in gates],"pass":all(x.status=="PASS" for x in gates)}; payload=canonical_json_bytes(report)
    if a.report:a.report.write_bytes(payload)
    sys.stdout.buffer.write(payload);return 0 if report["pass"] else 1
if __name__=="__main__":raise SystemExit(main())
