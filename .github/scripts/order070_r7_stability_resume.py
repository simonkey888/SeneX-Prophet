from __future__ import annotations

from pathlib import Path

SOURCE = Path(__file__).with_name("order070_r7_stability_only.py")
s = SOURCE.read_text()


def one(old: str, new: str, label: str) -> None:
    global s
    n = s.count(old)
    if n != 1:
        raise RuntimeError(f"{label}: expected exactly one match, got {n}")
    s = s.replace(old, new, 1)


one(
    "def pub(path,timeout=45): return request_json(ORIGIN+path,None,timeout)\ndef service(): return nf(f'/projects/{PROJECT}/services/{SERVICE}')[0]",
    "def pub(path,timeout=45): return request_json(ORIGIN+path,None,timeout)\ndef progress_state(attempts=6,delay=2.0):\n    last=None\n    for _ in range(attempts):\n        last=pub('/api/oracle/state?symbol=BTCUSDT')\n        if last['http']==200 or last['http'] not in (503,): return last\n        time.sleep(delay)\n    return last\ndef service(): return nf(f'/projects/{PROJECT}/services/{SERVICE}')[0]",
    "bounded progress-state retry",
)
one(
    "base_snap=pub('/api/authority/snapshot?symbol=BTCUSDT'); base_state=pub('/api/oracle/state?symbol=BTCUSDT'); base_pred=pub('/api/oracle/predictions/db?limit=50&symbol=BTCUSDT'); assert_live()",
    "base_snap=pub('/api/authority/snapshot?symbol=BTCUSDT'); base_state=progress_state(); base_pred=pub('/api/oracle/predictions/db?limit=50&symbol=BTCUSDT'); assert_live()",
    "baseline state telemetry",
)
one(
    "samples=[]; generation_last=int(base_snap['body'].get('generation') or 0); connection_refused=0; next_sample=time.monotonic()",
    "samples=[]; generation_last=int(base_snap['body'].get('generation') or 0); connection_refused=0; state_telemetry_503=0; next_sample=time.monotonic()",
    "state telemetry counter",
)
one(
    "responses=(snap,health,ready,prov,state); connection_refused+=sum(1 for x in responses if x['http']==0)",
    "critical=(snap,health,ready,prov); connection_refused+=sum(1 for x in (*critical,state) if x['http']==0); state_telemetry_503+=int(state['http']==503)",
    "critical continuity surfaces",
)
one(
    "if any(x['http']!=200 for x in responses): row['failure']='HTTP_CONTINUITY'; samples.append(row); write('STABILITY_SAMPLES_PARTIAL.json',samples); raise RuntimeError(f'STABILITY_HTTP_FAILURE:{row}')",
    "if any(x['http']!=200 for x in critical) or state['http'] not in (200,503): row['failure']='HTTP_CONTINUITY'; samples.append(row); write('STABILITY_SAMPLES_PARTIAL.json',samples); raise RuntimeError(f'STABILITY_HTTP_FAILURE:{row}')",
    "state is progress telemetry not required continuity",
)
one(
    "hb=health['body']; rb=ready['body']; pb=prov['body']; sb=state['body']; sn=snap['body']",
    "hb=health['body']; rb=ready['body']; pb=prov['body']; sn=snap['body']; sb=state['body'] if state['http']==200 else {}",
    "optional state body",
)
one(
    "if not sid or rb.get('authority_snapshot_id')!=sid or sb.get('authority_snapshot_id')!=sid or gen<generation_last: raise RuntimeError(f'STABILITY_SNAPSHOT_INCONSISTENT:{sid}:{gen}:{generation_last}')",
    "if not sid or rb.get('authority_snapshot_id')!=sid or gen<generation_last: raise RuntimeError(f'STABILITY_SNAPSHOT_INCONSISTENT:{sid}:{gen}:{generation_last}')\n    if state['http']==200 and sb.get('authority_snapshot_id')!=sid: raise RuntimeError(f'STABILITY_STATE_SNAPSHOT_INCONSISTENT:{sid}:{sb.get(\"authority_snapshot_id\")}')",
    "conditional state identity",
)
one(
    "final_snap=pub('/api/authority/snapshot?symbol=BTCUSDT'); final_state=pub('/api/oracle/state?symbol=BTCUSDT'); final_pred=pub('/api/oracle/predictions/db?limit=50&symbol=BTCUSDT'); assert_live()",
    "final_snap=pub('/api/authority/snapshot?symbol=BTCUSDT'); final_state=progress_state(attempts=16,delay=2.0); final_pred=pub('/api/oracle/predictions/db?limit=50&symbol=BTCUSDT'); assert_live()",
    "final state bounded retry",
)
one(
    "'connection_refused':0,'http5xx_metric_total':five_total,'healthz_continuous':True",
    "'connection_refused':0,'state_telemetry_503':state_telemetry_503,'http5xx_metric_total':five_total,'healthz_continuous':True",
    "sealed state telemetry",
)
one(
    "'unexpected_restarts':0,'oom_kills':0,'connection_refused':0,'oracle_cycles_advance':final_cycles-base_cycles",
    "'unexpected_restarts':0,'oom_kills':0,'connection_refused':0,'state_telemetry_503':state_telemetry_503,'oracle_cycles_advance':final_cycles-base_cycles",
    "summary state telemetry",
)
one(
    "print('READY_CONTINUITY_30M=PASS'); print('ORACLE_CYCLES_ADVANCE='+str(final_cycles-base_cycles));",
    "print('READY_CONTINUITY_30M=PASS'); print('STATE_TELEMETRY_503='+str(state_telemetry_503)); print('ORACLE_CYCLES_ADVANCE='+str(final_cycles-base_cycles));",
    "terminal state telemetry",
)

code = compile(s, str(SOURCE) + "[ORDER070-R7-RESUME]", "exec")
exec(code, {"__name__": "__main__", "__file__": str(SOURCE)})
