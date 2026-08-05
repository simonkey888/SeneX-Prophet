#!/usr/bin/env python3
"""Build evidence for SENEX Phase F/H without fabricating unavailable production facts.

The builder inventories historical corpus candidates, records a fail-closed
backtest gate, hashes operational runbooks, and performs a static paper-only
security scan. It never accesses wallets, private keys, authenticated trading
endpoints, or real capital.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

CORPUS_SUFFIXES = (".json", ".jsonl", ".json.gz", ".jsonl.gz", ".csv", ".parquet")
FORBIDDEN_MODULES = ("web3", "eth_account", "py_clob_client.client")
FORBIDDEN_CALLS = {
    "post_order",
    "create_order",
    "submit_order",
    "cancel_order",
    "delete_order",
    "derive_api_key",
}
SCAN_ROOTS = (
    "polymarket/paper",
    "polymarket/monitoring",
    "tools/build_execution_truth_artifacts.py",
)
RUNBOOK_PATHS = (
    "docs/operations/SENEX_PRODUCTION_RUNBOOK.md",
    "docs/operations/SENEX_SECURITY_REVIEW.md",
    "docs/operations/SENEX_INCIDENT_ROLLBACK_DRILL.md",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def iter_corpus_files(roots: Iterable[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for root in roots:
        if not root.exists():
            continue
        paths = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
        for path in paths:
            lowered = path.name.lower()
            if not any(lowered.endswith(suffix) for suffix in CORPUS_SUFFIXES):
                continue
            records.append(
                {
                    "path": str(path.resolve()),
                    "size": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
    records.sort(key=lambda item: item["path"])
    return records


def static_security_scan(repo_root: Path) -> dict[str, Any]:
    violations: list[str] = []
    scanned: list[str] = []
    for relative in SCAN_ROOTS:
        root = repo_root / relative
        paths = [root] if root.is_file() else sorted(root.rglob("*.py")) if root.exists() else []
        for path in paths:
            scanned.append(str(path.relative_to(repo_root)))
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                    if any(name.startswith(FORBIDDEN_MODULES) for name in names):
                        violations.append(f"{path.relative_to(repo_root)}:import:{names}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module.startswith(FORBIDDEN_MODULES):
                        violations.append(f"{path.relative_to(repo_root)}:import:{module}")
                elif isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute):
                        name = node.func.attr
                    elif isinstance(node.func, ast.Name):
                        name = node.func.id
                    else:
                        name = ""
                    if name in FORBIDDEN_CALLS:
                        violations.append(f"{path.relative_to(repo_root)}:call:{name}")
    return {
        "status": "PASS" if not violations else "FAIL",
        "scanned_files": scanned,
        "violations": violations,
        "invariants": {
            "paper_only": True,
            "orders_enabled": False,
            "live_capital_locked": True,
            "wallet_or_private_key_access": False,
            "real_order_network_calls": 0,
            "real_capital_actions": 0,
        },
    }


def runbook_manifest(repo_root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    missing: list[str] = []
    for relative in RUNBOOK_PATHS:
        path = repo_root / relative
        if not path.is_file():
            missing.append(relative)
            continue
        files.append({"path": relative, "size": path.stat().st_size, "sha256": sha256(path)})
    return {
        "status": "PASS" if not missing else "FAIL",
        "files": files,
        "missing": missing,
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--corpus-root", action="append", default=[])
    parser.add_argument("--fixture-report")
    parser.add_argument("--northflank-status", default="BLOCKED_MISSING_CREDENTIAL")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)

    roots = [repo_root / "polymarket/results/h011_v3/raw_chain_v1", repo_root / "polymarket/results/v3"]
    roots.extend(Path(item).resolve() for item in args.corpus_root)
    env_roots = [item for item in os.environ.get("SENEX_HISTORICAL_CORPUS_ROOTS", "").split(os.pathsep) if item]
    roots.extend(Path(item).resolve() for item in env_roots)

    corpus_files = iter_corpus_files(roots)
    corpus_digest = hashlib.sha256(canonical_json(corpus_files).encode("utf-8")).hexdigest()
    inventory = {
        "source_sha": args.source_sha,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "roots": [str(path) for path in roots],
        "file_count": len(corpus_files),
        "total_bytes": sum(item["size"] for item in corpus_files),
        "inventory_sha256": corpus_digest,
        "files": corpus_files,
        "status": "AVAILABLE" if corpus_files else "UNAVAILABLE",
    }

    fixture: dict[str, Any] | None = None
    if args.fixture_report:
        fixture_path = Path(args.fixture_report)
        if fixture_path.is_file():
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    backtest_gate = {
        "source_sha": args.source_sha,
        "historical_corpus_status": inventory["status"],
        "historical_corpus_file_count": inventory["file_count"],
        "historical_backtest_executed": False,
        "historical_performance_metrics_emitted": False,
        "fixture_harness_executed": fixture is not None,
        "fixture_harness_result": fixture.get("result") if isinstance(fixture, dict) else None,
        "fixture_is_historical_evidence": False,
        "status": "BLOCKED_CORPUS_UNAVAILABLE" if not corpus_files else "BLOCKED_BACKTEST_ENGINE_NOT_IMPLEMENTED",
        "reason": (
            "No historical corpus is present in the exact source checkout or supplied corpus roots. Synthetic fixtures are contract evidence only and are not promoted to historical performance evidence."
            if not corpus_files
            else "A corpus is present, but the accepted stack has no authoritative historical strategy backtest engine."
        ),
    }

    security = static_security_scan(repo_root)
    runbooks = runbook_manifest(repo_root)
    checkpoint = {
        "source_sha": args.source_sha,
        "phase_c": args.northflank_status,
        "phase_d": "BLOCKED_BY_PHASE_C",
        "phase_e": "PUBLIC_BASELINE_ONLY_NOT_DEPLOYED",
        "phase_f": backtest_gate["status"],
        "phase_g": "NOT_STARTED_REQUIRES_DEPLOYED_24H_RUNTIME",
        "phase_h_predeployment": "PASS" if security["status"] == "PASS" and runbooks["status"] == "PASS" else "FAIL",
        "mission_complete": False,
        "production_mutated": False,
        "safety_invariants": security["invariants"],
    }

    outputs = {
        "phase_f_corpus_inventory.json": inventory,
        "phase_f_backtest_gate.json": backtest_gate,
        "phase_h_security_scan.json": security,
        "phase_h_runbook_manifest.json": runbooks,
        "phase_c_h_checkpoint.json": checkpoint,
    }
    for name, value in outputs.items():
        write_json(output / name, value)

    sums = [f"{sha256(output / name)}  {name}" for name in sorted(outputs)]
    (output / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")

    if security["status"] != "PASS" or runbooks["status"] != "PASS":
        return 1
    print(canonical_json(checkpoint))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
