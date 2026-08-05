from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_builder_fails_closed_without_historical_corpus(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"result": "PASS"}), encoding="utf-8")
    output = tmp_path / "evidence"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/build_phase_f_h_evidence.py"),
            "--repo-root",
            str(ROOT),
            "--output-dir",
            str(output),
            "--source-sha",
            "a" * 40,
            "--fixture-report",
            str(fixture),
        ],
        check=True,
    )
    gate = json.loads((output / "phase_f_backtest_gate.json").read_text(encoding="utf-8"))
    checkpoint = json.loads((output / "phase_c_h_checkpoint.json").read_text(encoding="utf-8"))
    assert gate["status"] == "BLOCKED_CORPUS_UNAVAILABLE"
    assert gate["historical_backtest_executed"] is False
    assert gate["fixture_harness_executed"] is True
    assert gate["fixture_is_historical_evidence"] is False
    assert checkpoint["mission_complete"] is False
    assert checkpoint["production_mutated"] is False
    assert checkpoint["phase_h_predeployment"] == "PASS"
