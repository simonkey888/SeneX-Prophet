from __future__ import annotations

import argparse
import json
from pathlib import Path

from .worker import run_job


def main() -> int:
    p = argparse.ArgumentParser(description="SENEX canonical ATM worker (AUD-067-R1)")
    p.add_argument("--job", required=True)
    p.add_argument("--workspace-root", required=True)
    p.add_argument("--state-root", required=True)
    p.add_argument("--target-root", required=True)
    p.add_argument("--canonical-senex-root", required=True)
    p.add_argument("--source-sha", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--cancel-file")
    a = p.parse_args()
    job = json.loads(Path(a.job).read_text(encoding="utf-8"))
    cancel_check = (lambda: Path(a.cancel_file).exists()) if a.cancel_file else (lambda: False)
    result = run_job(job, a.state_root, a.target_root, a.source_sha, a.workspace_root, a.canonical_senex_root,
                     cancel_check=cancel_check)
    output = Path(a.output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "job_id": result["job_id"], "worker_id": result["worker_id"]}, sort_keys=True))
    return 0 if result["status"] in {"SUCCEEDED", "CANCELLED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
