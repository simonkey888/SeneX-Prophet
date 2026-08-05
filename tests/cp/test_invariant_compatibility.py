"""Regression coverage for the legacy invariant compatibility boundary."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

POLYMARKET_DIR = Path(__file__).resolve().parent.parent.parent / "polymarket"
sys.path.insert(0, str(POLYMARKET_DIR))

from control_plane.invariant_monitor import (  # noqa: E402
    InvariantResult,
    check_invariants,
    invariant_summary,
)


def _legacy(status: str, invariant_id: str = "TEST") -> InvariantResult:
    return InvariantResult(
        invariant_id=invariant_id,
        status=status,
        severity="WARNING",
        reason="compatibility regression",
        evidence_hashes=tuple(),
    )


def _mapping(status: str, invariant_id: str = "TEST") -> dict:
    return {
        "invariant_id": invariant_id,
        "status": status,
        "severity": "WARNING",
        "reason": "compatibility regression",
        "evidence": {},
    }


def test_summary_accepts_modern_mappings() -> None:
    summary = invariant_summary([
        _mapping("PASS", "A"),
        _mapping("FAIL", "B"),
        _mapping("UNKNOWN", "C"),
        _mapping("NOT_APPLICABLE", "D"),
    ])
    assert summary == {
        "pass": 1,
        "fail": 1,
        "unknown": 1,
        "not_applicable": 1,
        "total": 4,
    }


def test_summary_accepts_legacy_results() -> None:
    summary = invariant_summary([_legacy("PASS", "A"), _legacy("UNKNOWN", "B")])
    assert summary["pass"] == 1
    assert summary["unknown"] == 1
    assert summary["total"] == 2


def test_summary_accepts_mixed_results() -> None:
    summary = invariant_summary([
        _legacy("PASS", "A"),
        _mapping("UNKNOWN", "B"),
        _legacy("NOT_APPLICABLE", "C"),
    ])
    assert summary["pass"] == 1
    assert summary["unknown"] == 1
    assert summary["not_applicable"] == 1
    assert summary["total"] == 3


def test_unknown_semantics_survive_legacy_normalization() -> None:
    result = _legacy("UNKNOWN", "SENTINEL")
    serialized = json.dumps(result.to_dict(), sort_keys=True)
    restored = json.loads(serialized)
    assert restored["status"] == "UNKNOWN"
    assert restored["status"] is not False
    assert invariant_summary([result])["unknown"] == 1


def test_check_invariants_keeps_catalog_at_31() -> None:
    results = check_invariants({})
    summary = invariant_summary(results)
    assert len(results) == 31
    assert summary["total"] == 31
    assert summary["unknown"] > 0


def test_invalid_result_type_is_rejected() -> None:
    with pytest.raises(TypeError, match="dict or InvariantResult"):
        invariant_summary([object()])  # type: ignore[list-item]
