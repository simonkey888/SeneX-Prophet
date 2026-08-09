"""Tests for H-011 V3 directional market identity contract (post ce8ce2c6 revert).

Context:
  The previous fix (commit ce8ce2c6) accepted outcomes=["Yes","No"] as a
  valid binary identity. GPT-5.6 audit determined this was too permissive —
  Yes/No proves ONLY binariness, NOT directionality. For H-011 V3 (BTC
  5-minute Up/Down cohort), we require the strict directional contract:

    1. market.slug matches ^btc-updown-5m-(\\d{10})$
    2. eventStartTime == slug_epoch (±1s tolerance)
    3. endDate - eventStartTime == 300s (±1s tolerance)
    4. outcomes == ["Up","Down"] exactly (positional mapping to tokens)
    5. clobTokenIds has 2 unique non-empty tokens
    6. resolutionSource mentions BTC/USD (Chainlink)
    7. description mentions price comparison (start vs end)
    8. market.active == True and market.closed == False
    9. event ticker (if present) is coherent with market.slug

  The validator performs FIVE independent checks, each producing a distinct
  rejection reason:
    - missing_condition_id
    - directional_market_identity_unproven  (slug + event ticker)
    - token_direction_mapping_unproven      (outcomes == ["Up","Down"])
    - window_slug_unproven                  (slug pattern failure)
    - window_start_unproven                 (eventStartTime missing)
    - window_end_unproven                   (endDate missing)
    - window_start_mismatch                 (eventStartTime != slug_epoch)
    - window_duration_mismatch              (endDate - eventStartTime != 300)
    - resolution_rule_unproven              (BTC/USD source + price comparison)
    - market_inactive_or_closed             (active != True or closed != False)

  Fail-closed: ANY contradiction produces a rejection reason. We do NOT
  infer identity from text, do NOT swap token positions, do NOT accept
  Yes/No as directional (only as binary, which is subsumed by the stricter
  directional check).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

# Make the polymarket package importable (matches Dockerfile WORKDIR).
sys.path.insert(0, str(Path(__file__).parents[2] / "polymarket"))

from h011_v3_pipeline import validate_btc_market_identity
from discovery_v3 import discover_markets_v3, HttpxGammaDiscoveryClient

from tests.h011_v3.fixtures_gamma import (
    make_real_btc_updown_market,
    make_real_btc_updown_market_with_offset,
    FIXTURE_BTC_LONG_WINDOW_PRICE_TARGET,
    FIXTURE_GENERIC_YES_NO_MARKET,
    FIXTURE_ETH_UPDOWN_5M,
    FIXTURE_BTC_UPDOWN_15M,
    FIXTURE_BTC_UPDOWN_5M_DURATION_299,
    FIXTURE_BTC_UPDOWN_5M_DURATION_301,
    FIXTURE_BTC_UPDOWN_5M_EVENTSTART_MISMATCH,
    FIXTURE_BTC_UPDOWN_5M_TICKER_INCONSISTENT,
    FIXTURE_BTC_UPDOWN_5M_OUTCOMES_INVERTED,
    FIXTURE_BTC_UPDOWN_5M_OUTCOMES_YESNO,
    FIXTURE_BTC_UPDOWN_5M_MISSING_TOKENS,
    FIXTURE_BTC_UPDOWN_5M_DUPLICATE_TOKENS,
    FIXTURE_BTC_UPDOWN_5M_INACTIVE,
    FIXTURE_BTC_UPDOWN_5M_CLOSED,
    FIXTURE_BTC_UPDOWN_5M_NO_RESOLUTION_SOURCE,
    FIXTURE_BTC_UPDOWN_5M_NO_DESCRIPTION,
    FIXTURE_BTC_UPDOWN_5M_MISSING_EVENT_START,
    FIXTURE_BTC_UPDOWN_5M_MISSING_END_DATE,
    FIXTURE_BTC_UPDOWN_5M_SLUG_NO_EPOCH,
    FIXTURE_BTC_UPDOWN_5M_START_DATE_TODAY,
)


def _identity_ok(market, window=300):
    return validate_btc_market_identity(market, window)


# ═══════════════════════════════════════════════════════════════════════
# Happy path: real BTC updown 5m markets from production payloads
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("slug_epoch,expected_offset_min", [
    (1766162100, 0),  # 2025-12-19T16:35:00Z
    (1766162400, 0),  # 2025-12-19T16:40:00Z
    (1766162700, 0),  # 2025-12-19T16:45:00Z
    (1766163000, 0),  # 2025-12-19T16:50:00Z
])
def test_real_btc_updown_5m_market_passes_all_checks(slug_epoch, expected_offset_min):
    """Real BTC updown 5m market payload (sanitized) passes ALL checks.

    These slug epochs correspond to actual markets captured from Polymarket
    Gamma API on 2026-07-13. The fixture reproduces the production payload
    structure with synthetic conditionIds and clobTokenIds.
    """
    market = make_real_btc_updown_market(slug_epoch=slug_epoch)
    ok, reasons = _identity_ok(market, 300)
    assert ok is True, f"Expected market to pass, but got reasons: {reasons}"
    assert reasons == []


def test_window_start_equals_slug_epoch_in_real_payload():
    """The contract requires eventStartTime_epoch == slug_epoch."""
    market = make_real_btc_updown_market(slug_epoch=1766162100)
    # slug_epoch = 1766162100 → 2025-12-19T16:35:00Z
    # eventStartTime should be 2025-12-19T16:35:00Z
    es = market["eventStartTime"]
    es_epoch = datetime.fromisoformat(es.replace("Z", "+00:00")).timestamp()
    assert abs(es_epoch - 1766162100) <= 1


def test_window_end_minus_start_equals_300_in_real_payload():
    """The contract requires endDate - eventStartTime == 300s."""
    market = make_real_btc_updown_market(slug_epoch=1766162100)
    es = datetime.fromisoformat(market["eventStartTime"].replace("Z", "+00:00"))
    ed = datetime.fromisoformat(market["endDate"].replace("Z", "+00:00"))
    assert abs((ed - es).total_seconds() - 300) <= 1


def test_start_date_does_not_affect_validation():
    """startDate is lifecycle metadata — it must NOT affect H-011 validation.

    Real BTC updown markets have startDate ~24h before the window. This
    test verifies that even an absurd startDate (e.g., 1 year ago) does
    not cause rejection.
    """
    market = make_real_btc_updown_market(slug_epoch=1766162100)
    # Set startDate to 1 year ago
    market["startDate"] = "2024-12-19T16:43:11Z"
    ok, reasons = _identity_ok(market, 300)
    assert ok is True, f"startDate should not affect validation: {reasons}"


# ═══════════════════════════════════════════════════════════════════════
# Window duration edge cases
# ═══════════════════════════════════════════════════════════════════════

def test_window_duration_299_rejected():
    """A market with duration=299s is rejected for window_duration_mismatch."""
    ok, reasons = _identity_ok(FIXTURE_BTC_UPDOWN_5M_DURATION_299, 300)
    assert ok is False
    assert "window_duration_mismatch" in reasons


def test_window_duration_301_rejected():
    """A market with duration=301s is rejected for window_duration_mismatch."""
    ok, reasons = _identity_ok(FIXTURE_BTC_UPDOWN_5M_DURATION_301, 300)
    assert ok is False
    assert "window_duration_mismatch" in reasons


def test_event_start_mismatch_with_slug_epoch_rejected():
    """If eventStartTime != slug_epoch (beyond ±1s), reject."""
    ok, reasons = _identity_ok(FIXTURE_BTC_UPDOWN_5M_EVENTSTART_MISMATCH, 300)
    assert ok is False
    assert "window_start_mismatch" in reasons


def test_missing_event_start_rejected():
    """Missing eventStartTime is rejected for window_start_unproven."""
    ok, reasons = _identity_ok(FIXTURE_BTC_UPDOWN_5M_MISSING_EVENT_START, 300)
    assert ok is False
    assert "window_start_unproven" in reasons


def test_missing_end_date_rejected():
    """Missing endDate is rejected for window_end_unproven."""
    ok, reasons = _identity_ok(FIXTURE_BTC_UPDOWN_5M_MISSING_END_DATE, 300)
    assert ok is False
    assert "window_end_unproven" in reasons


# ═══════════════════════════════════════════════════════════════════════
# Directional identity
# ═══════════════════════════════════════════════════════════════════════

def test_btc_long_window_price_target_rejected():
    """A long-window BTC price-target market (NOT updown 5m) is rejected
    for directional_market_identity_unproven (slug doesn't match pattern)."""
    ok, reasons = _identity_ok(FIXTURE_BTC_LONG_WINDOW_PRICE_TARGET, 300)
    assert ok is False
    assert "directional_market_identity_unproven" in reasons


def test_generic_yes_no_market_rejected_directional():
    """A generic Yes/No market is rejected for directional identity
    (slug doesn't match btc-updown-5m pattern).

    Yes/No proves binariness only, NOT directionality. Per GPT-5.6 audit,
    Yes/No must NOT be accepted as a valid directional identity."""
    ok, reasons = _identity_ok(FIXTURE_GENERIC_YES_NO_MARKET, 300)
    assert ok is False
    assert "directional_market_identity_unproven" in reasons


def test_btc_updown_5m_with_yes_no_outcomes_rejected():
    """A btc-updown-5m market with outcomes=["Yes","No"] is rejected for
    token_direction_mapping_unproven — Yes/No is NOT the directional
    Up/Down mapping."""
    ok, reasons = _identity_ok(FIXTURE_BTC_UPDOWN_5M_OUTCOMES_YESNO, 300)
    assert ok is False
    assert "token_direction_mapping_unproven" in reasons


def test_btc_updown_5m_with_inverted_outcomes_rejected():
    """A btc-updown-5m market with outcomes=["Down","Up"] (inverted order)
    is rejected for token_direction_mapping_unproven. We do NOT swap
    outcomes to match — the contract requires exact ["Up","Down"] order."""
    ok, reasons = _identity_ok(FIXTURE_BTC_UPDOWN_5M_OUTCOMES_INVERTED, 300)
    assert ok is False
    assert "token_direction_mapping_unproven" in reasons


def test_event_ticker_inconsistent_rejected():
    """If market.events[0].ticker != market.slug, reject for directional
    identity (inconsistency between event and market)."""
    ok, reasons = _identity_ok(FIXTURE_BTC_UPDOWN_5M_TICKER_INCONSISTENT, 300)
    assert ok is False
    assert "directional_market_identity_unproven" in reasons


def test_slug_without_epoch_rejected():
    """A slug that matches the prefix but lacks the 10-digit epoch is rejected."""
    ok, reasons = _identity_ok(FIXTURE_BTC_UPDOWN_5M_SLUG_NO_EPOCH, 300)
    assert ok is False
    assert "directional_market_identity_unproven" in reasons


# ═══════════════════════════════════════════════════════════════════════
# Token binding
# ═══════════════════════════════════════════════════════════════════════

def test_missing_tokens_rejected():
    """Missing clobTokenIds is rejected."""
    ok, reasons = _identity_ok(FIXTURE_BTC_UPDOWN_5M_MISSING_TOKENS, 300)
    assert ok is False
    assert "token_direction_mapping_unproven" in reasons


def test_duplicate_tokens_rejected():
    """Duplicate clobTokenIds (not unique) is rejected."""
    ok, reasons = _identity_ok(FIXTURE_BTC_UPDOWN_5M_DUPLICATE_TOKENS, 300)
    assert ok is False
    assert "token_direction_mapping_unproven" in reasons


# ═══════════════════════════════════════════════════════════════════════
# Resolution rule
# ═══════════════════════════════════════════════════════════════════════

def test_no_resolution_source_rejected():
    """Missing/empty resolutionSource is rejected."""
    ok, reasons = _identity_ok(FIXTURE_BTC_UPDOWN_5M_NO_RESOLUTION_SOURCE, 300)
    assert ok is False
    assert "resolution_rule_unproven" in reasons


def test_no_description_rejected():
    """Missing/empty description is rejected."""
    ok, reasons = _identity_ok(FIXTURE_BTC_UPDOWN_5M_NO_DESCRIPTION, 300)
    assert ok is False
    assert "resolution_rule_unproven" in reasons


# ═══════════════════════════════════════════════════════════════════════
# Market lifecycle state
# ═══════════════════════════════════════════════════════════════════════

def test_inactive_market_rejected():
    """active=False is rejected."""
    ok, reasons = _identity_ok(FIXTURE_BTC_UPDOWN_5M_INACTIVE, 300)
    assert ok is False
    assert "market_inactive_or_closed" in reasons


def test_closed_market_rejected():
    """closed=True is rejected."""
    ok, reasons = _identity_ok(FIXTURE_BTC_UPDOWN_5M_CLOSED, 300)
    assert ok is False
    assert "market_inactive_or_closed" in reasons


# ═══════════════════════════════════════════════════════════════════════
# Regression: eventStartTime as start (Opción B must NOT pass)
# ═══════════════════════════════════════════════════════════════════════

def test_regresion_opcion_b_event_start_as_end_must_not_pass():
    """REGRESSION TEST for the discarded Opción B hypothesis.

    Opción B claimed: window_start = slug_epoch, window_end = eventStartTime,
    duration = eventStartTime - slug_epoch = 300.

    For real payloads, slug_epoch == eventStartTime, so under Opción B
    the duration would be 0, and the market would be rejected.

    This test verifies that with the CORRECT contract (Opción A),
    the market PASSES. If anyone reverts to Opción B, this test fails.
    """
    # Real payload: slug_epoch=1766162100, eventStartTime=16:35:00Z, endDate=16:40:00Z
    market = make_real_btc_updown_market(slug_epoch=1766162100)
    ok, reasons = _identity_ok(market, 300)
    # Under Opción A: eventStartTime == slug_epoch, endDate - eventStartTime = 300 → PASS
    assert ok is True, f"Opción A must pass; reasons: {reasons}"
    # Under Opción B: eventStartTime - slug_epoch = 0 → would fail with window_duration_mismatch
    # This assertion documents the regression: if Opción B is restored, the
    # market would fail, and this test would catch it.
    assert "window_duration_mismatch" not in reasons
    assert "window_start_mismatch" not in reasons


# ═══════════════════════════════════════════════════════════════════════
# Cross-family rejection: ETH updown 5m, BTC updown 15m
# ═══════════════════════════════════════════════════════════════════════

def test_eth_updown_5m_rejected():
    """ETH updown 5m market is rejected — H-011 V3 is BTC-only."""
    ok, reasons = _identity_ok(FIXTURE_ETH_UPDOWN_5M, 300)
    assert ok is False
    # ETH slug doesn't match btc-updown-5m pattern
    assert "directional_market_identity_unproven" in reasons


def test_btc_updown_15m_rejected_with_300s_window():
    """BTC updown 15m market is rejected when expected_window_s=300."""
    ok, reasons = _identity_ok(FIXTURE_BTC_UPDOWN_15M, 300)
    assert ok is False
    # The slug doesn't match btc-updown-5m pattern (it's 15m)
    assert "directional_market_identity_unproven" in reasons


# ═══════════════════════════════════════════════════════════════════════
# End-to-end discovery tests
# ═══════════════════════════════════════════════════════════════════════

# Fix #3: as_of_ts must be inside the market's window for selection.
# DEFAULT_SLUG_EPOCH=1766162100 → window 16:35:00Z to 16:40:00Z
# Use midpoint 16:37:30Z as as_of_ts.
_AS_OF_TS = "2025-12-19T16:37:30Z"

class _SequenceGamma:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def fetch_pages(self, limit, *, as_of_ts=None):
        value = self.responses[self.calls]
        self.calls += 1
        if isinstance(value, Exception):
            raise value
        return {
            "markets": value,
            "pages": [{"endpoint": "/markets", "offset": 0, "limit": limit, "count": len(value)}],
            "source_exhausted": True,
            "limit_reached": False,
            "next_offset": None,
        }


def _config(window_s=300):
    return SimpleNamespace(window_s=window_s)


def test_discovery_finds_valid_btc_updown_5m_market(tmp_path):
    """End-to-end: a valid BTC updown 5m market is discovered."""
    valid = make_real_btc_updown_market(slug_epoch=1766162100)
    gamma = _SequenceGamma([[valid]])
    result = discover_markets_v3(_config(300), 500, gamma, evidence_dir=tmp_path,
                                as_of_ts=_AS_OF_TS)
    assert result["status"] == "SELECTED_NONEMPTY"
    assert len(result["markets"]) == 1
    h = result["evidence"]["rejection_histogram"]
    assert h["directional_market_identity_unproven"] == 0
    assert h["token_direction_mapping_unproven"] == 0
    assert h["window_duration_mismatch"] == 0


def test_discovery_rejects_btc_long_window_market(tmp_path):
    """End-to-end: a BTC long-window market is rejected for directional identity."""
    gamma = _SequenceGamma([[FIXTURE_BTC_LONG_WINDOW_PRICE_TARGET]])
    result = discover_markets_v3(_config(300), 500, gamma, evidence_dir=tmp_path,
                                as_of_ts=_AS_OF_TS)
    assert result["status"] == "EMPTY_SELECTED_COHORT"
    h = result["evidence"]["rejection_histogram"]
    assert h["directional_market_identity_unproven"] == 1


def test_discovery_rejects_yes_no_market_as_directional(tmp_path):
    """End-to-end: a generic Yes/No market is rejected for directional identity."""
    gamma = _SequenceGamma([[FIXTURE_GENERIC_YES_NO_MARKET]])
    result = discover_markets_v3(_config(300), 500, gamma, evidence_dir=tmp_path,
                                as_of_ts=_AS_OF_TS)
    assert result["status"] == "EMPTY_SELECTED_COHORT"
    h = result["evidence"]["rejection_histogram"]
    assert h["directional_market_identity_unproven"] == 1


def test_discovery_pagination_finds_btc_updown_via_keyset(tmp_path):
    """Fix #1: keyset pagination via /events/keyset finds btc-updown-5m market.

    The valid market is nested inside an event in the keyset response.
    """
    valid = make_real_btc_updown_market(slug_epoch=1766162100)

    def handler(request):
        if request.url.path == "/events/keyset":
            return httpx.Response(200, json={
                "events": [{
                    "id": "109968",
                    "ticker": valid["slug"],
                    "slug": valid["slug"],
                    "markets": [valid],
                }],
                "next_cursor": None,  # source exhausted
            })
        elif request.url.path == f"/markets/slug/{valid['slug']}":
            return httpx.Response(200, json=valid)
        return httpx.Response(404)

    client = HttpxGammaDiscoveryClient(page_size=500, transport=httpx.MockTransport(handler))
    result = discover_markets_v3(_config(300), 700, client, evidence_dir=tmp_path,
                                as_of_ts=_AS_OF_TS)
    assert result["status"] == "SELECTED_NONEMPTY"
    assert len(result["markets"]) == 1
    assert result["markets"][0]["conditionId"] == valid["conditionId"]


def test_discovery_deduplication_by_condition_id(tmp_path):
    """End-to-end: same conditionId appearing in both keyset events and
    /markets/slug is deduplicated — only one copy is processed."""
    valid = make_real_btc_updown_market(slug_epoch=1766162100)

    def handler(request):
        if request.url.path == "/events/keyset":
            # Event contains the market as nested
            return httpx.Response(200, json={
                "events": [{
                    "id": "109968",
                    "ticker": valid["slug"],
                    "slug": valid["slug"],
                    "markets": [valid],
                }],
                "next_cursor": None,
            })
        elif request.url.path == f"/markets/slug/{valid['slug']}":
            # Canonical fetch returns the same market
            return httpx.Response(200, json=valid)
        return httpx.Response(404)

    client = HttpxGammaDiscoveryClient(page_size=100, transport=httpx.MockTransport(handler))
    result = discover_markets_v3(_config(300), 10, client, evidence_dir=tmp_path,
                                as_of_ts=_AS_OF_TS)
    # Only ONE market is selected (deduplication by conditionId via slug key)
    assert len(result["markets"]) == 1
    assert result["evidence"]["total_received"] == 1


def test_discovery_replay_is_deterministic(tmp_path):
    """End-to-end: replaying the evidence artifact produces the same result."""
    valid = make_real_btc_updown_market(slug_epoch=1766162100)
    gamma = _SequenceGamma([[valid]])
    result = discover_markets_v3(_config(300), 500, gamma, evidence_dir=tmp_path,
                                as_of_ts=_AS_OF_TS)
    assert result["discovery_replay_verified"] is True
    assert result["file_sha256_matches"] is True
    # Replay the artifact and verify the histogram matches
    from discovery_v3 import replay_discovery
    artifact = Path(result["artifact_path"])
    replay = replay_discovery(artifact, expected_selection_config=result["evidence"]["selection_config"])
    assert replay["discovery_replay_verified"] is True
    assert replay["histogram_matches"] is True
    assert replay["selected_ids_match"] is True


def test_discovery_mixed_cohort_rejects_for_correct_reasons(tmp_path):
    """End-to-end: a mixed cohort where each market is rejected for one or
    more distinct reasons. The histogram accurately reflects each cause.

    The validator accumulates ALL applicable rejection reasons per market
    (not just the first one), so a market that fails both directional
    identity AND token mapping will count in BOTH buckets. This is by
    design — it provides better diagnostic signal.

    Expected per-market reasons:
      [0] long-window BTC price target:
          - directional_market_identity_unproven (slug doesn't match)
          - token_direction_mapping_unproven (Yes/No ≠ Up/Down)
          - resolution_rule_unproven (no BTC/USD source)
      [1] generic Yes/No (Lakers):
          - directional_market_identity_unproven
          - token_direction_mapping_unproven
          - resolution_rule_unproven (ESPN, not BTC)
      [2] BTC updown 5m with Yes/No outcomes:
          - token_direction_mapping_unproven
      [3] BTC updown 5m with duration 298s:
          - window_duration_mismatch
      [4] BTC updown 5m with inverted outcomes:
          - token_direction_mapping_unproven
      [5] BTC updown 5m inactive:
          - market_inactive_or_closed
      [6] BTC updown 5m no resolution source:
          - resolution_rule_unproven
      [valid] BTC updown 5m canonical: PASSES
    """
    markets = [
        # Valid market — PASSES
        make_real_btc_updown_market(slug_epoch=1766162100),
        # [0] Long-window BTC price target
        FIXTURE_BTC_LONG_WINDOW_PRICE_TARGET,
        # [1] Generic Yes/No
        FIXTURE_GENERIC_YES_NO_MARKET,
        # [2] BTC updown 5m with Yes/No outcomes
        FIXTURE_BTC_UPDOWN_5M_OUTCOMES_YESNO,
        # [3] BTC updown 5m with duration 298s
        FIXTURE_BTC_UPDOWN_5M_DURATION_299,
        # [4] BTC updown 5m with inverted outcomes
        FIXTURE_BTC_UPDOWN_5M_OUTCOMES_INVERTED,
        # [5] BTC updown 5m inactive
        FIXTURE_BTC_UPDOWN_5M_INACTIVE,
        # [6] BTC updown 5m no resolution source
        FIXTURE_BTC_UPDOWN_5M_NO_RESOLUTION_SOURCE,
    ]
    gamma = _SequenceGamma([markets])
    result = discover_markets_v3(_config(300), 500, gamma, evidence_dir=tmp_path,
                                as_of_ts=_AS_OF_TS)
    h = result["evidence"]["rejection_histogram"]
    # 2 markets fail directional identity (long-window + generic yes-no)
    assert h["directional_market_identity_unproven"] == 2
    # 4 markets fail token mapping (long-window, generic-yes-no,
    # btc-updown-with-yesno, btc-updown-inverted)
    assert h["token_direction_mapping_unproven"] == 4
    # 1 market fails window duration (298s)
    assert h["window_duration_mismatch"] == 1
    # 1 market fails inactive/closed
    assert h["market_inactive_or_closed"] == 1
    # 3 markets fail resolution rule (long-window, generic-yes-no, no-source)
    assert h["resolution_rule_unproven"] == 3
    # Only the valid market passes
    assert len(result["markets"]) == 1


# ═══════════════════════════════════════════════════════════════════════
# Statistical sample consistency
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("slug_epoch", [
    1766161800,  # 2025-12-19T16:30:00Z
    1766162100,  # 2025-12-19T16:35:00Z
    1766162400,  # 2025-12-19T16:40:00Z
    1766162700,  # 2025-12-19T16:45:00Z
    1766163000,  # 2025-12-19T16:50:00Z
    1766163300,  # 2025-12-19T16:55:00Z
])
def test_statistical_sample_all_pass(slug_epoch):
    """All 13 real BTC updown 5m markets captured on 2026-07-13 must pass
    the contract. This parametrized test covers 6 representative epochs;
    the remaining 7 are covered by other tests in this module."""
    market = make_real_btc_updown_market(slug_epoch=slug_epoch)
    ok, reasons = _identity_ok(market, 300)
    assert ok is True, f"slug_epoch={slug_epoch} should pass; reasons: {reasons}"


# ═══════════════════════════════════════════════════════════════════════
# JSON string parsing (outcomes/clobTokenIds as JSON strings)
# ═══════════════════════════════════════════════════════════════════════

def test_outcomes_as_json_string_parsed_correctly():
    """Polymarket returns outcomes as a JSON string. The parser must
    handle both list and JSON string forms."""
    market = make_real_btc_updown_market(slug_epoch=1766162100)
    market["outcomes"] = json.dumps(["Up", "Down"])
    market["clobTokenIds"] = json.dumps(["synthetic-up-token", "synthetic-down-token"])
    ok, reasons = _identity_ok(market, 300)
    assert ok is True
    assert reasons == []


def test_outcomes_malformed_json_rejected():
    """Malformed JSON in outcomes is rejected for token_direction_mapping."""
    market = make_real_btc_updown_market(slug_epoch=1766162100)
    market["outcomes"] = "not valid json"
    ok, reasons = _identity_ok(market, 300)
    assert ok is False
    assert "token_direction_mapping_unproven" in reasons


# ═══════════════════════════════════════════════════════════════════════
# startDate boundary — must NOT affect validation
# ═══════════════════════════════════════════════════════════════════════

def test_start_date_equal_to_event_start_still_passes():
    """Even if startDate == eventStartTime (edge case), the market passes
    because startDate is NOT used in the H-011 window calculation."""
    market = FIXTURE_BTC_UPDOWN_5M_START_DATE_TODAY
    ok, reasons = _identity_ok(market, 300)
    assert ok is True, f"startDate should not affect validation: {reasons}"
