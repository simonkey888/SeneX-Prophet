#!/usr/bin/env python3
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("NF_API", "https://api.northflank.com/v1").rstrip("/")
TOKEN = os.environ["NORTHFLANK_SENEX_MAINTENANCE_TOKEN"]
PROJECT = os.environ.get("PROJECT_ID", "seneciobot")
SERVICE = os.environ.get("SERVICE_ID", "senecio-h011")
SOURCE_VOLUME = os.environ.get("SOURCE_VOLUME_ID", "h011-results-vol")
PRODUCT_SHA = os.environ["PRODUCT_SHA"]
PRODUCT_TREE = os.environ["PRODUCT_TREE"]
PRODUCT_BRANCH = os.environ.get("PRODUCT_BRANCH", "feat/h011-v3-discovery-refresh")
RUN_ID = os.environ.get("GITHUB_RUN_ID", str(int(time.time())))
OUT = pathlib.Path(os.environ.get("EVIDENCE_DIR", "evidence"))
OUT.mkdir(parents=True, exist_ok=True)
REPORT = OUT / "phase_d.json"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
    "User-Agent": "senex-arq-phase-d/3",
}
SENSITIVE = re.compile(
    r"(secret|token|password|credential|private|runtimeenvironment|runtimefiles|"
    r"buildarguments|buildfiles|dockersecretmounts|value|content)", re.I
)

state: dict[str, object] = {
    "schema_version": "senex-phase-d-manual-clone-v3",
    "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    "product_sha": PRODUCT_SHA,
    "product_tree": PRODUCT_TREE,
    "product_branch": PRODUCT_BRANCH,
    "project_id": PROJECT,
    "service_id": SERVICE,
    "source_volume_id": SOURCE_VOLUME,
    "requests": [],
    "phase_c": "PASS",
    "phase_d": "IN_PROGRESS",
    "secret_values_exported": False,
    "safety": {
        "paper_only": True,
        "orders_enabled": False,
        "live_capital_locked": True,
        "real_order_network_calls": 0,
        "wallet_or_private_key_access": 0,
        "real_capital_actions": 0,
    },
}


def scrub(value, key: str = ""):
    if isinstance(value, dict):
        result = {}
        for raw_key, item in value.items():
            name = str(raw_key)
            if SENSITIVE.search(name):
                if isinstance(item, dict):
                    result[name] = {
                        "names": sorted(map(str, item.keys())),
                        "values_redacted": True,
                    }
                elif isinstance(item, list):
                    result[name] = {"count": len(item), "values_redacted": True}
                else:
                    result[name] = "<redacted>"
            else:
                result[name] = scrub(item, name)
        return result
    if isinstance(value, list):
        return [scrub(item, key) for item in value]
    return value


def persist() -> None:
    REPORT.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def request(method: str, route: str, payload=None, *, label: str | None = None, retries: int = 1):
    encoded = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    headers = dict(HEADERS)
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(API + route, headers=headers, data=encoded, method=method)
        status = 0
        raw = b""
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
        digest = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8", "replace")
        try:
            obj = json.loads(text or "{}")
        except Exception:
            obj = {"non_json": True, "safe_text": text[:240]}
        record = {
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "attempt": attempt,
            "label": label or route,
            "method": method,
            "path": route,
            "status": status,
            "body_sha256": digest,
            "bytes": len(raw),
        }
        state["requests"].append(record)
        state.setdefault("responses", {})[label or route] = scrub(obj)
        persist()
        if 200 <= status < 300:
            return obj
        last = RuntimeError(
            f"{method} {route} status={status} body_sha256={digest} safe={scrub(obj)}"
        )
        if attempt < retries:
            time.sleep(min(5 * attempt, 20))
    raise last or RuntimeError(f"request failed: {method} {route}")


def data(obj):
    return obj.get("data", obj) if isinstance(obj, dict) else obj


def poll(route: str, label: str, predicate, timeout: int = 2400, interval: int = 10):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = data(request("GET", route, label=label, retries=3))
        if predicate(last):
            return last
        time.sleep(interval)
    raise RuntimeError(f"timeout polling {route}: {scrub(last)}")


def scale(instances: int):
    request(
        "POST",
        f"/projects/{PROJECT}/services/{SERVICE}/scale",
        {"instances": instances},
        label=f"scale_{instances}",
    )
    return poll(
        f"/projects/{PROJECT}/services/{SERVICE}/deployment",
        f"scale_{instances}_poll",
        lambda obj: int(obj.get("instances", -1)) == instances,
        timeout=900,
    )


VERIFIER = r'''import hashlib,json
from pathlib import Path

def manifest(root):
    root=Path(root); rows=[]; total=0
    if root.is_dir():
        for p in sorted(x for x in root.rglob('*') if x.is_file() and not x.is_symlink()):
            rel=p.relative_to(root).as_posix(); h=hashlib.sha256(); size=0
            with p.open('rb') as f:
                while True:
                    block=f.read(1024*1024)
                    if not block: break
                    h.update(block); size += len(block)
            rows.append((rel,size,h.hexdigest())); total += size
    digest=hashlib.sha256()
    for rel,size,sha in rows: digest.update(f'{sha} {size} {rel}\n'.encode())
    return {'exists':root.is_dir(),'files':len(rows),'bytes':total,'manifest_sha256':digest.hexdigest()}

roots={'source':manifest('/source'),'backup':manifest('/backup'),'restore':manifest('/restore')}
result={'roots':roots,'source_equals_backup':roots['source']==roots['backup'],'backup_equals_restore':roots['backup']==roots['restore'],'source_equals_restore':roots['source']==roots['restore']}
chain=Path('/restore/h011_v3/raw_chain_v1')
result['raw_chain_exists']=chain.is_dir()
if chain.is_dir():
    try:
        import h011_v3_raw_transaction as rt
        from h011_v3_committed_snapshot import validate_committed_chain_under_lock
        from h011_v3_raw_recovery import recover_raw_scan_transaction
        with rt.RawChainLock(chain,rt.DEFAULT_MARKER_POLICY.manifest_prefix).acquire() as guard:
            result['recovery']=recover_raw_scan_transaction(guard=guard,raw_directory=chain,policy=rt.DEFAULT_MARKER_POLICY)
            result['chain']=validate_committed_chain_under_lock(guard=guard,raw_directory=chain,policy=rt.DEFAULT_MARKER_POLICY)
        result['chain_verified']=True
    except Exception as exc:
        result['chain_verified']=False
        result['chain_error']=f'{type(exc).__name__}: {exc}'
else:
    result['chain_verified']=None
print('SENEX_CLONE_RESULT='+json.dumps(result,sort_keys=True,separators=(',',':')))
if not (result['source_equals_backup'] and result['backup_equals_restore'] and result['source_equals_restore']): raise SystemExit(2)
if result['raw_chain_exists'] and result['chain_verified'] is not True: raise SystemExit(3)
'''


def main() -> None:
    scaled_zero = False
    source_attached_to_job = False
    job_id = None
    try:
        deployment = data(
            request(
                "GET",
                f"/projects/{PROJECT}/services/{SERVICE}/deployment",
                label="deployment_before",
                retries=3,
            )
        )
        internal = deployment.get("internal") or {}
        rollback = {
            "build_id": internal.get("buildId"),
            "sha": internal.get("deployedSHA"),
            "branch": internal.get("branch"),
            "instances": deployment.get("instances"),
        }
        if not rollback["build_id"] or not rollback["sha"]:
            raise RuntimeError("rollback identity incomplete")
        state["rollback"] = rollback

        volume = data(
            request(
                "GET",
                f"/projects/{PROJECT}/volumes/{SOURCE_VOLUME}",
                label="source_volume_before",
                retries=3,
            )
        )
        spec = volume.get("spec") or {}
        if volume.get("status") != "BOUND" or int(spec.get("storageSize", 0)) < 6144:
            raise RuntimeError(f"source volume identity/durability invalid: {scrub(volume)}")
        state["source_volume"] = {
            "id": volume.get("id"),
            "name": volume.get("name"),
            "status": volume.get("status"),
            "mounts": volume.get("mounts"),
            "spec": spec,
            "createdAt": volume.get("createdAt"),
            "updatedAt": volume.get("updatedAt"),
            "attachedObjects": volume.get("attachedObjects"),
        }

        build_id = data(
            request(
                "POST",
                f"/projects/{PROJECT}/services/{SERVICE}/build",
                {"sha": PRODUCT_SHA},
                label="exact_build_start",
            )
        ).get("id")
        if not build_id:
            raise RuntimeError("exact build id missing")
        build = poll(
            f"/projects/{PROJECT}/services/{SERVICE}/build/{build_id}",
            "exact_build_poll",
            lambda obj: bool(obj.get("concluded")),
            timeout=3600,
            interval=15,
        )
        if not build.get("success") or build.get("sha") != PRODUCT_SHA:
            raise RuntimeError(f"exact build failed or SHA mismatch: {scrub(build)}")
        state["exact_build"] = {
            "id": build_id,
            "sha": build.get("sha"),
            "status": build.get("status"),
            "createdAt": build.get("createdAt"),
            "buildConcludedAt": build.get("buildConcludedAt"),
        }

        scale(0)
        scaled_zero = True
        containers = data(
            request(
                "GET",
                f"/projects/{PROJECT}/services/{SERVICE}/containers?per_page=100",
                label="containers_after_scale_zero",
                retries=3,
            )
        )
        container_list = containers.get("containers", []) if isinstance(containers, dict) else []
        if any(item.get("status") == "TASK_RUNNING" for item in container_list):
            raise RuntimeError("service still has running containers after scale zero")

        suffix = RUN_ID[-9:]
        verifier_b64 = base64.b64encode(VERIFIER.encode()).decode()
        job_payload = {
            "name": f"Senex clone verify {suffix}",
            "description": f"Paper-only clone verification {PRODUCT_SHA[:12]}",
            "billing": {"deploymentPlan": "nf-compute-20"},
            "deployment": {
                "internal": {
                    "id": SERVICE,
                    "branch": PRODUCT_BRANCH,
                    "buildId": build_id,
                    "buildSHA": PRODUCT_SHA,
                },
                "docker": {
                    "configType": "customCommand",
                    "customCommand": "python3 /tmp/verify.py",
                },
                "storage": {"ephemeralStorage": {"storageSize": 1024}},
            },
            "runtimeFiles": {
                "/tmp/verify.py": {"data": verifier_b64, "encoding": "base64"}
            },
            "settings": {
                "backoffLimit": 0,
                "runOnSourceChange": "never",
                "activeDeadlineSeconds": 1800,
            },
        }
        job = data(
            request("POST", f"/projects/{PROJECT}/jobs", job_payload, label="clone_job_create")
        )
        job_id = job.get("id")
        if not job_id:
            raise RuntimeError(f"clone verification job id missing: {scrub(job)}")
        state["clone_job"] = {
            "id": job_id,
            "name": job.get("name"),
            "createdAt": job.get("createdAt"),
        }

        size = int(spec.get("storageSize", 6144))
        backup = data(
            request(
                "POST",
                f"/projects/{PROJECT}/volumes",
                {
                    "name": f"senex-backup-{suffix}",
                    "mounts": [{"volumeMountPath": "", "containerMountPath": "/backup"}],
                    "spec": {"storageClassName": "nvme", "storageSize": size},
                    "source": {"type": "volume", "sourceId": SOURCE_VOLUME},
                    "attachedObjects": [{"id": job_id, "type": "job"}],
                },
                label="backup_clone_create",
            )
        )
        backup_id = backup.get("id")
        if not backup_id:
            raise RuntimeError(f"backup clone id missing: {scrub(backup)}")
        backup = poll(
            f"/projects/{PROJECT}/volumes/{backup_id}",
            "backup_clone_poll",
            lambda obj: obj.get("status") == "BOUND",
            timeout=1800,
        )
        state["manual_backup"] = {
            "id": backup_id,
            "name": backup.get("name"),
            "status": backup.get("status"),
            "source_volume_id": SOURCE_VOLUME,
            "mounts": backup.get("mounts"),
            "spec": backup.get("spec"),
            "createdAt": backup.get("createdAt"),
            "updatedAt": backup.get("updatedAt"),
        }

        restore = data(
            request(
                "POST",
                f"/projects/{PROJECT}/volumes",
                {
                    "name": f"senex-restore-{suffix}",
                    "mounts": [{"volumeMountPath": "", "containerMountPath": "/restore"}],
                    "spec": {"storageClassName": "nvme", "storageSize": size},
                    "source": {"type": "volume", "sourceId": backup_id},
                    "attachedObjects": [{"id": job_id, "type": "job"}],
                },
                label="restore_clone_create",
            )
        )
        restore_id = restore.get("id")
        if not restore_id:
            raise RuntimeError(f"restore clone id missing: {scrub(restore)}")
        restore = poll(
            f"/projects/{PROJECT}/volumes/{restore_id}",
            "restore_clone_poll",
            lambda obj: obj.get("status") == "BOUND",
            timeout=1800,
        )
        state["isolated_restore"] = {
            "id": restore_id,
            "name": restore.get("name"),
            "status": restore.get("status"),
            "source_volume_id": backup_id,
            "mounts": restore.get("mounts"),
            "spec": restore.get("spec"),
            "createdAt": restore.get("createdAt"),
            "updatedAt": restore.get("updatedAt"),
        }

        request(
            "POST",
            f"/projects/{PROJECT}/volumes/{SOURCE_VOLUME}/attach",
            {"nfObject": {"id": job_id, "type": "job"}},
            label="attach_source_to_clone_job",
        )
        source_attached_to_job = True

        run = data(
            request(
                "POST",
                f"/projects/{PROJECT}/jobs/{job_id}/runs",
                {},
                label="clone_job_run_start",
            )
        )
        run_id = run.get("id")
        if not run_id:
            raise RuntimeError(f"clone verification run id missing: {scrub(run)}")
        run = poll(
            f"/projects/{PROJECT}/jobs/{job_id}/runs/{run_id}",
            "clone_job_run_poll",
            lambda obj: bool(obj.get("concluded")),
            timeout=1800,
        )
        if run.get("status") != "SUCCESS":
            raise RuntimeError(f"clone verification job failed: {scrub(run)}")
        query = urllib.parse.urlencode(
            {
                "runId": run_id,
                "queryType": "range",
                "duration": 3600,
                "lineLimit": 1000,
                "direction": "forward",
            }
        )
        logs = data(
            request(
                "GET",
                f"/projects/{PROJECT}/jobs/{job_id}/logs?{query}",
                label="clone_job_logs",
                retries=3,
            )
        )
        marker = None
        for entry in logs if isinstance(logs, list) else []:
            line = str(entry.get("log", ""))
            if "SENEX_CLONE_RESULT=" in line:
                try:
                    marker = json.loads(line.split("SENEX_CLONE_RESULT=", 1)[1].strip())
                except Exception:
                    pass
        if not marker:
            raise RuntimeError("clone verification marker missing")
        if not all(
            marker.get(name)
            for name in (
                "source_equals_backup",
                "backup_equals_restore",
                "source_equals_restore",
            )
        ):
            raise RuntimeError(f"clone equality failed: {scrub(marker)}")
        if marker.get("raw_chain_exists") and marker.get("chain_verified") is not True:
            raise RuntimeError(f"restored raw-chain validation failed: {scrub(marker)}")
        state["clone_verification"] = {
            "run_id": run_id,
            "status": run.get("status"),
            "startedAt": run.get("startedAt"),
            "concludedAt": run.get("concludedAt"),
            "result": scrub(marker),
        }
        state["phase_d"] = "PASS"
        persist()
    except Exception as exc:
        state["phase_d"] = "FAILED"
        state["failure"] = {"error_class": type(exc).__name__, "message": str(exc)}
        persist()
        raise
    finally:
        if source_attached_to_job and job_id:
            try:
                request(
                    "POST",
                    f"/projects/{PROJECT}/volumes/{SOURCE_VOLUME}/detach",
                    {"nfObject": {"id": job_id, "type": "job"}},
                    label="detach_source_from_clone_job",
                )
            except Exception as exc:
                state["detach_warning"] = {
                    "error_class": type(exc).__name__,
                    "message": str(exc),
                }
        if scaled_zero:
            try:
                scale(1)
            except Exception as exc:
                state["scale_restore_failure"] = {
                    "error_class": type(exc).__name__,
                    "message": str(exc),
                }
        state["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        persist()
        raw = REPORT.read_bytes()
        (OUT / "SHA256SUMS").write_text(
            hashlib.sha256(raw).hexdigest() + "  phase_d.json\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
    print(
        json.dumps(
            {
                "phase_d": state["phase_d"],
                "build_id": state.get("exact_build", {}).get("id"),
                "backup_volume_id": state.get("manual_backup", {}).get("id"),
                "restore_volume_id": state.get("isolated_restore", {}).get("id"),
                "verification": state.get("clone_verification"),
                "secret_values_exported": False,
            },
            sort_keys=True,
        )
    )
