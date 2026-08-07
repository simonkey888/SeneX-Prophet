from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class FillModelConfig:
    latency_buffer_ms: int = 250
    adverse_selection_window_ms: int = 1000
    max_book_age_ms: int = 1500
    queue_position_mode: str = "DISABLED_AGGREGATE_L2_ONLY"
    schema_version: str = "senex-paper-fill-v1"

    def __post_init__(self) -> None:
        if self.latency_buffer_ms < 0 or self.adverse_selection_window_ms < 0 or self.max_book_age_ms < 0:
            raise ValueError("time budgets must be non-negative")
        if self.queue_position_mode not in {"DISABLED_AGGREGATE_L2_ONLY", "ESTIMATED_LOW_CONFIDENCE"}:
            raise ValueError("exact queue position is unsupported from aggregate L2")


@dataclass(frozen=True)
class PaperFillRequest:
    side: str
    quantity: float
    limit_price: float
    book_age_ms: int
    levels: tuple[tuple[float, float], ...]
    reference_mid_after_window: float | None = None

    def __post_init__(self) -> None:
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if not 0 <= self.limit_price <= 1:
            raise ValueError("prediction-market limit_price must be within [0,1]")
        if self.book_age_ms < 0:
            raise ValueError("book_age_ms must be non-negative")


@dataclass(frozen=True)
class PaperFillResult:
    status: str
    requested_qty: float
    filled_qty: float
    unfilled_qty: float
    average_fill_price: float | None
    spread_crossing: bool
    visible_depth_consumed: float
    partial_fill: bool
    no_fill: bool
    latency_buffer_ms: int
    book_staleness: str
    adverse_selection: float | None
    queue_position_exact: bool
    queue_position_mode: str
    confidence: str
    reason: str
    schema_version: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_levels(levels: Iterable[tuple[float, float]], side: str) -> list[tuple[float, float]]:
    clean = [(float(price), max(0.0, float(size))) for price, size in levels if float(size) > 0]
    return sorted(clean, key=lambda item: item[0], reverse=(side == "SELL"))


def simulate_paper_fill(request: PaperFillRequest, config: FillModelConfig | None = None) -> PaperFillResult:
    """Deterministic fill against visible aggregate L2 only.

    BUY consumes asks priced <= limit. SELL consumes bids priced >= limit. No
    hidden liquidity or exact queue identity is invented. Stale books fail closed.
    """

    cfg = config or FillModelConfig()
    if request.book_age_ms > cfg.max_book_age_ms:
        return PaperFillResult(
            "NO_FILL_STALE_BOOK", request.quantity, 0.0, request.quantity, None,
            False, 0.0, False, True, cfg.latency_buffer_ms, "STALE", None,
            False, cfg.queue_position_mode, "HIGH", "book older than configured maximum",
            cfg.schema_version,
        )

    levels = _normalize_levels(request.levels, request.side)
    eligible = []
    for price, size in levels:
        if request.side == "BUY" and price <= request.limit_price:
            eligible.append((price, size))
        elif request.side == "SELL" and price >= request.limit_price:
            eligible.append((price, size))

    remaining = request.quantity
    notional = 0.0
    consumed = 0.0
    first_price = eligible[0][0] if eligible else None
    for price, size in eligible:
        take = min(remaining, size)
        if take <= 0:
            break
        remaining -= take
        consumed += take
        notional += take * price
        if remaining <= 1e-12:
            remaining = 0.0
            break

    filled = request.quantity - remaining
    avg = None if filled <= 0 else notional / filled
    partial = 0 < filled < request.quantity
    no_fill = filled <= 0
    status = "NO_FILL" if no_fill else "PARTIAL_FILL" if partial else "FILLED"
    adverse = None
    if avg is not None and request.reference_mid_after_window is not None:
        later = float(request.reference_mid_after_window)
        adverse = later - avg if request.side == "BUY" else avg - later

    return PaperFillResult(
        status=status,
        requested_qty=request.quantity,
        filled_qty=filled,
        unfilled_qty=remaining,
        average_fill_price=avg,
        spread_crossing=first_price is not None,
        visible_depth_consumed=consumed,
        partial_fill=partial,
        no_fill=no_fill,
        latency_buffer_ms=cfg.latency_buffer_ms,
        book_staleness="FRESH",
        adverse_selection=adverse,
        queue_position_exact=False,
        queue_position_mode=cfg.queue_position_mode,
        confidence="MEDIUM" if cfg.queue_position_mode == "DISABLED_AGGREGATE_L2_ONLY" else "LOW",
        reason="aggregate visible depth simulation; no order-level queue identity",
        schema_version=cfg.schema_version,
    )


def request_from_book(*, side: str, quantity: float, limit_price: float, book_age_ms: int, book: Mapping[str, Any], reference_mid_after_window: float | None = None) -> PaperFillRequest:
    raw = book.get("asks" if side == "BUY" else "bids") or []
    levels: list[tuple[float, float]] = []
    for level in raw:
        if isinstance(level, Mapping):
            levels.append((float(level["price"]), float(level["size"])))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            levels.append((float(level[0]), float(level[1])))
    return PaperFillRequest(side, quantity, limit_price, book_age_ms, tuple(levels), reference_mid_after_window)
