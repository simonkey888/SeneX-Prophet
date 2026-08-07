from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .contracts import RawEvent


@dataclass(frozen=True)
class OfficialPolymarketEndpoints:
    gamma_api: str = "https://gamma-api.polymarket.com"
    clob_rest: str = "https://clob.polymarket.com"
    data_api: str = "https://data-api.polymarket.com"
    clob_market_ws: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class OfficialPolymarketSource:
    """Unauthenticated public-market reader.

    Only GET endpoints are implemented. Trading/user-authenticated CLOB surfaces
    are deliberately absent from this class.
    """

    def __init__(self, endpoints: OfficialPolymarketEndpoints | None = None, timeout: float = 10.0):
        self.endpoints = endpoints or OfficialPolymarketEndpoints()
        self.timeout = timeout

    def _get_json(self, url: str) -> Any:
        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "senex-signal-lab/1"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - fixed official hosts only
            return json.loads(response.read().decode("utf-8"))

    def markets(self, *, limit: int = 50, active: bool = True) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"limit": max(1, min(int(limit), 100)), "active": str(bool(active)).lower()})
        data = self._get_json(f"{self.endpoints.gamma_api}/markets?{query}")
        return list(data) if isinstance(data, list) else []

    def market(self, market_id: str) -> dict[str, Any]:
        encoded = urllib.parse.quote(str(market_id), safe="")
        data = self._get_json(f"{self.endpoints.gamma_api}/markets/{encoded}")
        return dict(data) if isinstance(data, dict) else {}

    def book(self, token_id: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"token_id": token_id})
        data = self._get_json(f"{self.endpoints.clob_rest}/book?{query}")
        return dict(data) if isinstance(data, dict) else {}

    def midpoint(self, token_id: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"token_id": token_id})
        data = self._get_json(f"{self.endpoints.clob_rest}/midpoint?{query}")
        return dict(data) if isinstance(data, dict) else {}

    def spread(self, token_id: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"token_id": token_id})
        data = self._get_json(f"{self.endpoints.clob_rest}/spread?{query}")
        return dict(data) if isinstance(data, dict) else {}

    def trades(self, *, market: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
        if market:
            params["market"] = market
        data = self._get_json(f"{self.endpoints.data_api}/trades?{urllib.parse.urlencode(params)}")
        return list(data) if isinstance(data, list) else []

    def open_interest(self, market: str) -> Any:
        query = urllib.parse.urlencode({"market": market})
        return self._get_json(f"{self.endpoints.data_api}/oi?{query}")

    def holders(self, market: str, *, limit: int = 20) -> Any:
        query = urllib.parse.urlencode({"market": market, "limit": max(1, min(int(limit), 50))})
        return self._get_json(f"{self.endpoints.data_api}/holders?{query}")


class MarketDataNormalizer:
    EVENT_MAP = {
        "book": "BOOK_SNAPSHOT",
        "price_change": "BOOK_DELTA",
        "best_bid_ask": "BEST_BID_ASK",
        "last_trade_price": "LAST_TRADE_PRICE",
        "new_market": "MARKET_META",
        "market_resolved": "RESOLUTION_METADATA",
    }

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @classmethod
    def websocket_event(cls, message: Mapping[str, Any], *, received_time: str | None = None) -> RawEvent:
        payload = dict(message)
        source_type = str(payload.get("event_type", ""))
        event_type = cls.EVENT_MAP.get(source_type, "DATA_HEALTH_EVENT")
        market_id = str(payload.get("market") or payload.get("condition_id") or "UNKNOWN")
        token_id_raw = payload.get("asset_id") or payload.get("token_id")
        token_id = None if token_id_raw is None else str(token_id_raw)
        event_time = str(payload.get("timestamp") or payload.get("event_time") or received_time or cls._now())
        if event_time.isdigit():
            raw = int(event_time)
            seconds = raw / 1000.0 if raw > 10_000_000_000 else float(raw)
            event_time = datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        received = received_time or cls._now()
        cursor = payload.get("sequence") or payload.get("hash") or payload.get("timestamp")
        import hashlib
        event_id = hashlib.sha256(json.dumps({"source": source_type, "market": market_id, "token": token_id, "cursor": cursor, "payload": payload}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return RawEvent.build(
            event_id=event_id,
            event_type=event_type,
            market_id=market_id,
            token_id=token_id,
            event_time=event_time,
            received_time=received,
            sequence_or_source_cursor=cursor,
            source="POLYMARKET_CLOB_WEBSOCKET",
            payload=payload,
        )


def mirrored_depth_provenance_finding() -> dict[str, Any]:
    """Result of SENEX-MIRROR-001 using official public schemas only.

    The documented public book schema exposes aggregated price/size levels plus
    market, asset, timestamp, hash, tick/min-size and neg-risk metadata. The
    public market WebSocket likewise exposes book/price-level state, not a field
    identifying whether visible depth is direct versus mirrored/synthetic.
    """
    return {
        "experiment_id": "SENEX-MIRROR-001",
        "status": "RESEARCH_ONLY",
        "result": "NOT_OBSERVABLE_FROM_PUBLIC_DATA",
        "claim_direct_synthetic_provenance_available": False,
        "heuristic_promoted_to_provenance": False,
        "reason": "Official public CLOB orderbook and market-channel schemas expose aggregate levels without direct-versus-mirrored provenance fields.",
        "official_references": [
            "https://docs.polymarket.com/api-reference/market-data/get-order-book",
            "https://docs.polymarket.com/market-data/websocket/market-channel",
        ],
    }
