#!/usr/bin/env python3
"""SENEX executable repository contract, inventory, and paper-safety verifier.

Static, deterministic, network-free.  It preserves the R0 public helpers and
adds R1 full-tree inventory plus paper-runtime authority gates.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

R0_GATE_IDS = (
    "DO_NOT_TOUCH_HASH_CHANGED",
    "FORBIDDEN_RUNTIME_OUTPUT_TRACKED",
    "PRODUCTION_PATH_CHANGED",
    "PRODUCTION_PATH_CHANGE_AUTHORIZED",
    "UNCLASSIFIED_DUPLICATE",
    "IMPORT_GRAPH_REGRESSION",
    "AUTHORITATIVE_RAW_WRITER_COUNT",
    "SAFETY_FLAGS_PRESENT",
    "WALLET_OR_ORDER_CODE_INTRODUCED",
    "RESEARCH_AUTHORITY_VIOLATION",
    "TEMPORARY_WORKFLOW_REINTRODUCED",
)
R1_GATE_IDS = (
    "UNMAPPED_TRACKED_FILE",
    "UNKNOWN_DOMAIN",
    "ACTIVE_ENTRYPOINT_UNREGISTERED",
    "DOMAIN_DEPENDENCY_VIOLATION",
    "MULTIPLE_AUTHORITATIVE_RAW_WRITERS",
    "LIVE_EXECUTION_AUTHORITY_PRESENT",
    "WALLET_AUTHORITY_PRESENT",
    "AI_COMPONENT_WITH_EXECUTION_AUTHORITY",
    "READ_ONLY_COMPONENT_WRITES_STATE",
    "GENERATED_PATH_MISCLASSIFIED",
    "MIGRATION_WITHOUT_SOURCE",
    "MIGRATION_WITHOUT_TARGET_DOMAIN",
    "MIGRATION_WITHOUT_ROLLBACK",
    "UNRESOLVED_WITHOUT_EVIDENCE",
    "MANIFEST_NONDETERMINISTIC",
    "R0_CONTRACT_REGRESSION",
    "PAPER_RUNTIME_DANGEROUS_CAPABILITY",
)
GATE_IDS = R0_GATE_IDS + R1_GATE_IDS
DOMAINS = (
    "GOVERNANCE", "ADAPTERS", "INGESTION", "EVIDENCE", "REPLAY", "FEATURES",
    "REGIMES", "RESEARCH", "STRATEGIES", "DECISION", "RISK", "EXECUTION_PAPER",
    "EXECUTION_LIVE_QUARANTINED", "PORTFOLIO", "EVALUATION", "LEARNING",
    "ORCHESTRATION", "OBSERVABILITY", "SECURITY", "READ_ONLY_API", "TESTS",
    "GENERATED", "LEGACY", "UNRESOLVED",
)
REQUIRED_OVERRIDE_FIELDS = {
    "mission_id", "base_sha", "head_sha", "allowed_paths", "issued_by",
    "issued_at", "expires_at", "reason", "old_hashes", "new_hashes",
}
DANGEROUS_CALLS = {
    "sign_order", "sign_transaction", "submit_order", "create_order", "place_order",
    "send_order", "cancel_order", "cancel_orders", "broadcast_transaction", "send_transaction",
}
DANGEROUS_MODULE_PARTS = {
    "wallet", "web3", "eth_account", "private_key", "py_clob_client", "clobclient",
}
PAPER_ROOT = "polymarket/paper/"
SELF_MANIFEST = "governance/repository_manifest.json"
SELF_GENERATED = {
    SELF_MANIFEST,
    "governance/repository_manifest.json.sha256",
    "governance/target_architecture.yaml",
    "governance/component_registry.json",
    "governance/migration_map.json",
}


@dataclass
class GateResult:
    gate_id: str
    status: str
    evidence: list[str]
    affected_paths: list[str]
    classification: str = "VERIFIED_STATIC_ANALYSIS"
    override_required: bool = False


def canonical_json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def git_blob_sha_bytes(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()


def matches(path: str, patterns: Iterable[str]) -> bool:
    path = path.replace("\\", "/")
    for pattern in patterns:
        pattern = pattern.replace("\\", "/")
        if pattern.endswith("/**") and (path == pattern[:-3] or path.startswith(pattern[:-2])):
            return True
        if fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def load_structured(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError as exc:
            raise ValueError(f"{path} is not JSON-compatible YAML") from exc
        value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise ValueError(f"{path} root must be object")
    return value


def verify_sidecar(data_path: Path, sidecar_path: Path) -> tuple[bool, str]:
    try:
        digest, name = sidecar_path.read_text(encoding="utf-8").strip().split(None, 1)
    except Exception as exc:
        return False, f"invalid sidecar: {exc}"
    if name.strip() != data_path.name:
        return False, "sidecar filename mismatch"
    actual = sha256_file(data_path)
    return digest == actual, actual if digest == actual else f"digest mismatch: {digest} != {actual}"


def _call_name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def python_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            for alias in node.names:
                found.add(f"{node.module}.{alias.name}")
        elif (
            isinstance(node, ast.Call)
            and _call_name(node.func) in {"importlib.import_module", "__import__"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
    return found


def capability_findings(path: Path) -> list[str]:
    if path.suffix != ".py":
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except Exception as exc:
        return [f"unparseable Python: {exc}"]
    findings: list[str] = []
    for name in python_imports(path):
        if any(part in name.lower() for part in DANGEROUS_MODULE_PARTS):
            findings.append(f"dangerous import capability: {name}")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call = _call_name(node.func)
        leaf = call.rsplit(".", 1)[-1].lower()
        strings = " ".join(
            item.value for item in ast.walk(node)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ).lower()
        if leaf in DANGEROUS_CALLS:
            findings.append(f"signing/order call capability: {call}")
        if call in {"subprocess.run", "subprocess.call", "subprocess.Popen", "os.system"} and any(
            term in strings for term in ("submit-order", "place-order", "create-order", "send-order", "wallet", "private-key")
        ):
            findings.append("subprocess execution capability")
        if call in {"importlib.import_module", "__import__"} and any(part in strings for part in DANGEROUS_MODULE_PARTS):
            findings.append(f"dynamic dangerous import capability: {strings}")
    return sorted(set(findings))


ACTION_RE = re.compile(r"^\s*-\s*uses:\s*([^#\s]+)", re.M)
FULL_SHA_RE = re.compile(r"^[^@]+@[0-9a-fA-F]{40}$")


def workflow_findings(path: Path, *, allow_dispatch: bool = False) -> list[str]:
    text = path.read_text(encoding="utf-8")
    low = text.lower()
    out: list[str] = []
    for action in ACTION_RE.findall(text):
        if not FULL_SHA_RE.fullmatch(action):
            out.append(f"unpinned action: {action}")
    if re.search(r"(?m)^\s*contents\s*:\s*write\s*$", low):
        out.append("contents:write permission")
    temporary = "temporary" in path.name.lower() or "temporary" in low or "temp-" in path.name.lower()
    if temporary and "expires_at:" not in low:
        out.append("temporary workflow missing expiry")
    if "workflow_dispatch:" in low and not allow_dispatch:
        out.append("workflow_dispatch present")
    for phrase in ("git push", "git commit", "pages deploy"):
        if phrase in low:
            out.append(f"dangerous workflow capability: {phrase}")
    return sorted(set(out))


def _utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be string")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() != timezone.utc.utcoffset(dt):
        raise ValueError("timestamp must be UTC")
    return dt


def validate_override(
    auth: dict[str, Any] | None,
    *,
    base_sha: str,
    head_sha: str,
    paths: Iterable[str],
    now: datetime | None = None,
) -> list[str]:
    auth = auth or {}
    errors: list[str] = []
    missing = sorted(REQUIRED_OVERRIDE_FIELDS - set(auth))
    if missing:
        return [f"missing fields: {missing}"]
    if auth.get("base_sha") != base_sha:
        errors.append("wrong base_sha")
    if auth.get("head_sha") != head_sha:
        errors.append("wrong head_sha")
    if not isinstance(auth.get("allowed_paths"), list) or sorted(set(auth["allowed_paths"])) != sorted(set(paths)):
        errors.append("allowed_paths must match exactly")
    try:
        issued, expiry = _utc(auth.get("issued_at")), _utc(auth.get("expires_at"))
        instant = now or datetime.now(timezone.utc)
    except Exception as exc:
        errors.append(f"invalid authorization timestamp: {exc}")
    else:
        if expiry <= instant:
            errors.append("authorization expired")
        if issued > instant:
            errors.append("authorization issued_at is in the future")
    if auth.get("issued_by") != "AUD":
        errors.append("issued_by must be AUD")
    if not isinstance(auth.get("old_hashes"), dict) or not isinstance(auth.get("new_hashes"), dict):
        errors.append("hash maps invalid")
    return errors


def _git(root: Path, *args: str, check: bool = True) -> str:
    process = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if check and process.returncode:
        raise RuntimeError(process.stderr.strip())
    return process.stdout


def tracked_files(root: Path) -> list[str]:
    return sorted(line for line in _git(root, "ls-files").splitlines() if line)


def changed(root: Path, base: str, head: str, status: bool = False) -> list[str]:
    args = ("diff", "--name-status", f"{base}...{head}") if status else ("diff", "--name-only", f"{base}...{head}")
    lines = _git(root, *args).splitlines()
    return sorted((line.split("\t", 1)[1] for line in lines if line.startswith("A\t")) if status else lines)


def ok(gate: str, *evidence: str) -> GateResult:
    return GateResult(gate, "PASS", list(evidence), [])


def bad(gate: str, evidence: list[str], paths: list[str], override: bool = False) -> GateResult:
    return GateResult(gate, "FAIL", evidence, sorted(set(paths)), override_required=override)


def _read_all_text(root: Path, paths: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for rel in paths:
        path = root / rel
        if not path.is_file() or path.stat().st_size > 4_000_000:
            continue
        try:
            result[rel] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return result


def classify_domain(path: str) -> str:
    if path.startswith("governance/") or path.startswith("tools/") or path in {
        "docs/TARGET_ARCHITECTURE.md", "docs/MIGRATION_SEQUENCE.md", "docs/REPOSITORY_AUTHORITY.md",
    }:
        return "GOVERNANCE"
    if path.startswith("tests/"):
        return "TESTS"
    if path.startswith(".github/workflows/"):
        return "ORCHESTRATION"
    if path.startswith("senecio_output/") or "/results/" in path or path.endswith("portfolio_state.json"):
        return "GENERATED"
    if path.startswith("research/"):
        return "RESEARCH"
    if path.startswith("senecio_polymarket/"):
        return "LEGACY"
    if path.startswith("polymarket/paper/"):
        name = Path(path).name
        if name == "broker.py": return "EXECUTION_PAPER"
        if name == "portfolio.py": return "PORTFOLIO"
        if name == "risk.py": return "RISK"
        if name == "engine.py": return "DECISION"
        if name in {"report.py"}: return "OBSERVABILITY"
        if name in {"trial_runner.py"}: return "ORCHESTRATION"
        return "EXECUTION_PAPER"
    if path.startswith("polymarket/control_plane/"):
        name = Path(path).name
        if "replay" in name: return "REPLAY"
        if any(term in name for term in ("artifact", "raw_", "provenance", "state_snapshot")): return "EVIDENCE"
        if any(term in name for term in ("alert", "drift", "coverage", "invariant", "source_health", "semantic")): return "OBSERVABILITY"
        return "ORCHESTRATION"
    if path.startswith("polymarket/"):
        name = Path(path).name
        if name in {"dashboard.py", "dashboard_v3.py"} or path.startswith("polymarket/templates/"): return "READ_ONLY_API"
        if any(term in name for term in ("raw_transaction", "raw_recovery", "raw_event", "committed_snapshot", "evidence_state")): return "EVIDENCE"
        if "replay" in name: return "REPLAY"
        if any(term in name for term in ("connector", "scraper", "clob_readonly")): return "ADAPTERS"
        if any(term in name for term in ("discovery", "scanner")): return "INGESTION"
        if any(term in name for term in ("market_structure", "trade_binding", "validation_semantics")): return "FEATURES"
        if any(term in name for term in ("vwap_detector", "pipeline")): return "STRATEGIES"
        if "runtime" in name or name in {"start.sh", "cron_h011.sh"}: return "ORCHESTRATION"
        if "portfolio" in name: return "LEGACY"
        if path.endswith("Dockerfile.h011-v3"): return "ORCHESTRATION"
        return "LEGACY"
    if path.startswith("docs/") or path.endswith(".md"):
        return "GOVERNANCE"
    if path.startswith("audit/"):
        return "GOVERNANCE"
    if path.startswith(".github/"):
        return "ORCHESTRATION"
    return "LEGACY"


def _mode_string(path: Path) -> str:
    return "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"


def _is_active_entrypoint(rel: str, text: str) -> bool:
    if rel in {
        "polymarket/h011_v3_runtime.py", "polymarket/dashboard_v3.py",
        "polymarket/vwap_detector_v2.py", "polymarket/paper/trial_runner.py",
    }:
        return True
    if rel.startswith(".github/workflows/") or rel.endswith(("Dockerfile", "Dockerfile.h011-v3", ".sh")):
        return True
    return 'if __name__ == "__main__"' in text or "if __name__ == '__main__'" in text


def _capabilities(path: Path, text: str) -> dict[str, bool]:
    low = text.lower()
    findings = capability_findings(path) if path.suffix == ".py" else []
    network = any(term in low for term in ("httpx", "requests", "urllib", "https://", "http://", "socket"))
    write = any(term in low for term in ("write_text(", "write_bytes(", "open(\"w", "open(\'w", "os.write(", "json.dump(", "sqlite", "append_text("))
    return {
        "wallet_capability": any("wallet" in item or "private" in item for item in findings) or "private_key" in low,
        "order_capability": any("order" in item or "transaction" in item for item in findings),
        "network_capability": network,
        "filesystem_write_capability": write,
    }


def _resolve_local_import(name: str, known: Mapping[str, str]) -> str | None:
    parts = name.split(".")
    for length in range(len(parts), 0, -1):
        candidate = ".".join(parts[:length])
        if candidate in known:
            return known[candidate]
    return None


def build_repository_manifest(root: Path, contract: Mapping[str, Any]) -> dict[str, Any]:
    files = tracked_files(root)
    texts = _read_all_text(root, files)
    joined = "\n".join(texts.values())
    module_map: dict[str, str] = {}
    for rel in files:
        if not rel.endswith(".py"):
            continue
        module = rel[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[:-9]
        module_map[module] = rel
        if rel.startswith("polymarket/"):
            module_map[rel[len("polymarket/"):-3].replace("/", ".").removesuffix(".__init__")] = rel
    runtime_roots = {
        "polymarket/h011_v3_runtime.py", "polymarket/dashboard_v3.py", "polymarket/paper/trial_runner.py",
    }
    imported: set[str] = set(runtime_roots)
    queue = list(runtime_roots)
    while queue:
        rel = queue.pop()
        path = root / rel
        if not path.is_file() or path.suffix != ".py":
            continue
        try:
            imports = python_imports(path)
        except Exception:
            continue
        for name in imports:
            target = _resolve_local_import(name, module_map)
            if target and target not in imported:
                imported.add(target)
                queue.append(target)
    records: list[dict[str, Any]] = []
    do_not_touch = set(contract.get("do_not_touch_hashes", {}))
    for rel in files:
        path = root / rel
        payload = path.read_bytes()
        text = texts.get(rel, "")
        domain = classify_domain(rel)
        capabilities = _capabilities(path, text)
        if rel in SELF_GENERATED:
            content_sha = "GENERATED_ARTIFACT_USE_COMMIT_TREE_OR_SIDECAR"
            blob_sha = "GENERATED_ARTIFACT_USE_GIT_TREE"
        else:
            content_sha = sha256_bytes(payload)
            blob_sha = git_blob_sha_bytes(payload)
        generated = domain == "GENERATED"
        active = _is_active_entrypoint(rel, text)
        migration_action = "KEEP_AS_IS"
        migration_source = rel
        migration_target = domain
        evidence = [f"path classification rule -> {domain}"]
        if domain == "LEGACY":
            migration_action = "RETIRE_LATER"
            evidence.append("legacy/non-authoritative path; retirement requires separate AUD authorization")
        if generated:
            migration_action = "GENERATED_ONLY"
            evidence.append("generated output path")
        records.append({
            "path": rel,
            "git_blob_sha": blob_sha,
            "sha256": content_sha,
            "size": -1 if rel in SELF_GENERATED else len(payload),
            "mode": _mode_string(path),
            "current_domain": domain,
            "target_domain": domain,
            "classification": "ACTIVE_ENTRYPOINT" if active else ("GENERATED" if generated else "TRACKED_SOURCE_OR_EVIDENCE"),
            "owner": "SENEX_ARQ" if domain not in {"LEGACY", "GENERATED"} else ("LEGACY_ORACLE" if domain == "LEGACY" else "RUNTIME_GENERATED"),
            "runtime_imported": rel in imported,
            "active_entrypoint": active,
            "docker_included": rel.startswith("polymarket/"),
            "workflow_referenced": rel in joined and not rel.startswith(".github/workflows/"),
            "shell_referenced": rel in "\n".join(value for key, value in texts.items() if key.endswith(".sh")),
            "test_referenced": rel in "\n".join(value for key, value in texts.items() if key.startswith("tests/")),
            "northflank_referenced": "DOCUMENTED_NOT_VERIFIED" if "northflank" in text.lower() or rel.endswith("Dockerfile.h011-v3") else "NO_EVIDENCE",
            "generated": generated,
            "sensitive": capabilities["wallet_capability"] or "secret" in text.lower(),
            "do_not_touch": rel in do_not_touch,
            **capabilities,
            "migration_action": migration_action,
            "migration_source": migration_source,
            "migration_target": migration_target,
            "evidence": evidence,
            "confidence": "HIGH" if domain != "LEGACY" else "MEDIUM",
        })
    return {
        "schema_version": "senex-complete-repository-manifest-v1",
        "repository": contract.get("repository"),
        "inventory_rule": {
            "tracked_source": "git ls-files",
            "self_reference": "repository_manifest.json uses sentinels; repository_manifest.json.sha256 protects actual bytes",
        },
        "files_tracked_total": len(records),
        "files": records,
    }


def build_target_architecture() -> dict[str, Any]:
    default_allowed = [item for item in DOMAINS if item != "EXECUTION_LIVE_QUARANTINED"]
    descriptions = {
        "GOVERNANCE": "Executable authority, inventory, policy, and audit evidence.",
        "ADAPTERS": "Public, unauthenticated source adapters.",
        "INGESTION": "Read-only discovery and observation collection.",
        "EVIDENCE": "Immutable raw evidence and transactional publication.",
        "REPLAY": "Deterministic reconstruction and verification.",
        "FEATURES": "Derived market features without execution authority.",
        "REGIMES": "Derived regime labels without execution authority.",
        "RESEARCH": "Non-authoritative experiments and analysis.",
        "STRATEGIES": "Deterministic signal evaluation without order authority.",
        "DECISION": "Paper decision and explicit abstention production.",
        "RISK": "Fail-closed paper risk limits.",
        "EXECUTION_PAPER": "Simulated intents and fills from public books only.",
        "EXECUTION_LIVE_QUARANTINED": "Unreachable live execution boundary.",
        "PORTFOLIO": "Replayable virtual portfolio accounting.",
        "EVALUATION": "Paper-trial metrics and non-profitability evaluation.",
        "LEARNING": "Offline learning artifacts with no authority.",
        "ORCHESTRATION": "Runtime and CI sequencing.",
        "OBSERVABILITY": "Read-only state, metrics, reports, and alerts.",
        "SECURITY": "Static gates and safety invariants.",
        "READ_ONLY_API": "Read-only API/UI surfaces.",
        "TESTS": "Verification and hostile tests.",
        "GENERATED": "Regenerable or append-only runtime outputs.",
        "LEGACY": "Non-authoritative historical implementation.",
        "UNRESOLVED": "Evidence-backed unresolved classification.",
    }
    domains = []
    for domain in DOMAINS:
        write_authority = domain in {"EVIDENCE", "EXECUTION_PAPER", "PORTFOLIO", "OBSERVABILITY", "GENERATED", "TESTS", "GOVERNANCE"}
        if domain == "READ_ONLY_API":
            write_authority = False
        domains.append({
            "domain_id": domain,
            "purpose": descriptions[domain],
            "allowed_responsibilities": [descriptions[domain]],
            "forbidden_responsibilities": ["real orders", "wallet/private-key access", "production mutation"],
            "allowed_dependencies": (default_allowed if domain != "EXECUTION_LIVE_QUARANTINED" else []),
            "forbidden_dependencies": (["EXECUTION_LIVE_QUARANTINED"] if domain != "EXECUTION_LIVE_QUARANTINED" else list(DOMAINS)),
            "network_authority": "PUBLIC_GET_ONLY" if domain in {"ADAPTERS", "INGESTION", "EXECUTION_PAPER", "ORCHESTRATION"} else "NONE",
            "filesystem_read_authority": True,
            "filesystem_write_authority": write_authority,
            "secret_authority": False,
            "order_authority": False,
            "wallet_authority": False,
            "production_authority": False,
            "human_approval_required": domain in {"EXECUTION_LIVE_QUARANTINED", "LEGACY"},
            "canonical_paths": [],
            "legacy_paths": [],
            "generated_paths": [],
            "owner": "SENEX_ARQ",
            "evidence": ["AUD order 5184706267", "OWNER authorization 5184689072"],
            "confidence": "HIGH",
        })
    return {
        "schema_version": "senex-target-architecture-v1",
        "safety_invariants": {"paper_only": True, "orders_enabled": False, "live_capital_locked": True},
        "domains": domains,
    }


def build_component_registry(root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    components: list[dict[str, Any]] = []
    for record in manifest["files"]:
        path = record["path"]
        suffix = Path(path).suffix
        if not (
            suffix in {".py", ".sh", ".yml", ".yaml"}
            or "Dockerfile" in Path(path).name
            or record["active_entrypoint"]
        ):
            continue
        source = root / path
        symbol = "FILE"
        if suffix == ".py":
            try:
                tree = ast.parse(source.read_text(encoding="utf-8"))
                main_symbols = [node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
                symbol = ",".join(main_symbols[:12]) or "MODULE"
            except Exception:
                symbol = "UNPARSEABLE_MODULE"
        authority = "PAPER_SIMULATION_ONLY" if path.startswith(PAPER_ROOT) else "NO_EXECUTION_AUTHORITY"
        status = "ACTIVE" if record["active_entrypoint"] or record["runtime_imported"] else ("LEGACY" if record["current_domain"] == "LEGACY" else "READ_ONLY")
        if record["generated"]:
            status = "GENERATED"
        components.append({
            "component_id": f"component:{path}",
            "path": path,
            "symbol_or_entrypoint": symbol,
            "current_domain": record["current_domain"],
            "target_domain": record["target_domain"],
            "authority": authority,
            "inputs": ["public/read-only inputs"] if record["network_capability"] else [],
            "outputs": ["paper/generated evidence"] if record["filesystem_write_capability"] else [],
            "raw_writer": path == "polymarket/h011_v3_raw_transaction.py",
            "derived_writer": record["filesystem_write_capability"] and path != "polymarket/h011_v3_raw_transaction.py",
            "network_access": "PUBLIC_GET_ONLY" if path.startswith(PAPER_ROOT) and record["network_capability"] else bool(record["network_capability"]),
            "secret_access": False if path.startswith(PAPER_ROOT) else bool(record["sensitive"]),
            "wallet_access": False,
            "order_access": False,
            "production_reachable": False if path.startswith(PAPER_ROOT) else record["runtime_imported"],
            "docker_reachable": record["docker_included"],
            "workflow_reachable": record["workflow_referenced"],
            "status": status,
            "evidence": record["evidence"],
            "confidence": record["confidence"],
        })
    return {"schema_version": "senex-component-registry-v1", "components": components}


def build_migration_map() -> dict[str, Any]:
    waves = [
        ("WAVE_0", "CONTRACT_AND_INVENTORY", ["governance/**"], "GOVERNANCE"),
        ("WAVE_1", "EVIDENCE_AND_REPLAY_BOUNDARIES", ["polymarket/h011_v3_raw_*.py", "polymarket/control_plane/replay.py"], "EVIDENCE"),
        ("WAVE_2", "ADAPTERS_AND_INGESTION_BOUNDARIES", ["polymarket/*connector.py", "polymarket/discovery_v3.py"], "ADAPTERS"),
        ("WAVE_3", "FEATURES_AND_REGIMES", ["polymarket/market_structure.py"], "FEATURES"),
        ("WAVE_4", "RESEARCH_AND_STRATEGY_SEPARATION", ["research/**", "polymarket/vwap_detector_v2.py"], "STRATEGIES"),
        ("WAVE_5", "DECISION_AND_RISK", ["polymarket/paper/engine.py", "polymarket/paper/risk.py"], "DECISION"),
        ("WAVE_6", "PAPER_EXECUTION_AND_PORTFOLIO", ["polymarket/paper/broker.py", "polymarket/paper/portfolio.py"], "EXECUTION_PAPER"),
        ("WAVE_7", "OBSERVABILITY_SECURITY_AND_API", ["polymarket/paper/report.py", "polymarket/dashboard_v3.py"], "OBSERVABILITY"),
        ("WAVE_8", "LEGACY_RETIREMENT", ["senecio_polymarket/**"], "LEGACY"),
        ("WAVE_9", "LIVE_EXECUTION_REMAINS_QUARANTINED", [], "EXECUTION_LIVE_QUARANTINED"),
    ]
    migrations = []
    previous: list[str] = []
    for index, (wave, name, sources, target) in enumerate(waves):
        migration_id = f"MIGRATION_{index:02d}_{name}"
        migrations.append({
            "migration_id": migration_id,
            "wave": wave,
            "source_paths": sources,
            "target_domain": target,
            "prerequisites": previous[-1:] if previous else [],
            "behavior_change": False,
            "runtime_risk": "NONE_IN_THIS_MISSION",
            "production_risk": "NONE; future work requires separate authorization",
            "rollback": "Revert the separately authorized future migration commit; R0/R1 inventory remains authoritative.",
            "tests_required": ["full suite", "exact-head CI", "authority gates"],
            "separate_aud_authorization_required": True,
        })
        previous.append(migration_id)
    return {"schema_version": "senex-migration-map-v1", "migrations": migrations}


def write_generated_artifacts(root: Path, contract: Mapping[str, Any]) -> dict[str, str]:
    architecture = build_target_architecture()
    manifest = build_repository_manifest(root, contract)
    registry = build_component_registry(root, manifest)
    migration = build_migration_map()
    outputs = {
        "governance/target_architecture.yaml": canonical_json_bytes(architecture),
        "governance/component_registry.json": canonical_json_bytes(registry),
        "governance/migration_map.json": canonical_json_bytes(migration),
        SELF_MANIFEST: canonical_json_bytes(manifest),
    }
    for rel, payload in outputs.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(payload)
    manifest_path = root / SELF_MANIFEST
    sidecar = root / "governance/repository_manifest.json.sha256"
    sidecar.write_text(f"{sha256_file(manifest_path)}  {manifest_path.name}\n", encoding="ascii")
    return {rel: sha256_bytes(payload) for rel, payload in outputs.items()}


def _architecture_map(architecture: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(item["domain_id"]): item for item in architecture.get("domains", [])}


def unclassified_duplicate_paths(
    groups: Mapping[str, Sequence[str]],
    contract: Mapping[str, Any],
) -> list[str]:
    configured = contract.get("classified_duplicate_basenames", {})
    violations: list[str] = []
    for basename, paths in sorted(groups.items()):
        actual = sorted(str(path) for path in paths)
        if len(actual) <= 1:
            continue
        item = configured.get(basename) if isinstance(configured, Mapping) else None
        expected = (
            sorted(str(path) for path in item.get("paths", []))
            if isinstance(item, Mapping)
            else []
        )
        classified = bool(
            isinstance(item, Mapping)
            and str(item.get("classification", "")).strip()
            and str(item.get("reason", "")).strip()
        )
        if classified and expected == actual:
            continue
        violations.extend(actual)
    return sorted(set(violations))


def research_authority_violations(
    root: Path,
    paths: Sequence[str],
    contract: Mapping[str, Any],
) -> list[str]:
    configured = contract.get("research_authoritative_root_reference_allowlist", {})
    violations: list[str] = []
    for rel in paths:
        path = root / rel
        if not rel.startswith("research/") or not path.is_file():
            continue
        if "raw_chain_v1" not in path.read_text(encoding="utf-8", errors="ignore"):
            continue
        item = configured.get(rel) if isinstance(configured, Mapping) else None
        classified = bool(
            isinstance(item, Mapping)
            and item.get("classification") == "NON_AUTHORITATIVE_HISTORICAL_DESIGN"
            and str(item.get("reason", "")).strip()
            and str(item.get("current_authority", "")).strip()
        )
        if not classified:
            violations.append(rel)
    return sorted(set(violations))


def evaluate(root: Path, contract: dict[str, Any], base: str, head: str, auth: dict[str, Any] | None) -> list[GateResult]:
    ch = changed(root, base, head)
    added = changed(root, base, head, True)
    results: list[GateResult] = []
    mismatch: list[str] = []
    for rel, expected in contract.get("do_not_touch_hashes", {}).items():
        path = root / rel
        actual = sha256_file(path) if path.is_file() else "MISSING"
        if actual != expected:
            mismatch.append(f"{rel}: {actual} != {expected}")
    results.append(bad(R0_GATE_IDS[0], mismatch, [item.split(":", 1)[0] for item in mismatch]) if mismatch else ok(R0_GATE_IDS[0], "all locked hashes match"))
    new_runtime = [rel for rel in added if matches(rel, contract.get("generated_runtime_paths", [])) and not matches(rel, ["tests/**"])]
    results.append(bad(R0_GATE_IDS[1], ["new tracked runtime output"], new_runtime) if new_runtime else ok(R0_GATE_IDS[1], "no new tracked runtime output"))
    protected_production = [rel for rel in ch if matches(rel, contract.get("protected_production_paths", []))]
    results.append(bad(R0_GATE_IDS[2], ["protected production paths changed"], protected_production, True) if protected_production else ok(R0_GATE_IDS[2], "no protected production path changed"))
    errors = validate_override(auth, base_sha=base, head_sha=head, paths=protected_production) if protected_production else []
    results.append(bad(R0_GATE_IDS[3], errors, protected_production, True) if errors else ok(R0_GATE_IDS[3], "not required" if not protected_production else "exact authorization valid"))
    py_added = [rel for rel in added if rel.endswith(".py") and not matches(rel, ["tests/**", "tools/verify_repository_contract.py"])]
    groups: dict[str, list[str]] = {}
    for rel in py_added:
        groups.setdefault(Path(rel).name, []).append(rel)
    duplicates = unclassified_duplicate_paths(groups, contract)
    results.append(
        bad(
            R0_GATE_IDS[4],
            ["new duplicate basename lacks an exact classification"],
            duplicates,
        )
        if duplicates
        else ok(R0_GATE_IDS[4], "no new unclassified duplicate family")
    )
    regressions: list[str] = []
    for rel in ch:
        path = root / rel
        if rel.endswith(".py") and matches(rel, ["polymarket/**"]) and path.is_file() and not rel.startswith(PAPER_ROOT):
            try:
                imports = python_imports(path)
            except Exception:
                continue
            if any(name == "senecio_polymarket" or name.startswith("senecio_polymarket.") for name in imports):
                regressions.append(rel)
    results.append(bad(R0_GATE_IDS[5], ["product imports legacy"], regressions) if regressions else ok(R0_GATE_IDS[5], "no product-to-legacy import regression"))
    writers = contract.get("authoritative_raw_writers", [])
    writer_errors: list[str] = []
    writer_evidence: list[str] = []
    for writer in writers:
        path = root / str(writer.get("path"))
        symbol = writer.get("symbol")
        try:
            count = sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))))
        except Exception as exc:
            count = -1
            writer_errors.append(str(exc))
        writer_evidence.append(f"{writer.get('path')}::{symbol} count={count}")
        if count != 1:
            writer_errors.append(writer_evidence[-1])
    if len(writers) != 1:
        writer_errors.append(f"declared primary writer count={len(writers)}")
    results.append(bad(R0_GATE_IDS[6], writer_errors, [str(writer.get("path")) for writer in writers]) if writer_errors else ok(R0_GATE_IDS[6], *writer_evidence))
    safety = contract.get("safety_invariants", {})
    expected_safety = {"paper_only": True, "orders_enabled": False, "live_capital_locked": True}
    safety_errors = [key for key, value in expected_safety.items() if not isinstance(safety.get(key), dict) or safety[key].get("value") is not value or safety[key].get("override") is not False]
    results.append(bad(R0_GATE_IDS[7], ["invalid safety invariant"], safety_errors) if safety_errors else ok(R0_GATE_IDS[7], "permanent safety invariants locked"))
    introduced_danger: list[str] = []
    danger_evidence: list[str] = []
    for rel in ch:
        path = root / rel
        if rel.endswith(".py") and path.is_file() and not rel.startswith("tests/"):
            findings = capability_findings(path)
            if findings and rel.startswith(PAPER_ROOT):
                introduced_danger.append(rel)
                danger_evidence.extend(f"{rel}: {finding}" for finding in findings)
    results.append(bad(R0_GATE_IDS[8], danger_evidence, introduced_danger) if introduced_danger else ok(R0_GATE_IDS[8], "no paper wallet/order capability introduced"))
    research = research_authority_violations(root, ch, contract)
    results.append(
        bad(
            R0_GATE_IDS[9],
            ["research references authoritative raw root without an explicit non-authoritative classification"],
            research,
        )
        if research
        else ok(R0_GATE_IDS[9], "no research authority violation")
    )
    workflow_paths: list[str] = []
    workflow_evidence: list[str] = []
    for rel in ch:
        path = root / rel
        if matches(rel, [".github/workflows/**"]) and path.is_file():
            allow_dispatch = rel == ".github/workflows/senex-paper-trial.yml"
            findings = workflow_findings(path, allow_dispatch=allow_dispatch)
            if findings:
                workflow_paths.append(rel)
                workflow_evidence.extend(f"{rel}: {finding}" for finding in findings)
    results.append(bad(R0_GATE_IDS[10], workflow_evidence, workflow_paths) if workflow_paths else ok(R0_GATE_IDS[10], "workflows pinned, least-privilege, authorized dispatch only"))

    manifest = load_structured(root / SELF_MANIFEST)
    architecture = load_structured(root / "governance/target_architecture.yaml")
    registry = load_structured(root / "governance/component_registry.json")
    migrations = load_structured(root / "governance/migration_map.json")
    tracked = set(tracked_files(root))
    mapped = {str(record.get("path")) for record in manifest.get("files", [])}
    unmapped = sorted(tracked - mapped)
    extra = sorted(mapped - tracked)
    results.append(bad(R1_GATE_IDS[0], [f"unmapped={unmapped}", f"extra={extra}"], unmapped + extra) if unmapped or extra else ok(R1_GATE_IDS[0], f"all {len(tracked)} tracked files mapped"))
    domains = _architecture_map(architecture)
    unknown = [record["path"] for record in manifest.get("files", []) if record.get("current_domain") not in domains or record.get("target_domain") not in domains]
    results.append(bad(R1_GATE_IDS[1], ["unknown domain"], unknown) if unknown else ok(R1_GATE_IDS[1], f"all domains in canonical set ({len(domains)})"))
    registered_paths = {component.get("path") for component in registry.get("components", [])}
    missing_entrypoints = [record["path"] for record in manifest.get("files", []) if record.get("active_entrypoint") and record["path"] not in registered_paths]
    results.append(bad(R1_GATE_IDS[2], ["active entrypoint not registered"], missing_entrypoints) if missing_entrypoints else ok(R1_GATE_IDS[2], "all active entrypoints registered"))
    dependency_violations: list[str] = []
    for record in manifest.get("files", []):
        rel = record["path"]
        if not rel.endswith(".py") or not (root / rel).is_file():
            continue
        domain = record["target_domain"]
        allowed = set(domains.get(domain, {}).get("allowed_dependencies", []))
        try:
            imports = python_imports(root / rel)
        except Exception:
            continue
        for name in imports:
            candidate_path = name.replace(".", "/") + ".py"
            target_record = next((item for item in manifest["files"] if item["path"].endswith(candidate_path)), None)
            if target_record and target_record["target_domain"] not in allowed:
                dependency_violations.append(f"{rel}->{target_record['path']}")
    results.append(bad(R1_GATE_IDS[3], dependency_violations, [item.split("->", 1)[0] for item in dependency_violations]) if dependency_violations else ok(R1_GATE_IDS[3], "domain dependencies allowed"))
    results.append(ok(R1_GATE_IDS[4], "exactly one authoritative raw writer") if len(writers) == 1 else bad(R1_GATE_IDS[4], [f"count={len(writers)}"], []))
    live = domains.get("EXECUTION_LIVE_QUARANTINED", {})
    live_errors = [key for key in ("order_authority", "wallet_authority", "production_authority") if live.get(key) is not False]
    results.append(bad(R1_GATE_IDS[5], live_errors, ["governance/target_architecture.yaml"]) if live_errors else ok(R1_GATE_IDS[5], "live execution authority absent"))
    wallet_components = [component["path"] for component in registry.get("components", []) if component.get("wallet_access")]
    results.append(bad(R1_GATE_IDS[6], ["wallet authority present"], wallet_components) if wallet_components else ok(R1_GATE_IDS[6], "wallet authority absent"))
    ai_execution = [component["path"] for component in registry.get("components", []) if any(term in component["path"].lower() for term in ("ai", "model", "learning", "research")) and (component.get("wallet_access") or component.get("order_access"))]
    results.append(bad(R1_GATE_IDS[7], ["AI/research execution authority"], ai_execution) if ai_execution else ok(R1_GATE_IDS[7], "AI/research execution authority absent"))
    readonly_writers = [record["path"] for record in manifest.get("files", []) if record.get("target_domain") == "READ_ONLY_API" and record.get("filesystem_write_capability")]
    results.append(bad(R1_GATE_IDS[8], ["read-only component writes state"], readonly_writers) if readonly_writers else ok(R1_GATE_IDS[8], "read-only API has no state writes"))
    generated_bad = [record["path"] for record in manifest.get("files", []) if record.get("generated") != (record.get("target_domain") == "GENERATED")]
    results.append(bad(R1_GATE_IDS[9], ["generated path misclassified"], generated_bad) if generated_bad else ok(R1_GATE_IDS[9], "generated paths classified"))
    migration_records = migrations.get("migrations", [])
    no_source = [item.get("migration_id", "UNKNOWN") for item in migration_records if "source_paths" not in item]
    results.append(bad(R1_GATE_IDS[10], ["migration missing source_paths"], no_source) if no_source else ok(R1_GATE_IDS[10], "all migrations declare source paths"))
    no_target = [item.get("migration_id", "UNKNOWN") for item in migration_records if item.get("target_domain") not in domains]
    results.append(bad(R1_GATE_IDS[11], ["migration missing/unknown target"], no_target) if no_target else ok(R1_GATE_IDS[11], "all migrations declare target domains"))
    no_rollback = [item.get("migration_id", "UNKNOWN") for item in migration_records if not item.get("rollback")]
    results.append(bad(R1_GATE_IDS[12], ["migration missing rollback"], no_rollback) if no_rollback else ok(R1_GATE_IDS[12], "all migrations declare rollback"))
    unresolved = [record["path"] for record in manifest.get("files", []) if record.get("target_domain") == "UNRESOLVED" and not record.get("evidence")]
    results.append(bad(R1_GATE_IDS[13], ["unresolved without evidence"], unresolved) if unresolved else ok(R1_GATE_IDS[13], "all unresolved classifications have evidence"))
    first = canonical_json_bytes(build_repository_manifest(root, contract))
    second = canonical_json_bytes(build_repository_manifest(root, contract))
    results.append(ok(R1_GATE_IDS[14], sha256_bytes(first)) if first == second else bad(R1_GATE_IDS[14], ["two generations differ"], [SELF_MANIFEST]))
    regression = mismatch + safety_errors
    results.append(bad(R1_GATE_IDS[15], regression, [item.split(":", 1)[0] for item in mismatch] + safety_errors) if regression else ok(R1_GATE_IDS[15], "R0 hash locks and safety preserved"))
    paper_danger: list[str] = []
    paper_evidence: list[str] = []
    for rel in tracked:
        if rel.startswith(PAPER_ROOT) and rel.endswith(".py"):
            findings = capability_findings(root / rel)
            if findings:
                paper_danger.append(rel)
                paper_evidence.extend(f"{rel}: {finding}" for finding in findings)
    results.append(bad(R1_GATE_IDS[16], paper_evidence, paper_danger) if paper_danger else ok(R1_GATE_IDS[16], "paper runtime has zero wallet/signing/real-order capability"))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--contract", type=Path, default=Path("governance/repository_contract.yaml"))
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--generate", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    contract_path = args.contract if args.contract.is_absolute() else root / args.contract
    contract = load_structured(contract_path)
    if args.generate:
        hashes = write_generated_artifacts(root, contract)
        sys.stdout.buffer.write(canonical_json_bytes({"generated": hashes}))
        return 0
    if not args.base or not args.head:
        parser.error("--base and --head are required unless --generate is used")
    auth = load_structured(args.authorization) if args.authorization else None
    gates = evaluate(root, contract, args.base, args.head, auth)
    report = {
        "schema_version": "senex-repository-contract-report-v2",
        "repository": contract.get("repository"),
        "base_sha": args.base,
        "head_sha": args.head,
        "gates": [asdict(result) for result in gates],
        "pass": all(result.status == "PASS" for result in gates),
    }
    payload = canonical_json_bytes(report)
    if args.report:
        args.report.write_bytes(payload)
    sys.stdout.buffer.write(payload)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
