import os
import subprocess

branch = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME")
if not branch:
    raise SystemExit("cannot determine target branch")

subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True)
subprocess.run(["git", "add", "senecio_polymarket", "polymarket", "scripts"], check=True)
if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
    print("No repair changes to persist")
else:
    subprocess.run(["git", "commit", "-m", "fix(score-002): enforce proof-qualified scoring and safe reconciliation [score-002-autofix]"], check=True)
    subprocess.run(["git", "push", "origin", branch], check=True)
    print("Authorized SCORE-002 repair persisted")
