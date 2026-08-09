import hashlib
import gzip
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from polymarket.discovery_v3 import (
    BoundedGammaDiscoveryClient,
    discover_markets_v3,
    HttpxGammaDiscoveryClient,
    monitor_discovery_loop,
    replay_discovery,
)


# Canonical H-011 V3 BTC 5-min Up/Down market fixture.
# Matches the production Polymarket contract (verified 2026-07-13 across
# 13 markets): slug = btc-updown-5m-<10-digit-epoch>, outcomes = ["Up","Down"],
# eventStartTime == slug_epoch, endDate = eventStartTime + 300s.
DEFAULT_SLUG_EPOCH = 1766162100  # 2025-12-19T16:35:00Z
DEFAULT_WINDOW_S = 300

# Fix #3: as_of_ts must be inside the market's window for selection.
# Use slug_epoch + 150 (midpoint of the 5-min window) as the default as_of_ts.
DEFAULT_AS_OF_TS = datetime.fromtimestamp(
    DEFAULT_SLUG_EPOCH + 150, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def market(*, cid="0x" + "a" * 64, slug_epoch=DEFAULT_SLUG_EPOCH,
           window_s=DEFAULT_WINDOW_S, outcomes=None,
           resolution_source="https://data.chain.link/streams/btc-usd",
           description=None, ticker=None, active=True, closed=False,
           override_slug=None, override_event_start=None, override_end_date=None,
           override_start_date=None):
    """Build a canonical btc-updown-5m market fixture.

    Defaults produce a market that PASSES validate_btc_market_identity.
    Override parameters to construct invalid variants for rejection tests.
    """
    slug = override_slug or f"btc-updown-5m-{slug_epoch}"
    event_start = override_event_start or datetime.fromtimestamp(
        slug_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_date = override_end_date or datetime.fromtimestamp(
        slug_epoch + window_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # startDate is ~24h before (lifecycle metadata, NOT the H-011 window)
    start_date = override_start_date or datetime.fromtimestamp(
        slug_epoch - 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if description is None:
        desc = (
            'This market will resolve to "Up" if the Bitcoin price at the end of '
            'the time range specified in the title is greater than or equal to '
            'the price at the beginning of that range. Otherwise, it will resolve '
            'to "Down".'
        )
    else:
        desc = description
    return {
        "conditionId": cid,
        "id": "900001",
        "slug": slug,
        "question": "Bitcoin Up or Down",
        "description": desc,
        "resolutionSource": resolution_source,
        "outcomes": outcomes or ["Up", "Down"],
        "clobTokenIds": [f"{cid}-up-token-synthetic", f"{cid}-down-token-synthetic"],
        "outcomePrices": '["0.48", "0.52"]',
        "startDate": start_date,
        "endDate": end_date,
        "startDateIso": start_date[:10],
        "endDateIso": end_date[:10],
        "eventStartTime": event_start,
        "active": active,
        "closed": closed,
        "acceptingOrders": True,
        "feesEnabled": True,
        "volumeNum": 5234.50,
        "negRisk": False,
        "events": [{
            "id": "109968",
            "ticker": ticker or slug,
            "slug": slug,
            "title": "Bitcoin Up or Down",
            "description": desc,
            "resolutionSource": resolution_source,
        }],
    }


class SequenceGamma:
    """Test double that returns pre-built market lists directly.

    Bypasses the real HTTP client. The `pages` field uses the new keyset
    format with endpoint='/events/keyset' for compatibility with replay.
    """
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def fetch_pages(self, limit, *, as_of_ts=None):
        value = self.responses[self.calls]
        self.calls += 1
        if isinstance(value, Exception):
            raise value
        source_exhausted = len(value) < limit
        return {
            "markets": value,
            "pages": [{"endpoint": "/events/keyset", "cursor": None,
                       "next_cursor": None, "limit": limit, "count": len(value)}],
            "source_exhausted": source_exhausted,
            "limit_reached": not source_exhausted,
            "next_offset": len(value) if not source_exhausted else None,
            "discovery_metrics": {
                "markets_api_objects": 0,
                "events_api_objects": len(value),
                "event_nested_markets_flattened": len(value),
                "markets_from_markets_endpoint": 0,
                "records_before_dedup": len(value),
                "unique_markets_after_dedup": len(value),
                "duplicates_removed": 0,
                "cross_source_conflicts_count": 0,
                "missing_identifiers_count": 0,
            },
            "endpoint_states": {
                "/events/keyset": {
                    "status": "exhausted" if source_exhausted else "limit_reached",
                    "error": None,
                    "loop_detected": False,
                    "api_objects_received": len(value),
                    "flattened_markets": len(value),
                }
            },
            "any_source_error": False,
        }


def config():
    return SimpleNamespace(window_s=300)


def test_first_cycle_empty_second_cycle_processes_new_btc_market(tmp_path):
    gamma = SequenceGamma([[], [market()]])
    discovered_counts = []

    def discover():
        return discover_markets_v3(config(), 500, gamma, evidence_dir=tmp_path,
                                   as_of_ts=DEFAULT_AS_OF_TS)

    results = monitor_discovery_loop(
        discover=discover,
        process=lambda result: discovered_counts.append(len(result["markets"])),
        sleep=lambda _: None,
        max_cycles=2,
    )
    assert gamma.calls == 2
    assert discovered_counts == [0, 1]
    assert results == [None, None]


def test_monitor_refreshes_gamma_every_cycle(tmp_path):
    gamma = SequenceGamma([[], [], []])
    monitor_discovery_loop(
        discover=lambda: discover_markets_v3(config(), 500, gamma, evidence_dir=tmp_path,
                                             as_of_ts=DEFAULT_AS_OF_TS),
        process=lambda result: result["status"],
        sleep=lambda _: None,
        max_cycles=3,
    )
    assert gamma.calls == 3


def test_window_300_rejects_structurally_valid_window_900(tmp_path):
    # Build a market with a 900s window (endDate - eventStartTime = 900s)
    gamma = SequenceGamma([[market(window_s=900)]])
    result = discover_markets_v3(config(), 500, gamma, evidence_dir=tmp_path,
                                 as_of_ts=DEFAULT_AS_OF_TS)
    assert result["status"] == "EMPTY_SELECTED_COHORT"
    assert result["evidence"]["rejection_histogram"]["window_duration_mismatch"] == 1


def test_rejection_histogram_preserves_all_reasons(tmp_path):
    invalid = {
        "id": "999999",
        "slug": "ethereum-market",
        "startDate": None,
        "endDate": None,
        "eventStartTime": None,
        "outcomes": ["Higher", "Lower"],
        "clobTokenIds": ["a", "b"],
        "outcomePrices": ["0.4", "0.6"],
        "resolutionSource": "",
        "description": "",
        "active": False,
        "closed": True,
    }
    gamma = SequenceGamma([[invalid]])
    result = discover_markets_v3(config(), 500, gamma, evidence_dir=tmp_path,
                                 as_of_ts=DEFAULT_AS_OF_TS)
    histogram = result["evidence"]["rejection_histogram"]
    assert histogram["missing_condition_id"] == 1
    assert histogram["directional_market_identity_unproven"] == 1
    assert histogram["token_direction_mapping_unproven"] == 1
    assert histogram["resolution_rule_unproven"] == 1
    assert histogram["market_inactive_or_closed"] == 1


def test_market_without_conditionId_or_id_rejected_with_missing_structural_identifier(tmp_path):
    invalid = {
        "slug": "anonymous-market",
        "outcomes": ["Up", "Down"],
        "clobTokenIds": ["a", "b"],
        "outcomePrices": ["0.4", "0.6"],
    }
    gamma = SequenceGamma([[invalid]])
    result = discover_markets_v3(config(), 500, gamma, evidence_dir=tmp_path,
                                 as_of_ts=DEFAULT_AS_OF_TS)
    histogram = result["evidence"]["rejection_histogram"]
    assert histogram["missing_structural_identifier"] == 1


def test_valid_market_after_position_200_is_discovered(tmp_path):
    generic = [
        {
            "conditionId": f"0x{i:064x}", "slug": f"generic-{i}",
            "outcomes": ["Yes", "No"], "clobTokenIds": [f"a{i}-synthetic", f"b{i}-synthetic"],
            "outcomePrices": '["0.4", "0.6"]',
        }
        for i in range(220)
    ]
    valid_market = market()
    gamma = SequenceGamma([generic + [valid_market]])
    result = discover_markets_v3(config(), 500, gamma, evidence_dir=tmp_path,
                                 as_of_ts=DEFAULT_AS_OF_TS)
    assert result["status"] == "SELECTED_NONEMPTY"
    assert result["evidence"]["total_received"] == 221
    assert result["markets"][0]["conditionId"] == valid_market["conditionId"]


def test_gamma_failure_is_not_empty_cohort(tmp_path):
    gamma = SequenceGamma([RuntimeError("Gamma unavailable")])
    result = discover_markets_v3(config(), 500, gamma, evidence_dir=tmp_path,
                                 as_of_ts=DEFAULT_AS_OF_TS)
    assert result["status"] == "DISCOVERY_SOURCE_FAILED"
    assert result["discovery_complete"] is False


def test_empty_gamma_is_source_empty(tmp_path):
    gamma = SequenceGamma([[]])
    result = discover_markets_v3(config(), 500, gamma, evidence_dir=tmp_path,
                                 as_of_ts=DEFAULT_AS_OF_TS)
    assert result["status"] == "DISCOVERY_SOURCE_EMPTY"
    assert result["discovery_complete"] is True


def test_discovery_sidecar_matches_exact_bytes(tmp_path):
    gamma = SequenceGamma([[market()]])
    result = discover_markets_v3(config(), 500, gamma, evidence_dir=tmp_path,
                                 as_of_ts=DEFAULT_AS_OF_TS)
    artifact = result["artifact_path"]
    with open(artifact, "rb") as handle:
        expected = hashlib.sha256(handle.read()).hexdigest()
    with open(artifact + ".sha256", encoding="ascii") as handle:
        sidecar = handle.read().strip()
    assert sidecar == expected
    assert result["file_sha256_matches"] is True


def test_keyset_client_finds_btc_updown_candidate(tmp_path):
    """Fix #1: keyset pagination via /events/keyset finds btc-updown-5m market."""
    valid_market = market()

    def handler(request):
        if request.url.path == "/events/keyset":
            return httpx.Response(200, json={
                "events": [{
                    "id": "109968",
                    "ticker": valid_market["slug"],
                    "slug": valid_market["slug"],
                    "markets": [valid_market],
                }],
                "next_cursor": None,  # source exhausted
            })
        elif request.url.path == f"/markets/slug/{valid_market['slug']}":
            return httpx.Response(200, json=valid_market)
        return httpx.Response(404)

    client = HttpxGammaDiscoveryClient(page_size=500, transport=httpx.MockTransport(handler))
    result = discover_markets_v3(config(), 500, client, evidence_dir=tmp_path,
                                 as_of_ts=DEFAULT_AS_OF_TS)
    assert result["status"] == "SELECTED_NONEMPTY"
    assert result["markets"][0]["conditionId"] == valid_market["conditionId"]
    assert result["evidence"]["source_exhausted"] is True


def test_keyset_client_truncated_when_max_pages_reached(tmp_path):
    """Fix #1: keyset pagination stops at max_pages_per_endpoint."""
    valid_market = market()
    call_count = 0

    def handler(request):
        nonlocal call_count
        if request.url.path == "/events/keyset":
            call_count += 1
            # Always return a next_cursor (never exhausts)
            return httpx.Response(200, json={
                "events": [],
                "next_cursor": f"cursor-{call_count}",
            })
        return httpx.Response(404)

    client = HttpxGammaDiscoveryClient(page_size=500, transport=httpx.MockTransport(handler),
                                       max_pages_per_endpoint=3)
    result = discover_markets_v3(config(), 500, client, evidence_dir=tmp_path,
                                 as_of_ts=DEFAULT_AS_OF_TS)
    # Hit max_pages → limit_reached
    assert result["evidence"]["source_exhausted"] is False
    assert result["evidence"]["limit_reached"] is True


def test_bounded_client_queries_only_current_slug_and_selects(tmp_path):
    valid_market = market()
    requested_paths = []

    def handler(request):
        requested_paths.append(request.url.path)
        if request.url.path == f"/markets/slug/{valid_market['slug']}":
            return httpx.Response(200, json=valid_market)
        if request.url.path == f"/events/slug/{valid_market['slug']}":
            return httpx.Response(200, json={
                "id": "109968",
                "slug": valid_market["slug"],
                "ticker": valid_market["slug"],
                "description": valid_market["description"],
                "resolutionSource": valid_market["resolutionSource"],
                "markets": [valid_market],
            })
        return httpx.Response(404)

    client = BoundedGammaDiscoveryClient(transport=httpx.MockTransport(handler))
    result = discover_markets_v3(
        config(), 1, client, evidence_dir=tmp_path, as_of_ts=DEFAULT_AS_OF_TS,
    )

    assert result["status"] == "SELECTED_NONEMPTY"
    assert result["discovery_complete"] is True
    assert result["discovery_replay_verified"] is True
    assert requested_paths == [
        f"/markets/slug/{valid_market['slug']}",
        f"/events/slug/{valid_market['slug']}",
    ]
    assert result["evidence"]["discovery_metrics"]["discovery_scope"] == (
        "bounded_current_btc_5m_slug"
    )


def test_bounded_client_dual_404_is_proven_empty_cohort(tmp_path):
    def handler(request):
        return httpx.Response(404)

    client = BoundedGammaDiscoveryClient(transport=httpx.MockTransport(handler))
    result = discover_markets_v3(
        config(), 1, client, evidence_dir=tmp_path, as_of_ts=DEFAULT_AS_OF_TS,
    )

    assert result["status"] == "DISCOVERY_SOURCE_EMPTY"
    assert result["discovery_complete"] is True
    assert result["discovery_replay_verified"] is True
    assert result["evidence"]["endpoint_states"]["/markets/slug"]["not_found"] == 1
    assert result["evidence"]["endpoint_states"]["/events/slug"]["not_found"] == 1


def test_bounded_client_asymmetric_404_fails_closed(tmp_path):
    valid_market = market()

    def handler(request):
        if request.url.path.startswith("/markets/slug/"):
            return httpx.Response(200, json=valid_market)
        return httpx.Response(404)

    client = BoundedGammaDiscoveryClient(transport=httpx.MockTransport(handler))
    result = discover_markets_v3(
        config(), 1, client, evidence_dir=tmp_path, as_of_ts=DEFAULT_AS_OF_TS,
    )

    assert result["status"] == "DISCOVERY_SOURCE_FAILED"
    assert result["discovery_complete"] is False
    assert result["markets"] == []
    assert "presence conflict" in result["evidence"]["source_error"]


def test_bounded_client_http_error_fails_closed(tmp_path):
    def handler(request):
        if request.url.path.startswith("/markets/slug/"):
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(404)

    client = BoundedGammaDiscoveryClient(transport=httpx.MockTransport(handler))
    result = discover_markets_v3(
        config(), 1, client, evidence_dir=tmp_path, as_of_ts=DEFAULT_AS_OF_TS,
    )

    assert result["status"] == "DISCOVERY_SOURCE_FAILED"
    assert result["discovery_complete"] is False
    assert result["markets"] == []


def test_evidence_is_compressed_atomic_and_replay_verified(tmp_path):
    valid_market = market()
    result = discover_markets_v3(config(), 500, SequenceGamma([[valid_market]]), evidence_dir=tmp_path,
                                 as_of_ts=DEFAULT_AS_OF_TS)
    artifact = tmp_path / result["artifact_path"]
    assert str(artifact).endswith(".json.gz")
    assert not list(tmp_path.glob("*.tmp"))
    evidence = json.loads(gzip.decompress(artifact.read_bytes()))
    assert evidence["raw_gamma"][0]["conditionId"] == valid_market["conditionId"]
    replay = replay_discovery(artifact, expected_selection_config=result["evidence"]["selection_config"])
    assert replay["discovery_replay_verified"] is True
    assert result["discovery_replay_verified"] is True


def test_discovery_replay_detects_tampered_selection(tmp_path):
    result = discover_markets_v3(config(), 500, SequenceGamma([[market()]]), evidence_dir=tmp_path,
                                 as_of_ts=DEFAULT_AS_OF_TS)
    artifact = tmp_path / result["artifact_path"]
    evidence = json.loads(gzip.decompress(artifact.read_bytes()))
    evidence["selected_condition_ids"] = ["tampered"]
    artifact.write_bytes(gzip.compress(json.dumps(evidence).encode(), mtime=0))
    replay = replay_discovery(artifact, expected_selection_config=result["evidence"]["selection_config"])
    assert replay["selected_ids_match"] is False
    assert replay["discovery_replay_verified"] is False


def test_retention_count_and_bytes_keep_newest(tmp_path):
    last_result = None
    for index in range(4):
        last_result = discover_markets_v3(
            config(), 500, SequenceGamma([[market(cid=f"0x{index:064x}")]]),
            evidence_dir=tmp_path, retention_count=2, retention_bytes=1,
            as_of_ts=DEFAULT_AS_OF_TS,
        )
    artifacts = list(tmp_path.glob("discovery_*.json.gz"))
    assert len(artifacts) == 1
    assert artifacts[0] == tmp_path / last_result["artifact_path"]
    assert artifacts[0].with_suffix(".gz.sha256").exists()


def test_v3_dockerfile_packages_discovery_module():
    dockerfile = (__import__("pathlib").Path(__file__).parents[2] / "polymarket" / "Dockerfile.h011-v3").read_text()
    assert "polymarket/discovery_v3.py" in dockerfile


@pytest.mark.parametrize("prices", [
    [], ["0.4"], ["0.2", "0.3", "0.5"], ["NaN", "0.5"],
    ["Infinity", "0.5"], ["-0.1", "1.1"], ["1.1", "-0.1"], ["nope", "1.0"],
])
def test_invalid_outcome_prices_are_rejected(tmp_path, prices):
    candidate = market()
    candidate["outcomePrices"] = prices
    result = discover_markets_v3(config(), 500, SequenceGamma([[candidate]]), evidence_dir=tmp_path,
                                 as_of_ts=DEFAULT_AS_OF_TS)
    assert result["status"] == "EMPTY_SELECTED_COHORT"
    assert result["evidence"]["rejection_histogram"]["invalid_outcome_prices"] == 1


def test_missing_outcome_prices_is_accepted(tmp_path):
    """Missing/None outcomePrices means "no trades yet" — this is valid for
    newly listed markets (e.g., btc-updown-5m markets that have not yet
    seen any trade activity). The market should still be discoverable."""
    candidate = market()
    candidate.pop("outcomePrices")
    result = discover_markets_v3(config(), 500, SequenceGamma([[candidate]]), evidence_dir=tmp_path,
                                 as_of_ts=DEFAULT_AS_OF_TS)
    assert result["status"] == "SELECTED_NONEMPTY"
    assert result["evidence"]["rejection_histogram"]["invalid_outcome_prices"] == 0


def test_none_outcome_prices_is_accepted(tmp_path):
    """outcomePrices=None (explicit None) is also accepted as "no trades yet"."""
    candidate = market()
    candidate["outcomePrices"] = None
    result = discover_markets_v3(config(), 500, SequenceGamma([[candidate]]), evidence_dir=tmp_path,
                                 as_of_ts=DEFAULT_AS_OF_TS)
    assert result["status"] == "SELECTED_NONEMPTY"
    assert result["evidence"]["rejection_histogram"]["invalid_outcome_prices"] == 0


def test_valid_and_resolved_outcome_prices_are_distinct(tmp_path):
    valid = discover_markets_v3(config(), 500, SequenceGamma([[market()]]), evidence_dir=tmp_path,
                                as_of_ts=DEFAULT_AS_OF_TS)
    assert valid["status"] == "SELECTED_NONEMPTY"
    resolved_market = market()
    resolved_market["outcomePrices"] = ["0.96", "0.04"]
    resolved = discover_markets_v3(config(), 500, SequenceGamma([[resolved_market]]), evidence_dir=tmp_path,
                                   as_of_ts=DEFAULT_AS_OF_TS)
    assert resolved["status"] == "EMPTY_SELECTED_COHORT"
    assert resolved["evidence"]["rejection_histogram"]["resolved_extreme_prices"] == 1


@pytest.mark.parametrize("field,value", [
    ("window_s", 900), ("max_markets", 3), ("gamma_limit", 999),
    ("resolved_price_threshold", 0.90),
])
def test_replay_rejects_tampered_selection_configuration(tmp_path, field, value):
    result = discover_markets_v3(config(), 500, SequenceGamma([[market()]]), evidence_dir=tmp_path,
                                 as_of_ts=DEFAULT_AS_OF_TS)
    artifact = tmp_path / result["artifact_path"]
    evidence = json.loads(gzip.decompress(artifact.read_bytes()))
    original = dict(evidence["selection_config"])
    evidence["selection_config"][field] = value
    artifact.write_bytes(gzip.compress(json.dumps(evidence).encode(), mtime=0))
    replay = replay_discovery(artifact, expected_selection_config=original)
    assert replay["discovery_replay_verified"] is False
    assert replay[{
        "window_s": "window_matches", "max_markets": "max_markets_matches",
        "gamma_limit": "gamma_limit_matches", "resolved_price_threshold": "price_threshold_matches",
    }[field]] is False


@pytest.mark.parametrize("mutation", [
    lambda evidence: evidence["pages"][0].update(count=-1),  # invalid negative count
    lambda evidence: evidence["pages"][0].update(limit=0),   # invalid limit
    lambda evidence: evidence.update(limit_reached=True),    # inconsistent flags
    lambda evidence: evidence.update(source_exhausted=False),
])
def test_replay_rejects_tampered_pagination_metadata(tmp_path, mutation):
    result = discover_markets_v3(config(), 500, SequenceGamma([[market()]]), evidence_dir=tmp_path,
                                 as_of_ts=DEFAULT_AS_OF_TS)
    artifact = tmp_path / result["artifact_path"]
    evidence = json.loads(gzip.decompress(artifact.read_bytes()))
    mutation(evidence)
    artifact.write_bytes(gzip.compress(json.dumps(evidence).encode(), mtime=0))
    replay = replay_discovery(artifact, expected_selection_config=result["evidence"]["selection_config"])
    assert replay["discovery_replay_verified"] is False
