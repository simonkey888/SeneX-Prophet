from __future__ import annotations
import argparse, json
from pathlib import Path
from .worker import run_job

def main() -> int:
    p=argparse.ArgumentParser(description="SENEX ATM worker (AUD-067)")
    p.add_argument("--job",required=True); p.add_argument("--state-root",required=True)
    p.add_argument("--target-root",required=True); p.add_argument("--source-sha",required=True); p.add_argument("--output",required=True)
    a=p.parse_args(); job=json.loads(Path(a.job).read_text())
    result=run_job(job,a.state_root,a.target_root,a.source_sha)
    Path(a.output).write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    print(json.dumps({"status":result["status"],"job_id":result["job_id"],"worker_id":result["worker_id"]},sort_keys=True))
    return 0 if result["status"] in {"SUCCEEDED","CANCELLED"} else 1
if __name__=="__main__": raise SystemExit(main())
