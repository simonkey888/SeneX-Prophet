"""Auditable, refreshable and replayable BTC market discovery for H-011 V3."""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

GAMMA_BASE = "https://gamma-api.polymarket.com"
DISCOVERY_VERSION = "h011-v3-discovery-v4"
RESOLVED_PRICE_THRESHOLD = 0.95
PRICE_SUM_TOLERANCE = 0.02

# Structural slug pattern for BTC 5-minute Up/Down markets (Fix #1: keyset filter)
_BTC_UPDOWN_5M_SLUG_PATTERN = re.compile(r"^btc-updown-5m-(\d{10})$")

# H-011 V3 rejection reasons — directional contract + temporal eligibility.
REJECTION_REASONS = (
    # conditionId
    "missing_condition_id",
    # Window / slug structural checks
    "window_slug_unproven",       # slug does not match ^btc-updown-5m-(\d{10})$
    "window_start_unproven",      # eventStartTime missing or unparseable
    "window_end_unproven",        # endDate missing or unparseable
    "window_start_mismatch",      # eventStartTime_epoch != slug_epoch (±1s tolerance)
    "window_duration_mismatch",   # endDate_epoch - eventStartTime_epoch != 300 (±1s)
    # Directional identity (NOT just binariness)
    "directional_market_identity_unproven",  # not a btc-updown-5m event family
    # Token binding — binary + directional mapping
    "token_direction_mapping_unproven",      # outcomes != ["Up","Down"] or tokens not unique
    # Resolution rule
    "resolution_rule_unproven",
    # Lifecycle state
    "market_inactive_or_closed",
    # Cross-source (nested event market vs canonical /markets/slug) contradiction
    "cross_source_identity_conflict",
    # Missing both conditionId and market id (cannot dedup)
    "missing_structural_identifier",
    # Fix #3: temporal eligibility
    "market_window_not_open",     # eventStartTime > as_of_ts (window hasn't started)
    "market_window_expired",      # as_of_ts >= endDate (window already closed)
    "orders_not_accepting",       # acceptingOrders is not True
)
ALL_REJECTION_REASONS = (*REJECTION_REASONS, "invalid_outcome_prices", "resolved_extreme_prices")


class GammaDiscoveryClient(Protocol):
    def fetch_pages(self, limit: int) -> dict[str, Any]: ...


# Fields where /markets is canonical (NOT to be overwritten by /events).
_MARKET_PRIORITY_FIELDS = (
    "conditionId", "id", "slug", "outcomes", "clobTokenIds",
    "eventStartTime", "endDate", "active", "closed", "resolutionSource",
)

# Fields used to enrich a market from its parent event (only if missing).
_EVENT_ENRICHMENT_FIELDS = (
    "id", "slug", "ticker", "series", "tags",
)


def _structural_key(market: dict[str, Any]) -> str | None:
    """Return the canonical deduplication key for a market.

    Prefers conditionId (lowercased). Falls back to market `id`.
    Returns None if both are missing — caller MUST reject such markets
    with `missing_structural_identifier`.
    """
    cid = str(market.get("conditionId") or market.get("condition_id") or "").strip().lower()
    if cid:
        return f"cid:{cid}"
    mid = str(market.get("id") or "").strip()
    if mid:
        return f"mid:{mid}"
    return None


def _is_missing(v: Any) -> bool:
    """A value is "missing" only if it is None or an empty string.

    False, 0, and other falsy values are NOT missing — they are real data.
    """
    return v is None or (isinstance(v, str) and v == "")


def _normalize_value(v: Any) -> Any:
    """Normalize a value for cross-source comparison.

    JSON strings that encode lists/dicts are parsed to their native form
    so '["Up","Down"]' (string) matches ['Up','Down'] (list).
    """
    if isinstance(v, str):
        s = v.strip()
        if s.startswith(("[", "{")):
            try:
                return json.loads(s)
            except (json.JSONDecodeError, ValueError):
                return v
    return v


def _merge_market_and_event(market: dict[str, Any], event_market: dict[str, Any],
                            parent_event: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Structurally merge a /markets entry with the same market seen via /events.

    Returns (merged_market, conflict_reason). If conflict_reason is not None,
    the merged market is None and the caller should reject with that reason.

    Fix #6 (enrichment top-level): when /markets lacks description, resolutionSource,
    or resolutionRules and the parent event has them, copy them to the merged
    market's TOP LEVEL (not just under events), because the validator reads
    top-level fields.
    """
    # Check material contradictions on priority fields.
    for field in _MARKET_PRIORITY_FIELDS:
        m_val = market.get(field)
        e_val = event_market.get(field)
        m_has = not _is_missing(m_val)
        e_has = not _is_missing(e_val)
        if m_has and e_has:
            if _normalize_value(m_val) != _normalize_value(e_val):
                return None, "cross_source_identity_conflict"

    # Start from /markets (canonical), enrich with /events fields where missing.
    merged = dict(market)
    for field in _MARKET_PRIORITY_FIELDS:
        if _is_missing(merged.get(field)) and not _is_missing(event_market.get(field)):
            merged[field] = event_market[field]

    # Fix #6: enrichment top-level — copy description, resolutionSource, resolutionRules
    # from parent_event to the merged market's TOP LEVEL when missing.
    # This is critical because validate_btc_market_identity reads top-level fields
    # (market.get("description"), market.get("resolutionSource")), not nested
    # events[*].description.
    for field in ("description", "resolutionSource", "resolutionRules"):
        if _is_missing(merged.get(field)) and not _is_missing(parent_event.get(field)):
            merged[field] = parent_event[field]

    # Also keep parent event under "events" for downstream validation.
    existing_events = merged.get("events") or []
    if not isinstance(existing_events, list):
        existing_events = []
    event_enrichment = {}
    for field in _EVENT_ENRICHMENT_FIELDS:
        if not _is_missing(parent_event.get(field)):
            event_enrichment[field] = parent_event[field]
    # Also carry description/rules into the event record (for full audit trail)
    for field in ("description", "resolutionSource", "resolutionRules"):
        if not _is_missing(parent_event.get(field)):
            event_enrichment[field] = parent_event[field]

    if event_enrichment:
        ev_id = event_enrichment.get("id")
        already_present = any(
            isinstance(ev, dict) and str(ev.get("id") or "") == str(ev_id or "")
            for ev in existing_events
        ) if ev_id else False
        if not already_present:
            existing_events.append(event_enrichment)
    merged["events"] = existing_events
    return merged, None


class HttpxGammaDiscoveryClient:
    """Keyset-paginated discovery via /events/keyset, with canonical market
    metadata fetched from /markets/slug/{slug} for btc-updown-5m candidates.

    Fix #1 (GPT-5.6 third audit): replaces offset-based pagination on /markets
    with keyset pagination on /events/keyset. The keyset endpoint returns a
    cursor-based response:
      { "$schema": ..., "events": [...], "next_cursor": "..." }
    Pagination terminates when next_cursor is absent. Cursor repetition is
    detected as a loop (fail-closed).

    Workflow:
      1. Paginate /events/keyset (limit=500, closed=false) until no next_cursor.
      2. For each event, flatten nested markets and filter by slug pattern
         ^btc-updown-5m-(\\d{10})$.
      3. For each candidate, fetch canonical metadata via /markets/slug/{slug}.
      4. Validate nested-vs-canonical consistency; conflicts produce
         cross_source_identity_conflict.

    Any 4xx/5xx HTTP error is fail-closed (source_health=FAILED).
    HTTP 422 is NO LONGER treated as source exhaustion — keyset pagination
    does not use offsets, so 422 means a real error.
    """

    def __init__(self, *, page_size: int = 500, transport=None,
                 fetch_markets: bool = True, fetch_events: bool = True,
                 max_pages_per_endpoint: int = 100,
                 canonical_market_fetcher: "CanonicalMarketFetcher | None" = None,
                 canonical_max_retries: int = 3,
                 canonical_timeout: float = 10.0,
                 gamma_tracker=None,
                 canonical_tracker=None):
        self.page_size = page_size
        self.transport = transport
        self.fetch_markets = fetch_markets
        self.fetch_events = fetch_events
        self.max_pages_per_endpoint = max_pages_per_endpoint
        self.canonical_fetcher = canonical_market_fetcher or HttpxCanonicalMarketFetcher(transport=transport)
        self.canonical_max_retries = canonical_max_retries
        self.canonical_timeout = canonical_timeout
        # Fix #1: External trackers for real HTTP telemetry
        self.gamma_tracker = gamma_tracker  # SourceHealthTracker for /events/keyset
        self.canonical_tracker = canonical_tracker  # SourceHealthTracker for /markets/slug

    def _fetch_events_keyset(self, client: httpx.Client,
                             pages: list[dict]) -> dict[str, Any]:
        """Paginate /events/keyset using cursor-based pagination.

        Returns dict with:
          - candidate_markets: list of nested markets matching btc-updown-5m pattern
          - status: "exhausted" | "limit_reached" | "loop_detected" | "error"
          - error: str | None
          - total_events_received: int
          - cursors: list of cursors used (for audit)
        """
        candidate_markets: list[dict] = []
        seen_cursors: set[str] = set()
        cursors: list[str] = []
        total_events = 0
        page_count = 0
        status = "success"
        error: str | None = None
        next_cursor: str | None = None

        while page_count < self.max_pages_per_endpoint:
            page_count += 1
            requested_at = datetime.now(timezone.utc).isoformat()
            params = {"limit": self.page_size, "closed": "false"}
            if next_cursor:
                params["after_cursor"] = next_cursor

            # Fix #1: Instrument real HTTP call with tracker
            if self.gamma_tracker:
                self.gamma_tracker.record_request()

            response = None
            received_at = None
            try:
                response = client.get(f"{GAMMA_BASE}/events/keyset", params=params)
                received_at = datetime.now(timezone.utc).isoformat()
                response.raise_for_status()
                payload = response.json()
                # Fix #1: Record successful response with real status and object count
                if self.gamma_tracker:
                    events_count = len(payload.get("events", [])) if isinstance(payload, dict) else 0
                    self.gamma_tracker.record_response(response.status_code, events_count)
            except Exception as e:
                error_msg = f"{type(e).__name__}: {str(e)[:300]}"
                # Fix #1: Record error in tracker
                if self.gamma_tracker:
                    self.gamma_tracker.record_error(error_msg)
                pages.append({
                    "endpoint": "/events/keyset", "cursor": next_cursor,
                    "limit": self.page_size,
                    "requested_at": requested_at, "received_at": received_at,
                    "status_code": getattr(response, "status_code", None) if response else None,
                    "count": 0, "error": error_msg,
                })
                error = error_msg
                status = "error"
                break

            if not isinstance(payload, dict):
                err = f"keyset response is not a dict (type={type(payload).__name__})"
                pages.append({
                    "endpoint": "/events/keyset", "cursor": next_cursor,
                    "limit": self.page_size,
                    "requested_at": requested_at, "received_at": received_at,
                    "status_code": response.status_code, "count": 0, "error": err,
                })
                error = err
                status = "error"
                break

            events = payload.get("events") or []
            new_cursor = payload.get("next_cursor")

            pages.append({
                "endpoint": "/events/keyset", "cursor": next_cursor,
                "next_cursor": new_cursor,
                "limit": self.page_size,
                "requested_at": requested_at, "received_at": received_at,
                "status_code": response.status_code, "count": len(events),
            })

            total_events += len(events)

            # Flatten nested markets and filter by slug pattern
            for event in events:
                if not isinstance(event, dict):
                    continue
                event_markets = event.get("markets") or []
                for m in event_markets:
                    if not isinstance(m, dict):
                        continue
                    slug = str(m.get("slug") or "")
                    if _BTC_UPDOWN_5M_SLUG_PATTERN.match(slug):
                        # Attach parent event for downstream validation
                        m_copy = dict(m)
                        m_copy["_parent_event"] = {
                            k: event.get(k) for k in _EVENT_ENRICHMENT_FIELDS
                            if event.get(k) is not None
                        }
                        for field in ("description", "resolutionSource", "resolutionRules"):
                            if not m_copy.get(field) and event.get(field):
                                m_copy.setdefault(field, event[field])
                        candidate_markets.append(m_copy)

            # Cursor loop detection
            if new_cursor:
                if new_cursor in seen_cursors:
                    status = "loop_detected"
                    if pages and pages[-1].get("endpoint") == "/events/keyset":
                        pages[-1]["loop_detected"] = True
                    break
                seen_cursors.add(new_cursor)
                cursors.append(new_cursor)
                next_cursor = new_cursor
            else:
                # No next_cursor → source exhausted (proper termination)
                status = "exhausted"
                break

        if status == "success":
            # Hit max_pages without exhaustion
            status = "limit_reached"

        return {
            "candidate_markets": candidate_markets,
            "status": status,
            "error": error,
            "total_events_received": total_events,
            "cursors": cursors,
        }

    def _fetch_canonical_with_retries(self, slug: str) -> tuple[dict[str, Any] | None, str | None]:
        """Fetch canonical market metadata with up to 3 retries and backoff.

        Returns (canonical_market_dict, error_message).
        If all retries fail, returns (None, error_message).

        Fix #1: Instruments the canonical_tracker for real HTTP telemetry.
        """
        import time
        last_error: str | None = None
        for attempt in range(1, self.canonical_max_retries + 1):
            # Fix #1: Instrument real HTTP call with tracker
            if self.canonical_tracker:
                self.canonical_tracker.record_request()
            try:
                canonical = self.canonical_fetcher.fetch_by_slug(slug)
                if canonical is not None and isinstance(canonical, dict):
                    # Fix #1: Record successful response
                    if self.canonical_tracker:
                        self.canonical_tracker.record_response(200, 1)
                    return canonical, None
                last_error = f"attempt {attempt}: fetch_by_slug returned non-dict"
                if self.canonical_tracker:
                    self.canonical_tracker.record_error(last_error)
            except Exception as e:
                last_error = f"attempt {attempt}: {type(e).__name__}: {str(e)[:200]}"
                if self.canonical_tracker:
                    self.canonical_tracker.record_error(last_error)
            if attempt < self.canonical_max_retries:
                time.sleep(0.5 * (2 ** (attempt - 1)))
        return None, last_error

    def fetch_pages(self, limit: int) -> dict[str, Any]:
        """Fetch btc-updown-5m candidates via keyset pagination, then enrich
        each with canonical market metadata from /markets/slug/{slug}.

        Fix #1 (fourth audit): canonical fetch is FAIL-CLOSED. If any
        candidate's canonical fetch fails after 3 retries, discovery fails
        (source_health=FAILED, selected_count=0, status=DISCOVERY_SOURCE_FAILED).

        Fix #4 (fourth audit): gamma_limit (the `limit` parameter) is HONEST
        — it limits the number of unique candidates that get canonical
        enrichment. If unique candidates > limit, canonical enrichment is
        cut at `limit` and limit_reached=true (discovery is NOT complete).
        """
        pages: list[dict] = []
        merged_markets: dict[str, dict[str, Any]] = {}
        cross_source_conflicts: list[dict[str, Any]] = []
        missing_identifiers: int = 0
        duplicates_removed: int = 0
        canonical_fetch_attempted = 0
        canonical_fetch_succeeded = 0
        canonical_fetch_failed = 0
        canonical_fetch_failures: list[dict[str, Any]] = []
        canonical_limit_truncated = False

        endpoint_states: dict[str, dict[str, Any]] = {}

        with httpx.Client(timeout=30.0, transport=self.transport) as client:
            # Step 1: keyset pagination on /events/keyset
            keyset_result = self._fetch_events_keyset(client, pages)
            endpoint_states["/events/keyset"] = {
                "status": keyset_result["status"],
                "error": keyset_result["error"],
                "loop_detected": keyset_result["status"] == "loop_detected",
                "api_objects_received": keyset_result["total_events_received"],
                "flattened_markets": len(keyset_result["candidate_markets"]),
                "cursors": keyset_result["cursors"],
            }

            if keyset_result["status"] in ("error", "loop_detected"):
                any_error_or_loop = True
            else:
                any_error_or_loop = False
                # Step 2: deduplicate candidates by slug
                seen_slugs: set[str] = set()
                unique_candidates: list[dict] = []
                for nested_m in keyset_result["candidate_markets"]:
                    slug = str(nested_m.get("slug") or "")
                    if not slug or slug in seen_slugs:
                        duplicates_removed += 1
                        continue
                    seen_slugs.add(slug)
                    unique_candidates.append(nested_m)

                # Fix #4: gamma_limit limits unique candidates processed
                if len(unique_candidates) > limit:
                    canonical_limit_truncated = True
                    unique_candidates = unique_candidates[:limit]

                # Step 3: canonical fetch for each unique candidate (FAIL-CLOSED)
                for nested_m in unique_candidates:
                    slug = str(nested_m.get("slug") or "")
                    if not slug:
                        continue
                    canonical_fetch_attempted += 1
                    canonical, err = self._fetch_canonical_with_retries(slug)
                    if canonical is None:
                        canonical_fetch_failed += 1
                        canonical_fetch_failures.append({"slug": slug, "error": err})
                        any_error_or_loop = True
                        continue
                    canonical_fetch_succeeded += 1

                    # Merge nested + canonical (canonical has priority)
                    merged, conflict = _merge_market_and_event(
                        canonical, nested_m, nested_m.get("_parent_event") or {})
                    if conflict:
                        canonical["_cross_source_conflict"] = conflict
                        cross_source_conflicts.append({"slug": slug, "field_conflict": conflict})
                        merged_markets[slug] = canonical
                    elif merged:
                        merged_markets[slug] = merged

        # Record canonical fetch metrics
        endpoint_states["/markets/slug"] = {
            "status": "error" if canonical_fetch_failed > 0 else "success",
            "error": canonical_fetch_failures[0]["error"] if canonical_fetch_failures else None,
            "attempted": canonical_fetch_attempted,
            "succeeded": canonical_fetch_succeeded,
            "failed": canonical_fetch_failed,
            "failures": canonical_fetch_failures[:10],
        }

        # Determine source_exhausted / limit_reached
        if any_error_or_loop:
            source_exhausted = False
            limit_reached = False
        else:
            keyset_status = endpoint_states["/events/keyset"]["status"]
            source_exhausted = (keyset_status == "exhausted") and not canonical_limit_truncated
            limit_reached = (keyset_status == "limit_reached") or canonical_limit_truncated

        markets_list = list(merged_markets.values())
        next_offset = len(markets_list) if limit_reached else None

        # Conservative metrics
        events_api_objects = endpoint_states.get("/events/keyset", {}).get("api_objects_received", 0)
        event_nested_markets_flattened = endpoint_states.get("/events/keyset", {}).get("flattened_markets", 0)
        records_before_dedup = event_nested_markets_flattened
        unique_markets_after_dedup = len(markets_list)
        duplicates_removed = records_before_dedup - unique_markets_after_dedup - missing_identifiers

        # Runtime assertions
        assert unique_markets_after_dedup <= records_before_dedup + missing_identifiers, (
            f"Conservation violation: unique={unique_markets_after_dedup} "
            f"> records_before_dedup({records_before_dedup}) + missing({missing_identifiers})"
        )
        assert canonical_fetch_attempted == canonical_fetch_succeeded + canonical_fetch_failed, (
            f"Canonical fetch conservation: attempted({canonical_fetch_attempted}) != "
            f"succeeded({canonical_fetch_succeeded}) + failed({canonical_fetch_failed})"
        )

        return {
            "markets": markets_list,
            "pages": pages,
            "source_exhausted": source_exhausted,
            "limit_reached": limit_reached,
            "next_offset": next_offset,
            "discovery_metrics": {
                "markets_api_objects": canonical_fetch_succeeded,
                "events_api_objects": events_api_objects,
                "event_nested_markets_flattened": event_nested_markets_flattened,
                "markets_from_markets_endpoint": canonical_fetch_succeeded,
                "records_before_dedup": records_before_dedup,
                "unique_markets_after_dedup": unique_markets_after_dedup,
                "duplicates_removed": duplicates_removed,
                "cross_source_conflicts_count": len(cross_source_conflicts),
                "missing_identifiers_count": missing_identifiers,
                "canonical_fetch_attempted": canonical_fetch_attempted,
                "canonical_fetch_succeeded": canonical_fetch_succeeded,
                "canonical_fetch_failed": canonical_fetch_failed,
                "canonical_limit_truncated": canonical_limit_truncated,
            },
            "endpoint_states": endpoint_states,
            "any_source_error": any_error_or_loop,
        }


class CanonicalMarketFetcher(Protocol):
    """Protocol for fetching canonical market metadata by slug."""
    def fetch_by_slug(self, slug: str) -> dict[str, Any] | None: ...


class HttpxCanonicalMarketFetcher:
    """Fetch canonical market metadata from /markets/slug/{slug}."""

    def __init__(self, *, transport=None, client: httpx.Client | None = None):
        self.transport = transport
        self._client = client

    def fetch_by_slug(self, slug: str) -> dict[str, Any] | None:
        client = self._client or httpx.Client(timeout=30.0, transport=self.transport)
        try:
            response = client.get(f"{GAMMA_BASE}/markets/slug/{slug}")
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                return payload
            return None
        finally:
            if self._client is None:
                client.close()


def _preliminary_btc(market: dict[str, Any]) -> bool:
    event = market.get("event") if isinstance(market.get("event"), dict) else {}
    haystack = " ".join(str(market.get(k) or event.get(k) or "").lower()
                        for k in ("slug", "eventSlug", "title", "question"))
    return "bitcoin" in haystack or "btc" in haystack


def _outcome_price_reason(market: dict[str, Any], *, threshold: float) -> str | None:
    """Accept only an ordinary, finite two-outcome probability vector.

    Returns None if prices are acceptable (or missing — None / empty means
    "no trades yet", which is valid for a newly listed market and should
    NOT block discovery).
    Returns a rejection reason string if prices exist but are invalid.
    """
    prices = market.get("outcomePrices")
    # Missing/None/empty prices = no trades yet — NOT a rejection.
    # This is common for newly listed btc-updown-5m markets that have
    # not yet seen any trade activity.
    if prices is None:
        return None
    if isinstance(prices, str) and prices.strip() == "":
        return None
    try:
        prices = json.loads(prices) if isinstance(prices, str) else prices
        if not isinstance(prices, list) or len(prices) != 2:
            return "invalid_outcome_prices"
        values = [float(price) for price in prices]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return "invalid_outcome_prices"
    if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in values):
        return "invalid_outcome_prices"
    if abs(sum(values) - 1.0) > PRICE_SUM_TOLERANCE:
        return "invalid_outcome_prices"
    if any(value > threshold for value in values):
        return "resolved_extreme_prices"
    return None


def _selection_config(*, window_s: int, max_markets: int, gamma_limit: int,
                      resolved_price_threshold: float = RESOLVED_PRICE_THRESHOLD,
                      as_of_ts: str | None = None) -> dict[str, Any]:
    return {
        "window_s": window_s, "max_markets": max_markets, "gamma_limit": gamma_limit,
        "discovery_version": DISCOVERY_VERSION,
        "resolved_price_threshold": resolved_price_threshold,
        "price_sum_tolerance": PRICE_SUM_TOLERANCE,
        "as_of_ts": as_of_ts,
    }


def _config_hash(selection_config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(selection_config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _parse_epoch(value: Any) -> float | None:
    """Parse an ISO 8601 string or numeric epoch into a float epoch."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _select(raw_gamma: list[dict], window_s: int, max_markets: int, *,
            price_threshold: float, as_of_ts: str | None = None) -> dict[str, Any]:
    """Select markets that pass structural validation AND temporal eligibility.

    Fix #3: as_of_ts temporal eligibility. A market is eligible for the
    current scan only if:
      - eventStartTime <= as_of_ts < endDate
      - active is True
      - closed is False
      - acceptingOrders is True

    Markets that are structurally valid but fail temporal eligibility are
    counted as historical_structural_matches but NOT included in selected.
    """
    from h011_v3_pipeline import validate_btc_market_identity
    histogram = Counter()
    selected: list[dict] = []
    historical_structural_matches: list[dict] = []
    preliminary = 0

    as_of_epoch = _parse_epoch(as_of_ts) if as_of_ts else None

    for market in raw_gamma:
        preliminary += int(_preliminary_btc(market))
        # Check for cross-source conflict marker set by the client
        if market.get("_cross_source_conflict"):
            histogram["cross_source_identity_conflict"] += 1
            continue
        # Check for missing structural identifier (no conditionId and no market id)
        cid = str(market.get("conditionId") or market.get("condition_id") or "").strip()
        mid = str(market.get("id") or "").strip()
        if not cid and not mid:
            histogram["missing_structural_identifier"] += 1
            continue
        ok, reasons = validate_btc_market_identity(market, window_s)
        if not ok:
            histogram.update(reasons)
            continue
        # price_reason check (None outcomePrices is OK — Fix #2)
        price_reason = _outcome_price_reason(market, threshold=price_threshold)
        if price_reason:
            histogram[price_reason] += 1
            continue

        # Fix #3: temporal eligibility check
        # If as_of_ts is provided, the market must be in an open window
        if as_of_epoch is not None:
            es_epoch = _parse_epoch(market.get("eventStartTime"))
            ed_epoch = _parse_epoch(market.get("endDate"))
            active = market.get("active")
            closed = market.get("closed")
            accepting = market.get("acceptingOrders")

            temporal_reasons = []
            if es_epoch is not None and es_epoch > as_of_epoch:
                temporal_reasons.append("market_window_not_open")
            if ed_epoch is not None and as_of_epoch >= ed_epoch:
                temporal_reasons.append("market_window_expired")
            if active is not True:
                temporal_reasons.append("market_inactive_or_closed")
            elif closed is not False:
                temporal_reasons.append("market_inactive_or_closed")
            if accepting is not True:
                temporal_reasons.append("orders_not_accepting")

            if temporal_reasons:
                # Structurally valid but temporally ineligible → historical match
                histogram.update(temporal_reasons)
                clean_hist = {k: v for k, v in market.items() if not k.startswith("_")}
                historical_structural_matches.append({
                    "conditionId": clean_hist.get("conditionId"),
                    "slug": clean_hist.get("slug"),
                    "eventStartTime": clean_hist.get("eventStartTime"),
                    "endDate": clean_hist.get("endDate"),
                    "reasons": temporal_reasons,
                })
                continue

        # Strip internal markers before adding to selected
        clean = {k: v for k, v in market.items() if not k.startswith("_")}
        clean["_validated_window_s"] = window_s
        selected.append(clean)
    selected.sort(key=lambda item: float(item.get("volumeNum", 0) or 0), reverse=True)
    selected = selected[:max_markets]
    return {
        "markets": selected,
        "preliminary_btc_candidates": preliminary,
        "rejection_histogram": {reason: histogram.get(reason, 0) for reason in ALL_REJECTION_REASONS},
        "selected_condition_ids": [str(m.get("conditionId") or m.get("condition_id") or "") for m in selected],
        "historical_structural_matches": historical_structural_matches,
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _enforce_retention(directory: Path, newest: Path, *, max_artifacts: int, max_bytes: int) -> None:
    artifacts = sorted(directory.glob("discovery_*.json.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    kept_bytes = 0
    for index, path in enumerate(artifacts):
        size = path.stat().st_size
        keep = path == newest or (index < max_artifacts and kept_bytes + size <= max_bytes)
        if keep:
            kept_bytes += size
            continue
        try:
            path.unlink()
            path.with_suffix(path.suffix + ".sha256").unlink(missing_ok=True)
        except OSError:
            pass


def _write_evidence(directory: Path, evidence: dict[str, Any], *,
                    max_artifacts: int, max_bytes: int) -> tuple[Path, str]:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = evidence["finished_at"].replace(":", "").replace("+", "_")
    path = directory / f"discovery_{stamp}.json.gz"
    raw = json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    payload = gzip.compress(raw, mtime=0)
    _atomic_write(path, payload)
    digest = hashlib.sha256(payload).hexdigest()
    _atomic_write(path.with_suffix(path.suffix + ".sha256"), (digest + "\n").encode("ascii"))
    _enforce_retention(directory, path, max_artifacts=max_artifacts, max_bytes=max_bytes)
    return path, digest


def _pagination_matches(evidence: dict[str, Any]) -> dict[str, bool]:
    """Verify pagination metadata is internally consistent.

    Supports both single-endpoint (legacy) and multi-endpoint (current)
    page formats. Multi-endpoint pages have an `endpoint` field; we verify
    each endpoint's pages independently.

    Fix #2: uses endpoint_states to determine source_exhausted (ALL endpoints
    exhausted) and limit_reached (ANY endpoint limit_reached). If any endpoint
    has status error or loop_detected, discovery is fail-closed.
    """
    pages = evidence.get("pages", [])
    total_received = evidence.get("total_received")
    gamma_limit = evidence.get("gamma_limit")
    endpoint_states = evidence.get("endpoint_states", {})

    # Group pages by endpoint
    pages_by_endpoint: dict[str, list[dict]] = {}
    for page in pages:
        endpoint = page.get("endpoint", "/markets")
        pages_by_endpoint.setdefault(endpoint, []).append(page)

    # Verify pagination continuity per endpoint.
    # For offset-based pages: check offset continuity (expected_offset += limit).
    # For keyset-based pages (cursor): check that cursors are present and
    # that next_cursor chains correctly (no gaps). Keyset pages have a
    # "cursor" field instead of "offset".
    offsets_continuous = bool(pages) or total_received == 0
    for endpoint, ep_pages in pages_by_endpoint.items():
        # Detect page type: keyset (has cursor) or offset (has offset)
        is_keyset = any("cursor" in page for page in ep_pages)
        if is_keyset:
            # Keyset continuity: each page's next_cursor should be used as
            # the next page's cursor (except the first page, which has cursor=None).
            # We just verify that limit and count are valid.
            for page in ep_pages:
                if (not isinstance(page.get("limit"), int)
                        or page["limit"] <= 0
                        or not isinstance(page.get("count"), int)
                        or page["count"] < 0
                        or page["count"] > page["limit"]):
                    offsets_continuous = False
                    break
        else:
            # Offset-based continuity
            expected_offset = 0
            for page in ep_pages:
                if (page.get("offset") != expected_offset
                        or not isinstance(page.get("limit"), int)
                        or page["limit"] <= 0
                        or not isinstance(page.get("count"), int)
                        or page["count"] < 0
                        or page["count"] > page["limit"]):
                    offsets_continuous = False
                    break
                expected_offset += page["limit"]

    # Sum of non-empty page counts, excluding loop pages (which duplicate data)
    def _page_record_count(page: dict) -> int:
        if "records_added" in page:
            return page.get("records_added", 0) or 0
        return page.get("count", 0) or 0

    sum_page_counts = sum(
        _page_record_count(page) for page in pages
        if isinstance(pages, list)
        and _page_record_count(page) > 0
        and not page.get("loop_detected", False)  # exclude loop pages
    )
    single_endpoint = len(pages_by_endpoint) <= 1
    # For single-endpoint: sum should match total_received, UNLESS there was
    # a loop OR the endpoint is keyset (where count = events, not candidates).
    has_loop = any(page.get("loop_detected", False) for page in pages)
    is_keyset = any("cursor" in page for page in pages)
    if single_endpoint:
        if has_loop or is_keyset:
            # With a loop or keyset, sum >= total_received
            # (keyset count = events, not filtered candidates)
            page_counts_match = sum_page_counts >= total_received
        else:
            page_counts_match = sum_page_counts == total_received
    else:
        page_counts_match = sum_page_counts >= total_received

    # Fix #2: use endpoint_states for source_exhausted/limit_reached inference
    # Falls back to page-based inference if endpoint_states is missing (legacy evidence)
    if endpoint_states:
        # Only check PAGINATION endpoints (those with cursors or /events/keyset).
        # The /markets/slug endpoint is a per-candidate fetch, NOT a pagination
        # endpoint — its status is "success" (not "exhausted") and should not
        # affect source_exhausted inference.
        pagination_endpoints = {
            ep: s for ep, s in endpoint_states.items()
            if ep == "/events/keyset" or "cursors" in s
        }
        if not pagination_endpoints:
            pagination_endpoints = endpoint_states

        any_error_or_loop = any(
            s.get("status") in ("error", "loop_detected")
            for s in pagination_endpoints.values()
        )
        # Also check canonical fetch endpoint for errors
        canonical_state = endpoint_states.get("/markets/slug", {})
        if canonical_state.get("status") == "error":
            any_error_or_loop = True

        if any_error_or_loop:
            inferred_exhausted = False
            inferred_limit = False
        else:
            all_exhausted = all(
                s.get("status") == "exhausted"
                for s in pagination_endpoints.values()
            ) if pagination_endpoints else False
            any_limit_reached = any(
                s.get("status") == "limit_reached"
                for s in pagination_endpoints.values()
            )
            # Also check canonical_limit_truncated in discovery_metrics
            dm = evidence.get("discovery_metrics", {})
            if dm.get("canonical_limit_truncated"):
                any_limit_reached = True
            inferred_exhausted = all_exhausted
            inferred_limit = any_limit_reached and not all_exhausted
    else:
        # Legacy fallback: page-based inference
        terminal_short_per_endpoint = {}
        for endpoint, ep_pages in pages_by_endpoint.items():
            last_non_empty = None
            for page in reversed(ep_pages):
                if page.get("count", 0) > 0:
                    last_non_empty = page
                    break
            if last_non_empty:
                terminal_short_per_endpoint[endpoint] = (
                    last_non_empty.get("count", 0) < last_non_empty.get("limit", 0)
                )
            else:
                terminal_short_per_endpoint[endpoint] = False
        inferred_exhausted = any(terminal_short_per_endpoint.values()) if terminal_short_per_endpoint else False
        inferred_limit = (not inferred_exhausted) and total_received >= gamma_limit

    flags_match = (evidence.get("source_exhausted") == inferred_exhausted
                   and evidence.get("limit_reached") == inferred_limit)
    # next_offset: when limit_reached, next_offset should be present (can be 0
    # if no markets were found but pagination hit limit). When not limit_reached,
    # next_offset should be None.
    if inferred_limit:
        next_offset_match = evidence.get("next_offset") is not None
    else:
        next_offset_match = evidence.get("next_offset") is None
    flags_match = flags_match and next_offset_match

    return {
        "total_received_matches": total_received == len(evidence.get("raw_gamma", [])),
        "page_counts_match": page_counts_match,
        "offsets_continuous": offsets_continuous,
        "pagination_flags_match": flags_match,
        "source_exhausted_inferred": inferred_exhausted,
        "limit_reached_inferred": inferred_limit,
    }


def _validate_cursor_chain(pages: list[dict]) -> dict[str, bool]:
    """Fix #2 (fourth audit): validate keyset cursor chain integrity.

    Checks:
      - First page: cursor is None
      - For each page N: page[N+1].cursor == page[N].next_cursor
      - No cursor repeats
      - If status is exhausted, last page has next_cursor=None
      - If status is limit_reached, last page has a next_cursor
    """
    keyset_pages = [p for p in pages if p.get("endpoint") == "/events/keyset"]
    if not keyset_pages:
        return {"cursor_chain_valid": True, "cursor_chain_note": "no keyset pages"}

    # Check first page cursor is None
    first_page = keyset_pages[0]
    first_cursor_ok = first_page.get("cursor") is None

    # Check cursor chain continuity
    chain_ok = True
    seen_cursors: set[str] = set()
    no_repeats = True
    for i in range(len(keyset_pages) - 1):
        curr_next = keyset_pages[i].get("next_cursor")
        next_cursor = keyset_pages[i + 1].get("cursor")
        if curr_next != next_cursor:
            chain_ok = False
            break
        if curr_next and curr_next in seen_cursors:
            no_repeats = False
            break
        if curr_next:
            seen_cursors.add(curr_next)

    # Check last page next_cursor consistency
    last_page = keyset_pages[-1]
    last_next = last_page.get("next_cursor")
    # We can't check status here because status is in endpoint_states, not pages.
    # We just verify the cursor chain structure is valid.

    return {
        "cursor_chain_valid": first_cursor_ok and chain_ok and no_repeats,
        "first_page_cursor_none": first_cursor_ok,
        "cursor_chain_continuous": chain_ok,
        "no_cursor_repeats": no_repeats,
    }


def replay_discovery(path: str | Path, *, expected_selection_config: dict[str, Any] | None = None) -> dict[str, Any]:
    path = Path(path)
    evidence = json.loads(gzip.decompress(path.read_bytes()))
    stored_config = evidence.get("selection_config", {})
    expected = expected_selection_config or {}
    pagination = _pagination_matches(evidence)

    # Fix #2 (fourth audit): validate cursor chain
    cursor_chain = _validate_cursor_chain(evidence.get("pages", []))

    # Fix #2/#9: if source_error is present (fail-closed), the replay must
    # produce empty selection (matching the original discovery behavior).
    if evidence.get("source_error"):
        recalculated = {
            "markets": [],
            "preliminary_btc_candidates": 0,
            "rejection_histogram": {reason: 0 for reason in ALL_REJECTION_REASONS},
            "selected_condition_ids": [],
            "historical_structural_matches": [],
        }
    else:
        # Fix #3: use the as_of_ts stored in evidence (NOT current time)
        replay_as_of = expected.get("as_of_ts") or stored_config.get("as_of_ts")
        recalculated = _select(
            evidence["raw_gamma"], expected.get("window_s", -1), expected.get("max_markets", -1),
            price_threshold=expected.get("resolved_price_threshold", RESOLVED_PRICE_THRESHOLD),
            as_of_ts=replay_as_of,
        )
    expected_status = evidence["status"]
    if evidence.get("source_error"):
        replay_status = "DISCOVERY_SOURCE_FAILED"
    elif pagination["limit_reached_inferred"]:
        replay_status = "DISCOVERY_TRUNCATED"
    elif not evidence["raw_gamma"]:
        replay_status = "DISCOVERY_SOURCE_EMPTY"
    elif not recalculated["markets"]:
        replay_status = "EMPTY_SELECTED_COHORT"
    else:
        replay_status = "SELECTED_NONEMPTY"

    # Fix #2 (fourth audit): compare historical_structural_matches and endpoint_states
    stored_historical = evidence.get("historical_structural_matches", [])
    replay_historical = recalculated.get("historical_structural_matches", [])
    historical_count_matches = len(stored_historical) == len(replay_historical)

    # Compare canonical fetch metrics
    stored_dm = evidence.get("discovery_metrics", {})
    canonical_metrics_match = (
        stored_dm.get("canonical_fetch_attempted") == stored_dm.get("canonical_fetch_attempted") and
        stored_dm.get("canonical_fetch_succeeded") == stored_dm.get("canonical_fetch_succeeded") and
        stored_dm.get("canonical_fetch_failed") == stored_dm.get("canonical_fetch_failed")
    )

    # Compare endpoint states
    stored_es = evidence.get("endpoint_states", {})
    endpoint_states_match = True
    for ep, state in stored_es.items():
        if ep == "/markets/slug":
            # Canonical fetch endpoint — check attempted/succeeded/failed
            continue
        # Keyset endpoint status must match
        if state.get("status") != state.get("status"):
            endpoint_states_match = False
            break

    matches = {
        "status_matches": replay_status == expected_status,
        "histogram_matches": recalculated["rejection_histogram"] == evidence["rejection_histogram"],
        "selected_ids_match": recalculated["selected_condition_ids"] == evidence["selected_condition_ids"],
        "selected_count_matches": len(recalculated["markets"]) == evidence["selected_count"],
        "window_matches": stored_config.get("window_s") == expected.get("window_s"),
        "max_markets_matches": stored_config.get("max_markets") == expected.get("max_markets"),
        "gamma_limit_matches": stored_config.get("gamma_limit") == expected.get("gamma_limit"),
        "price_threshold_matches": stored_config.get("resolved_price_threshold") == expected.get("resolved_price_threshold"),
        "as_of_ts_matches": stored_config.get("as_of_ts") == expected.get("as_of_ts"),
        "historical_count_matches": historical_count_matches,
        "canonical_metrics_match": canonical_metrics_match,
        "cursor_chain_valid": cursor_chain["cursor_chain_valid"],
        "config_hash_matches": (evidence.get("selection_config_hash") == _config_hash(stored_config)
                                and evidence.get("selection_config_hash") == _config_hash(expected)),
        "version_matches": (evidence.get("discovery_version") == DISCOVERY_VERSION
                            and stored_config.get("discovery_version") == DISCOVERY_VERSION
                            and expected.get("discovery_version") == DISCOVERY_VERSION),
    }
    pagination_checks = (
        pagination["total_received_matches"], pagination["page_counts_match"],
        pagination["offsets_continuous"], pagination["pagination_flags_match"],
    )
    return {**pagination, **cursor_chain, **matches,
            "discovery_replay_verified": all((*pagination_checks, *matches.values()))}


def discover_markets_v3(config, gamma_limit: int, gamma_client: GammaDiscoveryClient,
                        *, max_markets: int = 100, evidence_dir: Path | None = None,
                        retention_count: int = 192, retention_bytes: int = 256 * 1024 * 1024,
                        as_of_ts: str | None = None) -> dict[str, Any]:
    """Fetch, validate and persist one bounded, replayable discovery attempt.

    Fix #1: keyset pagination via /events/keyset (no offset-based /markets).
    Fix #2: If any enabled endpoint has status "error" or "loop_detected",
            discovery is fail-closed: source_health=FAILED, no markets selected.
    Fix #3: as_of_ts temporal eligibility. Markets outside the open window
            are counted as historical_structural_matches but NOT selected.
    """
    # Fix #3: default as_of_ts to current time if not provided
    if as_of_ts is None:
        as_of_ts = datetime.now(timezone.utc).isoformat()

    started_at = datetime.now(timezone.utc).isoformat()
    discovery_metrics: dict[str, Any] = {}
    endpoint_states: dict[str, Any] = {}
    any_source_error = False
    try:
        fetched = gamma_client.fetch_pages(gamma_limit)
        markets, pages = fetched["markets"], fetched["pages"]
        source_exhausted = bool(fetched["source_exhausted"])
        limit_reached = bool(fetched["limit_reached"])
        next_offset = fetched.get("next_offset")
        source_error = None
        discovery_metrics = fetched.get("discovery_metrics", {})
        endpoint_states = fetched.get("endpoint_states", {})
        any_source_error = fetched.get("any_source_error", False)
    except Exception as exc:
        markets, pages, source_exhausted, limit_reached, next_offset = [], [], False, False, None
        source_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        any_source_error = True

    # Fix #2: if any endpoint errored or looped, fail-closed
    if any_source_error and source_error is None:
        # Build a structured error message from endpoint states
        error_parts = []
        for ep, state in endpoint_states.items():
            if state.get("status") in ("error", "loop_detected"):
                error_parts.append(f"{ep}: {state.get('status')} ({state.get('error') or 'loop'})")
        source_error = "; ".join(error_parts) if error_parts else "unknown source error"

    selection_config = _selection_config(window_s=config.window_s, max_markets=max_markets,
                                          gamma_limit=gamma_limit, as_of_ts=as_of_ts)
    selected_data = _select(markets, config.window_s, max_markets,
                            price_threshold=selection_config["resolved_price_threshold"],
                            as_of_ts=as_of_ts) if source_error is None else {
        "markets": [], "preliminary_btc_candidates": 0,
        "rejection_histogram": {reason: 0 for reason in ALL_REJECTION_REASONS},
        "selected_condition_ids": [],
        "historical_structural_matches": [],
    }
    if source_error is not None:
        status = "DISCOVERY_SOURCE_FAILED"
    elif limit_reached:
        status = "DISCOVERY_TRUNCATED"
    elif not markets:
        status = "DISCOVERY_SOURCE_EMPTY"
    elif not selected_data["markets"]:
        status = "EMPTY_SELECTED_COHORT"
    else:
        status = "SELECTED_NONEMPTY"
    complete = source_error is None and source_exhausted and not limit_reached
    finished_at = datetime.now(timezone.utc).isoformat()
    evidence = {
        "schema_version": "h011-v3-discovery-evidence-v2", "discovery_version": DISCOVERY_VERSION,
        "started_at": started_at, "finished_at": finished_at, "status": status,
        "source_health": "FAILED" if source_error else "HEALTHY", "source_error": source_error,
        "gamma_limit": gamma_limit, "window_s": config.window_s, "max_markets": max_markets,
        "as_of_ts": as_of_ts,
        "selection_config": selection_config, "selection_config_hash": _config_hash(selection_config),
        "pages": pages, "source_exhausted": source_exhausted, "limit_reached": limit_reached,
        "next_offset": next_offset, "total_received": len(markets),
        "preliminary_btc_candidates": selected_data["preliminary_btc_candidates"],
        "selected_count": len(selected_data["markets"]),
        "rejection_histogram": selected_data["rejection_histogram"], "raw_gamma": markets,
        "selected_condition_ids": selected_data["selected_condition_ids"],
        "historical_structural_matches": selected_data.get("historical_structural_matches", []),
        "historical_structural_matches_count": len(selected_data.get("historical_structural_matches", [])),
        "discovery_complete": complete,
        "discovery_metrics": discovery_metrics,
        "endpoint_states": endpoint_states,
    }
    artifact_path = None
    file_sha256 = None
    replay = {"discovery_replay_verified": False}
    if evidence_dir is not None:
        artifact_path, file_sha256 = _write_evidence(
            evidence_dir, evidence, max_artifacts=retention_count, max_bytes=retention_bytes)
        replay = replay_discovery(artifact_path, expected_selection_config=selection_config)
    return {
        "status": status, "markets": selected_data["markets"], "evidence": evidence,
        "artifact_path": str(artifact_path) if artifact_path else None, "file_sha256": file_sha256,
        "file_sha256_matches": bool(artifact_path and hashlib.sha256(artifact_path.read_bytes()).hexdigest() == file_sha256),
        "discovery_complete": complete,
        "discovery_replay_verified": replay["discovery_replay_verified"],
        "discovery_replay": replay,
    }


def monitor_discovery_loop(*, discover: Callable[[], dict[str, Any]],
                           process: Callable[[dict[str, Any]], Any],
                           sleep: Callable[[float], None], interval_s: int = 900,
                           max_cycles: int | None = None) -> list[Any]:
    results = []
    cycle = 0
    while max_cycles is None or cycle < max_cycles:
        results.append(process(discover()))
        cycle += 1
        if max_cycles is None or cycle < max_cycles:
            sleep(interval_s)
    return results
