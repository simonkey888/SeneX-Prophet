"""Generate a truthful, read-only monitoring site from verified artifacts only."""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
from typing import Any, Mapping

MONITORING_CONTRACT_VERSION = "SENEX_MONITORING_TRUTH_V1"
UNKNOWN = "UNKNOWN"
PENDING = "PENDING"
UNVERIFIED = "UNVERIFIED"


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _value(source: Mapping[str, Any], key: str, *, missing: str = UNKNOWN) -> Any:
    value = source.get(key)
    return missing if value is None else value


def build_monitoring_model(
    *,
    execution_manifest: Mapping[str, Any],
    trial_summary: Mapping[str, Any],
    source_time_audit: Mapping[str, Any],
    fee_report: Mapping[str, Any],
    sequential_report: Mapping[str, Any],
    settlement_report: Mapping[str, Any],
    risk_authority_map: Mapping[str, Any],
    artifact_digest: str,
    generated_at_utc: str,
) -> dict[str, Any]:
    """Project source artifacts without creating an alternative truth model."""
    safety = dict(execution_manifest.get("safety_invariants") or {})
    positions = list(settlement_report.get("positions") or [])
    scenarios = list(sequential_report.get("scenarios") or [])
    leg_incidents = [item for item in scenarios if item.get("completion_status") != "BOTH_LEGS_FILL"]
    model = {
        "contract_version": MONITORING_CONTRACT_VERSION,
        "provenance": {
            "code_sha": _value(execution_manifest, "code_sha"),
            "tree_sha": _value(execution_manifest, "tree_sha"),
            "config_sha": _value(execution_manifest, "config_sha"),
            "experiment_id": _value(execution_manifest, "experiment_id"),
            "trial_id": _value(trial_summary, "trial_id"),
            "artifact_digest": artifact_digest,
            "generation_time_utc": generated_at_utc,
        },
        "safety": {
            "paper_only": safety.get("paper_only", UNVERIFIED),
            "orders_enabled": safety.get("orders_enabled", UNVERIFIED),
            "live_capital_locked": safety.get("live_capital_locked", UNVERIFIED),
            "profitability_statement": "PROFITABILITY_NOT_ESTABLISHED",
            "real_order_controls": 0,
            "mutating_controls": 0,
        },
        "overview": {
            "run_state": _value(trial_summary, "run_state", missing="COMPLETED_FIXTURE_VALIDATION"),
            "last_verified_observation": _value(source_time_audit, "last_verified_observation_utc"),
            "acceptance_result": _value(execution_manifest, "acceptance_result", missing=UNVERIFIED),
        },
        "data_quality": {
            "discovered_windows": _value(trial_summary, "gamma_discovered_windows"),
            "valid_windows": _value(trial_summary, "valid_order_book_windows"),
            "malformed_windows": _value(trial_summary, "malformed_source_windows"),
            "closed_or_no_book_windows": _value(trial_summary, "expected_closed_no_book_windows"),
            "failed_windows": _value(trial_summary, "unexpected_source_failures"),
            "source_timestamp_utc": _value(source_time_audit, "source_timestamp_utc"),
            "received_timestamp_utc": _value(source_time_audit, "received_timestamp_utc"),
            "source_age_ms": _value(source_time_audit, "source_age_ms"),
            "pair_skew_ms": _value(source_time_audit, "pair_skew_ms"),
            "source_time_result": _value(source_time_audit, "result", missing=UNVERIFIED),
        },
        "execution": {
            "execution_model_version": _value(execution_manifest, "execution_model_version"),
            "fee_enabled": _value(fee_report, "fee_enabled", missing=UNVERIFIED),
            "raw_fee_schedule_reference": _value(fee_report, "raw_schedule_hash", missing=UNVERIFIED),
            "fee_model_version": _value(execution_manifest, "fee_model_version"),
            "fees_applied_usd": _value(trial_summary, "fees_applied_usd"),
            "decisions": _value(trial_summary, "decisions_total"),
            "abstentions": dict(trial_summary.get("abstention_counts") or {}),
            "simulated_intents": _value(trial_summary, "order_intents"),
            "simulated_fills": _value(trial_summary, "fills"),
            "fill_label": "SIMULATED",
            "leg_risk_incidents": len(leg_incidents),
            "scenarios": scenarios,
        },
        "portfolio_and_settlement": {
            "open_positions": sum(1 for item in positions if item.get("state") == "OPEN_UNMARKED"),
            "marked_positions": sum(1 for item in positions if item.get("state") == "OPEN_MARKED"),
            "pending_resolution": sum(1 for item in positions if item.get("state") == "RESOLUTION_PENDING"),
            "settled_positions": sum(1 for item in positions if item.get("state") == "SETTLED"),
            "realized_settled_pnl": _value(settlement_report, "realized_settled_pnl", missing=PENDING),
            "marked_unsettled_pnl": _value(settlement_report, "marked_unsettled_pnl", missing=UNKNOWN),
            "equity_known": _value(settlement_report, "equity_known", missing=UNVERIFIED),
            "unknown_valuation_positions": _value(settlement_report, "unknown_valuation_positions"),
            "positions": positions,
        },
        "risk_and_safety": {
            "authority_result": _value(risk_authority_map, "result", missing=UNVERIFIED),
            "authoritative_paper_components": list(risk_authority_map.get("authoritative_paper_components") or []),
            "duplicate_authoritative_components": list(risk_authority_map.get("duplicate_authoritative_components") or []),
        },
        "evidence_and_replay": {
            "raw_chain_verified": _value(trial_summary, "raw_chain_verified", missing=UNVERIFIED),
            "replay_verified": _value(trial_summary, "replay_verified", missing=UNVERIFIED),
            "settlement_replay": _value(settlement_report, "replay_result", missing=UNVERIFIED),
            "artifact_digest": artifact_digest,
        },
        "readiness_gates": list(execution_manifest.get("gates") or []),
        "known_unknowns": list(execution_manifest.get("known_unknowns") or []),
    }
    model["source_artifact_hash"] = hashlib.sha256(canonical_bytes({
        "execution_manifest": execution_manifest,
        "trial_summary": trial_summary,
        "source_time_audit": source_time_audit,
        "fee_report": fee_report,
        "sequential_report": sequential_report,
        "settlement_report": settlement_report,
        "risk_authority_map": risk_authority_map,
    })).hexdigest()
    return model


def _display(value: Any) -> str:
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return str(value)


def _section(title: str, values: Mapping[str, Any]) -> str:
    rows = []
    for key, value in values.items():
        rows.append(f"<tr><th>{html.escape(key)}</th><td>{html.escape(_display(value))}</td></tr>")
    return f'<section id="{html.escape(title.lower())}"><h2>{html.escape(title)}</h2><table>{"".join(rows)}</table></section>'


def render_monitoring_site(*, model: Mapping[str, Any], output_dir: Path) -> dict[str, Any]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    data_path = root / "data.json"
    data_path.write_bytes(canonical_bytes(model))
    sections = [
        ("OVERVIEW", model["overview"]),
        ("DATA_QUALITY", model["data_quality"]),
        ("EXECUTION", model["execution"]),
        ("PORTFOLIO_AND_SETTLEMENT", model["portfolio_and_settlement"]),
        ("RISK_AND_SAFETY", model["risk_and_safety"]),
        ("EVIDENCE_AND_REPLAY", model["evidence_and_replay"]),
        ("READINESS_GATES", {"gates": model["readiness_gates"], "known_unknowns": model["known_unknowns"]}),
    ]
    css = """body{font-family:system-ui,sans-serif;margin:0;background:#f4f4f4;color:#111}.banner{background:#111;color:#fff;padding:18px 24px;position:sticky;top:0}.banner strong{font-size:22px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:16px;padding:20px}section{background:#fff;border:1px solid #ccc;border-radius:8px;padding:14px;overflow:auto}table{border-collapse:collapse;width:100%}th,td{padding:7px;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}th{width:38%}.warning{font-weight:700}.mono{font-family:ui-monospace,monospace;font-size:12px}"""
    (root / "styles.css").write_text(css + "\n", encoding="utf-8")
    provenance = model["provenance"]
    safety = model["safety"]
    document = f"""<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>SENEX Paper Monitoring</title><link rel=\"stylesheet\" href=\"styles.css\"></head><body><header class=\"banner\"><strong>PAPER ONLY</strong> · paper_only={html.escape(_display(safety['paper_only']))} · orders_enabled={html.escape(_display(safety['orders_enabled']))} · live_capital_locked={html.escape(_display(safety['live_capital_locked']))}<div class=\"warning\">PROFITABILITY_NOT_ESTABLISHED</div><div class=\"mono\">SHA {html.escape(_display(provenance['code_sha']))} · artifact {html.escape(_display(provenance['artifact_digest']))}</div></header><main class=\"grid\">{''.join(_section(title, values) for title, values in sections)}</main><script type=\"application/json\" id=\"senex-artifact-projection\">{html.escape(json.dumps(model, sort_keys=True, separators=(',', ':'), ensure_ascii=False))}</script></body></html>"""
    (root / "index.html").write_text(document, encoding="utf-8")
    preview = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720"><rect width="1280" height="720" fill="#f4f4f4"/><rect width="1280" height="120" fill="#111"/><text x="40" y="48" font-family="sans-serif" font-size="30" fill="white">SENEX — PAPER ONLY</text><text x="40" y="86" font-family="monospace" font-size="18" fill="white">PROFITABILITY_NOT_ESTABLISHED</text><text x="40" y="155" font-family="monospace" font-size="17">code_sha={html.escape(_display(provenance['code_sha']))}</text><text x="40" y="188" font-family="monospace" font-size="17">experiment_id={html.escape(_display(provenance['experiment_id']))}</text><text x="40" y="235" font-family="sans-serif" font-size="24">DATA QUALITY</text><text x="40" y="270" font-family="monospace" font-size="17">source_age_ms={html.escape(_display(model['data_quality']['source_age_ms']))} pair_skew_ms={html.escape(_display(model['data_quality']['pair_skew_ms']))}</text><text x="40" y="320" font-family="sans-serif" font-size="24">EXECUTION</text><text x="40" y="355" font-family="monospace" font-size="17">fills=SIMULATED:{html.escape(_display(model['execution']['simulated_fills']))} leg_incidents={html.escape(_display(model['execution']['leg_risk_incidents']))}</text><text x="40" y="405" font-family="sans-serif" font-size="24">PORTFOLIO + SETTLEMENT</text><text x="40" y="440" font-family="monospace" font-size="17">equity_known={html.escape(_display(model['portfolio_and_settlement']['equity_known']))} pending={html.escape(_display(model['portfolio_and_settlement']['pending_resolution']))}</text><text x="40" y="490" font-family="sans-serif" font-size="24">EVIDENCE + REPLAY</text><text x="40" y="525" font-family="monospace" font-size="17">raw_chain={html.escape(_display(model['evidence_and_replay']['raw_chain_verified']))} replay={html.escape(_display(model['evidence_and_replay']['replay_verified']))}</text><text x="40" y="590" font-family="sans-serif" font-size="22">UNKNOWN stays UNKNOWN · PENDING stays PENDING · no controls</text></svg>'''
    (root / "rendered_monitoring_preview.svg").write_text(preview, encoding="utf-8")
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(root.iterdir()) if path.is_file()}
    return {"contract_version": MONITORING_CONTRACT_VERSION, "files": hashes, "result": "PASS"}
