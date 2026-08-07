from __future__ import annotations

from typing import Any, Mapping


SAFE_FIELDS = (
    "microprice",
    "top_book_imbalance",
    "depth_weighted_imbalance",
    "visible_depth",
    "spread",
    "liquidity_shock",
    "book_staleness",
    "signed_trade_flow",
    "flow_burst",
    "paper_fill_quality",
)


def microstructure_terminal_projection(values: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only UI projection; it cannot express an order/trading action."""

    projected = {key: values.get(key) for key in SAFE_FIELDS}
    projected.update({
        "paper_only": True,
        "orders_enabled": False,
        "live_capital_locked": True,
        "execution_controls": False,
        "wallet_controls": False,
        "status": "RESEARCH_ONLY",
    })
    return projected


def data_quality_badges(*, stale: bool, sequence_gaps: int, replay_verified: bool, queue_exact: bool = False) -> list[dict[str, str]]:
    return [
        {"label": "BOOK", "status": "STALE" if stale else "FRESH"},
        {"label": "SEQUENCE", "status": "GAPS" if sequence_gaps else "CONTIGUOUS"},
        {"label": "REPLAY", "status": "VERIFIED" if replay_verified else "UNVERIFIED"},
        {"label": "QUEUE", "status": "EXACT" if queue_exact else "AGGREGATE_ONLY"},
    ]


def visual_smoke_fragment() -> str:
    """Framework-independent fragment for browser/static QC."""

    return (
        '<section id="microstructure-research" data-paper-only="true">'
        '<h3>MICROSTRUCTURE / PAPER FILL QUALITY</h3>'
        '<div>MICROPRICE · IMBALANCE · DEPTH · LIQUIDITY SHOCK · STALENESS · FLOW BURST</div>'
        '<div>QUEUE: AGGREGATE_ONLY · EXECUTION: DISABLED · RESEARCH_ONLY</div>'
        '</section>'
    )
