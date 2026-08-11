"""
SENECIO Polymarket Connector — H-010 Edge Detection + H-011 Arbitrage
======================================================================
Read-only connector to Polymarket Gamma API + CLOB API.
Fetches active election markets with P > 0.70 (favorite-longshot bias signal).
Extended with orderbook retrieval for liquidity arbitrage scanning (H-011).

API Reference:
  - Gamma API: https://gamma-api.polymarket.com (market data, events)
  - CLOB API:  https://clob.polymarket.com (orderbook, price levels)

Dependencies: httpx (stdlib + httpx only)
"""
from __future__ import annotations
import json, os, sys
from datetime import datetime, timezone
from typing import Optional

import httpx

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

ELECTION_KW = ["election", "president", "vote", "candidate", "congress",
               "senate", "governor", "parliament", "mayor", "prime minister"]
P_THRESHOLD = 0.70       # H-010 signal threshold
MIN_VOLUME = 10_000      # H-010 minimum volume (USD)
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")


# ═══════════════════════════════════════════════════════════════════════
# Gamma API — Market Data (existing H-010)
# ═══════════════════════════════════════════════════════════════════════

def fetch_election_events(limit: int = 50) -> list[dict]:
    """Fetch active events that match election keywords."""
    with httpx.Client(timeout=30.0) as c:
        r = c.get(f"{GAMMA_BASE}/events", params={"limit": limit, "active": "true", "closed": "false"})
        r.raise_for_status()
        events = r.json()
    return [e for e in events if any(kw in (e.get("title", "") + e.get("slug", "")).lower() for kw in ELECTION_KW)]


def extract_signal_markets(events: list[dict]) -> list[dict]:
    """Extract markets where YES probability > P_THRESHOLD (favorite-longshot signal)."""
    signals = []
    for ev in events:
        for m in ev.get("markets", []):
            try:
                prices = json.loads(m.get("outcomePrices", "[]"))
                if len(prices) < 2:
                    continue
                p_yes = float(prices[0])
                vol = float(m.get("volumeNum", 0))
                if p_yes >= P_THRESHOLD and vol >= MIN_VOLUME and m.get("active") and not m.get("closed"):
                    signals.append({
                        "market_id": m.get("id"), "question": m.get("question", "")[:200],
                        "p_yes": round(p_yes, 4), "p_no": round(1 - p_yes, 4),
                        "volume_usd": round(vol, 2), "end_date": m.get("endDateIso", ""),
                        "signal": "FADE_YES", "event_title": ev.get("title", "")[:100],
                        "snapshot_utc": datetime.now(timezone.utc).isoformat(),
                        "fee_bps": int(m.get("takerBaseFee", 1000)),
                    })
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
    return signals


def fetch_all_active_markets(limit: int = 200) -> list[dict]:
    """
    Fetch ALL active binary markets from Gamma API.
    Used by liquidity_scanner.py for arbitrage detection.
    Returns raw market dicts with full metadata.
    """
    all_markets = []
    offset = 0
    page_size = min(limit, 100)  # API limit per request

    with httpx.Client(timeout=30.0) as c:
        while len(all_markets) < limit:
            r = c.get(f"{GAMMA_BASE}/markets", params={
                "limit": page_size,
                "offset": offset,
                "active": "true",
                "closed": "false",
            })
            if r.status_code != 200:
                print(f"  [Gamma] Error fetching markets page: {r.status_code}")
                break

            page = r.json()
            if not page:
                break

            all_markets.extend(page)
            offset += page_size

            if len(page) < page_size:
                break  # no more pages

    return all_markets[:limit]


def fetch_market_by_id(market_id: str) -> Optional[dict]:
    """Fetch a single market by its condition_id / token_id from Gamma API."""
    with httpx.Client(timeout=15.0) as c:
        try:
            r = c.get(f"{GAMMA_BASE}/markets/{market_id}")
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
    return None


# ═══════════════════════════════════════════════════════════════════════
# CLOB API — Orderbook Data (H-011 Arbitrage)
# ═══════════════════════════════════════════════════════════════════════

def fetch_orderbook(market_id: str) -> Optional[dict]:
    """
    Fetch the full orderbook for a binary market from the Polymarket CLOB API.

    The CLOB API returns bids and asks with price and size.
    Endpoint: GET /book?token_id={market_id}

    Returns dict:
    {
        "market_id": str,
        "bids": [{"price": float, "size": float}, ...],  # sorted desc by price
        "asks": [{"price": float, "size": float}, ...],  # sorted asc by price
        "best_bid": float or None,
        "best_ask": float or None,
        "spread": float or None,
        "snapshot_utc": str,
    }

    Note: Polymarket uses condition_id as the market identifier for the CLOB.
    The Gamma API's market "id" maps to condition_id in CLOB.
    If the market_id is a slug, we need to resolve it first.
    """
    result = {
        "market_id": market_id,
        "bids": [],
        "asks": [],
        "best_bid": None,
        "best_ask": None,
        "spread": None,
        "snapshot_utc": datetime.now(timezone.utc).isoformat(),
    }

    try:
        with httpx.Client(timeout=15.0) as c:
            # Try CLOB book endpoint
            r = c.get(f"{CLOB_BASE}/book", params={"token_id": market_id})
            if r.status_code != 200:
                # Fallback: try market-wide endpoint
                r = c.get(f"{CLOB_BASE}/markets/{market_id}/book")
                if r.status_code != 200:
                    return result
            data = r.json()

        # Parse orderbook data
        # CLOB API returns: {"market": str, "asset_id": str, "bids": [...], "asks": [...]}
        raw_bids = data.get("bids", [])
        raw_asks = data.get("asks", [])

        bids = []
        for b in raw_bids:
            try:
                price = float(b.get("price", 0))
                size = float(b.get("size", 0))
                if price > 0 and size > 0:
                    bids.append({"price": price, "size": size})
            except (ValueError, TypeError):
                continue

        asks = []
        for a in raw_asks:
            try:
                price = float(a.get("price", 0))
                size = float(a.get("size", 0))
                if price > 0 and size > 0:
                    asks.append({"price": price, "size": size})
            except (ValueError, TypeError):
                continue

        # Sort bids descending, asks ascending
        bids.sort(key=lambda x: x["price"], reverse=True)
        asks.sort(key=lambda x: x["price"])

        result["bids"] = bids
        result["asks"] = asks

        if bids:
            result["best_bid"] = bids[0]["price"]
        if asks:
            result["best_ask"] = asks[0]["price"]
        if result["best_bid"] and result["best_ask"]:
            result["spread"] = round(result["best_ask"] - result["best_bid"], 4)

    except httpx.TimeoutException:
        print(f"  [CLOB] Timeout fetching orderbook for {market_id}")
    except Exception as e:
        print(f"  [CLOB] Error fetching orderbook for {market_id}: {e}")

    return result


def fetch_price_levels(market_id: str) -> Optional[dict]:
    """
    Fetch mid-market price levels from CLOB API.
    Simpler alternative to full orderbook — just last trade + best bid/ask.

    Endpoint: GET /prices?token_id={market_id}
    """
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.get(f"{CLOB_BASE}/prices", params={"token_id": market_id})
            if r.status_code != 200:
                return None
            data = r.json()
            return {
                "market_id": market_id,
                "last_price": data.get("last_price"),
                "best_bid": data.get("best_bid"),
                "best_ask": data.get("best_ask"),
                "mid": data.get("mid"),
                "snapshot_utc": datetime.now(timezone.utc).isoformat(),
            }
    except Exception as e:
        print(f"  [CLOB] Error fetching prices for {market_id}: {e}")
        return None


def persist_to_supabase(signals: list[dict]) -> int:
    """Insert signal markets into Supabase polymarket_markets table."""
    if not signals:
        return 0
    with httpx.Client(base_url=f"{SUPABASE_URL}/rest/v1", headers={
        "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json", "Prefer": "return=minimal",
    }, timeout=15.0) as c:
        rows = [{
            "market_id": s["market_id"], "question": s["question"],
            "p_yes": s["p_yes"], "p_no": s["p_no"],
            "volume_usd": s["volume_usd"], "end_date": s["end_date"],
            "signal": s["signal"], "event_title": s["event_title"],
            "snapshot_utc": s["snapshot_utc"], "fee_bps": s["fee_bps"],
            "outcome": None, "pnl_net": None,
        } for s in signals]
        r = c.post("/polymarket_markets", json=rows)
        r.raise_for_status()
    return len(rows)


if __name__ == "__main__":
    print("H-010 Polymarket Connector — fetching election markets...")
    events = fetch_election_events(limit=50)
    print(f"  Election events found: {len(events)}")
    signals = extract_signal_markets(events)
    print(f"  Signal markets (P_YES >= {P_THRESHOLD}, vol >= {MIN_VOLUME}): {len(signals)}")
    for s in signals[:10]:
        print(f"    {s['p_yes']:.2f} | vol={s['volume_usd']:.0f} | {s['question'][:70]}")
    if signals:
        n = persist_to_supabase(signals)
        print(f"  Persisted to Supabase: {n}")
    else:
        print("  No signals to persist.")

    # H-011: Test orderbook fetch
    print("\nH-011: Testing orderbook fetch...")
    if signals:
        test_id = signals[0]["market_id"]
        book = fetch_orderbook(test_id)
        print(f"  Orderbook for {test_id[:20]}...: {len(book.get('bids', []))} bids, {len(book.get('asks', []))} asks")
        if book.get("best_bid") and book.get("best_ask"):
            print(f"  Best bid: {book['best_bid']:.4f}  Best ask: {book['best_ask']:.4f}  Spread: {book['spread']:.4f}")
        else:
            print(f"  No orderbook data available (may need condition_id instead of market id)")
