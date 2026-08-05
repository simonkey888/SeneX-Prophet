#!/usr/bin/env python3
"""Deterministic repository-wide paper-only safety gate.

The gate scans tracked executable/configuration surfaces, never secret values.
It rejects authenticated trading/order capabilities, trading credential loaders,
private-key/signing imports, and write-capable GitHub workflows.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

SOURCE_SUFFIXES = {
    ".py", ".pyi", ".sh", ".bash", ".zsh", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".conf", ".json", ".js", ".jsx",
    ".ts", ".tsx", ".html",
}
SOURCE_NAMES = {"Dockerfile", "Containerfile", "Procfile"}
EXCLUDED_PREFIXES = (
    ".git/", "docs/", "research/", "tests/", "governance/",
    "senecio_output/", ".pytest_cache/", "__pycache__/",
)
EXCLUDED_EXACT = {
    "SENECIO-CODE.md",
    "tools/verify_paper_only_repository.py",
}

DANGEROUS_CALLS = {
    "create_order", "create_market_order", "place_order", "place_market_order",
    "post_order", "submit_order", "send_order", "cancel_order", "cancel_orders",
    "delete_order", "fetch_positions", "fetch_balance", "fetch_my_trades",
    "fetch_open_orders", "fetch_closed_orders", "derive_api_key", "create_api_key",
    "sign_order", "sign_transaction", "send_transaction", "broadcast_transaction",
}
DANGEROUS_IMPORT_PARTS = {
    "py_clob_client", "eth_account", "web3", "eth_keys", "private_key",
    "walletconnect", "brownie", "ape_accounts",
}
TRADING_CREDENTIAL_RE = re.compile(
    r"(?i)(?:binance|bybit|kraken|okx|gate|mexc|bitget|polymarket|clob|exchange)"
    r".*(?:api[_-]?key|secret|token|password|private[_-]?key|wallet|mnemonic|seed)"
    r"|(?:api[_-]?key|secret|token|password|private[_-]?key|wallet|mnemonic|seed)"
    r".*(?:binance|bybit|kraken|okx|gate|mexc|bitget|polymarket|clob|exchange)"
)
PRIVATE_CONFIG_KEYS = {"apikey", "secret", "privatekey", "mnemonic", "seedphrase"}
WORKFLOW_WRITE_RE = re.compile(
    r"(?mi)^\s*(?:contents|pages|id-token|packages|deployments|actions)\s*:\s*write\s*$"
)
WORKFLOW_MUTATION_RE = re.compile(
    r"(?i)(?:\bgit\s+(?:push|commit)\b|actions/(?:deploy-pages|upload-pages-artifact)@)"
)
TEXT_DANGEROUS_CALL_RE = re.compile(
    r"(?i)\b(?:create_market_order|place_market_order|submit_order|post_order|"
    r"fetch_positions|fetch_balance|sign_transaction|send_transaction)\b"
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    evidence: str


def _call_name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _tracked_paths(repo_root: Path) -> list[str]:
    try:
        raw = subprocess.check_output(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            stderr=subprocess.DEVNULL,
        )
        paths = [item.decode("utf-8") for item in raw.split(b"\0") if item]
    except (subprocess.CalledProcessError, FileNotFoundError):
        paths = [
            str(path.relative_to(repo_root)).replace("\\", "/")
            for path in repo_root.rglob("*") if path.is_file()
        ]
    return sorted(set(paths))


def _eligible(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized in EXCLUDED_EXACT or normalized.startswith(EXCLUDED_PREFIXES):
        return False
    p = Path(normalized)
    return (
        p.suffix.lower() in SOURCE_SUFFIXES
        or p.name in SOURCE_NAMES
        or p.name.startswith("Dockerfile.")
    )


def _string_constant(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _python_findings(path: Path, relative: str) -> list[Finding]:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text, filename=relative)
    except SyntaxError as exc:
        return [Finding(relative, exc.lineno or 0, "PYTHON_PARSE", str(exc))]
    findings: list[Finding] = []
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.lineno, node.module))
    for line, name in imports:
        low = name.lower()
        if any(part in low for part in DANGEROUS_IMPORT_PARTS):
            findings.append(Finding(relative, line, "SIGNING_OR_WALLET_IMPORT", name))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call = _call_name(node.func)
            leaf = call.rsplit(".", 1)[-1].lower()
            if leaf in DANGEROUS_CALLS:
                findings.append(Finding(relative, node.lineno, "AUTHENTICATED_TRADING_CALL", call))
            if call in {"os.getenv", "os.environ.get", "getenv"} and node.args:
                name = _string_constant(node.args[0])
                if name and TRADING_CREDENTIAL_RE.search(name):
                    findings.append(Finding(relative, node.lineno, "TRADING_CREDENTIAL_LOADER", name))
            for keyword in node.keywords:
                if keyword.arg and keyword.arg.lower() in PRIVATE_CONFIG_KEYS:
                    findings.append(Finding(relative, node.lineno, "PRIVATE_CLIENT_CONFIGURATION", keyword.arg))
        elif isinstance(node, ast.Subscript):
            target = _call_name(node.value)
            key = _string_constant(node.slice)
            if target == "os.environ" and key and TRADING_CREDENTIAL_RE.search(key):
                findings.append(Finding(relative, node.lineno, "TRADING_CREDENTIAL_LOADER", key))
            if key and key.lower() in PRIVATE_CONFIG_KEYS and isinstance(node.ctx, ast.Store):
                findings.append(Finding(relative, node.lineno, "PRIVATE_CLIENT_CONFIGURATION", key))
    return findings


def _text_findings(path: Path, relative: str) -> list[Finding]:
    text = path.read_text(encoding="utf-8", errors="replace")
    findings: list[Finding] = []
    workflow = (
        relative.startswith(".github/workflows/")
        and Path(relative).suffix.lower() in {".yml", ".yaml"}
    )
    if workflow:
        for match in WORKFLOW_WRITE_RE.finditer(text):
            findings.append(Finding(
                relative,
                text.count("\n", 0, match.start()) + 1,
                "WORKFLOW_WRITE_PERMISSION",
                match.group(0).strip(),
            ))
        for match in WORKFLOW_MUTATION_RE.finditer(text):
            findings.append(Finding(
                relative,
                text.count("\n", 0, match.start()) + 1,
                "WORKFLOW_REPOSITORY_MUTATION",
                match.group(0),
            ))
    if Path(relative).suffix.lower() != ".py" and not workflow:
        for match in TEXT_DANGEROUS_CALL_RE.finditer(text):
            findings.append(Finding(
                relative,
                text.count("\n", 0, match.start()) + 1,
                "AUTHENTICATED_TRADING_SYMBOL",
                match.group(0),
            ))
        for line_no, line in enumerate(text.splitlines(), 1):
            for candidate in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", line):
                if TRADING_CREDENTIAL_RE.fullmatch(candidate):
                    findings.append(Finding(relative, line_no, "TRADING_CREDENTIAL_NAME", candidate))
    return findings


def scan_repository(repo_root: Path) -> dict[str, object]:
    root = repo_root.resolve()
    tracked = _tracked_paths(root)
    scanned = [path for path in tracked if _eligible(path) and (root / path).is_file()]
    findings: list[Finding] = []
    for relative in scanned:
        path = root / relative
        if path.suffix.lower() == ".py":
            findings.extend(_python_findings(path, relative))
        findings.extend(_text_findings(path, relative))
    unique = sorted({(f.path, f.line, f.rule, f.evidence) for f in findings})
    normalized = [Finding(*item) for item in unique]
    coverage = {
        "tracked_files": len(tracked),
        "scanned_files": len(scanned),
        "root_code": sum(
            "/" not in p and Path(p).suffix.lower() in SOURCE_SUFFIXES for p in scanned
        ),
        "package_code": sum(
            "/" in p and p.endswith((".py", ".js", ".ts", ".tsx", ".jsx"))
            for p in scanned
        ),
        "workflows": sum(p.startswith(".github/workflows/") for p in scanned),
        "dockerfiles": sum(
            Path(p).name == "Dockerfile" or Path(p).name.startswith("Dockerfile.")
            for p in scanned
        ),
        "configuration": sum(
            Path(p).suffix.lower() in {".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".json"}
            for p in scanned
        ),
    }
    digest_payload = json.dumps(
        [asdict(f) for f in normalized], sort_keys=True, separators=(",", ":")
    ).encode()
    return {
        "schema_version": "senex-paper-only-repository-scan-v1",
        "status": "PASS" if not normalized else "FAIL",
        "coverage": coverage,
        "scanned_paths": scanned,
        "findings": [asdict(f) for f in normalized],
        "findings_sha256": hashlib.sha256(digest_payload).hexdigest(),
        "secret_values_observed": False,
        "invariants": {
            "paper_only": True,
            "orders_enabled": False,
            "live_capital_locked": True,
            "real_order_network_calls": 0,
            "wallet_or_private_key_access": 0,
            "real_capital_actions": 0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output")
    args = parser.parse_args()
    report = scan_repository(Path(args.repo_root))
    rendered = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "coverage": report["coverage"],
        "finding_count": len(report["findings"]),
        "findings_sha256": report["findings_sha256"],
        "secret_values_observed": False,
    }, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
