from __future__ import annotations

import math
from statistics import median
from typing import Any, Iterable

from .contracts import FeatureValue, RawEvent, parse_time
from .store import PointInTimeStore


def _levels(payload: dict[str, Any], side: str) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for row in payload.get(side, []) or []:
        try:
            if isinstance(row, dict):
                price, size = float(row["price"]), float(row["size"])
            else:
                price, size = float(row[0]), float(row[1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if size >= 0:
            out.append((price, size))
    out.sort(key=lambda x: x[0], reverse=side == "bids")
    return out


def _book(event: RawEvent | None) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    if event is None:
        return [], []
    payload = dict(event.payload)
    return _levels(payload, "bids"), _levels(payload, "asks")


def _safe_ratio(num: float, den: float) -> float | None:
    return None if abs(den) < 1e-15 else num / den


def _mid(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> float | None:
    return (bids[0][0] + asks[0][0]) / 2.0 if bids and asks else None


def _total(levels: Iterable[tuple[float, float]]) -> float:
    return sum(size for _, size in levels)


class FeatureEngine:
    VERSION = "1.0.0"

    def __init__(self, store: PointInTimeStore):
        self.store = store

    def _events(self, market_id: str, as_of: str) -> list[RawEvent]:
        return self.store.events_as_of(as_of, market_id=market_id)

    @staticmethod
    def _max_time(events: list[RawEvent]) -> str | None:
        return max((event.event_time for event in events), key=parse_time) if events else None

    def _feature(
        self,
        feature_id: str,
        market_id: str,
        as_of: str,
        value: float | None,
        inputs: list[RawEvent],
        *,
        quality: str | None = None,
    ) -> FeatureValue:
        if quality is None:
            quality = "OK" if value is not None else "NOT_AVAILABLE"
        return FeatureValue(
            feature_id=feature_id,
            feature_version=self.VERSION,
            market_id=market_id,
            value=None if value is None or not math.isfinite(value) else float(value),
            as_of_event_time=as_of,
            input_event_max_time=self._max_time(inputs),
            sample_support=len(inputs),
            data_quality=quality,
        )

    def compute(self, market_id: str, as_of: str) -> dict[str, FeatureValue]:
        events = self._events(market_id, as_of)
        books = [event for event in events if event.event_type in {"BOOK_SNAPSHOT", "BOOK_DELTA", "BEST_BID_ASK"}]
        trades = [event for event in events if event.event_type in {"TRADE", "LAST_TRADE_PRICE"}]
        metadata = [event for event in events if event.event_type == "MARKET_META"]
        links = [event for event in events if event.event_type == "RELATED_MARKET_LINK"]
        current = books[-1] if books else None
        previous = books[-2] if len(books) > 1 else None
        bids, asks = _book(current)
        prev_bids, prev_asks = _book(previous)

        bid_qty = bids[0][1] if bids else 0.0
        ask_qty = asks[0][1] if asks else 0.0
        f01 = _safe_ratio(bid_qty - ask_qty, bid_qty + ask_qty)

        def weighted(levels: list[tuple[float, float]]) -> float:
            return sum(size / (index + 1) for index, (_, size) in enumerate(levels[:10]))

        wb, wa = weighted(bids), weighted(asks)
        f02 = _safe_ratio(wb - wa, wb + wa)

        mid = _mid(bids, asks)
        micro = None
        if bids and asks and bid_qty + ask_qty > 0:
            micro = (asks[0][0] * bid_qty + bids[0][0] * ask_qty) / (bid_qty + ask_qty)
        f03 = None if mid is None or micro is None else micro - mid
        f04 = None if not bids or not asks else asks[0][0] - bids[0][0]
        total_depth = _total(bids) + _total(asks)
        f05 = total_depth if bids or asks else None
        top_depth = bid_qty + ask_qty
        f06 = _safe_ratio(top_depth, total_depth) if total_depth else None

        f07 = None
        if len(books) >= 2:
            elapsed = (books[-1].event_dt - books[0].event_dt).total_seconds()
            if elapsed > 0:
                f07 = (len(books) - 1) / elapsed

        buy_flow = sell_flow = 0.0
        for event in trades:
            payload = dict(event.payload)
            try:
                size = float(payload.get("size", payload.get("quantity", 0.0)) or 0.0)
            except (TypeError, ValueError):
                continue
            side = str(payload.get("side", "")).upper()
            if side in {"BUY", "BID"}:
                buy_flow += size
            elif side in {"SELL", "ASK"}:
                sell_flow += size
        f08 = _safe_ratio(buy_flow - sell_flow, buy_flow + sell_flow)

        f09 = None if current is None else max(0.0, (parse_time(as_of) - current.event_dt).total_seconds())

        f10 = None
        if metadata:
            end_time = metadata[-1].payload.get("end_time") or metadata[-1].payload.get("close_time")
            if end_time:
                try:
                    f10 = max(0.0, (parse_time(str(end_time)) - parse_time(as_of)).total_seconds())
                except (TypeError, ValueError):
                    f10 = None

        prev_total = _total(prev_bids) + _total(prev_asks)
        f11 = None
        if previous is not None and prev_total > 0:
            f11 = max(-1.0, min(1.0, 1.0 - total_depth / prev_total))

        prev_spread = None if not prev_bids or not prev_asks else prev_asks[0][0] - prev_bids[0][0]
        f12 = None
        if f11 is not None and f04 is not None and prev_spread is not None:
            spread_widen = max(0.0, f04 - prev_spread)
            f12 = max(0.0, f11) + spread_widen

        peer_mids: list[float] = []
        for link in links:
            value = link.payload.get("peer_mid")
            try:
                if value is not None:
                    peer_mids.append(float(value))
            except (TypeError, ValueError):
                pass
        f13 = None if mid is None or not peer_mids else mid - median(peer_mids)

        components = [value for value in (f01, f03, f08, None if f12 is None else -f12) if value is not None]
        f14 = math.tanh(sum(components) / len(components)) if components else None

        neg_prices: list[float] = []
        for event in reversed(metadata):
            raw = event.payload.get("outcome_prices") or event.payload.get("neg_risk_prices")
            if isinstance(raw, list):
                try:
                    neg_prices = [float(item) for item in raw]
                except (TypeError, ValueError):
                    neg_prices = []
                if neg_prices:
                    break
        f15 = sum(neg_prices) - 1.0 if len(neg_prices) >= 2 else None

        all_book = books[-10:]
        all_trade = trades[-100:]
        values = {
            "F01": (f01, [current] if current else []),
            "F02": (f02, [current] if current else []),
            "F03": (f03, [current] if current else []),
            "F04": (f04, [current] if current else []),
            "F05": (f05, [current] if current else []),
            "F06": (f06, [current] if current else []),
            "F07": (f07, all_book),
            "F08": (f08, all_trade),
            "F09": (f09, [current] if current else []),
            "F10": (f10, metadata[-1:] if metadata else []),
            "F11": (f11, books[-2:]),
            "F12": (f12, books[-2:]),
            "F13": (f13, ([current] if current else []) + links),
            "F14": (f14, all_book + all_trade),
            "F15": (f15, metadata[-1:] if metadata else []),
        }
        result = {
            key: self._feature(key, market_id, as_of, value, [e for e in inputs if e is not None])
            for key, (value, inputs) in values.items()
        }
        return result

    def fair_value(self, market_id: str, as_of: str) -> dict[str, Any]:
        features = self.compute(market_id, as_of)
        events = self._events(market_id, as_of)
        books = [e for e in events if e.event_type in {"BOOK_SNAPSHOT", "BOOK_DELTA", "BEST_BID_ASK"}]
        bids, asks = _book(books[-1] if books else None)
        mid = _mid(bids, asks)
        if mid is None:
            return {"status": "UNVALIDATED", "fair_value": None, "mid_price": None, "featureset_hash": self.featureset_hash(features)}
        adjustment = 0.0
        for feature_id, weight in (("F01", 0.01), ("F03", 0.30), ("F08", 0.01), ("F14", 0.01)):
            value = features[feature_id].value
            if value is not None:
                adjustment += weight * value
        fair = min(1.0, max(0.0, mid + adjustment))
        return {
            "status": "UNVALIDATED",
            "fair_value": fair,
            "mid_price": mid,
            "featureset_hash": self.featureset_hash(features),
            "claim": "RESEARCH_ONLY_NOT_ALPHA",
        }

    @staticmethod
    def featureset_hash(features: dict[str, FeatureValue]) -> str:
        import hashlib
        import json
        payload = {key: value.to_dict() for key, value in sorted(features.items())}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
