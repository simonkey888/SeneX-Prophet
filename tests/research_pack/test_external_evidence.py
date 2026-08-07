from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "research" / "external_pack"
REQUIRED = {
    "repo","resolved_url","exists","owner","is_official","exact_commit_sha",
    "default_branch","last_push_at","archived","license_spdx","language","purpose",
    "senex_layer","wallet_dependency","order_execution_capability","external_network_behavior",
    "secret_handling","workflow_risk","dependency_risk","arm64_relevance","zero_cost_relevance",
    "btc_5m_relevance","copy_eligible","classification","reason","evidence_refs",
}


def _json(name: str):
    return json.loads((EVIDENCE / name).read_text(encoding="utf-8"))


def _sidecar(name: str) -> None:
    path = EVIDENCE / name
    digest, filename = (EVIDENCE / f"{name}.sha256").read_text(encoding="utf-8").split()
    assert filename == name
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()


def test_manifest_is_exactly_100_unique_and_schema_complete():
    manifest = _json("external_source_manifest.json")
    sources = manifest["sources"]
    assert manifest["aud_order_id"] == "AUD-SENEX-EXTERNAL-RESEARCH-EXTRACTION-PACK-019"
    assert manifest["parent_018_sha"] == "2f84a38d6037c8e5a94bc96566b791a9d4f4e680"
    assert len(sources) == len({source["repo"] for source in sources}) == 100
    assert all(REQUIRED <= set(source) for source in sources)
    assert all(source["copy_eligible"] is False for source in sources)
    assert sum(bool(source.get("deep_audited")) for source in sources) == 14
    assert manifest["policy"]["external_code_executed"] is False
    assert manifest["policy"]["external_dependencies_installed"] is False
    assert manifest["policy"]["cost_usd"] == 0
    _sidecar("external_source_manifest.json")


def test_tier_a_protocol_authority_and_archived_unresolved_are_explicit():
    by_repo = {source["repo"]: source for source in _json("external_source_manifest.json")["sources"]}
    assert by_repo["Polymarket/py-clob-client-v2"]["exact_commit_sha"] == "215fc63a8fd6ec3a10c7edb73997c9772d8686d3"
    assert by_repo["Polymarket/clob-client-v2"]["exact_commit_sha"] == "f3e1a05f868a1fd0c34ef85dfc45c6ce78f5bb69"
    assert by_repo["Polymarket/conditional-tokens"]["classification"] == "UNRESOLVED"
    assert by_repo["Polymarket/conditional-tokens"]["exists"] is False
    assert by_repo["Polymarket/ctf-exchange"]["classification"] == "STALE_OR_ARCHIVED"
    assert by_repo["Polymarket/ctf-exchange"]["archived"] is True
    assert by_repo["Polymarket/ctf-exchange"]["exact_commit_sha"] == "ed5c7708b7be3aa98bf5f0c6602b57cc498e2ef4"


def test_provenance_ledger_maps_every_capability_without_external_copy():
    ledger = _json("provenance_ledger.json")
    assert ledger["literal_external_code_copied"] is False
    assert ledger["external_dependencies_added"] == []
    assert set(ledger["capabilities"]) == {"CAP_A","CAP_B","CAP_C","CAP_D","CAP_E"}
    assert ledger["capabilities"]["CAP_D"]["status"] == "IMPLEMENTED_DISABLED"
    assert ledger["safety"] == {
        "paper_only": True,
        "orders_enabled": False,
        "live_capital_locked": True,
        "real_order_network_calls": 0,
        "wallet_or_private_key_access": 0,
        "real_capital_actions": 0,
    }
    _sidecar("provenance_ledger.json")


def test_builder_is_deterministic_in_isolated_directory(tmp_path):
    spec = importlib.util.spec_from_file_location("external_builder", ROOT / "tools" / "build_external_research_pack.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.OUT = tmp_path / "one"
    first = module.build()
    one_manifest = (module.OUT / "external_source_manifest.json").read_bytes()
    one_ledger = (module.OUT / "provenance_ledger.json").read_bytes()
    module.OUT = tmp_path / "two"
    second = module.build()
    assert first == second
    assert one_manifest == (module.OUT / "external_source_manifest.json").read_bytes()
    assert one_ledger == (module.OUT / "provenance_ledger.json").read_bytes()
