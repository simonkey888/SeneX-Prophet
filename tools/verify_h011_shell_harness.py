#!/usr/bin/env python3
"""Repository-owned static and unit audit for the H-011 strict Bash harness."""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
from pathlib import Path

REQUIRED_MARKERS = (
    "set -Eeuo pipefail",
    "trap on_err ERR",
    "trap on_exit EXIT",
    "write_result()",
    "--self-test",
    "--controlled-failure",
)
OPTIONAL_CI_VARIABLES = (
    "GITHUB_RUN_ID",
    "GITHUB_SHA",
    "GITHUB_WORKSPACE",
    "RUNNER_TEMP",
)


def _multi_local_assignment_errors(text: str) -> list[str]:
    errors: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        match = re.match(r"^\s*(?:local|declare|typeset)\s+(.+)$", line)
        if not match:
            continue
        try:
            tokens = shlex.split(match.group(1), posix=True)
        except ValueError as exc:
            errors.append(f"line {number}: cannot parse declaration: {exc}")
            continue
        assignments = [
            token
            for token in tokens
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token)
        ]
        if len(assignments) > 1:
            errors.append(
                f"line {number}: multiple local assignments are forbidden "
                f"under set -u: {assignments}"
            )
    return errors


def audit(script: Path) -> dict[str, object]:
    text = script.read_text(encoding="utf-8")
    completed = subprocess.run(
        ["bash", "-n", str(script)],
        text=True,
        capture_output=True,
        check=False,
    )
    errors: list[str] = []
    if completed.returncode:
        errors.append(f"bash -n failed: {completed.stderr.strip()}")
    for marker in REQUIRED_MARKERS:
        if marker not in text:
            errors.append(f"missing required marker: {marker}")
    errors.extend(_multi_local_assignment_errors(text))
    required_download_guards = (
        "if ! python3 -m pip download",
        "xargs -0 -r sha256sum",
        '[[ -s "$dest/SHA256SUMS" ]]',
    )
    for guard in required_download_guards:
        if guard not in text:
            errors.append(f"missing fail-closed download guard: {guard}")
    for name in OPTIONAL_CI_VARIABLES:
        unsafe = re.compile(rf"\${name}(?![A-Za-z0-9_])|\${{{name}}}")
        for number, line in enumerate(text.splitlines(), 1):
            if unsafe.search(line):
                errors.append(
                    f"line {number}: optional CI variable {name} must use "
                    "an explicit default"
                )
    fixture = (
        "set -u\n"
        "bad_fixture() {\n"
        "  local n=$1 out=\"$n/x\"\n"
        "}\n"
    )
    fixture_errors = _multi_local_assignment_errors(fixture)
    if not fixture_errors:
        errors.append(
            "static checker self-test failed to reject same-statement local expansion"
        )
    return {
        "schema_version": "senex-h011-shell-harness-audit-v1",
        "status": "PASS" if not errors else "FAIL",
        "bash_syntax_check": "PASS" if completed.returncode == 0 else "FAIL",
        "unbound_variable_static_or_unit_check": "PASS" if not errors else "FAIL",
        "same_statement_local_assignments": "FORBIDDEN",
        "fixture_rejected": bool(fixture_errors),
        "script": str(script),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.script)
    rendered = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
