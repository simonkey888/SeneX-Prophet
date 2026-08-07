#!/usr/bin/env python3
"""Deterministically materialize SENEX Order 019 external-research evidence.

No network calls, package installation, external code execution, or secret reads.
The 100-source corpus is inventory metadata. Only 14 Tier A/B sources are used as
implementation references and every implementation is independent reimplementation.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ORDER = "AUD-SENEX-EXTERNAL-RESEARCH-EXTRACTION-PACK-019"
PARENT = "2f84a38d6037c8e5a94bc96566b791a9d4f4e680"
OUT = Path("research/external_pack")

REPOS = [
"Polymarket/py-clob-client-v2","Polymarket/clob-client-v2","Polymarket/conditional-tokens","Polymarket/ctf-exchange","Jon-Becker/prediction-market-analysis","pmxt-dev/pmxt","nkaz001/hftbacktest","nautechsystems/nautilus_trader","evan-kolberg/prediction-market-backtesting","warproxxx/poly_data","humanplane/cross-market-state-fusion","Oddpool/PredictionMarketBench","visualHFT/VisualHFT","nkaz001/market-making-backtest","QuantConnect/Lean","polakowo/vectorbt","mementum/backtrader","kernc/backtesting.py","quantopian/zipline","microsoft/qlib","ccxt/ccxt","hummingbot/hummingbot","freqtrade/freqtrade","jesse-ai/jesse","hudson-and-thames/mlfinlab","AI4Finance-Foundation/FinRL","tensortrade-org/tensortrade","goldmansachs/gs-quant","Superalgos/Superalgos","Drakkar-Software/OctoBot","hummingbot/gateway","hummingbot/hummingbot-api","google-research/timesfm","TauricResearch/TradingAgents","asavinov/intelligent-trading-bot","mikegianfelice/Hunter","notlelouch/ArbiBot","fiit-ba/ML-for-arbitrage-in-cryptoexchanges","AryaSingh22/The-Flash-Loan","kaymen99/aave-flashloan-arbitrage","IBQ-SUP/Solana-Flash-loan-bot","ViditGalav/Flashloan-Arbitrage","backtrader2/backtrader","hummingbot/quants-lab","Drakkar-Software/OctoBot-market-making","Drakkar-Software/OctoBot-Prediction-Market","Zmey56/dca-bot","kashyapnathan/crypto-arbitrage","OpenHands/OpenHands","crewAIInc/crewAI","n8n-io/n8n","Aider-AI/aider","Polymarket/agents","NYTEMODEONLY/polyterm","QwenLM/Qwen3-Coder","elder-plinius/G0DM0D3","smtg-ai/claude-squad","msitarzewski/agency-agents","cvxv666/ClaudeAgentOneClick","MiroMindAI/MiroThinker","obra/superpowers","OpenBB-finance/OpenBB","virattt/dexter","financial-datasets/mcp-server","calesthio/Crucix","mortada/fredapi","txbabaxyz/mlmodelpoly","FiatFiorino/polymarket-assistant-tool","tradingview/lightweight-charts","matteoprata/LOBCAST","jrajath94/orderbook-simulator","Quentin-Piot/prediction-market-backtester","mnc13/PROClaim","SII-WANGZJ/Polymarket_data","Polymarket/py-clob-client","Polymarket/clob-client","Polymarket/real-time-data-client","Polymarket/conditional-token-examples","Polymarket/examples","Polymarket/poly-market-maker","Polymarket/poly-market-maker-v2","Polymarket/market-maker-keeper","Polymarket/subgraph","gnosis/conditional-tokens-contracts","gnosis/conditional-tokens-market-makers","gnosis/conditional-tokens-documentation","duneanalytics/spellbook","ethereum/web3.py","web3/web3.js","Uniswap/v3-core","scikit-learn/scikit-learn","numpy/numpy","pandas-dev/pandas","scipy/scipy","statsmodels/statsmodels","pytest-dev/pytest","hypothesisworks/hypothesis","plotly/plotly.py","apache/arrow","duckdb/duckdb"]

DEEP: dict[str, dict[str, Any]] = {
"Polymarket/py-clob-client-v2":{"sha":"215fc63a8fd6ec3a10c7edb73997c9772d8686d3","branch":"main","push":"2026-08-05T08:20:24Z","license":"MIT","language":"Python","class":"PROTOCOL_AUTHORITY","layer":"ADAPTER_SCHEMA","purpose":"Current official Python CLOB V2 public semantics/types; authenticated execution excluded."},
"Polymarket/clob-client-v2":{"sha":"f3e1a05f868a1fd0c34ef85dfc45c6ce78f5bb69","branch":"main","push":"2026-08-05T08:16:21Z","license":"MIT","language":"TypeScript","class":"PROTOCOL_AUTHORITY","layer":"ADAPTER_SCHEMA","purpose":"Current official TypeScript CLOB V2 public semantics/types; execution excluded."},
"Polymarket/conditional-tokens":{"sha":None,"branch":None,"push":None,"license":"UNKNOWN","language":None,"class":"UNRESOLVED","layer":"PROTOCOL_REFERENCE","purpose":"Requested authority path; exact GitHub repository returned 404.","exists":False,"archived":None},
"Polymarket/ctf-exchange":{"sha":"ed5c7708b7be3aa98bf5f0c6602b57cc498e2ef4","branch":"main","push":"2026-05-11T17:13:36Z","license":"MIT","language":"Solidity","class":"STALE_OR_ARCHIVED","layer":"PROTOCOL_REFERENCE","purpose":"Archived official CTF exchange; historical semantics only.","archived":True},
"Jon-Becker/prediction-market-analysis":{"sha":"5330e823d197b3e1eeec8ea1992cfce9f648cf69","branch":"main","push":"2026-08-01T16:58:42Z","license":"MIT","language":"Python","class":"PRIMARY_EXTRACTION_SOURCE","layer":"DATASET_EVALUATION","purpose":"Prediction-market dataset and microstructure methodology."},
"pmxt-dev/pmxt":{"sha":"4a367d812541154002eedda36b0916a3cf68e0f2","branch":"main","push":"2026-07-18T01:45:52Z","license":"NOASSERTION","language":None,"class":"SECONDARY_REFERENCE","layer":"ADAPTER_SCHEMA","purpose":"Cross-venue prediction-market API abstraction reference."},
"nkaz001/hftbacktest":{"sha":"5f3ec40b2afb764e0fea112f941ed85523ef4e88","branch":"master","push":"2025-12-23T15:31:05Z","license":"NOASSERTION","language":"Rust/Python","class":"METHODOLOGY_REFERENCE","layer":"PAPER_EXECUTION_REPLAY","purpose":"Order-book replay and realistic fill methodology."},
"nautechsystems/nautilus_trader":{"sha":"570d4d796dee347dd5d6445c16780847b883757a","branch":"develop","push":"2026-08-07T08:41:05Z","license":"NOASSERTION","language":"Rust/Python","class":"EXECUTION_QUARANTINE","layer":"REPLAY_MICROSTRUCTURE","purpose":"Event/replay/microstructure architecture; all live execution surfaces quarantined."},
"evan-kolberg/prediction-market-backtesting":{"sha":"c76e77af00ef53472a9da8f66dae7fdd2d3e5928","branch":"v4.1-alpha","push":"2026-05-16T21:24:44Z","license":"NOASSERTION","language":"Python","class":"PRIMARY_EXTRACTION_SOURCE","layer":"VALIDATION_PAPER_FILL","purpose":"Prediction-market backtesting and snapshot methodology."},
"warproxxx/poly_data":{"sha":"cab11bd5a2fc41a67c4c643835a4766907208b55","branch":"main","push":"2026-06-29T22:10:32Z","license":"NOASSERTION","language":"Python","class":"DATASET_REFERENCE","layer":"INGESTION_EVIDENCE","purpose":"Polymarket public-data shape/provenance reference."},
"humanplane/cross-market-state-fusion":{"sha":"3e35d8d8f737273256bbb07a1f4575e2beeb8357","branch":"master","push":"2026-01-03T18:56:13Z","license":"NOASSERTION","language":"Python","class":"METHODOLOGY_REFERENCE","layer":"DISABLED_CROSS_MARKET","purpose":"Cross-market state interface concepts; live adapter disabled in SENEX."},
"Oddpool/PredictionMarketBench":{"sha":"611d66941717310858683278940df21c33c406f2","branch":"main","push":"2026-01-26T02:45:17Z","license":"NOASSERTION","language":None,"class":"DATASET_REFERENCE","layer":"EVALUATION","purpose":"Prediction-market benchmark/evaluation methodology."},
"visualHFT/VisualHFT":{"sha":"d4ad9b609af6382894c453db95e4bf8801ebcb04","branch":"master","push":"2026-08-04T16:42:21Z","license":"NOASSERTION","language":"C#","class":"UI_REFERENCE","layer":"READ_ONLY_UI","purpose":"Depth/microstructure visualization concepts; no assets/trading controls copied."},
"nkaz001/market-making-backtest":{"sha":"a7355e42ad9019413dea422499336554a5c92a2c","branch":"master","push":"2023-12-04T14:51:09Z","license":"NOASSERTION","language":"Python","class":"METHODOLOGY_REFERENCE","layer":"PAPER_FILL","purpose":"Market-making backtest and queue/fill methodology reference."}}

EXECUTION_TOKENS = tuple(x.lower() for x in ("hummingbot","freqtrade","jesse","Superalgos","OctoBot","intelligent-trading-bot","ArbiBot","arbitrage","Flash","dca-bot","poly-market-maker","market-maker-keeper","conditional-tokens-market-makers"))
REQUIRED = ("repo","resolved_url","exists","owner","is_official","exact_commit_sha","default_branch","last_push_at","archived","license_spdx","language","purpose","senex_layer","wallet_dependency","order_execution_capability","external_network_behavior","secret_handling","workflow_risk","dependency_risk","arm64_relevance","zero_cost_relevance","btc_5m_relevance","copy_eligible","classification","reason","evidence_refs")


def _entry(repo: str) -> dict[str, Any]:
    owner = repo.split("/", 1)[0]
    if repo in DEEP:
        d = DEEP[repo]
        execution = d["class"] == "EXECUTION_QUARANTINE"
        unresolved = d["class"] == "UNRESOLVED"
        return {"repo":repo,"resolved_url":f"https://github.com/{repo}","exists":d.get("exists",True),"owner":owner,"is_official":owner=="Polymarket","exact_commit_sha":d["sha"],"default_branch":d["branch"],"last_push_at":d["push"],"archived":d.get("archived",False),"license_spdx":d["license"],"language":d["language"],"purpose":d["purpose"],"senex_layer":d["layer"],"wallet_dependency":"UNKNOWN" if unresolved else "QUARANTINED_IF_PRESENT" if execution or owner=="Polymarket" else "NO_REQUIRED_FOR_STATIC_REVIEW","order_execution_capability":"UNKNOWN" if unresolved else "QUARANTINED_IF_PRESENT" if execution or owner=="Polymarket" else "NO_REAL_ORDER_REQUIRED","external_network_behavior":"UNKNOWN" if unresolved else "STATIC_REVIEW_ONLY_NO_RUNTIME_IMPORT","secret_handling":"UNKNOWN" if unresolved else "NO_SECRET_READ_BY_SENEX_019","workflow_risk":"NOT_EXECUTED","dependency_risk":"NO_IMPORT_NO_INSTALL","arm64_relevance":"REFERENCE_ONLY","zero_cost_relevance":"YES_REFERENCE_ONLY","btc_5m_relevance":"HIGH" if repo in {"Polymarket/py-clob-client-v2","Polymarket/clob-client-v2","Jon-Becker/prediction-market-analysis","nkaz001/hftbacktest","nautechsystems/nautilus_trader","evan-kolberg/prediction-market-backtesting","warproxxx/poly_data"} else "MEDIUM","copy_eligible":False,"classification":d["class"],"reason":"Independent reimplementation/static semantics only; no external code/package/workflow consumed." if not unresolved else "Exact requested repository returned GitHub 404; no implemented capability depends on it.","evidence_refs":["GITHUB_STATIC_API_2026-08-07","OWNER_CORPUS_OR_AUD_ORDER_019"],"deep_audited":True}
    execution = any(token in repo.lower() for token in EXECUTION_TOKENS)
    classification = "EXECUTION_QUARANTINE" if execution else "UI_REFERENCE" if repo == "tradingview/lightweight-charts" else "SECONDARY_REFERENCE"
    return {"repo":repo,"resolved_url":f"https://github.com/{repo}","exists":None,"owner":owner,"is_official":owner=="Polymarket","exact_commit_sha":None,"default_branch":None,"last_push_at":None,"archived":None,"license_spdx":"UNVERIFIED","language":None,"purpose":"Corpus inventory candidate; not used as implementation authority.","senex_layer":"REFERENCE_ONLY","wallet_dependency":"UNVERIFIED","order_execution_capability":"QUARANTINE" if execution else "UNVERIFIED","external_network_behavior":"UNVERIFIED","secret_handling":"UNVERIFIED","workflow_risk":"NOT_EXECUTED","dependency_risk":"NO_IMPORT_NO_INSTALL","arm64_relevance":"REFERENCE_ONLY","zero_cost_relevance":"REFERENCE_ONLY","btc_5m_relevance":"LOW_OR_UNASSESSED","copy_eligible":False,"classification":classification,"reason":"Inventory-only source from OWNER corpus/reference expansion; no code, package, workflow or asset consumed.","evidence_refs":["OWNER_CORPUS_FILE_LIBRARY_OR_ORDER_019"],"deep_audited":False}


def _render(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _write_json_with_sha(path: Path, value: Any) -> str:
    text = _render(value)
    path.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    path.with_name(path.name + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return digest


def build() -> dict[str, str]:
    if len(REPOS) != 100 or len(set(REPOS)) != 100:
        raise RuntimeError("SOURCE_CORPUS_NOT_EXACTLY_100_UNIQUE")
    sources = [_entry(repo) for repo in REPOS]
    if any(not set(REQUIRED).issubset(source) for source in sources):
        raise RuntimeError("SOURCE_MANIFEST_REQUIRED_FIELD_MISSING")
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version":"senex-external-source-manifest-v1","aud_order_id":ORDER,"parent_018_sha":PARENT,"generated_from":"STATIC_PINNED_INVENTORY_NO_NETWORK","counts":{"inventoried":100,"deep_audited":14,"resolved_or_explicit_404":14,"inventory_only_unverified":86},"policy":{"cost_usd":0,"direct_copy_default":False,"external_code_executed":False,"external_dependencies_installed":False,"external_live_adapter_added":False,"wallet":False,"private_key":False,"signing":False,"real_order_submit":False,"real_order_cancel":False},"sources":sources}
    manifest_sha = _write_json_with_sha(OUT / "external_source_manifest.json", manifest)
    provenance = {"schema_version":"senex-external-provenance-ledger-v1","aud_order_id":ORDER,"parent_018_sha":PARENT,"implementation_mode":"INDEPENDENT_REIMPLEMENTATION_NO_EXTERNAL_CODE_EXECUTED","literal_external_code_copied":False,"external_dependencies_added":[],"capabilities":{"CAP_A":{"status":"IMPLEMENTED_ISOLATED","module":"polymarket/research_pack/external_schema.py","sources":["Polymarket/py-clob-client-v2@215fc63a8fd6ec3a10c7edb73997c9772d8686d3","Polymarket/clob-client-v2@f3e1a05f868a1fd0c34ef85dfc45c6ce78f5bb69","warproxxx/poly_data@cab11bd5a2fc41a67c4c643835a4766907208b55"],"claim":"public schema cross-check only"},"CAP_B":{"status":"IMPLEMENTED_ISOLATED","module":"polymarket/research_pack/fill_model.py","sources":["nkaz001/hftbacktest@5f3ec40b2afb764e0fea112f941ed85523ef4e88","evan-kolberg/prediction-market-backtesting@c76e77af00ef53472a9da8f66dae7fdd2d3e5928","nkaz001/market-making-backtest@a7355e42ad9019413dea422499336554a5c92a2c"],"claim":"aggregate-L2 paper fill; exact queue position disabled"},"CAP_C":{"status":"IMPLEMENTED_ISOLATED","module":"polymarket/research_pack/replay_v2.py","sources":["nkaz001/hftbacktest@5f3ec40b2afb764e0fea112f941ed85523ef4e88","nautechsystems/nautilus_trader@570d4d796dee347dd5d6445c16780847b883757a"],"claim":"finite captured deterministic replay; no live reads"},"CAP_D":{"status":"IMPLEMENTED_DISABLED","module":"polymarket/research_pack/cross_market.py","sources":["humanplane/cross-market-state-fusion@3e35d8d8f737273256bbb07a1f4575e2beeb8357"],"claim":"fixture/synthetic interface only; external adapter hard disabled"},"CAP_E":{"status":"IMPLEMENTED_ISOLATED","module":"polymarket/research_pack/terminal.py","sources":["visualHFT/VisualHFT@d4ad9b609af6382894c453db95e4bf8801ebcb04","Oddpool/PredictionMarketBench@611d66941717310858683278940df21c33c406f2"],"claim":"read-only microstructure diagnostics; no trading controls"}},"safety":{"paper_only":True,"orders_enabled":False,"live_capital_locked":True,"real_order_network_calls":0,"wallet_or_private_key_access":0,"real_capital_actions":0}}
    provenance_sha = _write_json_with_sha(OUT / "provenance_ledger.json", provenance)
    return {"external_manifest_sha256":manifest_sha,"provenance_ledger_sha256":provenance_sha}


if __name__ == "__main__":
    print(json.dumps(build(), sort_keys=True))
