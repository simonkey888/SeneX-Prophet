from __future__ import annotations

import ast, json, math
from datetime import datetime, timezone
from pathlib import Path
from .worker_core import *
def operate(job,root,ctl):
    req=job["structured_requirements"]; op=req.get("operation"); allowed=job["allowed_paths"]; budget=[0]
    if op=="provenance_hash_validation":
        files=[]
        for rel in req.get("inputs",[]):
            data=read_bytes(root,rel,allowed,ctl,budget); files.append({"path":rel,"sha256":sha256_bytes(data),"bytes":len(data)})
        status="PASS"
        for exp in req.get("expected_files",[]):
            got=next((x for x in files if x["path"]==exp.get("path")),None)
            if not got or got["sha256"]!=exp.get("sha256") or got["bytes"]!=exp.get("bytes"): status="FAIL"
        return {"operation":op,"status":status,"files":files},[]
    if op=="deterministic_data_replay":
        rows=load_json(root,req["input"],allowed,ctl,budget)
        if not isinstance(rows,list): raise WorkerInputError("replay input must be list")
        keys=req.get("sort_keys",["ts","id"]); ordered=sorted(rows,key=lambda r:tuple(str(r.get(k,"")) for k in keys))
        return {"operation":op,"row_count":len(ordered),"replay_sha256":sha256_bytes(canon(ordered)),"rows":ordered},[]
    if op=="causal_cutoff_validation":
        rows=load_json(root,req["input"],allowed,ctl,budget)
        if not isinstance(rows,list): raise WorkerInputError("causal input must be list")
        violations=[]; missing=False; cutoff=parse_time(job["cutoff"],"cutoff").timestamp()
        for i,row in enumerate(rows):
            ctl.check("causal_loop")
            if not isinstance(row,dict): raise WorkerInputError("malformed temporal row")
            decision=finite(row.get("decision_ts"),"decision_ts")
            if decision>cutoff: violations.append({"row":i,"kind":"decision_after_cutoff"})
            for k in req.get("availability_time_fields",[]):
                if k not in row: raise WorkerInputError("missing temporal field "+k)
                if finite(row[k],k)>decision: violations.append({"row":i,"field":k,"kind":"future_availability"})
            for k in req.get("event_time_fields",[]):
                if k in row: finite(row[k],k)
            for k in req.get("label_time_fields",[]):
                if k not in row: raise WorkerInputError("missing temporal field "+k)
                if finite(row[k],k)<=decision: violations.append({"row":i,"field":k,"kind":"nonfuture_label"})
            for k in req.get("required_history_fields",[]):
                if row.get(k) is None: missing=True
        status="FAIL" if violations else "UNKNOWN" if missing or job["data_provenance"].get("complete") is not True else "PASS"
        return {"operation":op,"status":status,"violations":violations,"missing_prior_history":missing},[]
    if op=="statistical_evaluation":
        rows=load_json(root,req["input"],allowed,ctl,budget)
        if not isinstance(rows,list): raise WorkerInputError("statistical input must be list")
        min_n=int(req.get("min_evidence_n",3))
        if min_n<2: raise WorkerInputError("min_evidence_n too small")
        ds=[]
        for row in rows:
            if not isinstance(row,dict): raise WorkerInputError("malformed statistical row")
            ds.append(finite(row.get(req["candidate_field"]),"candidate")-finite(row.get(req["baseline_field"]),"baseline"))
        if not ds: return {"operation":op,"n":0,"orientation":"INCONCLUSIVE","mean_delta":None,"ci95":None},[]
        m=sum(ds)/len(ds)
        if len(ds)<min_n: return {"operation":op,"n":len(ds),"orientation":"INCONCLUSIVE","mean_delta":m,"ci95":None,"min_evidence_n":min_n},[]
        var=sum((x-m)**2 for x in ds)/(len(ds)-1); se=math.sqrt(var/len(ds)); lo=m-1.96*se; hi=m+1.96*se
        orientation="IMPROVEMENT" if hi<0 else "DEGRADATION" if lo>0 else "INCONCLUSIVE"
        if job["data_provenance"].get("complete") is not True: orientation="INCONCLUSIVE"
        return {"operation":op,"n":len(ds),"mean_delta":m,"ci95":[lo,hi],"orientation":orientation,"min_evidence_n":min_n},[]
    if op=="robustness_regime_stress":
        rows=load_json(root,req["input"],allowed,ctl,budget)
        if not isinstance(rows,list): raise WorkerInputError("robustness input must be list")
        n=int(req.get("train_end_index",0))
        if n<1 or n>=len(rows): raise WorkerInputError("explicit train_end_index required")
        vals=[finite(r.get(req["value_field"]),req["value_field"]) for r in rows]; train=sorted(vals[:n]); med=train[len(train)//2]; ev=vals[n:]
        return {"operation":op,"status":"PASS","train_end_index":n,"train_only_median":med,"evaluation_n":len(ev)},[]
    if op=="bounded_python_pipeline_repair":
        raw=read_bytes(root,req["input"],allowed,ctl,budget)
        try: src=raw.decode("utf-8","strict")
        except UnicodeDecodeError as e: raise WorkerInputError("invalid UTF-8") from e
        try: ast.parse(src)
        except SyntaxError as e: raise WorkerInputError("source syntax invalid") from e
        patched=src; applied=[]
        for item in req.get("replacements",[]):
            ctl.check("repair_loop"); old=str(item["old"]); new=str(item["new"]); expected=int(item.get("expected_count",1)); count=patched.count(old)
            if count!=expected: raise WorkerInputError("replacement count mismatch")
            patched=patched.replace(old,new); applied.append({"old_sha256":sha256_bytes(old.encode()),"new_sha256":sha256_bytes(new.encode()),"count":count})
        try: ast.parse(patched)
        except SyntaxError as e: raise WorkerInputError("patched syntax invalid") from e
        checks=[]
        for c in req.get("static_checks",[]):
            ok=(c["contains"] in patched) if "contains" in c else (c["not_contains"] not in patched) if "not_contains" in c else False
            checks.append({"name":c.get("name","static"),"pass":ok})
        if not all(c["pass"] for c in checks): return {"operation":op,"status":"FAIL","checks":checks},[]
        art=write_bytes(root,req.get("patch_output","artifacts/repair.py"),allowed,patched.encode(),ctl)
        return {"operation":op,"status":"PASS","source_sha256":sha256_bytes(raw),"patched_sha256":art["sha256"],"applied":applied,"checks":checks,"artifact":art,"target_execution":0},[art]
    raise WorkerInputError("unsupported operation")

def semantic_status(result):
    s=result.get("status") if isinstance(result,dict) else None
    return "FAILED" if s=="FAIL" else "SUCCEEDED"

def completion(job,source,state,status,result,artifacts,error_class=None):
    worker_status="PASS" if status=="SUCCEEDED" else "INCONCLUSIVE" if status=="CANCELLED" else "FAIL"
    return {"protocol_version":PROTOCOL_VERSION,"job_id":job["job_id"],"canonical_opportunity_id":job["canonical_opportunity_id"],"worker_id":WORKER_ID,"worker_version":WORKER_VERSION,"source_sha":source,"work_lease_id":job["work_lease_id"],"attempt":job["attempt"],"scope_hash":job["scope_hash"],"status":status,"task_result":result,"artifacts":artifacts,"tests":[],"worker_assessment":{"status":worker_status,"authoritative":False,"scope":"LOCAL_EVIDENCE_ONLY"},"external_acceptance":{"status":"NOT_EVALUATED_BY_WORKER","authoritative":False},"provenance":{"target":job["target_repository_or_dataset"],"verified_snapshot":state["verified_snapshot"],"data_provenance":job["data_provenance"]},"side_effects":{"production_mutations":0,"supabase_writes":0,"northflank_mutations":0,"runtime017_mutations":0,"threshold_weight_tuning":0,"real_trading":0,"wallet_or_payment":0,"outgoing_spend_usd":0,"network_requests":0,"child_processes":0},"outgoing_spend_usd":0,"started_at":state["started_at"],"finished_at":datetime.now(timezone.utc).isoformat(),"error_class":error_class}

def run_job(job,state_root,target_root,source_sha,workspace_root,canonical_senex_root,now=None,clock=None,cancel_check=None):
    now=(now or datetime.now(timezone.utc)).astimezone(timezone.utc); clock=clock or (lambda:datetime.now(timezone.utc)); cancel_check=cancel_check or (lambda:False)
    workspace=Path(workspace_root); target=Path(target_root); state_root=Path(state_root); canonical=Path(canonical_senex_root)
    validate_job(job,now); check_workspace(workspace,target,state_root,canonical)
    ctl=Ctl(parse_time(job["job_deadline"],"job_deadline"),clock,cancel_check)
    try: verified=verify_snapshot(job,target,ctl)
    except CancellationRequested as e: raise JobRejected(str(e)) from e
    except DeadlineExceeded as e: raise JobRejected(str(e)) from e
    key=state_key(job); sp=state_root/(key+".json"); ep=state_root/(key+".events.jsonl")
    state=json.loads(sp.read_text()) if sp.exists() else None
    if state:
        if state.get("scope_hash")!=job["scope_hash"]: raise JobRejected("execution tuple reused with different scope")
        if state.get("status") in TERMINAL: return state["completion"]
    else:
        started=datetime.now(timezone.utc).isoformat(); state={"status":"ACKED","scope_hash":job["scope_hash"],"started_at":started,"verified_snapshot":verified,"crash_flags":{},"progress_count":0}; atomic_json(sp,state); append_event(ep,{"event":"ACK","scope_hash":job["scope_hash"],"at":started})
    sim=job["structured_requirements"].get("simulate_crash_at")
    if sim=="after_ack" and not state["crash_flags"].get(sim): state["crash_flags"][sim]=True; atomic_json(sp,state); raise CrashInjected(sim)
    if state.get("status")=="RESULT_READY" and "pending" in state:
        result=state["pending"]["result"]; artifacts=state["pending"]["artifacts"]; status=state["pending"]["status"]
    else:
        state["status"]="RUNNING"; state["progress_count"]+=1; atomic_json(sp,state); append_event(ep,{"event":"PROGRESS","step":"STARTED","n":state["progress_count"]})
        if sim=="during_work" and not state["crash_flags"].get(sim): state["crash_flags"][sim]=True; atomic_json(sp,state); raise CrashInjected(sim)
        try:
            ctl.check("before_operation"); result,artifacts=operate(job,target,ctl); status=semantic_status(result)
        except CancellationRequested as e: result={"error":str(e)}; artifacts=[]; status="CANCELLED"; error_class="CANCELLED"
        except DeadlineExceeded as e: result={"error":str(e)}; artifacts=[]; status="FAILED"; error_class="DEADLINE_EXCEEDED"
        except WorkerInputError as e: result={"error":str(e)}; artifacts=[]; status="FAILED"; error_class="WORKER_INPUT_ERROR"
        else: error_class=None
        state["status"]="RESULT_READY"; state["pending"]={"result":result,"artifacts":artifacts,"status":status,"error_class":error_class}; state["progress_count"]+=1; atomic_json(sp,state); append_event(ep,{"event":"PROGRESS","step":"RESULT_MATERIALIZED","n":state["progress_count"]}); append_event(ep,{"event":"RESULT_READY","n":state["progress_count"]})
    error_class=state.get("pending",{}).get("error_class")
    if sim=="after_artifact_before_finalize" and not state["crash_flags"].get(sim): state["crash_flags"][sim]=True; atomic_json(sp,state); raise CrashInjected(sim)
    out=completion(job,source_sha,state,status,result,artifacts,error_class)
    state["status"]=status; state["completion"]=out; atomic_json(sp,state); append_event(ep,{"event":"TERMINAL","status":status,"at":out["finished_at"]}); return out

def independent_check_completion(job,out,target_root):
    reasons=[]
    if out.get("scope_hash")!=compute_scope_hash(job): reasons.append("scope_hash_mismatch")
    for f in ("job_id","canonical_opportunity_id","work_lease_id"):
        if out.get(f)!=job.get(f): reasons.append(f+"_mismatch")
    root=Path(target_root)
    for a in out.get("artifacts",[]):
        try: data=(root/a["path"]).read_bytes()
        except Exception: reasons.append("artifact_missing"); continue
        if sha256_bytes(data)!=a.get("sha256") or len(data)!=a.get("bytes"): reasons.append("artifact_hash_mismatch")
    checks={c.get("name"):c.get("pass") for c in out.get("task_result",{}).get("checks",[]) if isinstance(c,dict)}
    for c in job.get("frozen_acceptance_criteria",[]):
        if c.get("type")=="static_check" and checks.get(c.get("name")) is not True: reasons.append("frozen_predicate_failed:"+str(c.get("name")))
        if c.get("type")=="zero_spend" and out.get("outgoing_spend_usd")!=0: reasons.append("spend_nonzero")
    if out.get("external_acceptance",{}).get("authoritative") is not False: reasons.append("worker_claimed_external_authority")
    return {"status":"FAIL" if reasons else "PASS","authoritative":True,"reasons":sorted(set(reasons))}
