"""AUD-055 authoritative score truth contract.

Only proof-qualified 1h outcomes are observed. The public authoritative score
remains null until the global and both directional evidence/quality gates pass.
Observed win rate is diagnostic-only before calibration.
"""
from __future__ import annotations

from typing import Any

from .settlement_proof import filter_proof_qualified

MIN_GLOBAL_N = 100
MIN_DIRECTION_N = 30
LONG_MIN_WIN_RATE_PCT = 50.0
SHORT_MIN_WIN_RATE_PCT = 55.0
GLOBAL_MIN_WIN_RATE_PCT = 52.0


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


def build_authoritative_score(
    rows: list[dict[str, Any]],
    runner_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the public score payload without overstating statistical authority."""
    runner_state = runner_state or {}
    verified = filter_proof_qualified(rows)

    by_direction: dict[str, dict[str, Any]] = {}
    for direction in ("LONG", "SHORT", "FLAT"):
        by_direction[direction] = _bucket([
            row for row in verified
            if str(row.get("prediction") or "").upper() == direction
        ])

    global_bucket = _bucket(verified)
    observed = global_bucket["win_rate_pct"] if global_bucket["verified"] else None

    gates = {
        "long_1h": _gate(
            by_direction["LONG"],
            min_n=MIN_DIRECTION_N,
            threshold_pct=LONG_MIN_WIN_RATE_PCT,
        ),
        "short_1h": _gate(
            by_direction["SHORT"],
            min_n=MIN_DIRECTION_N,
            threshold_pct=SHORT_MIN_WIN_RATE_PCT,
        ),
        "global_1h": _gate(
            global_bucket,
            min_n=MIN_GLOBAL_N,
            threshold_pct=GLOBAL_MIN_WIN_RATE_PCT,
        ),
    }

    global_n = global_bucket["verified"]
    enough_samples = bool(
        global_n >= MIN_GLOBAL_N
        and by_direction["LONG"]["verified"] >= MIN_DIRECTION_N
        and by_direction["SHORT"]["verified"] >= MIN_DIRECTION_N
    )

    if global_n == 0:
        score_status = "UNKNOWN"
    elif not enough_samples:
        score_status = "INSUFFICIENT_EVIDENCE"
    elif all(gate["pass"] for gate in gates.values()):
        score_status = "CALIBRATED"
    else:
        score_status = "REJECTED"

    authoritative_score_pct = observed if score_status == "CALIBRATED" else None
    short_only_paper_mode = bool(gates["short_1h"]["pass"] and not gates["long_1h"]["pass"])

    directional_stats = runner_state.get("directional_stats") if isinstance(runner_state, dict) else {}
    by_window = directional_stats.get("by_window", {}) if isinstance(directional_stats, dict) else {}

    return {
        "version": "AUD-055-score-truth-v1",
        "score_status": score_status,
        "authoritative_score_pct": authoritative_score_pct,
        "observed_win_rate_pct": observed,
        "observed_win_rate_diagnostic_only": True,
        # Backward-compatible alias. Explicitly diagnostic and never the authoritative field.
        "win_rate_pct": observed if observed is not None else 0.0,
        "win_rate_pct_is_authoritative": False,
        "total_predictions": len(rows),
        "verified": global_n,
        "wins": global_bucket["wins"],
        "losses": global_bucket["losses"],
        "by_direction": by_direction,
        "by_window": by_window,
        "gates": gates,
        "minimum_evidence": {
            "global_n": MIN_GLOBAL_N,
            "direction_n": MIN_DIRECTION_N,
        },
        "short_only_paper_mode": short_only_paper_mode,
        "trade_mode": "PAPER",
        "live_capital_locked": True,
    }
