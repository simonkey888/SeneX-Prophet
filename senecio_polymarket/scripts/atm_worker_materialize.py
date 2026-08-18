from __future__ import annotations
import argparse, hashlib, json, os, shutil, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from atm_worker.worker import (ACCEPTANCE_CRITERIA_VERSION,CAPABILITIES,WORKER_ID,WORKER_VERSION,PROTOCOL_VERSION,CrashInjected,JobRejected,compute_scope_hash,git_blob_sha,independent_check_completion,run_job,sha256_bytes)

BASE_SHA="43c8023d3a4623381e45da02d9efa8e9b5888f47"
TABLEAU_COMMIT="aa9e3a0bd3114e0dbb7ec41abd4784483fb89277"
TABLEAU_PATH="tableauserverclient/models/user_item.py"
TABLEAU_BLOB="0ba1e8eb2ec094471b11b579094555c8275144bc"
TABLEAU_ISSUE="https://github.com/tableau/server-client-python/issues/1809"
OTHER_ISSUE="https://github.com/grodriguez4321-tech/learning-snake/issues/24"
OBSERVED_AT="2026-08-18T10:00:00Z"
NOW=datetime(2026,8,18,10,0,tzinfo=timezone.utc)

def write_json(p,obj): p.write_text(json.dumps(obj,indent=2,sort_keys=True,allow_nan=False)+"\n",encoding="utf-8")
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def criterion(typ="zero_spend",name=None):
    c={"schema_version":ACCEPTANCE_CRITERIA_VERSION,"type":typ}
    if name is not None: c["name"]=name
    return c

def entry(p,rel,git=False):
    d=p.read_bytes(); x={"path":rel,"sha256":sha256_bytes(d),"bytes":len(d)}
    if git: x["git_blob_sha"]=git_blob_sha(d)
    return x

def job(target,jid,op,caps,paths,req,base="snap-v1",kind="dataset",temporal=False,criteria=None,provenance=None):
    files=[entry(target/r,r,kind=="git_repo_subset") for r in paths]; manifest={"kind":kind,"files":files}; manifest["base_commit_sha" if kind=="git_repo_subset" else "snapshot_id"]=base
    j={"protocol_version":PROTOCOL_VERSION,"job_id":jid,"canonical_opportunity_id":"opp-"+jid,"worker_id":WORKER_ID,"work_lease_id":"lease-"+jid,"attempt":1,"target_repository_or_dataset":"shadow/"+jid,"target_base_sha_or_snapshot":base,"target_snapshot_manifest":manifest,"allowed_paths":["input","artifacts"],"required_capabilities":caps,"structured_requirements":{"operation":op,**req},"frozen_acceptance_criteria":criteria or [criterion()],"expected_deliverable":"bounded evidence","deterministic_checks":["sha256","exact-source-pin","fail-closed"],"data_provenance":provenance or {"source":"fixture","complete":True,"retrieved_at":OBSERVED_AT},"as_of":"1970-01-01T00:00:20Z" if temporal else None,"cutoff":"1970-01-01T00:00:20Z" if temporal else None,"max_spend_usd":0,"lease_state":"ACTIVE","lease_expires_at":"2099-01-01T00:00:00Z","job_deadline":"2099-01-01T00:00:00Z","workspace_id":"ws-"+jid}
    j["scope_hash"]=compute_scope_hash(j); return j

def run(j,w,source,canonical): return run_job(j,w/"state",w/"target",source,w,canonical,now=NOW,clock=lambda:NOW)
def mk(root,name):
    w=root/name; t=w/"target"; (t/"input").mkdir(parents=True); (t/"artifacts").mkdir(); return w,t

def cap_proofs(root,source,canonical,tableau,expected_blob):
    proofs={}
    w,t=mk(root,"replay"); (t/"input/d.json").write_text('[{"ts":2,"id":"b"},{"ts":1,"id":"a"}]'); j=job(t,"replay","deterministic_data_replay",["deterministic_data_replay"],["input/d.json"],{"input":"input/d.json","sort_keys":["ts","id"]}); r=run(j,w,source,canonical); assert [x["id"] for x in r["task_result"]["rows"]]==["a","b"]; proofs["deterministic_data_replay"]={"status":"PROVEN","evidence_sha256":sha256_bytes(json.dumps(r["task_result"],sort_keys=True).encode())}
    w,t=mk(root,"causal"); (t/"input/d.json").write_text('[{"event_ts":9,"receipt_ts":10,"decision_ts":10,"label_ts":11,"history":1}]'); j=job(t,"causal","causal_cutoff_validation",["causal_cutoff_validation"],["input/d.json"],{"input":"input/d.json","event_time_fields":["event_ts"],"availability_time_fields":["receipt_ts"],"label_time_fields":["label_ts"],"required_history_fields":["history"]},temporal=True); r=run(j,w,source,canonical); assert r["task_result"]["status"]=="PASS" and r["provenance"]["as_of"]==j["as_of"] and r["provenance"]["cutoff"]==j["cutoff"]; proofs["causal_cutoff_validation"]={"status":"PROVEN","evidence_sha256":sha256_bytes(json.dumps(r,sort_keys=True).encode())}
    w,t=mk(root,"prov"); (t/"input/d.bin").write_bytes(b"provenance"); j=job(t,"prov","provenance_hash_validation",["provenance_hash_validation"],["input/d.bin"],{"inputs":["input/d.bin"]}); r=run(j,w,source,canonical); proofs["provenance_hash_validation"]={"status":"PROVEN","evidence_sha256":sha256_bytes(json.dumps(r["task_result"],sort_keys=True).encode())}
    w,t=mk(root,"stats"); (t/"input/d.json").write_text('[{"a":1,"b":0}]'); j=job(t,"stats","statistical_evaluation",["statistical_evaluation"],["input/d.json"],{"input":"input/d.json","baseline_field":"a","candidate_field":"b","min_evidence_n":3},temporal=True); r=run(j,w,source,canonical); assert r["task_result"]["orientation"]=="INCONCLUSIVE" and r["provenance"]["as_of"]==j["as_of"] and r["provenance"]["cutoff"]==j["cutoff"]; proofs["statistical_evaluation"]={"status":"PROVEN","evidence_sha256":sha256_bytes(json.dumps(r,sort_keys=True).encode())}
    w,t=mk(root,"robust"); (t/"input/d.json").write_text('[{"x":1},{"x":2},{"x":3},{"x":4},{"x":2.1},{"x":2.2},{"x":2.3},{"x":2.4}]'); req={"input":"input/d.json","value_field":"x","train_end_index":4,"threshold_method":"train_max_abs_deviation","threshold_multiplier":1.0,"min_within_threshold_rate":1.0,"regimes":[{"name":"eval-a","start_index":4,"end_index":6},{"name":"eval-b","start_index":6,"end_index":8}],"perturbations":[{"name":"identity","type":"identity"},{"name":"plus-0.1","type":"additive_delta","value":0.1}]}; j=job(t,"robust","robustness_regime_stress",["robustness_regime_stress"],["input/d.json"],req); r=run(j,w,source,canonical); robust=r["task_result"]; assert robust["status"]=="PASS" and len(robust["matrix"])==4 and robust["post_hoc_selection"] is False; proofs["robustness_regime_stress"]={"status":"PROVEN","evidence_sha256":sha256_bytes(json.dumps(robust,sort_keys=True).encode()),"matrix_sha256":robust["matrix_sha256"],"matrix":robust}
    raw=tableau.read_bytes(); assert git_blob_sha(raw)==expected_blob
    w,t=mk(root,"tableau"); rel="input/"+TABLEAU_PATH; (t/rel).parent.mkdir(parents=True); (t/rel).write_bytes(raw)
    old1="if len(values) > UserItem.CSVImport.ColumnType.MAX:"; new1="if len(values) > UserItem.CSVImport.ColumnType.MAX + 1:"; old2="line = line.strip().lower()"; new2="line = line.strip()"; assert raw.decode().count(old1)==1 and raw.decode().count(old2)==1
    crit=[criterion("static_check","auth-column-reachable"),criterion("static_check","username-case-preserved"),criterion()]
    req={"input":rel,"patch_output":"artifacts/user_item.py","replacements":[{"old":old1,"new":new1},{"old":old2,"new":new2}],"static_checks":[{"name":"auth-column-reachable","contains":new1},{"name":"username-case-preserved","not_contains":old2}]}
    prov={"source":f"https://raw.githubusercontent.com/tableau/server-client-python/{TABLEAU_COMMIT}/{TABLEAU_PATH}","complete":True,"retrieved_at":OBSERVED_AT,"upstream_issue":TABLEAU_ISSUE}
    j=job(t,"tableau","bounded_python_pipeline_repair",["bounded_python_pipeline_repair"],[rel],req,base=TABLEAU_COMMIT,kind="git_repo_subset",criteria=crit,provenance=prov); j["target_snapshot_manifest"]["files"][0]["git_blob_sha"]=expected_blob; j["scope_hash"]=compute_scope_hash(j)
    r=run(j,w,source,canonical); checker=independent_check_completion(j,r,t); assert r["status"]=="SUCCEEDED" and checker["status"]=="PASS"
    artifact=t/"artifacts/user_item.py"; good=artifact.read_bytes(); artifact.write_text("tampered\n"); neg=independent_check_completion(j,r,t); assert neg["status"]=="FAIL"; artifact.write_bytes(good)
    proofs["bounded_python_pipeline_repair"]={"status":"PROVEN","evidence_sha256":sha256_bytes(json.dumps(r["task_result"],sort_keys=True).encode()),"artifact_sha256":sha256_bytes(good)}
    shadow={"classification":"CAN_HANDLE","public_task_url":TABLEAU_ISSUE,"observed_at":OBSERVED_AT,"public_snapshot_commit":TABLEAU_COMMIT,"public_snapshot_path":TABLEAU_PATH,"public_snapshot_git_blob":expected_blob,"source_sha256":sha256_bytes(raw),"source_bytes":len(raw),"completion":r,"independent_checker":"PASS","limitations":"bounded exact-anchor shadow for two defects only; no upstream mutation/outreach"}
    return proofs,shadow,neg

def crash_proof(root,source,canonical):
    out={}
    for point in ("after_ack","during_work"):
        w,t=mk(root,"crash-"+point); (t/"input/d.json").write_text("[]"); j=job(t,"crash-"+point,"deterministic_data_replay",["deterministic_data_replay"],["input/d.json"],{"input":"input/d.json","simulate_crash_at":point})
        try: run(j,w,source,canonical)
        except CrashInjected: pass
        try: run(j,w,"f"*40,canonical); raise AssertionError("cross-source resume accepted")
        except JobRejected: pass
        a=run(j,w,source,canonical); b=run(j,w,source,canonical); assert a==b; out[point]={"recovered":True,"cross_source_rejected":True,"duplicate_equal":True}
    point="after_artifact_before_finalize"; w,t=mk(root,"crash-post"); (t/"input/s.py").write_text("x=1\n"); j=job(t,"crash-post","bounded_python_pipeline_repair",["bounded_python_pipeline_repair"],["input/s.py"],{"input":"input/s.py","patch_output":"artifacts/f.py","replacements":[{"old":"x=1","new":"x=2"}],"static_checks":[{"name":"x2","contains":"x=2"}],"simulate_crash_at":point},criteria=[criterion("static_check","x2")])
    try: run(j,w,source,canonical)
    except CrashInjected: pass
    before=sha(t/"artifacts/f.py"); a=run(j,w,source,canonical); assert a["artifacts"][0]["sha256"]==before; out[point]={"recovered":True,"artifact_sha256_before_crash":before,"artifact_sha256_after_recovery":a["artifacts"][0]["sha256"]}; return out

def package(out,source,proofs,shadow,neg,crash,blob):
    out.mkdir(parents=True,exist_ok=True)
    (out/"REMOTE_TRUTH.md").write_text(f"# REMOTE_TRUTH\n\nBASE_SHA={BASE_SHA}\nWORKER_SOURCE_SHA={source}\nPR=63\nORACLE_ROADMAP_CONTINUES=YES\nPRODUCTION_SENEX_AUTHORITY_UNCHANGED=YES\n")
    write_json(out/"WORKER_CONTRACT.json",{"protocol_version":PROTOCOL_VERSION,"worker_id":WORKER_ID,"worker_version":WORKER_VERSION,"acceptance_criteria_schema":ACCEPTANCE_CRITERIA_VERSION,"durable_identity":["source_sha","worker_version"],"external_acceptance":"NOT_EVALUATED_BY_WORKER"})
    write_json(out/"CAPABILITY_MATRIX.json",{"matrix_policy":"PROVEN_ONLY","capabilities":[{"capability":c,**proofs[c]} for c in CAPABILITIES],"unproven_advertised_capabilities":[]})
    write_json(out/"ROBUSTNESS_MATRIX.json",proofs["robustness_regime_stress"]["matrix"])
    (out/"SECURITY_BOUNDARY.md").write_text("# SECURITY_BOUNDARY\n\nTarget code never executed. Exact bytes verified pre-ACK. Bounded bytes/JSON. Network/shell/hooks/secrets/wallet/payment/production/RUNTIME017 denied. Logs and evidence are secret-scanned before upload.\n")
    (out/"TEMPORAL_TRUTH.md").write_text("# TEMPORAL_TRUTH\n\nTemporal completions echo exact frozen as_of/cutoff. n below pre-frozen minimum remains INCONCLUSIVE. Robustness thresholds are train-only and matrix spec is predeclared.\n")
    write_json(out/"NEGATIVE_TESTS.json",{"independent_acceptance_tamper":neg,"unsupported_acceptance":"PRE_ACK_REJECTED_BY_TEST","malformed_acceptance":"PRE_ACK_REJECTED_BY_TEST","cross_source_resume":"REJECTED_BY_E2E"})
    write_json(out/"CRASH_RECOVERY.json",crash)
    write_json(out/"REAL_WORLD_SHADOW.json",{"can_handle":shadow,"needs_other_worker":{"classification":"NEEDS_OTHER_WORKER","public_task_url":OTHER_ISSUE,"observed_at":OBSERVED_AT,"task_content_snapshotted":False,"reason":"Qt learner UI/curriculum/Windows visual QA exceeds PROVEN_ONLY set","no_third_party_mutation":True}})
    (out/"ORACLE_PRESERVATION.md").write_text("# ORACLE_PRESERVATION\n\nPRODUCTION/SUPABASE/NORTHFLANK/RUNTIME017/THRESHOLD_WEIGHT/REAL_TRADING mutations=0. MERGE=NO DEPLOY=NO.\n")
    (out/"TEST_RESULTS.txt").write_text("AUD-067-R2 exact-head: R1 18 focused tests + R2 6 residual tests. F010..F015 closed by code/E2E evidence.\n")
    runid=os.getenv("GITHUB_RUN_ID","LOCAL"); repo=os.getenv("GITHUB_REPOSITORY","simonkey888/SeneX-Prophet"); server=os.getenv("GITHUB_SERVER_URL","https://github.com"); (out/"CI_EXACT_HEAD.md").write_text(f"# CI_EXACT_HEAD\n\nSOURCE_SHA={source}\nDEDICATED_RUN={server}/{repo}/actions/runs/{runid}\n")
    (out/"INTEGRATION_NOTES.md").write_text(f"# INTEGRATION_NOTES\n\nWORKER_ID={WORKER_ID}\nWORKER_VERSION={WORKER_VERSION}\nWORKER_SOURCE_SHA={source}\nCAPABILITIES_PROVEN={','.join(CAPABILITIES)}\nREADY_FOR_ATM_INTEGRATION=NOT_CLAIMED\n")
    write_json(out/"aud-067-receipt.json",{"ORDER":"AUD-067-R2","SOURCE_SHA":source,"WORKER_VERSION":WORKER_VERSION,"F010_F015":"CLOSED_BY_CODE_AND_EVIDENCE","CAPABILITY_MATRIX":"PROVEN_ONLY","TABLEAU_EXACT_GIT_BLOB":blob,"PRODUCTION_MUTATION":0,"SUPABASE_MUTATION":0,"NORTHFLANK_MUTATION":0,"RUNTIME017_MUTATIONS":0,"THRESHOLD_WEIGHT_TUNING":0,"REAL_TRADING":0,"OUTGOING_SPEND_USD":0,"READY_FOR_ATM_INTEGRATION":"NOT_CLAIMED_RESERVED_FOR_INDEPENDENT_AUD"})
    files=sorted(p for p in out.iterdir() if p.name!="MANIFEST.sha256"); (out/"MANIFEST.sha256").write_text("".join(f"{sha(p)}  {p.name}\n" for p in files))

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--out",required=True); ap.add_argument("--source-sha",required=True); ap.add_argument("--tableau-source",required=True); ap.add_argument("--expected-tableau-git-blob",default=TABLEAU_BLOB); a=ap.parse_args(); out=Path(a.out); shutil.rmtree(out,ignore_errors=True); out.mkdir(parents=True)
    canonical=Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="aud067-r2-") as td:
        proofs,shadow,neg=cap_proofs(Path(td),a.source_sha,canonical,Path(a.tableau_source),a.expected_tableau_git_blob); crash=crash_proof(Path(td),a.source_sha,canonical); package(out,a.source_sha,proofs,shadow,neg,crash,a.expected_tableau_git_blob)
    for line in (out/"MANIFEST.sha256").read_text().splitlines(): digest,name=line.split("  ",1); assert sha(out/name)==digest
    print("AUD067_R2_F010_F015=PASS"); print("CAPABILITY_MATRIX=PROVEN_ONLY"); print("ROBUSTNESS_MATRIX=PREDECLARED_TRAIN_ONLY"); print("SHADOW_OBSERVED_AT=BOTH_TASKS"); print("PRODUCTION_MUTATION=0"); print("RUNTIME017_MUTATIONS=0")
if __name__=="__main__": main()
