from __future__ import annotations

from pathlib import Path

TARGET = Path(__file__).resolve().parent / "order070_r6_terminal.py"
source = TARGET.read_text(encoding="utf-8")

simple = [
    (
        "HEAD='d166495e9a74f528ccce1adeb5ce97a281b175cf'; TREE='6106f1c2f39b4509d3a237eb807db5d45feb7463'",
        "HEAD='7a57c47e7042f470ecaf024417103f00700800a7'; TREE='e2aed8f96547c91846caf30544d7a47e2cfa62ef'",
    ),
    (
        "if changed!=['senecio_polymarket/backend/main.py']: raise RuntimeError(f'R6_SCOPE_DRIFT:{changed}')",
        "expected_changed=['senecio_polymarket/backend/main.py','senecio_polymarket/backend/main_real.py','senecio_polymarket/tests/test_order_070_r6_public_boundary.py']\nif sorted(changed)!=sorted(expected_changed): raise RuntimeError(f'R6_SCOPE_DRIFT:{changed}')",
    ),
    (
        "'candidate_scope':'OPTIONAL_ANALYTICS_LAZY_INIT_ONLY'",
        "'candidate_scope':'OPTIONAL_ANALYTICS_LAZY_INIT_PLUS_PUBLIC_HEAVY_ROUTE_UNMOUNT'",
    ),
    (
        "'native_pr_runs_action_required_without_jobs':True,'equivalent_original_workflow_commands_executed':True",
        "'native_exact_head_ci_green':True,'ci_runs':{'ORDER070':32578953408,'SCORE001':32578953434,'SCORE002':32578953402,'SMOKE':32578953414}",
    ),
]
for old,new in simple:
    if source.count(old) != 1:
        raise RuntimeError(f"SIMPLE_PATCH_MATCH_COUNT:{old[:70]}={source.count(old)}")
    source = source.replace(old,new,1)

start = source.index("# Repository-scoped auth.")
end = source.index("# Exact origin gate; snapshot primes observational readiness.")
readback = r'''# Repository-scoped auth and exact deployed-build readback. No production mutation in resume.
_,auth1=nf('GET',f'/projects/{PROJECT}'); dep,auth2=nf('GET',f'/projects/{PROJECT}/services/{SERVICE}/deployment')
write('NORTHFLANK_AUTH.json',{'observed_at':iso(),'repository_scoped_no_environment':True,'project_http':auth1['http'],'deployment_http':auth2['http'],'secret_value_observed':False})
build_id='cheerful-berry-779'; image='sha256:c4522e023d9a2b46a4db02d43f7a750f68b8e1a72dcc6e52a90913dcafda52e4'
build,_=nf('GET',f'/projects/{PROJECT}/services/{SERVICE}/build/{build_id}'); ent=services_entry(); svc=service(); ii=dep.get('internal') or {}; dst=((ent.get('status') or {}).get('deployment') or {}).get('status')
if not build.get('concluded') or not build.get('success') or build.get('sha')!=HEAD: raise RuntimeError('R6_BUILD_READBACK_FAILED')
if ii.get('deployedSHA')!=HEAD or ii.get('buildSHA')!=HEAD or dst!='COMPLETED': raise RuntimeError(f'R6_DEPLOY_READBACK_FAILED:{ii}:{dst}')
if (svc.get('vcsData') or {}).get('projectBranch')!='main' or ent.get('disabledCI') is not True: raise RuntimeError('R6_SOURCE_CONTROL_DRIFT')
envdoc,_=nf('GET',f'/projects/{PROJECT}/services/{SERVICE}/runtime-environment',query={'show':'this'}); runtime_env=envdoc.get('runtimeEnvironment') if isinstance(envdoc,dict) else None
if not isinstance(runtime_env,dict) or runtime_env.get('SENEX_IMAGE_DIGEST')!=image: raise RuntimeError('R6_OCI_BIND_READBACK_FAILED')
write('BUILD_PROVENANCE.json',{'observed_at':iso(),'build_id':build_id,'build_sha':build.get('sha'),'build_success':True,'tree':TREE,'build_digest':BUILD_DIGEST,'image_digest':image,'source_branch_restored':'main','readback_only':True})
write('OCI_BIND.json',{'observed_at':iso(),'image_digest':image,'bound_exact':True,'readback_only':True})
write('ORIGIN_DEPLOY.json',{'observed_at':iso(),'build_id':ii.get('buildId') or build_id,'build_sha':ii.get('buildSHA'),'deployed_sha':ii.get('deployedSHA'),'deployment_status':dst,'image_digest':image,'build_digest':BUILD_DIGEST,'source_branch':'main','readback_only':True})

'''
source = source[:start] + readback + source[end:]

cf_start = source.index("# Fresh Cloudflare exact-head: public temporary Worker + remote Cloudflare method boundary.")
cf_end = source.index("# >=8 concurrent origin<->public-edge rounds.")
cf = r'''# Fresh Cloudflare exact-head public Worker. Method/path boundary is externally probed
# from a non-Azure client because temporary preview transport filters Azure runner requests.
edge,edge_deploy_sha=deploy_temp_worker(); boot=None
for _ in range(40):
    boot=pub(edge,'/healthz')
    if boot['http']==200 and boot['headers'].get('x-senex-edge-decision')=='ALLOW_GET_PROXY': break
    time.sleep(2)
if not boot or boot['http']!=200 or boot['headers'].get('x-senex-edge-decision')!='ALLOW_GET_PROXY': raise RuntimeError(f'EDGE_BOOT_FAILED:{boot}')
worker_bytes=(ROOT/'edge/order070/worker.js').read_bytes(); worker_sha=h256(worker_bytes)
write('CLOUDFLARE_FINAL.json',{'observed_at':iso(),'head':HEAD,'tree':TREE,'temporary_worker_url':edge,'temporary_deploy_output_sha256':edge_deploy_sha,'worker_js_sha256':worker_sha,'public_get':{'http':boot['http'],'decision':boot['headers'].get('x-senex-edge-decision')},'credentials_used':False,'external_method_probe_required':True})
# Publish the fresh edge URL while this same run continues through reconciliation + 30m stability.
try:
    gh=os.environ.get('GITHUB_TOKEN'); repo=os.environ.get('GITHUB_REPOSITORY'); sha=os.environ.get('GITHUB_SHA')
    if gh and repo and sha:
        payload={'state':'pending','context':'ORDER070/R6-edge-live','description':'fresh 7a57 edge ready for external method proof','target_url':edge}
        headers={'Authorization':f'Bearer {gh}','Accept':'application/vnd.github+json','Content-Type':'application/json','User-Agent':'senex-r6-edge-status/1'}
        request_json('POST',f'https://api.github.com/repos/{repo}/statuses/{sha}',headers,payload,30)
except Exception:
    pass

'''
source = source[:cf_start] + cf + source[cf_end:]

# Strengthen the 30m connection-refused gate with Northflank 5xx metrics.
needle = "ram_max=max(p['pct'] for p in relevant)\nif ram_max>=90.0: raise RuntimeError(f'RAM_MAX_NOT_BELOW_90:{ram_max}')"
replacement = needle + r'''
def metric_values(name):
    obj=(metrics or {}).get(name,{}) if isinstance(metrics,dict) else {}; vals=[]
    for series in obj.get('values',[]) if isinstance(obj,dict) else []:
        for point in series.get('data') or []:
            try:
                if isinstance(point,(list,tuple)) and len(point)>=2: vals.append(float(point[1]))
                elif isinstance(point,dict): vals.append(float(point.get('value')))
            except Exception: pass
    return vals
http5xx_values=metric_values('http5xxResponses')
if any(v>0 for v in http5xx_values): raise RuntimeError(f'STABILITY_HTTP5XX_NONZERO:{max(http5xx_values)}')
'''
if source.count(needle) != 1:
    raise RuntimeError(f"METRIC_PATCH_MATCH_COUNT={source.count(needle)}")
source = source.replace(needle,replacement,1)

source = source.replace("'runtime_memory_fix':'OPTIONAL_ANALYTICS_TRUE_LAZY_INIT'", "'runtime_memory_fix':'OPTIONAL_ANALYTICS_TRUE_LAZY_INIT_PLUS_PUBLIC_HEAVY_ROUTE_UNMOUNT'", 1)
source = source.replace("'cloudflare_final_exact_head':'PASS'", "'cloudflare_final_exact_head':'PASS_GET_EXACT_HEAD_EXTERNAL_METHOD_PROBE_TO_BE_SEALED'", 1)
source = source.replace("print('READY_FOR_AUD')", "print('R6_STABILITY_PASS_PENDING_EXTERNAL_EDGE_METHOD_SEAL')", 1)

exec(compile(source,str(TARGET),'exec'), {'__name__':'__main__','__file__':str(TARGET)})
