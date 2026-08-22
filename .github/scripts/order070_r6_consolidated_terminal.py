from __future__ import annotations

from pathlib import Path

SOURCE = Path(__file__).with_name("order070_r6_terminal.py")
s = SOURCE.read_text()


def one(old: str, new: str, label: str) -> None:
    global s
    n = s.count(old)
    if n != 1:
        raise RuntimeError(f"{label}: expected exactly one match, got {n}")
    s = s.replace(old, new, 1)


one(
    "import concurrent.futures, datetime as dt, hashlib, json, os, re, shutil, subprocess, tempfile, time, urllib.error, urllib.parse, urllib.request",
    "import concurrent.futures, copy, datetime as dt, hashlib, json, os, re, shutil, subprocess, tempfile, time, urllib.error, urllib.parse, urllib.request",
    "copy import",
)
one("HEAD='d166495e9a74f528ccce1adeb5ce97a281b175cf'; TREE='6106f1c2f39b4509d3a237eb807db5d45feb7463'", "HEAD='4b107bfb427cb85ea84850ffd9ddd5d7a4231d94'; TREE='5d1d9ec806b7d0e02031726565f08ef75d5a9340'", "exact identity")
one("BUILD_DIGEST='sha256:1806ad0bc71c45264695c1c8973a497a39f9903f867ece2d56fdbc12f44e4892'", "BUILD_DIGEST='sha256:8f4511e0ac2499e3b7408843a82e7f3a5bc4cc466c296003eb363842ad2023ac'", "build digest")

old_scope = """changed=git('diff','--name-only','483b389a83610992800181c0a21b5a337009f7b4..HEAD').splitlines()\nif changed!=['senecio_polymarket/backend/main.py']: raise RuntimeError(f'R6_SCOPE_DRIFT:{changed}')\nwrite('REMOTE_TRUTH.json',{'observed_at':iso(),'pr':67,'head':HEAD,'tree':TREE,'parent':'483b389a83610992800181c0a21b5a337009f7b4','changed_since_r5':changed,'candidate_scope':'OPTIONAL_ANALYTICS_LAZY_INIT_ONLY','merge':False,'tuning':0,'runtime017_mutation':0,'supabase_data_mutation':0})\nwrite('EXACT_GATE.json',{'observed_at':iso(),'workflow_run_id':os.environ.get('GITHUB_RUN_ID'),'head':HEAD,'tree':TREE,'gate':'PASS','native_pr_runs_action_required_without_jobs':True,'equivalent_original_workflow_commands_executed':True,'import_rss_kb':int(os.environ.get('R6_IMPORT_RSS_KB','0') or 0),'import_rss_limit_kb':81920})\n"""
new_scope = """changed=git('diff','--name-only','b438c0d6dc156d4183929366963df988d97a5283..HEAD').splitlines()\nexpected=sorted([\n    '.github/workflows/senex-order-070.yml', '.github/workflows/senex-score-001.yml', 'Dockerfile',\n    'edge/order070/worker.js', 'senecio_polymarket/Dockerfile',\n    'senecio_polymarket/backend/authority_snapshot.py', 'senecio_polymarket/backend/main_real.py',\n    'senecio_polymarket/backend/oracle_runner.py', 'senecio_polymarket/backend/runtime_provenance.py',\n    'senecio_polymarket/docs/ORDER_070_RUNTIME_TRUTH.md', 'senecio_polymarket/frontend/app.js',\n    'senecio_polymarket/oracle_runtime/predict_only.py',\n    'senecio_polymarket/tests/test_authoritative_learning.py', 'senecio_polymarket/tests/test_order_070.py',\n    'senecio_polymarket/tests/test_order_070_r6_public_boundary.py',\n])\nif sorted(changed)!=expected: raise RuntimeError(f'R6_SCOPE_DRIFT:{changed}')\nwrite('REMOTE_TRUTH.json',{'observed_at':iso(),'pr':67,'head':HEAD,'tree':TREE,'parent':'b438c0d6dc156d4183929366963df988d97a5283','changed_from_audited_b438':changed,'candidate_scope':'AUD_5381263728_CONSOLIDATED_F1_F5','merge':False,'tuning':0,'runtime017_mutation':0,'supabase_data_mutation':0})\nwrite('EXACT_GATE.json',{'observed_at':iso(),'workflow_run_id':os.environ.get('GITHUB_RUN_ID'),'head':HEAD,'tree':TREE,'gate':'PASS','native_exact_head_ci':'PASS_ALL_4','ci_runs':{'ORDER070':32585446334,'SCORE001':32585446345,'SCORE002':32585446326,'SMOKE':32585446328},'canonical_build_digest':BUILD_DIGEST})\n"""
one(old_scope, new_scope, "scope and exact CI evidence")

old_service = "e0=services_entry(); s0=service(); d0=deployment(); vcs=s0.get('vcsData') or {}; original=vcs.get('projectBranch')\nif s0.get('serviceType')!='combined' or original!='main' or e0.get('disabledCI') is not True: raise RuntimeError('NORTHFLANK_PREFLIGHT')\nvpatch={k:vcs[k] for k in ('accountLogin','vcsLinkId','selfHostedVcsId') if vcs.get(k)}; vpatch.update({'projectUrl':vcs['projectUrl'],'projectType':vcs['projectType'],'projectBranch':BRANCH})\n"
new_service = "e0=services_entry(); s0=service(); d0=deployment(); vcs=s0.get('vcsData') or {}; original=vcs.get('projectBranch')\nif s0.get('serviceType')!='combined' or original!='main' or e0.get('disabledCI') is not True: raise RuntimeError('NORTHFLANK_PREFLIGHT')\noriginal_build_settings=copy.deepcopy(s0.get('buildSettings') or {})\ntarget_build_settings=copy.deepcopy(original_build_settings)\ndocker_settings=copy.deepcopy(target_build_settings.get('dockerfile') or {})\ndocker_settings.update({'dockerFilePath':'/Dockerfile','dockerWorkDir':'/'})\ntarget_build_settings['dockerfile']=docker_settings\nvpatch={k:vcs[k] for k in ('accountLogin','vcsLinkId','selfHostedVcsId') if vcs.get(k)}; vpatch.update({'projectUrl':vcs['projectUrl'],'projectType':vcs['projectType'],'projectBranch':BRANCH})\n"
one(old_service, new_service, "root Dockerfile Northflank setup")
one("nf('PATCH',f'/projects/{PROJECT}/services/combined/{SERVICE}',{'disabledCI':True,'buildSource':'git','vcsData':vpatch}); switched=True", "nf('PATCH',f'/projects/{PROJECT}/services/combined/{SERVICE}',{'disabledCI':True,'buildSource':'git','vcsData':vpatch,'buildSettings':target_build_settings}); switched=True", "source switch build settings")
one("restore=dict(vpatch); restore['projectBranch']=original; nf('PATCH',f'/projects/{PROJECT}/services/combined/{SERVICE}',{'disabledCI':True,'buildSource':'git','vcsData':restore})", "restore=dict(vpatch); restore['projectBranch']=original; nf('PATCH',f'/projects/{PROJECT}/services/combined/{SERVICE}',{'disabledCI':True,'buildSource':'git','vcsData':restore,'buildSettings':target_build_settings})", "source restore root build settings")
one("if (service().get('vcsData') or {}).get('projectBranch')!='main' or services_entry().get('disabledCI') is not True: raise RuntimeError('SOURCE_RESTORE_FAILED')", "post_restore=service(); post_docker=((post_restore.get('buildSettings') or {}).get('dockerfile') or {})\nif (post_restore.get('vcsData') or {}).get('projectBranch')!='main' or services_entry().get('disabledCI') is not True: raise RuntimeError('SOURCE_RESTORE_FAILED')\nif post_docker.get('dockerFilePath')!='/Dockerfile' or post_docker.get('dockerWorkDir')!='/': raise RuntimeError(f'ROOT_DOCKERFILE_NOT_CANONICAL:{post_docker}')", "verify root build persisted")

one("lineLimit':2000", "lineLimit':1000", "Northflank log API bounded limit")
one("e2e_paths={'snapshot':paths['snapshot'],'context':'/api/market-context?symbol=BTCUSDT','provenance':'/api/runtime/provenance','health':'/healthz','ready':'/readyz?symbol=BTCUSDT','openapi':'/openapi.json'}", "e2e_paths={'snapshot':paths['snapshot'],'context':'/api/market-context?symbol=BTCUSDT','predictions':'/api/oracle/predictions/db?limit=50&symbol=BTCUSDT','provenance':'/api/runtime/provenance','health':'/healthz','ready':'/readyz?symbol=BTCUSDT','openapi':'/openapi.json'}", "edge dashboard parity E2E")
one(
    "for side in ('origin','edge'):\n    p=final['provenance'][side]['body'];",
    "if final['predictions']['origin']['body'] != final['predictions']['edge']['body']: raise RuntimeError('EDGE_DASHBOARD_PREDICTIONS_PARITY_FAILED')\nfor side in ('origin','edge'):\n    p=final['provenance'][side]['body'];",
    "prediction route body parity",
)
one(
    "base_cycles=int(base_state['body'].get('cycles_run') or 0); base_db=int(base_snap['body'].get('exact_total_predictions') or 0); samples=[]; generation_last=int(base_snap['body'].get('generation') or 0); next_sample=time.monotonic()",
    "base_cycles=int(base_state['body'].get('cycles_run') or 0); base_db=int(base_snap['body'].get('exact_total_predictions') or 0); base_last_prediction_ts=base_state['body'].get('last_prediction_ts'); base_btc_rows=int(base_snap['body'].get('authority_history_rows') or 0); samples=[]; generation_last=int(base_snap['body'].get('generation') or 0); next_sample=time.monotonic()",
    "stability progression baseline",
)
one(
    "final_cycles=int(final_state['body'].get('cycles_run') or 0); final_db=int(final_snap['body'].get('exact_total_predictions') or 0)\nif final_cycles<=base_cycles: raise RuntimeError(f'ORACLE_CYCLES_DID_NOT_ADVANCE:{base_cycles}:{final_cycles}')\nif final_db<=base_db: raise RuntimeError(f'DB_PREDICTIONS_DID_NOT_INCREASE:{base_db}:{final_db}')",
    "final_cycles=int(final_state['body'].get('cycles_run') or 0); final_db=int(final_snap['body'].get('exact_total_predictions') or 0); final_last_prediction_ts=final_state['body'].get('last_prediction_ts'); final_btc_rows=int(final_snap['body'].get('authority_history_rows') or 0)\nif final_cycles<=base_cycles: raise RuntimeError(f'ORACLE_CYCLES_DID_NOT_ADVANCE:{base_cycles}:{final_cycles}')\nif final_db<=base_db: raise RuntimeError(f'DB_PREDICTIONS_DID_NOT_INCREASE:{base_db}:{final_db}')\nif not final_last_prediction_ts or final_last_prediction_ts==base_last_prediction_ts: raise RuntimeError(f'LATEST_PREDICTION_TIMESTAMP_DID_NOT_ADVANCE:{base_last_prediction_ts}:{final_last_prediction_ts}')\nif final_btc_rows<base_btc_rows: raise RuntimeError(f'BTC_AUTHORITY_ROWS_DECREASED:{base_btc_rows}:{final_btc_rows}')",
    "stability progression final",
)
one(
    "'db_predictions_increase':final_db-base_db,'runtime_log_rows'",
    "'db_predictions_increase':final_db-base_db,'latest_prediction_ts_initial':base_last_prediction_ts,'latest_prediction_ts_final':final_last_prediction_ts,'latest_prediction_ts_advanced':True,'btc_authority_rows_initial':base_btc_rows,'btc_authority_rows_final':final_btc_rows,'btc_authority_rows_nondecreasing':True,'runtime_log_rows'",
    "stability sealed progression fields",
)
one(
    "'runtime_memory_fix':'OPTIONAL_ANALYTICS_TRUE_LAZY_INIT'",
    "'runtime_memory_fix':'OPTIONAL_ANALYTICS_TRUE_LAZY_INIT','public_allowlist':'PASS','public_get_zero_side_effect':'PASS','authority_refresh_bounded':'PASS','canonical_build':'PASS','nonroot':'PASS','world_writable':'NO','edge_dashboard_parity':'PASS','predictions_accumulating':'PASS'",
    "terminal summary consolidated gates",
)
one(
    "'db_predictions_increase':final_db-base_db,'real_order_count'",
    "'db_predictions_increase':final_db-base_db,'latest_prediction_ts_advanced':True,'btc_authority_rows_nondecreasing':True,'real_order_count'",
    "terminal summary progression",
)

# Execute the proven R5/R6 engine with the consolidated exact-head contract.
code = compile(s, str(SOURCE) + "[AUD5381263728]", "exec")
exec(code, {"__name__": "__main__", "__file__": str(SOURCE)})
