"""Fail-closed publication scanner for the bounded AUD-062 evidence set.

The report contains counts and classifications only.  Candidate values are
never printed or serialized.  Gzip inputs are decompressed in memory before
both deterministic-pattern and high-entropy scanning.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


SCANNER_VERSION = "AUD-062-publication-scan-v1"
DETECT_SECRETS_VERSION = "1.5.0"
BASE_SHA = "49c5f0a69609c005da80e48b585e91d8582a5ac6"
SOURCE_PATHS = (
    ".github/workflows/aud-062-forensics.yml",
    "senecio_polymarket/backend/research/aud062_forensics.py",
    "senecio_polymarket/scripts/run_aud062.py",
    "senecio_polymarket/scripts/aud062_publication_scan.py",
    "senecio_polymarket/tests/test_aud_062.py",
    "senecio_polymarket/docs/AUD-062-REPORT.md",
    "senecio_polymarket/oracle/institutional_core.py",
    "senecio_polymarket/oracle/survivability.py",
    "senecio_polymarket/oracle/exchange_connector.py",
    "senecio_polymarket/oracle_runtime/institutional_core.py",
    "senecio_polymarket/oracle_runtime/predict_only.py",
)
SENSITIVE_QUERY_KEYS = {
    "access_token", "api_key", "apikey", "auth", "authorization", "code",
    "credential", "key", "oauth_token", "password", "secret", "signature",
    "sig", "token", "x-amz-credential", "x-amz-signature", "x-goog-signature",
}
SENSITIVE_JSON_KEYS = {
    "authorization", "cookie", "set-cookie", "password", "passwd",
    "northflank_api_token", "github_token", "supabase_key", "service_role_key",
    "private_key", "seed_phrase", "mnemonic", "session_id", "access_token",
    "refresh_token", "client_secret",
}
PII_JSON_KEYS = {
    "email", "email_address", "phone", "phone_number", "full_name", "first_name",
    "last_name", "street_address", "ip_address", "customer_id", "user_id",
    "wallet_address", "national_id", "tax_id",
}
SAFE_SENTINELS = {"", "none", "null", "not_present", "redacted", "pass", "false"}
PUBLIC_HOSTS = {
    "api.github.com",
    "data.chain.link",
    "gamma-api.polymarket.com",
    "h011-web--senecio-h011--wbjggn89fnf8.code.run",
}
SEMANTIC_DIGEST_HINTS = (
    "hash", "sha", "tree", "lineage", "commit", "expected_base",
    "expected_tree", "deployed_sha", "condition_id", "blob",
)


def _json_payload(path: Path) -> object | None:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                return json.load(handle)
        if path.suffix == ".json":
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return None


def _walk(value: object, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, (*path, str(index)))
    else:
        yield path, value


def _safe_value(value: object) -> bool:
    if value is None or value is False:
        return True
    return str(value).strip().lower() in SAFE_SENTINELS


def _pattern_scan(paths: list[Path]) -> dict[str, int]:
    counts = {
        "sensitive_json_key_values": 0,
        "authorization_headers": 0,
        "cookie_headers": 0,
        "private_key_material": 0,
        "signed_or_credential_urls": 0,
        "url_userinfo_credentials": 0,
    }
    pem_prefix = "-" * 5 + "BEGIN "
    pem_suffix = "PRIVATE KEY" + "-" * 5
    bearer = re.compile(r"(?im)^\s*(?:authorization)\s*:\s*(?:basic|bearer)\s+[^\s${}{]+")
    cookie = re.compile(r"(?im)^\s*(?:cookie|set-cookie)\s*:\s*[^\s${}{]+")
    url_re = re.compile(r"https?://[^\s\"'<>]+")
    for path in paths:
        try:
            raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, EOFError, UnicodeDecodeError):
            continue
        payload = _json_payload(path)
        if payload is not None:
            for key_path, value in _walk(payload):
                key = key_path[-1].lower() if key_path else ""
                if key in SENSITIVE_JSON_KEYS and not _safe_value(value):
                    counts["sensitive_json_key_values"] += 1
        counts["authorization_headers"] += len(bearer.findall(text))
        counts["cookie_headers"] += len(cookie.findall(text))
        counts["private_key_material"] += text.count(pem_prefix + pem_suffix)
        for match in url_re.finditer(text):
            try:
                parsed = urlsplit(match.group(0).rstrip(".,);]"))
            except ValueError:
                counts["signed_or_credential_urls"] += 1
                continue
            if parsed.username or parsed.password:
                counts["url_userinfo_credentials"] += 1
            if any(key.lower() in SENSITIVE_QUERY_KEYS for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
                counts["signed_or_credential_urls"] += 1
    return counts


def _pii_and_scope_review(bundle: dict, payloads: list[object]) -> tuple[dict[str, int], list[str]]:
    pii = {"sensitive_pii_keys": 0, "email_values": 0}
    email_re = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    for payload in payloads:
        for key_path, value in _walk(payload):
            key = key_path[-1].lower() if key_path else ""
            if key in PII_JSON_KEYS and not _safe_value(value):
                pii["sensitive_pii_keys"] += 1
            if isinstance(value, str) and email_re.match(value):
                pii["email_values"] += 1

    failures: list[str] = []
    allowed_top = {
        "observation", "input_hashes", "dataset_provenance", "predictions_payload",
        "market_context", "scores", "polymarket_resolutions", "governance", "sanitization",
    }
    if set(bundle) - allowed_top:
        failures.append("UNRELATED_TOP_LEVEL_DATA")
    rows = (bundle.get("predictions_payload") or {}).get("predictions") or []
    if len(rows) != 348:
        failures.append("PREDICTION_ROW_COUNT")
    if any("enriched" in ((row.get("audit") or {})) for row in rows if isinstance(row, dict)):
        failures.append("UNRELATED_ENRICHED_METADATA")
    if any(key in row for row in rows if isinstance(row, dict) for key in ("created_at", "ev")):
        failures.append("DUPLICATE_UNMINIMIZED_FIELDS")
    provenance = bundle.get("dataset_provenance") or {}
    required_provenance = {
        "SOURCE_CLASS", "CAPTURE_TIME_UTC", "SOURCE_ENDPOINT_OR_CLASS",
        "RAW_OR_DERIVED", "TRANSFORMATION", "ROW_COUNT", "SHA256",
    }
    if not provenance or any(required_provenance - set(item) for item in provenance.values() if isinstance(item, dict)):
        failures.append("DATASET_PROVENANCE_INCOMPLETE")
    for payload in payloads:
        for _, value in _walk(payload):
            if not isinstance(value, str):
                continue
            if "PRIVATE" in value.upper() or "PROPRIETARY" in value.upper() or "PAID_FEED" in value.upper():
                failures.append("NONPUBLIC_SOURCE_LABEL")
            for url in re.findall(r"https?://[^\s\"'<>]+", value):
                try:
                    host = urlsplit(url.rstrip(".,);]")).hostname
                except ValueError:
                    host = None
                if host and host not in PUBLIC_HOSTS:
                    failures.append("NON_ALLOWLIST_SOURCE_HOST")
    return pii, sorted(set(failures))


def _digest_false_positive(secret_value: str, line: str, known_digests: set[str] | None = None) -> bool:
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", secret_value):
        return False
    if known_digests and secret_value.lower() in known_digests:
        return True
    lowered = line.lower()
    return any(hint in lowered for hint in SEMANTIC_DIGEST_HINTS)


def _known_digest_values(paths: list[Path]) -> set[str]:
    values: set[str] = set()
    for path in paths:
        payload = _json_payload(path)
        if payload is None:
            continue
        for key_path, value in _walk(payload):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
                continue
            lowered = tuple(part.lower() for part in key_path)
            if (
                any(part in {"artifact_hashes", "input_hashes"} for part in lowered)
                or lowered[-1:] == ("sha256",)
                or any(any(hint in part for hint in SEMANTIC_DIGEST_HINTS) for part in lowered)
            ):
                values.add(value.lower())
    return values


def _scanner_status_false_positive(filename: str, secret_type: str, line: str) -> bool:
    if secret_type != "Secret Keyword":
        return False
    name = Path(filename).name
    if name in {"aud062_publication_scan.py", "aud-062-publication-sanitization.json"}:
        return "PUBLICATION_" in line or line.strip() == 'if secret_type != "Secret Keyword":'
    # detect-secrets treats the GitHub Actions mapping key ``run: |`` as a
    # secret keyword. Match only that exact workflow syntax; no value exists.
    return name == "aud-062-forensics.yml" and line.strip() == "run: |"


def _entropy_scan(paths: list[Path]) -> dict[str, object]:
    try:
        from detect_secrets import SecretsCollection
        from detect_secrets.settings import default_settings
    except ImportError:
        return {"scanner": "detect-secrets", "version": None, "status": "FAIL", "reason": "DEPENDENCY_MISSING"}

    candidates = 0
    digest_false_positives = 0
    scanner_status_false_positives = 0
    confirmed = 0
    known_digests = _known_digest_values(paths)
    with tempfile.TemporaryDirectory() as directory:
        scan_paths: list[Path] = []
        for index, path in enumerate(paths):
            if path.suffix == ".gz":
                extracted = Path(directory) / f"decompressed-{index}.json"
                try:
                    extracted.write_bytes(gzip.decompress(path.read_bytes()))
                except (OSError, EOFError):
                    confirmed += 1
                    continue
                scan_paths.append(extracted)
            elif path.suffix not in {".png", ".jpg", ".jpeg", ".pdf"}:
                scan_paths.append(path)
        with default_settings():
            collection = SecretsCollection()
            for path in scan_paths:
                collection.scan_file(str(path))
        for filename, secrets in collection.data.items():
            lines = Path(filename).read_text(encoding="utf-8").splitlines()
            for secret in secrets:
                candidates += 1
                line = lines[secret.line_number - 1] if 0 < secret.line_number <= len(lines) else ""
                value = secret.secret_value or ""
                if _digest_false_positive(value, line, known_digests):
                    digest_false_positives += 1
                elif _scanner_status_false_positive(filename, secret.type, line):
                    scanner_status_false_positives += 1
                else:
                    confirmed += 1
    return {
        "scanner": "detect-secrets",
        "version": DETECT_SECRETS_VERSION,
        "network_verification": False,
        "candidate_count": candidates,
        "reviewed_digest_false_positive_count": digest_false_positives,
        "reviewed_scanner_status_false_positive_count": scanner_status_false_positives,
        "confirmed_secret_count": confirmed,
        "status": "PASS" if confirmed == 0 else "FAIL",
    }


def scan(repo_root: Path, evidence_dir: Path, input_path: Path) -> dict:
    paths = [repo_root / path for path in SOURCE_PATHS]
    paths.extend(sorted(evidence_dir.glob("aud-062-*")))
    paths.append(input_path)
    paths = sorted({path.resolve() for path in paths if path.is_file()})
    pattern = _pattern_scan(paths)
    bundle = _json_payload(input_path)
    if not isinstance(bundle, dict):
        bundle = {}
    payloads = [payload for payload in (_json_payload(path) for path in paths) if payload is not None]
    pii, scope_failures = _pii_and_scope_review(bundle, payloads)
    entropy = _entropy_scan(paths)
    pattern_confirmed = sum(pattern.values())
    secret_pass = pattern_confirmed == 0 and entropy.get("status") == "PASS"
    pii_pass = sum(pii.values()) == 0
    scope_pass = not scope_failures
    return {
        "version": SCANNER_VERSION,
        "authorization_comment": 5298610051,
        "base_sha": BASE_SHA,
        "capture_time_utc": (bundle.get("observation") or {}).get("captured_at"),
        "files_scanned": len(paths),
        "decompressed_archive_count": sum(path.suffix == ".gz" for path in paths),
        "scanners": [
            {
                "scanner": "aud062-deterministic-pattern-scan",
                "version": SCANNER_VERSION,
                "counts": pattern,
                "confirmed_secret_count": pattern_confirmed,
                "status": "PASS" if pattern_confirmed == 0 else "FAIL",
            },
            entropy,
        ],
        "pii_review": {"counts": pii, "status": "PASS" if pii_pass else "FAIL"},
        "scope_review": {"failure_categories": scope_failures, "status": "PASS" if scope_pass else "FAIL"},
        "PUBLICATION_SECRET_SCAN": "PASS" if secret_pass else "FAIL",
        "PUBLICATION_PII_REVIEW": "PASS" if pii_pass else "FAIL",
        "PUBLICATION_SCOPE_REVIEW": "PASS" if scope_pass else "FAIL",
        "PUBLICATION_NONPUBLIC_DATA": "NONE" if scope_pass else "DETECTED",
        "PUBLICATION_AUTH_HEADERS": "NONE" if pattern["authorization_headers"] == 0 else "DETECTED",
        "PUBLICATION_CREDENTIALS": "NONE" if secret_pass else "DETECTED",
        "detected_values_serialized": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = scan(args.repo_root.resolve(), args.evidence_dir.resolve(), args.input.resolve())
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    required = (
        report["PUBLICATION_SECRET_SCAN"], report["PUBLICATION_PII_REVIEW"],
        report["PUBLICATION_SCOPE_REVIEW"],
    )
    if any(value != "PASS" for value in required):
        raise SystemExit(1)
    print(json.dumps({
        "status": "PASS",
        "files_scanned": report["files_scanned"],
        "secret_candidates": sum(item.get("candidate_count", 0) for item in report["scanners"]),
        "confirmed_secrets": sum(item.get("confirmed_secret_count", 0) for item in report["scanners"]),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
