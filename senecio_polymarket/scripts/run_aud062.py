"""CLI for the deterministic AUD-062 forensic evidence bundle."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

from backend.research.aud062_forensics import canonical_json, file_hash, write_artifacts


PUBLIC_BASE = "https://h011-web--senecio-h011--wbjggn89fnf8.code.run"


def _bytes_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_gzip_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json(payload).encode("utf-8")
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            compressed.write(encoded)


def _read_gzip_json(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("AUD-062 input bundle must be a JSON object")
    return payload


def _canonical_hash(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _provenance(rows: list, *, source_class: str, captured_at: str,
                source: str, raw_or_derived: str, transformation: str) -> dict:
    return {
        "SOURCE_CLASS": source_class,
        "CAPTURE_TIME_UTC": captured_at,
        "SOURCE_ENDPOINT_OR_CLASS": source,
        "RAW_OR_DERIVED": raw_or_derived,
        "TRANSFORMATION": transformation,
        "ROW_COUNT": len(rows),
        "SHA256": _canonical_hash(rows),
    }


def _pick(value: dict, keys: tuple[str, ...]) -> dict:
    return {key: value.get(key) for key in keys if key in value}


def _sanitize_external(external: dict) -> dict:
    if not isinstance(external, dict):
        return {}
    result = _pick(external, ("version", "polymarket_model_edge_v1", "kalshi_cross_venue_v1"))
    poly = external.get("polymarket") or {}
    if isinstance(poly, dict):
        result["polymarket"] = _pick(poly, (
            "source", "version", "status", "observed_at", "eligible_for_prediction",
            "ws_connected", "slug", "start_ts", "end_ts", "seconds_to_close",
            "freshness_s", "up_probability", "down_probability", "directional_pressure",
            "up", "down",
        ))
    kalshi = external.get("kalshi") or {}
    if isinstance(kalshi, dict):
        clean = _pick(kalshi, (
            "source", "version", "status", "observed_at", "directional_use",
            "purpose", "horizon", "freshness_s",
        ))
        market = kalshi.get("market") or {}
        if isinstance(market, dict):
            clean["market"] = _pick(market, (
                "ticker", "event_ticker", "series_ticker", "close_time", "status",
                "horizon", "yes_probability", "no_probability", "yes_bid", "yes_ask",
                "no_bid", "no_ask", "yes_spread", "directional_use", "source",
            ))
        result["kalshi"] = clean
    boros = external.get("boros") or {}
    if isinstance(boros, dict):
        clean = _pick(boros, (
            "source", "version", "status", "observed_at", "directional_use",
            "purpose", "freshness_s",
        ))
        markets = boros.get("markets") or []
        clean["markets"] = [
            _pick(item, (
                "market_id", "symbol", "underlying_symbol", "funding_rate_symbol",
                "mark_apr", "mid_apr", "maturity", "next_settlement_time",
                "payment_period_s",
            ))
            for item in markets if isinstance(item, dict)
        ]
        result["boros"] = clean
    return result


def _sanitize_prediction(row: dict) -> dict:
    audit = row.get("audit") or {}
    clean_audit = _pick(audit, (
        "action_vector", "candle_ts", "exchange_used", "execution_state",
        "origin_price_v1", "outcomes_dual", "pipeline", "confidence_semantics_v1",
        "decision_replay_v1", "decision_waterfall_v1",
    ))
    clean_audit["external_markets_v1"] = _sanitize_external(audit.get("external_markets_v1") or {})
    return {
        **_pick(row, (
            "id", "ts", "symbol", "prediction", "confidence", "outcome",
            "price_now", "price_15m_later", "exchange_used",
        )),
        "audit": clean_audit,
    }


def _sanitize_bundle(bundle: dict) -> dict:
    captured_at = str((bundle.get("observation") or {}).get("captured_at") or "UNKNOWN")
    predictions = [
        _sanitize_prediction(row)
        for row in ((bundle.get("predictions_payload") or {}).get("predictions") or [])
        if isinstance(row, dict)
    ]
    resolution_source = bundle.get("polymarket_resolutions") or {}
    resolutions = [
        _pick(item, (
            "slug", "closed", "market_closed", "end_date", "outcomes",
            "outcome_prices", "resolved_winner", "resolution_source", "http_status",
        ))
        for item in (resolution_source.get("results") or []) if isinstance(item, dict)
    ]
    market_context = bundle.get("market_context") or {}
    clean_market = _pick(market_context, ("mode", "synthetic_demo_enabled", "safety"))
    for source in ("polymarket", "kalshi", "boros"):
        value = market_context.get(source) or {}
        if isinstance(value, dict):
            clean_market[source] = _pick(value, (
                "source", "status", "read_only", "freshness_s", "purpose", "horizon",
                "directional_use", "directional_pressure", "depth_used_for_pressure",
                "eligible_for_prediction", "seconds_to_close", "ws_connected",
            ))
    oracle = market_context.get("oracle") or {}
    if isinstance(oracle, dict):
        clean_market["oracle"] = _pick(oracle, (
            "trade_mode", "live_capital_locked", "predictions_count",
            "last_prediction_symbol", "last_prediction_ts", "directional_stats",
        ))
    scores = {}
    for symbol, score in (bundle.get("scores") or {}).items():
        if isinstance(score, dict):
            scores[symbol] = _pick(score, (
                "requested_symbol", "score_status", "authoritative_score_pct",
                "proof_qualified_rows_raw", "independent_1h_rows", "authority_cohort",
                "authority_horizon_seconds", "authority_1h", "observed_win_rate_pct",
                "observed_win_rate_diagnostic_only", "confidence_semantics",
                "confidence_probability_semantics", "reasons", "trade_mode",
                "orders_enabled", "live_capital_locked", "short_only_paper_mode",
                "gates", "quality",
            ))
    provenance = {
        "predictions_payload.predictions": _provenance(
            predictions, source_class="PUBLIC_RUNTIME", captured_at=captured_at,
            source=f"{PUBLIC_BASE}/api/oracle/predictions/db?limit=500",
            raw_or_derived="RAW_MINIMIZED",
            transformation="ALLOWLIST_FIELDS_REMOVE_ENRICHED_DUPLICATES_AND_UNRELATED_METADATA",
        ),
        "polymarket_resolutions.results": _provenance(
            resolutions, source_class="PUBLIC_MARKET_SOURCE", captured_at=captured_at,
            source="https://gamma-api.polymarket.com/events/slug/{slug}",
            raw_or_derived="RAW_MINIMIZED",
            transformation="ALLOWLIST_RESOLUTION_AND_SETTLEMENT_FIELDS_ONLY",
        ),
    }
    return {
        "observation": bundle.get("observation") or {},
        "input_hashes": bundle.get("input_hashes") or {},
        "dataset_provenance": provenance,
        "predictions_payload": {
            "source": (bundle.get("predictions_payload") or {}).get("source"),
            "count": len(predictions),
            "total_in_db": (bundle.get("predictions_payload") or {}).get("total_in_db"),
            "predictions": predictions,
        },
        "market_context": clean_market,
        "scores": scores,
        "polymarket_resolutions": {
            "source": resolution_source.get("source"),
            "requested": resolution_source.get("requested"),
            "results": resolutions,
        },
        "governance": bundle.get("governance") or {},
        "sanitization": {
            "version": "AUD-062-input-minimization-v1",
            "removed_prediction_fields": ["created_at", "ev", "audit.enriched"],
            "removed_unrelated_market_metadata": True,
            "raw_source_hashes_preserved": True,
        },
    }


def sanitize(args: argparse.Namespace) -> None:
    bundle = _read_gzip_json(args.input)
    sanitized = _sanitize_bundle(bundle)
    _write_gzip_json(args.output, sanitized)
    print(json.dumps({
        "status": "SANITIZED",
        "output": str(args.output),
        "rows": len(sanitized["predictions_payload"]["predictions"]),
        "bundle_sha256": _bytes_hash(args.output),
    }, sort_keys=True))


def capture(args: argparse.Namespace) -> None:
    predictions = _load(args.predictions)
    market = _load(args.market_context)
    btc_score = _load(args.btc_score)
    eth_score = _load(args.eth_score)
    resolutions = _load(args.polymarket_resolutions)
    paths = {
        "predictions": args.predictions,
        "market_context": args.market_context,
        "btc_score": args.btc_score,
        "eth_score": args.eth_score,
        "polymarket_resolutions": args.polymarket_resolutions,
    }
    bundle = {
        "observation": {
            "captured_at": args.captured_at,
            "read_only": True,
            "endpoints": [
                f"{PUBLIC_BASE}/api/oracle/predictions/db?limit=500",
                f"{PUBLIC_BASE}/api/market-context",
                f"{PUBLIC_BASE}/api/oracle/score?symbol=BTCUSDT",
                f"{PUBLIC_BASE}/api/oracle/score?symbol=ETHUSDT",
                "https://gamma-api.polymarket.com/events/slug/{slug}",
                "https://api.github.com/repos/simonkey888/SeneX-Prophet/branches/main",
                "https://api.github.com/repos/simonkey888/SeneX-Prophet/rulesets",
                "https://api.github.com/repos/simonkey888/SeneX-Prophet/deployments?sha=49c5f0a69609c005da80e48b585e91d8582a5ac6",
                "https://api.github.com/repos/simonkey888/SeneX-Prophet/commits/49c5f0a69609c005da80e48b585e91d8582a5ac6/status",
            ],
        },
        "input_hashes": {name: _bytes_hash(path) for name, path in paths.items()},
        "predictions_payload": predictions,
        "market_context": market,
        "scores": {"BTCUSDT": btc_score, "ETHUSDT": eth_score},
        "polymarket_resolutions": resolutions,
        "governance": {
            "observed_at": args.captured_at,
            "main_sha": "49c5f0a69609c005da80e48b585e91d8582a5ac6",
            "main_tree": "3e323bcc2795f97b29242883d3bf2a015c092ccd",
            "branch_protected": False,
            "ruleset_count": 0,
            "required_status_checks": [],
            "northflank_status_context": "northflank/simondalmassos-team/seneciobot/senecio-h011",
            "deployed_sha": "49c5f0a69609c005da80e48b585e91d8582a5ac6",
            "deployment_id": 5913306065,
            "build": "rugged-pump-6360",
            "auto_cd_evidence": "Northflank bot deployment/status for exact main SHA plus configured main tracking from AUD-061-LAND checkpoint",
            "current_contents_write_workflow": ".github/workflows/oracle.yml",
        },
    }
    _write_gzip_json(args.output, bundle)
    print(json.dumps({"status": "CAPTURED", "output": str(args.output), "rows": len(predictions.get("predictions") or []), "bundle_sha256": _bytes_hash(args.output)}, sort_keys=True))


def analyze(args: argparse.Namespace) -> None:
    bundle = _read_gzip_json(args.input)
    manifest = write_artifacts(bundle, args.output)
    from scripts.aud062_publication_scan import scan as publication_scan

    repo_root = Path(__file__).resolve().parents[2]
    report_path = args.output / "aud-062-publication-sanitization.json"
    manifest_path = args.output / "aud-062-manifest.json"

    def refresh_manifest() -> dict:
        current = json.loads(manifest_path.read_text(encoding="utf-8"))
        current["artifact_hashes"] = {
            path.name: file_hash(path)
            for path in sorted(args.output.glob("aud-062-*"))
            if path.name != "aud-062-manifest.json"
        }
        manifest_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return current

    for _ in range(4):
        manifest = refresh_manifest()
        report = publication_scan(repo_root, args.output, args.input)
        encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
        previous = report_path.read_text(encoding="utf-8") if report_path.exists() else None
        report_path.write_text(encoded, encoding="utf-8")
        if previous == encoded:
            break
    else:  # pragma: no cover - deterministic fixed-point guard
        raise RuntimeError("publication scan report did not converge")
    manifest = refresh_manifest()
    required = (
        report["PUBLICATION_SECRET_SCAN"], report["PUBLICATION_PII_REVIEW"],
        report["PUBLICATION_SCOPE_REVIEW"],
    )
    if any(value != "PASS" for value in required):
        raise RuntimeError("AUD-062 publication sanitization gate failed")
    print(json.dumps({
        "status": "ANALYZED",
        "total_decisions": manifest["row_counts"]["total_decisions"],
        "post_deploy_decisions": manifest["row_counts"]["post_deploy_decisions"],
        "finding_count": manifest["finding_count"],
        "material_finding_count": manifest["material_finding_count"],
        "output": str(args.output),
    }, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture_parser = sub.add_parser("capture", help="Freeze already-downloaded public read-only inputs")
    capture_parser.add_argument("--predictions", type=Path, required=True)
    capture_parser.add_argument("--market-context", type=Path, required=True)
    capture_parser.add_argument("--btc-score", type=Path, required=True)
    capture_parser.add_argument("--eth-score", type=Path, required=True)
    capture_parser.add_argument("--polymarket-resolutions", type=Path, required=True)
    capture_parser.add_argument("--captured-at", required=True)
    capture_parser.add_argument("--output", type=Path, required=True)
    capture_parser.set_defaults(func=capture)
    analyze_parser = sub.add_parser("analyze", help="Generate deterministic forensic artifacts")
    analyze_parser.add_argument("--input", type=Path, required=True)
    analyze_parser.add_argument("--output", type=Path, required=True)
    analyze_parser.set_defaults(func=analyze)
    sanitize_parser = sub.add_parser("sanitize", help="Apply the bounded AUD-062 publication minimization gate")
    sanitize_parser.add_argument("--input", type=Path, required=True)
    sanitize_parser.add_argument("--output", type=Path, required=True)
    sanitize_parser.set_defaults(func=sanitize)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
