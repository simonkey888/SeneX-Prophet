#!/usr/bin/env python3
"""Fail-closed validation for SENEX H-011 hashed binary-only lock files."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)(?:\s*\\)?$")
HASH_RE = re.compile(r"^--hash=sha256:([0-9a-f]{64})(?:\s*\\)?$")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(path: Path) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    current: dict[str, object] | None = None
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
            hashes = current["hashes"]
            assert isinstance(hashes, list)
            hashes.append(hashed.group(1))
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--test", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    runtime = parse_lock(args.runtime)
    test = parse_lock(args.test)
    errors = [*runtime["errors"], *test["errors"]]
    runtime_names = {str(x["name"]) for x in runtime["entries"]}
    test_names = {str(x["name"]) for x in test["entries"]}
    if "pytest" in runtime_names:
        errors.append("pytest must not be present in the runtime lock")
    if "pytest" not in test_names:
        errors.append("pytest must be present in the test lock")
    overlap = sorted(runtime_names & test_names)
    if overlap:
        errors.append(f"runtime/test locks overlap: {overlap}")
    report = {
        "schema_version": "senex-h011-dependency-lock-v1",
        "status": "PASS" if not errors else "FAIL",
        "runtime": runtime,
        "test": test,
        "runtime_and_test_separated": not overlap and "pytest" not in runtime_names,
        "unpinned_requirements": 0 if not errors else None,
        "unhashed_requirements": 0 if not errors else None,
        "source_distributions_allowed": 0,
        "only_binary": True,
        "pip_require_hashes": True,
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
