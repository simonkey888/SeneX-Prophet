from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / "order070_r6_terminal.py"
source = TARGET.read_text(encoding="utf-8")

replacements = [
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
    (
        "'runtime_memory_fix':'OPTIONAL_ANALYTICS_TRUE_LAZY_INIT'",
        "'runtime_memory_fix':'OPTIONAL_ANALYTICS_TRUE_LAZY_INIT_PLUS_PUBLIC_HEAVY_ROUTE_UNMOUNT'",
    ),
]

for old, new in replacements:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"WRAPPER_EXPECTED_ONE_MATCH:{old[:80]}:COUNT={count}")
    source = source.replace(old, new, 1)

# The final candidate must be the exact remote PR head before any production operation.
expected_head = "7a57c47e7042f470ecaf024417103f00700800a7"
expected_tree = "e2aed8f96547c91846caf30544d7a47e2cfa62ef"
if expected_head not in source or expected_tree not in source:
    raise RuntimeError("FINAL_IDENTITY_NOT_BOUND")

os.environ.setdefault("ORDER070_R6_FINAL_WRAPPER", "1")
code = compile(source, str(TARGET), "exec")
exec(code, {"__name__": "__main__", "__file__": str(TARGET)})
