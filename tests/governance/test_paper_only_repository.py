from __future__ import annotations

import json
from pathlib import Path

from tools.verify_paper_only_repository import scan_repository


REPO = Path(__file__).resolve().parents[2]


def test_current_repository_has_no_authenticated_trading_or_write_workflow() -> None:
    report = scan_repository(REPO)
    assert report["status"] == "PASS", json.dumps(report["findings"], indent=2)
    coverage = report["coverage"]
    assert coverage["root_code"] > 0
    assert coverage["package_code"] > 0
    assert coverage["workflows"] > 0
    assert coverage["dockerfiles"] > 0
    assert coverage["configuration"] > 0
    assert report["secret_values_observed"] is False


def test_gate_detects_pre_fix_authenticated_routes_and_workflow_mutation(tmp_path: Path) -> None:
    (tmp_path / "package").mkdir()
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / "exchange_connector.py").write_text(
        "import os\nsecret=os.environ.get('BINANCE_TESTNET_SECRET')\n"
        "def execute(ex):\n    return ex.create_market_order('BTC/USDT','buy',1)\n",
        encoding="utf-8",
    )
    (tmp_path / "package/private.py").write_text(
        "def positions(ex):\n    return ex.fetch_positions()\n", encoding="utf-8"
    )
    (tmp_path / ".github/workflows/oracle.yml").write_text(
        "permissions:\n  contents: write\njobs:\n  x:\n    steps:\n      - run: git push\n",
        encoding="utf-8",
    )
    report = scan_repository(tmp_path)
    assert report["status"] == "FAIL"
    rules = {item["rule"] for item in report["findings"]}
    assert "TRADING_CREDENTIAL_LOADER" in rules
    assert "AUTHENTICATED_TRADING_CALL" in rules
    assert "WORKFLOW_WRITE_PERMISSION" in rules
    assert "WORKFLOW_REPOSITORY_MUTATION" in rules


def test_public_exchange_data_connector_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")
    (tmp_path / "connector.py").write_text(
        "class Connector:\n"
        "    def ticker(self, ex):\n        return ex.fetch_ticker('BTC/USDT')\n"
        "    def book(self, ex):\n        return ex.fetch_order_book('BTC/USDT')\n",
        encoding="utf-8",
    )
    report = scan_repository(tmp_path)
    assert report["status"] == "PASS", report["findings"]
