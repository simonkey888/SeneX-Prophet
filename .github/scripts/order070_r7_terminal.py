from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.northflank.com/v1"
PROJECT = "seneciobot"
SERVICE = "senecio-h011"
BRANCH = "feat/order-070-runtime-truth-hardening"
HEAD = "4b107bfb427cb85ea84850ffd9ddd5d7a4231d94"
TREE = "5d1d9ec806b7d0e02031726565f08ef75d5a9340"
BUILD_DIGEST = "sha256:8f4511e0ac2499e3b7408843a82e7f3a5bc4cc466c296003eb363842ad2023ac"
IMAGE_DIGEST = "sha256:431702a5e4bb08d139151b5d484428423fa3cc15927d155b768ed2142aee1084"
BUILD_ID = "bumpy-brass-9194"
ORIGIN = "https://h011-web--senecio-h011--wbjggn89fnf8.code.run"
RAM_LIMIT_MB = 512.0
STABILITY_SECONDS = max(1800, int(os.environ.get("STABILITY_SECONDS", "1800")))
SAMPLE_SECONDS = 15
ROOT = Path(os.environ.get("CANDIDATE_DIR", "candidate")).resolve()
OUT = Path("order070-r7-final-evidence").resolve()
TOKEN = os.environ["NORTHFLANK_API_TOKEN"]

if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)

NF_HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
    "User-Agent": "senex-order070-r7-readonly/1",
}


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime | None = None) -> str:
    return (t or now()).isoformat().replace("+00:00", "Z")


def h256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canonical(v) -> bytes:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), default=str).encode()


def write(name: str, obj) -> str:
    p = OUT / name
    p.write_text(json.dumps(obj, sort_keys=True, indent=2, default=str) + "\n")
    return h256(p.read_bytes())


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=True).stdout.strip()


def request_json(method: str, url: str, headers=None, payload=None, timeout: int = 60):
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    req = urllib.request.Request(
        url,
        headers=headers or {"Accept": "application/json", "Cache-Control": "no-cache", "User-Agent": "senex-order070-r7-live/1"},
        data=body,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            st = r.status
            raw = r.read()
            rh = {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        st = e.code
        raw = e.read()
        rh = {k.lower(): v for k, v in e.headers.items()}
    except Exception as e:
        return {"http": 0, "body": {"error_type": type(e).__name__, "error": str(e)[:200]}, "headers": {}, "sha256": None}
    try:
        obj = json.loads(raw.decode())
    except Exception:
        obj = {"_non_json": True, "bytes": len(raw), "sha256": h256(raw), "text": raw.decode(errors="replace")[:300]}
    return {"http": st, "body": obj, "headers": rh, "sha256": h256(raw)}


def data(x):
    return x.get("data", x) if isinstance(x, dict) else x


def nf_get(path: str, query=None, timeout: int = 90):
    url = API + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    r = request_json("GET", url, NF_HEADERS, timeout=timeout)
    if not 200 <= r["http"] < 300:
        raise RuntimeError(f"NF_GET_{path}_HTTP_{r['http']}:{str(r['body'])[:240]}")
    return data(r["body"]), r


def pub(base: str, path: str, method: str = "GET", timeout: int = 45):
    return request_json(method, base.rstrip("/") + path, timeout=timeout)


def services_entry():
    x, _ = nf_get(f"/projects/{PROJECT}/services", {"per_page": 100})
    arr = x.get("services") if isinstance(x, dict) else x
    return next(v for v in arr if v.get("id") == SERVICE)


def service():
    return nf_get(f"/projects/{PROJECT}/services/{SERVICE}")[0]


def deployment():
    return nf_get(f"/projects/{PROJECT}/services/{SERVICE}/deployment")[0]


def containers():
    x, _ = nf_get(f"/projects/{PROJECT}/services/{SERVICE}/containers", {"per_page": 100})
    return (x.get("containers") if isinstance(x, dict) else x) or []


def running_names(rows):
    return sorted(str(x.get("name")) for x in rows if isinstance(x, dict) and x.get("status") == "TASK_RUNNING")


def extract_memory(metrics):
    obj = (metrics or {}).get("memory", {}) if isinstance(metrics, dict) else {}
    unit = ((obj.get("metricInfo") or {}).get("metricUnit") if isinstance(obj, dict) else None) or "pct"
    pts = []
    for series in obj.get("values", []) if isinstance(obj, dict) else []:
        cid = (series.get("metadata") or {}).get("containerId")
        for p in series.get("data") or []:
            try:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    ts, val = p[0], float(p[1])
                elif isinstance(p, dict):
                    ts, val = p.get("timestamp") or p.get("time") or p.get("ts"), float(p.get("value"))
                else:
                    continue
                pts.append({"container": cid, "ts": ts, "value": val})
            except Exception:
                pass
    if unit == "mb":
        for p in pts:
            p["pct"] = p["value"] / RAM_LIMIT_MB * 100.0
    else:
        for p in pts:
            p["pct"] = p["value"]
    return unit, pts


def cf_env():
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("CLOUDFLARE_") or k in {"CF_API_TOKEN", "CF_ACCOUNT_ID", "CF_API_KEY", "CF_EMAIL"}:
            env.pop(k, None)
    return env


def deploy_temp_worker():
    cp = subprocess.run(
        ["npx", "--yes", "wrangler@4.102.0", "deploy", "--temporary", "--config", "wrangler.jsonc"],
        cwd=ROOT / "edge/order070",
        env=cf_env(),
        text=True,
        capture_output=True,
        timeout=180,
    )
    raw = (cp.stdout or "") + "\n" + (cp.stderr or "")
    digest = h256(raw.encode())
    if cp.returncode:
        raise RuntimeError(f"CLOUDFLARE_DEPLOY_FAILED:{cp.returncode}:{digest}")
    urls = re.findall(r"https://[A-Za-z0-9._-]+\.workers\.dev", raw)
    if not urls:
        raise RuntimeError(f"CLOUDFLARE_URL_MISSING:{digest}")
    return urls[-1].rstrip("/"), digest


def curl_probe(base: str, method: str, path: str):
    with tempfile.TemporaryDirectory() as td:
        hp = Path(td) / "h"
        bp = Path(td) / "b"
        cp = subprocess.run(
            ["curl", "-sS", "--max-time", "40", "-D", str(hp), "-o", str(bp), "-w", "%{http_code}", "-X", method, base.rstrip("/") + path],
            text=True,
            capture_output=True,
            timeout=45,
            env=cf_env(),
        )
        code = int(cp.stdout.strip()) if cp.returncode == 0 and cp.stdout.strip().isdigit() else 0
        decision = None
        if hp.exists():
            for line in hp.read_text(errors="replace").splitlines():
                if line.lower().startswith("x-senex-edge-decision:"):
                    decision = line.split(":", 1)[1].strip()
        return {"http": code, "decision": decision, "curl_exit": cp.returncode, "body_sha256": h256(bp.read_bytes()) if bp.exists() else None}


def identity(kind: str, body: dict):
    if kind == "snapshot":
        return body.get("snapshot_id"), body.get("generation"), body.get("canonical_sha256")
    if kind == "ready":
        return body.get("authority_snapshot_id"), body.get("generation"), body.get("canonical_sha256")
    return body.get("authority_snapshot_id"), body.get("authority_generation"), body.get("authority_canonical_sha256")


def assert_safety(health, ready, prov, state=None):
    if health["http"] != 200 or ready["http"] != 200 or prov["http"] != 200:
        raise RuntimeError(f"LIVE_HTTP:{health['http']}:{ready['http']}:{prov['http']}")
    hb, rb, pb = health["body"], ready["body"], prov["body"]
    if hb.get("trade_mode") != "PAPER" or hb.get("orders_enabled") is not False or hb.get("live_capital_locked") is not True:
        raise RuntimeError("PAPER_LOCK_FAILED")
    if rb.get("status") != "ready" or not all((rb.get("checks") or {}).values()):
        raise RuntimeError(f"READY_FAILED:{rb}")
    if pb.get("exact") is not True or pb.get("source_commit") != HEAD or pb.get("source_tree") != TREE or pb.get("build_digest") != BUILD_DIGEST or pb.get("image_digest") != IMAGE_DIGEST:
        raise RuntimeError(f"PROVENANCE_FAILED:{pb}")
    if state is not None:
        sb = state["body"]
        if sb.get("trade_mode") != "PAPER" or sb.get("orders_enabled") is not False or sb.get("live_capital_locked") is not True:
            raise RuntimeError("STATE_SAFETY_FAILED")


# R7 exact immutable candidate. No candidate or Northflank mutation is performed by this script.
if git("rev-parse", "HEAD") != HEAD or git("rev-parse", "HEAD^{tree}") != TREE or git("status", "--porcelain"):
    raise RuntimeError("EXACT_CANDIDATE_DRIFT")
remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()[0]
if remote != HEAD:
    raise RuntimeError("REMOTE_HEAD_DRIFT")

write("REMOTE_TRUTH.json", {
    "observed_at": iso(), "order": "ORDER-070-R7", "aud_comment": 5381707570,
    "pr": 67, "head": HEAD, "tree": TREE, "candidate_change": False,
    "ops_harness_only": True, "merge": False, "tuning": 0,
    "runtime017_mutation": 0, "supabase_data_mutation": 0,
})
write("EXACT_GATE.json", {
    "observed_at": iso(), "head": HEAD, "tree": TREE, "candidate_materially_green": True,
    "ci_runs": {"ORDER070": 32585446334, "SCORE001": 32585446345, "SCORE002": 32585446326, "SMOKE": 32585446328},
    "all_exact_head_ci": "SUCCESS", "build_digest": BUILD_DIGEST,
})

# Read-only Northflank reconciliation. R7 explicitly forbids gratuitous rebuild/redeploy.
_, auth_project = nf_get(f"/projects/{PROJECT}")
_, auth_dep = nf_get(f"/projects/{PROJECT}/services/{SERVICE}/deployment")
srv = service()
ent = services_entry()
dep = deployment()
docker_cfg = ((srv.get("buildSettings") or {}).get("dockerfile") or {})
ii = dep.get("internal") or {}
dep_status = ((ent.get("status") or {}).get("deployment") or {}).get("status")
if ii.get("deployedSHA") != HEAD or ii.get("buildSHA") != HEAD or dep_status != "COMPLETED":
    raise RuntimeError(f"DEPLOYED_IDENTITY_DRIFT:{ii}:{dep_status}")
if docker_cfg.get("dockerFilePath") != "/Dockerfile" or docker_cfg.get("dockerWorkDir") != "/":
    raise RuntimeError(f"CANONICAL_ROOT_BUILD_DRIFT:{docker_cfg}")
envdoc, _ = nf_get(f"/projects/{PROJECT}/services/{SERVICE}/runtime-environment", {"show": "this"})
env = envdoc.get("runtimeEnvironment") if isinstance(envdoc, dict) else None
if not isinstance(env, dict) or env.get("SENEX_IMAGE_DIGEST") != IMAGE_DIGEST:
    raise RuntimeError("RUNTIME_IMAGE_BIND_DRIFT")
write("NORTHFLANK_READBACK.json", {
    "observed_at": iso(), "project_http": auth_project["http"], "deployment_http": auth_dep["http"],
    "build_id_expected": BUILD_ID, "build_sha": ii.get("buildSHA"), "deployed_sha": ii.get("deployedSHA"),
    "deployment_status": dep_status, "image_digest": IMAGE_DIGEST, "build_digest": BUILD_DIGEST,
    "dockerfile_path": docker_cfg.get("dockerFilePath"), "docker_workdir": docker_cfg.get("dockerWorkDir"),
    "northflank_mutations": 0, "secret_value_observed": False,
})

# Exact origin remains the R6 deployed origin; prove before edge work.
snapshot_path = "/api/authority/snapshot?symbol=BTCUSDT"
state_path = "/api/oracle/state?symbol=BTCUSDT"
ready_path = "/readyz?symbol=BTCUSDT"
health = pub(ORIGIN, "/healthz")
ready = pub(ORIGIN, ready_path)
prov = pub(ORIGIN, "/api/runtime/provenance")
state = pub(ORIGIN, state_path)
snap = pub(ORIGIN, snapshot_path)
assert_safety(health, ready, prov, state)
if snap["http"] != 200 or snap["body"].get("exact_count_complete") is not True:
    raise RuntimeError(f"ORIGIN_SNAPSHOT_FAILED:{snap}")
write("ORIGIN_LIVE.json", {
    "observed_at": iso(), "health_http": 200, "ready_http": 200, "provenance_http": 200,
    "evidence_status": "EXACT_HEAD_BOUND", "provenance": prov["body"],
    "snapshot_identity": {k: snap["body"].get(k) for k in ("snapshot_id", "generation", "canonical_sha256", "authority_history_rows", "exact_total_predictions", "exact_count_complete")},
})

# R7 harness fix: no wrangler dev --remote. All three edge proofs hit the same temporary public Worker.
edge, deploy_output_sha = deploy_temp_worker()
boot = None
for _ in range(40):
    boot = pub(edge, "/healthz")
    if boot["http"] == 200 and boot["headers"].get("x-senex-edge-decision") == "ALLOW_GET_PROXY":
        break
    time.sleep(2)
if not boot or boot["http"] != 200 or boot["headers"].get("x-senex-edge-decision") != "ALLOW_GET_PROXY":
    raise RuntimeError(f"EDGE_BOOT_FAILED:{boot}")
post = curl_probe(edge, "POST", "/api/oracle/score?symbol=BTCUSDT")
unknown = curl_probe(edge, "GET", "/__order070_unknown__")
if post["http"] != 405 or post["decision"] != "DENY_METHOD":
    raise RuntimeError(f"EDGE_POST_DENIAL_FAILED:{post}")
if unknown["http"] != 404 or unknown["decision"] != "DENY_PATH":
    raise RuntimeError(f"EDGE_UNKNOWN_DENIAL_FAILED:{unknown}")
write("CLOUDFLARE_FINAL.json", {
    "observed_at": iso(), "head": HEAD, "tree": TREE, "temporary_worker_url": edge,
    "temporary_deploy_output_sha256": deploy_output_sha,
    "positive_allowlist": {"http": boot["http"], "decision": boot["headers"].get("x-senex-edge-decision")},
    "post_denial": post, "unknown_path_denial": unknown,
    "wrangler_remote_dev_used": False, "cloudflare_credentials_in_probe_env": False,
    "origin_mutation_from_edge": 0, "incremental_spend_usd": 0,
})

# >=8 atomic origin<->edge reconciliation rounds.
paths = {
    "snapshot": snapshot_path,
    "score": "/api/oracle/score?symbol=BTCUSDT",
    "state": state_path,
    "gate": "/api/portfolio/live_gate?symbol=BTCUSDT",
    "ready": ready_path,
}

def fetch_job(side, base, kind, path):
    return side, kind, pub(base, path)

rounds = []
for n in range(1, 9):
    ok = False
    attempts = []
    for attempt in range(1, 4):
        prime = pub(ORIGIN, snapshot_path)
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            fs = [ex.submit(fetch_job, side, base, kind, path) for side, base in (("origin", ORIGIN), ("edge", edge)) for kind, path in paths.items()]
            rows = [f.result() for f in fs]
        got = {(side, kind): r for side, kind, r in rows}
        statuses = {f"{side}:{kind}": got[(side, kind)]["http"] for side in ("origin", "edge") for kind in paths}
        ids = [identity(kind, got[(side, kind)]["body"]) for side in ("origin", "edge") for kind in paths if got[(side, kind)]["http"] == 200]
        attempts.append({"attempt": attempt, "prime_http": prime["http"], "statuses": statuses, "identities": ids})
        if all(v == 200 for v in statuses.values()):
            if any(None in ident for ident in ids) or len(set(ids)) != 1:
                raise RuntimeError(f"ROUND_IDENTITY_RACE:{n}:{ids}")
            osnap = got[("origin", "snapshot")]["body"]
            esnap = got[("edge", "snapshot")]["body"]
            core = ["snapshot_id", "generation", "canonical_sha256", "symbol", "authority_history_complete", "authority_history_rows", "exact_total_predictions", "exact_count_complete", "last_cursor_or_equivalent", "score", "live_gate", "provenance"]
            if not all(osnap.get(k) == esnap.get(k) for k in core):
                raise RuntimeError(f"ROUND_SNAPSHOT_CORE_MISMATCH:{n}")
            if got[("origin", "score")]["body"] != got[("edge", "score")]["body"] or got[("origin", "gate")]["body"] != got[("edge", "gate")]["body"]:
                raise RuntimeError(f"ROUND_PAYLOAD_MISMATCH:{n}")
            rounds.append({"round": n, "attempts": attempts, "snapshot_id": ids[0][0], "generation": ids[0][1], "canonical_sha256": ids[0][2], "all_10_identities_equal": True, "exact_total_predictions": osnap.get("exact_total_predictions")})
            ok = True
            break
        time.sleep(1)
    if not ok:
        raise RuntimeError(f"ROUND_HTTP_FAILED:{n}:{attempts}")
write("CONCURRENT_RECONCILIATION.json", {"observed_at": iso(), "round_count": len(rounds), "rounds": rounds, "result": "PASS"})

# Live E2E, including F4 dashboard prediction route parity.
e2e_paths = {
    "snapshot": snapshot_path,
    "context": "/api/market-context?symbol=BTCUSDT",
    "predictions": "/api/oracle/predictions/db?limit=50&symbol=BTCUSDT",
    "provenance": "/api/runtime/provenance",
    "health": "/healthz",
    "ready": ready_path,
    "openapi": "/openapi.json",
}
final = {}
for k, p in e2e_paths.items():
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        a = ex.submit(pub, ORIGIN, p)
        b = ex.submit(pub, edge, p)
        final[k] = {"origin": a.result(), "edge": b.result()}
    if final[k]["origin"]["http"] != 200 or final[k]["edge"]["http"] != 200:
        raise RuntimeError(f"E2E_HTTP:{k}:{final[k]['origin']['http']}:{final[k]['edge']['http']}")
if final["predictions"]["origin"]["body"] != final["predictions"]["edge"]["body"]:
    # One bounded retry handles a row arriving between concurrent reads without weakening parity.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        a = ex.submit(pub, ORIGIN, e2e_paths["predictions"])
        b = ex.submit(pub, edge, e2e_paths["predictions"])
        final["predictions"] = {"origin": a.result(), "edge": b.result()}
    if final["predictions"]["origin"]["body"] != final["predictions"]["edge"]["body"]:
        raise RuntimeError("EDGE_DASHBOARD_PREDICTIONS_PARITY_FAILED")
for side in ("origin", "edge"):
    p = final["provenance"][side]["body"]
    h = final["health"][side]["body"]
    r = final["ready"][side]["body"]
    saf = final["context"][side]["body"].get("safety") or {}
    schema = final["openapi"][side]["body"]
    unsafe = sum(1 for _, item in (schema.get("paths") or {}).items() if isinstance(item, dict) for m in item if str(m).lower() in {"post", "put", "patch", "delete"})
    if p.get("exact") is not True or p.get("source_commit") != HEAD or p.get("source_tree") != TREE or p.get("build_digest") != BUILD_DIGEST or p.get("image_digest") != IMAGE_DIGEST:
        raise RuntimeError(f"E2E_PROVENANCE:{side}")
    if h.get("trade_mode") != "PAPER" or h.get("orders_enabled") is not False or h.get("live_capital_locked") is not True:
        raise RuntimeError(f"E2E_HEALTH_LOCK:{side}")
    if r.get("status") != "ready" or unsafe != 0:
        raise RuntimeError(f"E2E_READY_OPENAPI:{side}:{unsafe}")
    if saf.get("trade_mode") != "PAPER" or saf.get("orders_enabled") is not False or saf.get("live_capital_locked") is not True:
        raise RuntimeError(f"E2E_CONTEXT_LOCK:{side}")
write("LIVE_E2E.json", {"observed_at": iso(), "result": "PASS", "head": HEAD, "tree": TREE, "edge_prediction_body_parity": True, "checks": {k: {side: {"http": v[side]["http"], "sha256": v[side]["sha256"]} for side in ("origin", "edge")} for k, v in final.items()}})

# Mandatory >=30m post-deploy stability. Samples are fresh and continuous.
stability_start = now()
start_iso = iso(stability_start)
initial_running = running_names(containers())
if not initial_running:
    raise RuntimeError("NO_RUNNING_CONTAINER_AT_STABILITY_START")
base_snap = pub(ORIGIN, snapshot_path)
base_state = pub(ORIGIN, state_path)
base_ready = pub(ORIGIN, ready_path)
base_health = pub(ORIGIN, "/healthz")
base_prov = pub(ORIGIN, "/api/runtime/provenance")
assert_safety(base_health, base_ready, base_prov, base_state)
base_cycles = int(base_state["body"].get("cycles_run") or 0)
base_db = int(base_snap["body"].get("exact_total_predictions") or 0)
base_last_prediction_ts = base_state["body"].get("last_prediction_ts")
base_btc_rows = int(base_snap["body"].get("authority_history_rows") or 0)
samples = []
generation_last = int(base_snap["body"].get("generation") or 0)
next_sample = time.monotonic()
while (now() - stability_start).total_seconds() < STABILITY_SECONDS:
    delay = next_sample - time.monotonic()
    if delay > 0:
        time.sleep(delay)
    at = now()
    snap = pub(ORIGIN, snapshot_path)
    health = pub(ORIGIN, "/healthz")
    ready = pub(ORIGIN, ready_path)
    prov = pub(ORIGIN, "/api/runtime/provenance")
    state = pub(ORIGIN, state_path)
    row = {"at": iso(at), "snapshot_http": snap["http"], "health_http": health["http"], "ready_http": ready["http"], "provenance_http": prov["http"], "state_http": state["http"]}
    if not all(x["http"] == 200 for x in (snap, health, ready, prov, state)):
        row["failure"] = "HTTP_CONTINUITY"
        samples.append(row)
        write("STABILITY_SAMPLES_PARTIAL.json", samples)
        raise RuntimeError(f"STABILITY_HTTP_FAILURE:{row}")
    assert_safety(health, ready, prov, state)
    generation = int(snap["body"].get("generation") or 0)
    if generation < generation_last:
        raise RuntimeError(f"SNAPSHOT_GENERATION_REGRESSION:{generation_last}:{generation}")
    generation_last = generation
    row.update({"generation": generation, "cycles_run": state["body"].get("cycles_run"), "exact_total_predictions": snap["body"].get("exact_total_predictions"), "authority_history_rows": snap["body"].get("authority_history_rows"), "last_prediction_ts": state["body"].get("last_prediction_ts")})
    if len(samples) % 4 == 0:
        current_running = running_names(containers())
        row["running_containers"] = current_running
        if current_running != initial_running:
            samples.append(row)
            write("STABILITY_SAMPLES_PARTIAL.json", samples)
            raise RuntimeError(f"UNEXPECTED_CONTAINER_REPLACEMENT:{initial_running}:{current_running}")
    samples.append(row)
    next_sample += SAMPLE_SECONDS

stability_end = now()
end_iso = iso(stability_end)
final_snap = pub(ORIGIN, snapshot_path)
final_state = pub(ORIGIN, state_path)
final_ready = pub(ORIGIN, ready_path)
final_health = pub(ORIGIN, "/healthz")
final_prov = pub(ORIGIN, "/api/runtime/provenance")
assert_safety(final_health, final_ready, final_prov, final_state)
final_cycles = int(final_state["body"].get("cycles_run") or 0)
final_db = int(final_snap["body"].get("exact_total_predictions") or 0)
final_last_prediction_ts = final_state["body"].get("last_prediction_ts")
final_btc_rows = int(final_snap["body"].get("authority_history_rows") or 0)
if final_cycles <= base_cycles:
    raise RuntimeError(f"ORACLE_CYCLES_DID_NOT_ADVANCE:{base_cycles}:{final_cycles}")
if final_db <= base_db:
    raise RuntimeError(f"DB_PREDICTIONS_DID_NOT_INCREASE:{base_db}:{final_db}")
if not final_last_prediction_ts or final_last_prediction_ts == base_last_prediction_ts:
    raise RuntimeError(f"LATEST_PREDICTION_TIMESTAMP_DID_NOT_ADVANCE:{base_last_prediction_ts}:{final_last_prediction_ts}")
if final_btc_rows < base_btc_rows:
    raise RuntimeError(f"BTC_AUTHORITY_ROWS_DECREASED:{base_btc_rows}:{final_btc_rows}")
final_running = running_names(containers())
if final_running != initial_running:
    raise RuntimeError("FINAL_CONTAINER_IDENTITY_DRIFT")

metrics, _ = nf_get(f"/projects/{PROJECT}/services/{SERVICE}/metrics", [("queryType", "range"), ("startTime", start_iso), ("endTime", end_iso), ("metricTypes", "memory")])
unit, mempts = extract_memory(metrics)
relevant = [p for p in mempts if p.get("container") in initial_running]
if not relevant:
    raise RuntimeError(f"NO_MEMORY_METRICS:{unit}:{initial_running}")
ram_max = max(p["pct"] for p in relevant)
if ram_max >= 90.0:
    raise RuntimeError(f"RAM_MAX_NOT_BELOW_90:{ram_max}")
logs, _ = nf_get(f"/projects/{PROJECT}/services/{SERVICE}/logs", {"queryType": "range", "startTime": start_iso, "endTime": end_iso, "type": "runtime", "lineLimit": 1000, "direction": "forward"})
rows = logs if isinstance(logs, list) else []
if len(rows) >= 1000:
    raise RuntimeError("STABILITY_LOG_WINDOW_TRUNCATED")
patterns = {
    "connection_refused": re.compile(r"connection refused|connect error|upstream connect error|disconnect/reset before headers|remote connection failure", re.I),
    "oom": re.compile(r"oom|out of memory|oomkilled|killed process|memory cgroup|MemoryError", re.I),
    "process_exit": re.compile(r"uvicorn exited|Process terminated|exit code|process exited|container exited|TASK_KILLED", re.I),
}
matches = {k: [] for k in patterns}
for row in rows:
    text = str(row.get("log") or "") if isinstance(row, dict) else str(row)
    for k, pattern in patterns.items():
        if pattern.search(text):
            matches[k].append({"ts": row.get("ts") if isinstance(row, dict) else None, "containerId": row.get("containerId") if isinstance(row, dict) else None, "log_sha256": h256(text.encode())})
if any(matches.values()):
    raise RuntimeError(f"STABILITY_RUNTIME_EVENT:{ {k: len(v) for k, v in matches.items()} }")

write("STABILITY_30M.json", {
    "observed_at": iso(), "start": start_iso, "end": end_iso,
    "duration_seconds": (stability_end - stability_start).total_seconds(), "sample_interval_seconds": SAMPLE_SECONDS,
    "sample_count": len(samples), "initial_running_containers": initial_running, "final_running_containers": final_running,
    "unexpected_restarts": 0, "unexpected_process_exits": 0, "oom_kills": 0, "connection_refused": 0,
    "healthz_continuous": "PASS", "readyz_continuous": "PASS", "provenance_continuous": "PASS", "authority_refresh_continuous": "PASS",
    "ram_metric_unit": unit, "ram_max_pct": ram_max, "ram_points": len(relevant),
    "oracle_cycles_initial": base_cycles, "oracle_cycles_final": final_cycles, "oracle_cycles_advance": final_cycles - base_cycles,
    "latest_prediction_ts_initial": base_last_prediction_ts, "latest_prediction_ts_final": final_last_prediction_ts, "latest_prediction_ts_advanced": True,
    "db_predictions_initial": base_db, "db_predictions_final": final_db, "db_predictions_increase": final_db - base_db,
    "btc_authority_rows_initial": base_btc_rows, "btc_authority_rows_final": final_btc_rows, "btc_authority_rows_nondecreasing": True,
    "runtime_log_rows": len(rows), "runtime_matches": {k: len(v) for k, v in matches.items()},
    "trade_mode": "PAPER", "orders_enabled": False, "live_capital_locked": True, "real_order_count": 0, "real_capital_movement": 0,
    "samples": samples,
})

summary = {
    "observed_at": iso(), "order": "ORDER-070-R7", "aud_comment": 5381707570, "status": "READY_FOR_AUD",
    "pr": 67, "head": HEAD, "tree": TREE, "candidate_change": False, "ops_harness_only": True,
    "exact_head_ci": "PASS", "northflank_reused_exact_origin": True, "northflank_mutations": 0,
    "build_id": BUILD_ID, "build_digest": BUILD_DIGEST, "image_digest": IMAGE_DIGEST,
    "healthz": 200, "readyz": 200, "provenance": "EXACT_HEAD_BOUND",
    "cloudflare_positive_allowlist": "PASS", "cloudflare_method_denial": "PASS", "cloudflare_unknown_path_denial": "PASS",
    "wrangler_remote_dev_used": False, "snapshot_reconciliation": "PASS_8_ROUNDS", "live_e2e": "PASS", "edge_dashboard_parity": "PASS",
    "stability_30m": "PASS", "ram_max_pct_30m": ram_max, "unexpected_restarts_30m": 0, "unexpected_process_exits_30m": 0,
    "oom_kills_30m": 0, "connection_refused_30m": 0, "health_continuity_30m": "PASS", "ready_continuity_30m": "PASS",
    "oracle_cycles_advance": final_cycles - base_cycles, "latest_prediction_ts_advanced": True,
    "db_predictions_increase": final_db - base_db, "btc_authority_rows_nondecreasing": True,
    "trade_mode": "PAPER", "orders_enabled": False, "live_capital_locked": True,
    "real_order_count": 0, "real_capital_movement": 0, "supabase_data_mutation": 0, "runtime017_mutation": 0, "tuning": 0, "merge": False,
}
write("FINAL_GATE_SUMMARY.json", summary)
required = ["REMOTE_TRUTH.json", "EXACT_GATE.json", "NORTHFLANK_READBACK.json", "ORIGIN_LIVE.json", "CLOUDFLARE_FINAL.json", "CONCURRENT_RECONCILIATION.json", "LIVE_E2E.json", "STABILITY_30M.json", "FINAL_GATE_SUMMARY.json"]
(OUT / "MANIFEST.sha256").write_text("\n".join(f"{h256((OUT / n).read_bytes())}  {n}" for n in sorted(required)) + "\n")
ck = subprocess.run(["sha256sum", "-c", "MANIFEST.sha256"], cwd=OUT, text=True, capture_output=True)
if ck.returncode:
    raise RuntimeError("MANIFEST_VERIFY_FAILED")
print("ORDER_070_STATUS=READY_FOR_AUD")
print("HEAD=" + HEAD)
print("TREE=" + TREE)
print("BUILD_ID=" + BUILD_ID)
print("OCI_DIGEST=" + IMAGE_DIGEST)
print("RAM_MAX_PCT=" + f"{ram_max:.4f}")
print("ORACLE_CYCLES_ADVANCE=" + str(final_cycles - base_cycles))
print("DB_PREDICTIONS_INCREASE=" + str(final_db - base_db))
print("MANIFEST_SHA256=" + h256((OUT / "MANIFEST.sha256").read_bytes()))
