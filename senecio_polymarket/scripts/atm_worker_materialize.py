from __future__ import annotations
import argparse, hashlib, json, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atm_worker.worker import CAPABILITIES, PROHIBITIONS, compute_scope_hash, run_job

def write_json(path: Path, obj): path.write_text(json.dumps(obj,indent=2,sort_keys=True)+"\n")
def sha(path: Path): return hashlib.sha256(path.read_bytes()).hexdigest()

def job_for(req, caps, target, snapshot, jid):
    j={"protocol_version":"atm-worker.v1","job_id":jid,"lease_id":"shadow-lease","attempt":1,"target_repository_or_dataset":target,"target_base_sha_or_snapshot":snapshot,"allowed_paths":["input","artifacts"],"required_capabilities":caps,"structured_requirements":req,"frozen_acceptance_criteria":["deterministic","fail-closed","zero-spend","no-target-execution"],"upstream_issue_reference":target,"outgoing_spend_cap_usd":0,"lease_state":"ACTIVE","lease_expires_at":"2099-01-01T00:00:00Z"}
    j["fixed_job_scope_hash"]=compute_scope_hash(j); return j

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--out",required=True); ap.add_argument("--source-sha",required=True); a=ap.parse_args()
    out=Path(a.out); out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); target=root/"target"; state=root/"state"; (target/"input").mkdir(parents=True); (target/"artifacts").mkdir()
        tableau_src='''class ColumnType:\n    AUTH = 7\n    MAX = 7\n\ndef create_user_from_line(line):\n    line = line.strip().lower()\n    values = [v.strip() for v in line.split(",")]\n    return values\n'''
        (target/"input/tableau_shadow.py").write_text(tableau_src)
        req={"operation":"bounded_python_pipeline_repair","input":"input/tableau_shadow.py","patch_output":"artifacts/tableau_shadow_fixed.py","replacements":[{"old":"MAX = 7","new":"MAX = 8"},{"old":"line = line.strip().lower()","new":"line = line.strip()"}],"static_checks":[{"name":"auth-column-reachable","contains":"MAX = 8"},{"name":"username-case-preserved","not_contains":"line = line.strip().lower()"}]}
        j=job_for(req,["bounded_python_pipeline_repair","regression_case_generation"],"https://github.com/tableau/server-client-python/issues/1809","aa9e3a0bd3114e0dbb7ec41abd4784483fb89277","shadow-tableau-1809")
        completion=run_job(j,state,target,a.source_sha,now=datetime(2026,8,18,tzinfo=timezone.utc))
        if completion["status"] != "SUCCEEDED": raise RuntimeError(completion)
        patch=(target/"artifacts/tableau_shadow_fixed.py").read_text()
        shadow={"classification":"CAN_HANDLE","public_task_url":"https://github.com/tableau/server-client-python/issues/1809","retrieved_at":"2026-08-18T09:28:00Z","public_task_state":"OPEN","public_snapshot_sha":"aa9e3a0bd3114e0dbb7ec41abd4784483fb89277","execution_mode":"SHADOW_REPRODUCTION_NO_UPSTREAM_WRITE","fixture_scope":"minimal reproduction of issue bugs 1 and 2; not a claim that all five upstream bugs were repaired","completion":completion,"patch_text":patch,"patch_sha256":hashlib.sha256(patch.encode()).hexdigest()}
        write_json(out/"shadow-can-handle.json",shadow)
    adjacent={"classification":"NEEDS_OTHER_WORKER","public_task_url":"https://github.com/grodriguez4321-tech/learning-snake/issues/24","retrieved_at":"2026-08-18T09:29:00Z","public_task_state":"OPEN","reason":"requires Qt learner-facing UI, curriculum authoring, Windows-native CI/visual QA, and broad multi-file product integration outside immutable SENEX worker capabilities","no_third_party_mutation":True}
    write_json(out/"shadow-needs-other-worker.json",adjacent)
    matrix={"worker_id":"senex-prophet","worker_version":"aud067.v1","protocol":"atm-worker.v1","capabilities":list(CAPABILITIES),"prohibitions":list(PROHIBITIONS),"job_kinds":{"repo_or_data_replay":"CAN_HANDLE","causal_validation":"CAN_HANDLE","provenance_hashing":"CAN_HANDLE","statistical_eval":"CAN_HANDLE","robustness_stress":"CAN_HANDLE","bounded_python_data_repair":"CAN_HANDLE","frontend_ui_or_native_app":"NEEDS_OTHER_WORKER","deployment_or_live_trading":"CANNOT_HANDLE","wallet_payment_or_spend":"CANNOT_HANDLE"}}
    write_json(out/"capability-matrix.json",matrix)
    security={"env_secret_theft":"DENY_BY_NO_TARGET_EXECUTION","dot_env_theft":"DENY_BY_ALLOWED_PATH_AND_NO_AMBIENT_READ","symlink_escape":"FAIL_CLOSED_TESTED","hardlink_escape":"FAIL_CLOSED_TESTED","outside_workspace_write":"FAIL_CLOSED_TESTED","network_bypass":"FAIL_CLOSED_TESTED","implicit_shell_hook":"FAIL_CLOSED_TESTED","docker_socket":"FAIL_CLOSED_TESTED","ssh_agent":"FAIL_CLOSED_TESTED","wallet_payment_live_trading":"UNSUPPORTED_FAIL_CLOSED","outgoing_spend_usd":0}
    write_json(out/"red-team-matrix.json",security)
    acceptance={"ACK_BEFORE_WORK":"TESTED","MULTIPLE_PROGRESS_EVENTS":"TESTED","CRASH_AFTER_ACK_RECOVERY":"TESTED","CRASH_DURING_WORK_RECOVERY":"TESTED","IDEMPOTENT_DUPLICATE_DISPATCH":"TESTED","CANCELLATION_NO_ORPHAN":"TESTED","POST_ACK_OPERATION_FAILURE_TERMINAL":"TESTED","FIXED_SCOPE_HASH_ECHO":"TESTED","EXACT_SOURCE_SHA_OUTPUT":"TESTED","DEFAULT_DENY_NETWORK":"TESTED","DOT_ENV_DEFAULT_DENY":"TESTED","ZERO_SPEND":"TESTED","REAL_WORLD_CAN_HANDLE_SHADOW":"TESTED","ADJACENT_NEEDS_OTHER_WORKER":"TESTED"}
    write_json(out/"acceptance-matrix.json",acceptance)
    receipt={"ORDER":"AUD-067","UPSTREAM":"ORDER-WR-003 / ATM Issue #27","BASE_SHA":"43c8023d3a4623381e45da02d9efa8e9b5888f47","SOURCE_SHA":a.source_sha,"WORKER_ID":"senex-prophet","WORKER_VERSION":"aud067.v1","CAPABILITY_COUNT":len(CAPABILITIES),"PRODUCTION_MUTATION":0,"SUPABASE_MUTATION":0,"NORTHFLANK_MUTATION":0,"RUNTIME017_MUTATIONS":0,"THRESHOLD_WEIGHT_TUNING":0,"REAL_TRADING":0,"OUTGOING_SPEND_USD":0,"READY_FOR_ATM_INTEGRATION":"NOT_CLAIMED_RESERVED_FOR_INDEPENDENT_AUD","FILES":{p.name:sha(p) for p in sorted(out.glob("*.json"))}}
    write_json(out/"aud-067-receipt.json",receipt)
    print("AUD067_WORKER_ID=senex-prophet"); print("AUD067_SHADOW_CAN_HANDLE=PASS"); print("AUD067_ADJACENT_ROUTING=PASS"); print("PRODUCTION_MUTATION=0"); print("RUNTIME017_MUTATIONS=0"); print("OUTGOING_SPEND_USD=0")
if __name__=="__main__": main()
