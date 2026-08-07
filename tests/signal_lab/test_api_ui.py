from __future__ import annotations

import inspect

from fastapi import FastAPI

from polymarket.signal_lab.api import LIVE_TERMINAL_HTML, SignalLabService, build_router
from polymarket.signal_lab.contracts import RawEvent
from polymarket.signal_lab.sources import OfficialPolymarketEndpoints, mirrored_depth_provenance_finding


def _service() -> SignalLabService:
    service = SignalLabService()
    service.store.ingest(RawEvent.build(
        event_id="meta",
        event_type="MARKET_META",
        market_id="m1",
        token_id="yes",
        event_time="2026-08-07T00:00:00Z",
        received_time="2026-08-07T00:00:00Z",
        sequence_or_source_cursor="1",
        source="POLYMARKET_GAMMA_API",
        payload={"title": "BTC Up?", "category": "CRYPTO", "end_time": "2026-08-07T01:00:00Z"},
    ))
    service.store.ingest(RawEvent.build(
        event_id="book",
        event_type="BOOK_SNAPSHOT",
        market_id="m1",
        token_id="yes",
        event_time="2026-08-07T00:01:00Z",
        received_time="2026-08-07T00:01:00Z",
        sequence_or_source_cursor="2",
        source="POLYMARKET_CLOB_REST",
        payload={"bids": [{"price": "0.49", "size": "25"}], "asks": [{"price": "0.51", "size": "20"}]},
    ))
    return service


def test_system_truth_flags_are_unconditional():
    truth = _service().system_truth("2026-08-07T00:02:00Z")
    assert truth["paper_only"] is True
    assert truth["orders_enabled"] is False
    assert truth["live_capital_locked"] is True
    assert truth["real_order_network_calls"] == 0
    assert truth["wallet_or_private_key_access"] == 0
    assert truth["real_capital_actions"] == 0
    assert truth["replay_verified"] is True
    for key in (
        "ws_connected", "last_event_age", "sequence_gaps", "stale_data_count",
        "raw_chain_tip_hash", "active_experiment_id", "featureset_hash",
    ):
        assert key in truth


def test_api_has_no_live_execution_routes():
    router = build_router(_service())
    paths = {route.path: getattr(route, "methods", None) for route in router.routes}
    http_methods = set()
    for methods in paths.values():
        if methods:
            http_methods.update(methods)
    assert http_methods <= {"GET"}
    lowered = "\n".join(paths).lower()
    for forbidden in ("connect-wallet", "sign-transaction", "withdraw", "deposit", "live-position"):
        assert forbidden not in lowered
    assert "/signal-lab/ws" in paths


def test_router_contract_contains_all_read_only_surfaces():
    router = build_router(_service())
    paths = {route.path for route in router.routes}
    assert "/signal-lab" in paths
    assert "/signal-lab/api/system-truth" in paths
    assert "/signal-lab/api/markets" in paths
    assert "/signal-lab/api/evidence" in paths


def test_desktop_responsive_smoke():
    html = LIVE_TERMINAL_HTML
    assert 'grid-template-columns:240px minmax(420px,1fr) 330px' in html
    assert "MARKET RADAR" in html
    assert "SENEX SIGNAL / FAIR VALUE" in html
    assert "SYSTEM TRUTH" in html
    assert "EXPERIMENTS" in html and "EVIDENCE" in html


def test_mobile_responsive_smoke():
    html = LIVE_TERMINAL_HTML
    assert 'name="viewport"' in html
    assert "@media(max-width:680px)" in html
    assert ".shell{display:block}" in html
    assert "grid-template-columns:repeat(2,1fr)" in html


def test_ui_contains_no_trading_controls_and_no_false_alpha_claims():
    html = LIVE_TERMINAL_HTML.lower()
    assert "connect wallet" not in html
    assert "order entry" not in html
    assert "buy now" not in html
    assert "sell now" not in html
    assert "edge confirmed" not in html
    assert "alpha" not in html
    assert "unvalidated" in html
    assert "paper only" in html


def test_official_source_policy_is_public_market_only():
    endpoints = OfficialPolymarketEndpoints()
    assert endpoints.gamma_api == "https://gamma-api.polymarket.com"
    assert endpoints.clob_rest == "https://clob.polymarket.com"
    assert endpoints.data_api == "https://data-api.polymarket.com"
    assert endpoints.clob_market_ws.endswith("/ws/market")
    finding = mirrored_depth_provenance_finding()
    assert finding["result"] == "NOT_OBSERVABLE_FROM_PUBLIC_DATA"
    assert finding["claim_direct_synthetic_provenance_available"] is False
    assert finding["heuristic_promoted_to_provenance"] is False


def test_source_adapter_has_no_authenticated_mutation_methods():
    from polymarket.signal_lab.sources import OfficialPolymarketSource
    names = {name.lower() for name, value in inspect.getmembers(OfficialPolymarketSource, predicate=inspect.isfunction)}
    assert not ({"create_order", "place_order", "cancel_order", "sign_transaction"} & names)
