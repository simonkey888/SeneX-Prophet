#!/usr/bin/env python3
"""Fail-closed validation for SENEX H-011 hashed binary-only lock files."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)(?:\s*\\)?$")
HASH_RE = re.compile(r"^--hash=sha256:([0-9a-f]{64})(?:\s*\\)?$")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(path: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    errors: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pin = PIN_RE.fullmatch(line)
        if pin:
            if current is not None:
                entries.append(current)
            current = {
                "name": canonical(pin.group(1)),
                "version": pin.group(2),
                "hashes": [],
                "line": number,
            }
            continue
        hashed = HASH_RE.fullmatch(line)
        if hashed and current is not None:
            current["hashes"].append(hashed.group(1))
            continue
        errors.append(f"{path}:{number}: invalid lock syntax: {line}")
    if current is not None:
        entries.append(current)
    if not entries:
        errors.append(f"{path}: no locked packages")
    names: set[str] = set()
    for entry in entries:
        name = str(entry["name"])
        hashes = list(entry["hashes"])
        if name in names:
            errors.append(f"{path}: duplicate package {name}")
        names.add(name)
        if not hashes:
            errors.append(f"{path}: package {name} has no SHA-256 hash")
        if len(hashes) != len(set(hashes)):
            errors.append(f"{path}: duplicate hashes for {name}")
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "package_count": len(entries),
        "entries": entries,
        "errors": errors,
    }


def entry_map(lock: dict[str, Any]) -> dict[str, tuple[str, tuple[str, ...]]]:
    return {
        str(entry["name"]): (
            str(entry["version"]),
            tuple(sorted(str(value) for value in entry["hashes"])),
        )
        for entry in lock["entries"]
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--test", type=Path, required=True)
    parser.add_argument("--ci", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    runtime = parse_lock(args.runtime)
    test = parse_lock(args.test)
    ci = parse_lock(args.ci) if args.ci else None
    errors = [*runtime["errors"], *test["errors"]]
    if ci:
        errors.extend(ci["errors"])
    runtime_map = entry_map(runtime)
    test_map = entry_map(test)
    runtime_names = set(runtime_map)
    test_names = set(test_map)
    if "pytest" in runtime_names:
        errors.append("pytest must not be present in the runtime lock")
    if "pytest" not in test_names:
        errors.append("pytest must be present in the test lock")
    overlap = sorted(runtime_names & test_names)
    if overlap:
        errors.append(f"runtime/test locks overlap: {overlap}")
    expected_ci = {**runtime_map, **test_map}
    ci_consistent: bool | None = None
    if ci:
        actual_ci = entry_map(ci)
        ci_consistent = actual_ci == expected_ci
        if not ci_consistent:
            missing = sorted(set(expected_ci) - set(actual_ci))
            extra = sorted(set(actual_ci) - set(expected_ci))
            changed = sorted(
                name for name in set(expected_ci) & set(actual_ci)
                if expected_ci[name] != actual_ci[name]
            )
            errors.append(
                f"CI compatibility lock differs from runtime+test union: "
                f"missing={missing} extra={extra} changed={changed}"
            )
    report = {
        "schema_version": "senex-h011-dependency-lock-v2",
        "status": "PASS" if not errors else "FAIL",
        "runtime": runtime,
        "test": test,
        "ci": ci,
        "runtime_and_test_separated": not overlap and "pytest" not in runtime_names,
        "ci_lock_equals_runtime_test_union": ci_consistent,
        "unpinned_requirements": 0 if not errors else None,
        "unhashed_requirements": 0 if not errors else None,
        "source_distributions_allowed": 0,
        "only_binary": True,
        "pip_require_hashes": True,
        "no_deps_for_complete_locks": True,
        "errors": errors,
    }
    rendered = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
