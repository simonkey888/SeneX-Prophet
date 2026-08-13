"""AUD-055/R1 authoritative score truth contract.

Settlement truth is delegated exclusively to the current SCORE-002 proof gate.
Only proof-qualified 1h outcomes are eligible for scoring. Statistical authority
is instrument-scoped and remains null until sample, directional, Wilson, Brier,
and ECE gates all pass. Observed win rate is always diagnostic-only.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable

from .settlement_proof import filter_proof_qualified

MIN_GLOBAL_N = 100
MIN_DIRECTION_N = 30
LONG_MIN_WIN_RATE_PCT = 50.0
SHORT_MIN_WIN_RATE_PCT = 55.0
GLOBAL_MIN_WIN_RATE_PCT = 52.0
MIN_WILSON_LOWER = 0.50
MAX_BRIER = 0.25
MAX_ECE = 0.10


def _normalize_symbol(value: Any) -> str:
    return str(value or "").upper().replace("/", "").replace("-", "").strip()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _confidence(row: dict[str, Any]) -> float | None:
    value = _number(row.get("confidence"))
    if value is None or not 0.0 <= value <= 1.0:
        return None
    return value


def _wilson_lower(wins: int, total: int, z: float = 1.959963984540054) -> float:
    """95% Wilson lower confidence bound for a Bernoulli success rate."""
    if total <= 0:
        return 0.0
    observed = wins / total
    denominator = 1.0 + z * z / total
    centre = observed + z * z / (2.0 * total)
    radius = z * math.sqrt(
        (observed * (1.0 - observed) + z * z / (4.0 * total)) / total
    )
    return max(0.0, (centre - radius) / denominator)


def _ece(rows: list[dict[str, Any]], bins: int = 10) -> float | None:
    """Expected calibration error using confidence as P(correct direction)."""
    if not rows:
        return None
    confidence_rows: list[tuple[float, bool]] = []
    for row in rows:
        confidence = _confidence(row)
        if confidence is None:
            return None
        confidence_rows.append((confidence, row.get("outcome") == "WIN"))

    total = len(confidence_rows)
    error = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        bucket = [
            item for item in confidence_rows
            if lower <= item[0] < upper or (index == bins - 1 and item[0] == 1.0)
        ]
        if not bucket:
            continue
        mean_confidence = sum(item[0] for item in bucket) / len(bucket)
        accuracy = sum(item[1] for item in bucket) / len(bucket)
        error += len(bucket) / total * abs(accuracy - mean_confidence)
    return error


def _bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(1 for row in rows if row.get("outcome") == "WIN")
    losses = sum(1 for row in rows if row.get("outcome") == "LOSS")
    n = wins + losses
    return {
        "verified": n,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round((wins / n * 100.0) if n else 0.0, 2),
    }


def _gate(bucket: dict[str, Any], *, min_n: int, threshold_pct: float) -> dict[str, Any]:
    n = int(bucket.get("verified") or 0)
    win_rate_pct = float(bucket.get("win_rate_pct") or 0.0)
    return {
        "pass": bool(n >= min_n and win_rate_pct >= threshold_pct),
        "win_rate_pct": win_rate_pct,
        "n": n,
        "threshold_pct": threshold_pct,
        "min_n": min_n,
    }


def _quality_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    wins = sum(1 for row in rows if row.get("outcome") == "WIN")
    wilson = _wilson_lower(wins, n) if n else None
    confidences = [_confidence(row) for row in rows]
    confidence_complete = bool(n and all(value is not None for value in confidences))

    brier = None
    ece = None
    if confidence_complete:
        brier = sum(
            (float(confidences[index]) - (1.0 if row.get("outcome") == "WIN" else 0.0)) ** 2
            for index, row in enumerate(rows)
        ) / n
        ece = _ece(rows)

    return {
        "confidence_complete": confidence_complete,
        "valid_confidence_n": sum(value is not None for value in confidences),
        "wilson_lower_95": round(wilson, 6) if wilson is not None else None,
        "raw_confidence_brier": round(brier, 6) if brier is not None else None,
        "raw_confidence_ece": round(ece, 6) if ece is not None else None,
        "gates": {
            "confidence_complete": {
                "pass": confidence_complete,
                "required": True,
            },
            "wilson_lower_95": {
                "pass": bool(wilson is not None and wilson > MIN_WILSON_LOWER),
                "value": round(wilson, 6) if wilson is not None else None,
                "threshold": MIN_WILSON_LOWER,
                "operator": ">",
            },
            "brier": {
                "pass": bool(brier is not None and brier < MAX_BRIER),
                "value": round(brier, 6) if brier is not None else None,
                "threshold": MAX_BRIER,
                "operator": "<",
            },
            "ece": {
                "pass": bool(ece is not None and ece <= MAX_ECE),
                "value": round(ece, 6) if ece is not None else None,
                "threshold": MAX_ECE,
                "operator": "<=",
            },
        },
    }


def _empty_symbol_score(symbol: str | None) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "score_status": "UNKNOWN",
        "authoritative_score_pct": None,
        "observed_win_rate_pct": None,
        "observed_win_rate_diagnostic_only": True,
        "win_rate_pct": 0.0,
        "win_rate_pct_is_authoritative": False,
        "verified": 0,
        "wins": 0,
        "losses": 0,
        "posterior_accuracy": None,
        "by_direction": {direction: _bucket([]) for direction in ("LONG", "SHORT", "FLAT")},
        "gates": {
            "long_1h": _gate(_bucket([]), min_n=MIN_DIRECTION_N, threshold_pct=LONG_MIN_WIN_RATE_PCT),
            "short_1h": _gate(_bucket([]), min_n=MIN_DIRECTION_N, threshold_pct=SHORT_MIN_WIN_RATE_PCT),
            "global_1h": _gate(_bucket([]), min_n=MIN_GLOBAL_N, threshold_pct=GLOBAL_MIN_WIN_RATE_PCT),
        },
        "quality": _quality_metrics([]),
        "reasons": ["NO_PROOF_QUALIFIED_EVIDENCE"],
        "short_only_paper_mode": False,
    }


def _score_symbol(symbol: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_direction: dict[str, dict[str, Any]] = {}
    for direction in ("LONG", "SHORT", "FLAT"):
        by_direction[direction] = _bucket([
            row for row in rows
            if str(row.get("prediction") or "").upper() == direction
        ])

    global_bucket = _bucket(rows)
    observed = global_bucket["win_rate_pct"] if global_bucket["verified"] else None
    posterior = (
        (global_bucket["wins"] + 1.0) / (global_bucket["verified"] + 2.0)
        if global_bucket["verified"] else None
    )

    gates = {
        "long_1h": _gate(by_direction["LONG"], min_n=MIN_DIRECTION_N, threshold_pct=LONG_MIN_WIN_RATE_PCT),
        "short_1h": _gate(by_direction["SHORT"], min_n=MIN_DIRECTION_N, threshold_pct=SHORT_MIN_WIN_RATE_PCT),
        "global_1h": _gate(global_bucket, min_n=MIN_GLOBAL_N, threshold_pct=GLOBAL_MIN_WIN_RATE_PCT),
    }
    quality = _quality_metrics(rows)

    global_n = global_bucket["verified"]
    enough_samples = bool(
        global_n >= MIN_GLOBAL_N
        and by_direction["LONG"]["verified"] >= MIN_DIRECTION_N
        and by_direction["SHORT"]["verified"] >= MIN_DIRECTION_N
    )
    directional_gates_pass = all(gate["pass"] for gate in gates.values())
    quality_gates_pass = all(gate["pass"] for gate in quality["gates"].values())

    reasons: list[str] = []
    if global_n == 0:
        score_status = "UNKNOWN"
        reasons.append("NO_PROOF_QUALIFIED_EVIDENCE")
    elif not enough_samples:
        score_status = "INSUFFICIENT_EVIDENCE"
        reasons.append("INSUFFICIENT_PROOF_QUALIFIED_SAMPLE")
    else:
        if not directional_gates_pass:
            reasons.append("DIRECTIONAL_OR_GLOBAL_EDGE_GATE_FAILED")
        if not quality["confidence_complete"]:
            reasons.append("INVALID_OR_MISSING_CONFIDENCE")
        if quality["gates"]["wilson_lower_95"]["pass"] is False:
            reasons.append("EDGE_NOT_DEMONSTRATED_AT_95PCT")
        if quality["gates"]["brier"]["pass"] is False:
            reasons.append("RAW_CONFIDENCE_NOT_BETTER_THAN_NEUTRAL_BRIER")
        if quality["gates"]["ece"]["pass"] is False:
            reasons.append("RAW_CONFIDENCE_MISCALIBRATED")
        score_status = "CALIBRATED" if directional_gates_pass and quality_gates_pass else "REJECTED"

    authoritative_score_pct = (
        round(float(posterior) * 100.0, 2)
        if score_status == "CALIBRATED" and posterior is not None else None
    )
    short_only_paper_mode = bool(gates["short_1h"]["pass"] and not gates["long_1h"]["pass"])

    return {
        "symbol": symbol,
        "score_status": score_status,
        "authoritative_score_pct": authoritative_score_pct,
        "observed_win_rate_pct": observed,
        "observed_win_rate_diagnostic_only": True,
        "win_rate_pct": observed if observed is not None else 0.0,
        "win_rate_pct_is_authoritative": False,
        "verified": global_n,
        "wins": global_bucket["wins"],
        "losses": global_bucket["losses"],
        "posterior_accuracy": round(posterior, 6) if posterior is not None else None,
        "by_direction": by_direction,
        "gates": gates,
        "quality": quality,
        "reasons": reasons,
        "short_only_paper_mode": short_only_paper_mode,
    }


def build_authoritative_score(
    rows: Iterable[dict[str, Any]],
    runner_state: dict[str, Any] | None = None,
    *,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Build an instrument-scoped public score without cross-symbol pooling."""
    runner_state = runner_state or {}
    input_rows = list(rows)
    proof_qualified = filter_proof_qualified(input_rows)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in proof_qualified:
        row_symbol = _normalize_symbol(row.get("symbol"))
        if row_symbol:
            grouped[row_symbol].append(row)

    by_symbol = {
        row_symbol: _score_symbol(row_symbol, symbol_rows)
        for row_symbol, symbol_rows in sorted(grouped.items())
    }

    requested_symbol = _normalize_symbol(symbol) if symbol else None
    if requested_symbol:
        selected = by_symbol.get(requested_symbol) or _empty_symbol_score(requested_symbol)
        report_status = selected["score_status"]
        authoritative_score_pct = selected["authoritative_score_pct"]
    elif len(by_symbol) == 1:
        selected = next(iter(by_symbol.values()))
        report_status = selected["score_status"]
        authoritative_score_pct = selected["authoritative_score_pct"]
    else:
        selected = None
        report_status = "MULTI_INSTRUMENT_REPORT" if by_symbol else "UNKNOWN"
        authoritative_score_pct = None

    directional_stats = runner_state.get("directional_stats") if isinstance(runner_state, dict) else {}
    by_window = directional_stats.get("by_window", {}) if isinstance(directional_stats, dict) else {}

    selected_payload = selected or _empty_symbol_score(requested_symbol)
    return {
        "version": "AUD-055-R1-score-truth-v2",
        "score_scope": "PER_SYMBOL",
        "requested_symbol": requested_symbol,
        "score_status": report_status,
        "authoritative_score_pct": authoritative_score_pct,
        "observed_win_rate_pct": selected_payload["observed_win_rate_pct"] if selected else None,
        "observed_win_rate_diagnostic_only": True,
        "win_rate_pct": selected_payload["win_rate_pct"] if selected else 0.0,
        "win_rate_pct_is_authoritative": False,
        "total_predictions": sum(
            1 for row in input_rows
            if requested_symbol is None or _normalize_symbol(row.get("symbol")) == requested_symbol
        ),
        "input_rows": len(input_rows),
        "proof_qualified_rows": len(proof_qualified),
        "verified": selected_payload["verified"] if selected else 0,
        "wins": selected_payload["wins"] if selected else 0,
        "losses": selected_payload["losses"] if selected else 0,
        "posterior_accuracy": selected_payload["posterior_accuracy"] if selected else None,
        "by_direction": selected_payload["by_direction"] if selected else {},
        "by_window": by_window,
        "gates": selected_payload["gates"] if selected else {},
        "quality": selected_payload["quality"] if selected else {},
        "reasons": selected_payload["reasons"] if selected else ["SYMBOL_REQUIRED_FOR_AUTHORITATIVE_SCORE"],
        "by_symbol": by_symbol,
        "minimum_evidence": {
            "global_n": MIN_GLOBAL_N,
            "direction_n": MIN_DIRECTION_N,
        },
        "quality_thresholds": {
            "min_wilson_lower_95": MIN_WILSON_LOWER,
            "max_brier": MAX_BRIER,
            "max_ece": MAX_ECE,
        },
        "short_only_paper_mode": selected_payload["short_only_paper_mode"] if selected else False,
        "trade_mode": "PAPER",
        "live_capital_locked": True,
    }
