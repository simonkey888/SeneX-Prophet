#!/usr/bin/env python3
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def replace_once(path, old, new):
    p = ROOT / path
    text = p.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"{path}: expected one exact match, got {text.count(old)}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


def regex_once(path, pattern, repl):
    p = ROOT / path
    text = p.read_text(encoding="utf-8")
    new, n = re.subn(pattern, repl, text, count=1, flags=re.S)
    if n != 1:
        raise SystemExit(f"{path}: regex expected one match, got {n}: {pattern[:80]}")
    p.write_text(new, encoding="utf-8")


# R1-F001: candle maturity is part of evidence identity and validation.
replace_once(
    "senecio_polymarket/backend/settlement_contract.py",
    '''    candle_open_ms = int(candle[0])
    price = float(candle[4])
    observed = parse_utc(observed_at) if observed_at is not None else datetime.now(timezone.utc)
    if observed is None:
        return None
    return {
        "version": "historical-price-evidence-v1",
        "source": source,
        "symbol": normalized_symbol,
        "window_seconds": int(window_seconds),
        "target_epoch_ms": target_ms,
        "candle_open_epoch_ms": candle_open_ms,
        "candle_interval_ms": CANDLE_INTERVAL_MS,
        "target_offset_from_candle_open_ms": target_ms - candle_open_ms,
        "price": price,
        "observed_at": observed.isoformat(),
        "selection_rule": "ONE_MINUTE_CANDLE_CONTAINING_EXACT_TARGET",
    }
''',
    '''    candle_open_ms = int(candle[0])
    candle_close_ms = candle_open_ms + CANDLE_INTERVAL_MS
    price = float(candle[4])
    observed = parse_utc(observed_at) if observed_at is not None else datetime.now(timezone.utc)
    if observed is None:
        return None
    observed_ms = int(observed.timestamp() * 1000)
    # The current/last OHLCV candle is provisional until its interval closes.
    # Never freeze a provisional close into the immutable settlement CAS.
    if observed_ms < candle_close_ms:
        return None
    return {
        "version": "historical-price-evidence-v1",
        "source": source,
        "symbol": normalized_symbol,
        "window_seconds": int(window_seconds),
        "target_epoch_ms": target_ms,
        "candle_open_epoch_ms": candle_open_ms,
        "candle_close_epoch_ms": candle_close_ms,
        "candle_interval_ms": CANDLE_INTERVAL_MS,
        "target_offset_from_candle_open_ms": target_ms - candle_open_ms,
        "price": price,
        "observed_at": observed.isoformat(),
        "selection_rule": "ONE_MINUTE_CANDLE_CONTAINING_EXACT_TARGET",
        "maturity_rule": "OBSERVED_AT_GTE_CANDLE_CLOSE_EPOCH_MS",
    }
''',
)
replace_once(
    "senecio_polymarket/backend/settlement_contract.py",
    '''        open_ms = int(evidence.get("candle_open_epoch_ms"))
        interval_ms = int(evidence.get("candle_interval_ms"))
        price = float(evidence.get("price"))
''',
    '''        open_ms = int(evidence.get("candle_open_epoch_ms"))
        close_ms = int(evidence.get("candle_close_epoch_ms"))
        interval_ms = int(evidence.get("candle_interval_ms"))
        price = float(evidence.get("price"))
''',
)
replace_once(
    "senecio_polymarket/backend/settlement_contract.py",
    '''    if interval_ms != CANDLE_INTERVAL_MS:
        return False
    if not (open_ms <= target_ms < open_ms + interval_ms):
        return False
    if price <= 0 or not math.isfinite(price):
        return False
    return parse_utc(evidence.get("observed_at")) is not None
''',
    '''    if interval_ms != CANDLE_INTERVAL_MS:
        return False
    if close_ms != open_ms + interval_ms:
        return False
    if not (open_ms <= target_ms < close_ms):
        return False
    if price <= 0 or not math.isfinite(price):
        return False
    observed = parse_utc(evidence.get("observed_at"))
    if observed is None:
        return False
    return int(observed.timestamp() * 1000) >= close_ms
''',
)

# R1-F002: stateless bounded fairness within each verifier invocation.
supabase_section = '''PENDING_SCAN_PAGE_SIZE_MAX = 100
PENDING_SCAN_MAX_PAGES = 2
_pending_scan_diagnostics: dict[str, Any] = {}


def reset_pending_scan_cursor() -> None:
    """Compatibility reset: AUD-063-R1 scanning is stateless across invocations."""
    global _pending_scan_diagnostics
    _pending_scan_diagnostics = {}


def get_pending_scan_diagnostics() -> dict[str, Any]:
    return dict(_pending_scan_diagnostics)


async def fetch_pending_outcomes(
    older_than_seconds: int = 900,
    limit: int = 100,
    *,
    max_pages: int = PENDING_SCAN_MAX_PAGES,
) -> list[dict]:
    """Fetch bounded directional NULL rows with restart-safe intra-call keyset paging.

    Every invocation begins from the oldest eligible `(ts,id)` and advances a
    local keyset cursor for at most ``PENDING_SCAN_MAX_PAGES`` pages. Therefore
    process/container restart between cycles cannot erase progress needed to
    reach rows inside the declared per-invocation fairness bound. Rows beyond
    that explicit bound are not claimed starvation-free; diagnostics expose the
    cap. Failed rows remain retryable because no scan-progress mutation occurs.
    """
    global _pending_scan_diagnostics
    try:
        from datetime import timedelta

        bounded_limit = max(1, min(int(limit), PENDING_SCAN_PAGE_SIZE_MAX))
        bounded_pages = max(1, min(int(max_pages), PENDING_SCAN_MAX_PAGES))
        fairness_bound_rows = bounded_limit * bounded_pages
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        c = _get_client()
        base_params = {
            "select": "id,ts,symbol,prediction,confidence,price_now,exchange_used,audit",
            "outcome": "is.null",
            "prediction": "in.(LONG,SHORT)",
            "ts": f"lte.{cutoff}",
            "order": "ts.asc,id.asc",
        }

        metric_params = {
            "select": "id,ts,symbol,prediction",
            "outcome": "is.null",
            "prediction": "in.(LONG,SHORT)",
            "ts": f"lte.{cutoff}",
            "order": "ts.asc,id.asc",
            "limit": "1",
        }
        metric = await c.get(
            f"/{SUPABASE_TABLE}", params=metric_params, headers={"Prefer": "count=exact"}
        )
        eligible_count = None
        oldest = None
        if metric.status_code == 200:
            metric_rows = metric.json() or []
            if isinstance(metric_rows, list) and metric_rows:
                oldest = metric_rows[0]
            content_range = str(getattr(metric, "headers", {}).get("content-range", ""))
            if "/" in content_range:
                total = content_range.rsplit("/", 1)[-1]
                if total.isdigit():
                    eligible_count = int(total)

        collected: list[dict] = []
        cursor: tuple[str, str] | None = None
        pages_scanned = 0
        pass_complete = False
        error = None

        for _ in range(bounded_pages):
            params = dict(base_params)
            params["limit"] = str(bounded_limit)
            if cursor is not None:
                cursor_ts, cursor_id = cursor
                params["or"] = f"(ts.gt.{cursor_ts},and(ts.eq.{cursor_ts},id.gt.{cursor_id}))"
            r = await c.get(f"/{SUPABASE_TABLE}", params=params)
            if r.status_code != 200:
                log.error("supabase fetch_pending_outcomes failed: %s %s", r.status_code, r.text[:200])
                error = f"HTTP_{r.status_code}"
                break
            page = r.json() or []
            page = page if isinstance(page, list) else []
            pages_scanned += 1
            collected.extend(page)
            if len(page) < bounded_limit:
                pass_complete = True
                break
            last = page[-1]
            cursor = (str(last.get("ts") or ""), str(last.get("id") or ""))

        scan_cap_hit = (not pass_complete and error is None and pages_scanned >= bounded_pages)
        if eligible_count is not None and eligible_count <= fairness_bound_rows:
            fairness_scope = "RESTART_SAFE_FULL_VISIBLE_BACKLOG"
        elif eligible_count is None:
            fairness_scope = "RESTART_SAFE_PREFIX_ONLY_COUNT_UNKNOWN"
        else:
            fairness_scope = "RESTART_SAFE_PREFIX_ONLY_EXPLICIT_CAP"

        _pending_scan_diagnostics = {
            "eligible_directional_pending_count": eligible_count,
            "oldest_eligible_directional_pending_id": (oldest or {}).get("id"),
            "oldest_eligible_directional_pending_ts": (oldest or {}).get("ts"),
            "rows_scanned_last_pass": len(collected),
            "pages_scanned_last_pass": pages_scanned,
            "scan_cap_hit": scan_cap_hit,
            "cursor_before": None,
            "cursor_after": cursor,
            "pass_complete": pass_complete,
            "restart_safe_stateless": True,
            "fairness_bound_rows_per_invocation": fairness_bound_rows,
            "fairness_scope": fairness_scope,
            "error": error,
        }
        return collected
    except Exception as e:
        log.error("supabase fetch_pending_outcomes error: %s", e)
        _pending_scan_diagnostics = {
            "error": type(e).__name__,
            "rows_scanned_last_pass": 0,
            "pages_scanned_last_pass": 0,
            "restart_safe_stateless": True,
            "fairness_bound_rows_per_invocation": PENDING_SCAN_PAGE_SIZE_MAX * PENDING_SCAN_MAX_PAGES,
            "fairness_scope": "FAIL_CLOSED_ERROR",
        }
        return []

'''
regex_once(
    "senecio_polymarket/backend/supabase_client.py",
    r'_pending_scan_cursor: tuple\[str, str\] \| None = None\n_pending_scan_diagnostics: dict\[str, Any\] = \{\}.*?(?=async def update_outcome_dual\()',
    supabase_section,
)

# R1-F003 and verifier wiring.
replace_once(
    "senecio_polymarket/backend/oracle_runner.py",
    "  - Backfill routine now computes both 15m and 1h outcomes for already-settled rows\n",
    "  - Legacy startup resettlement is quarantined; settled-row repair belongs only to the reconciler\n",
)
replace_once(
    "senecio_polymarket/backend/oracle_runner.py",
    '''    # ACT-XXII-prereq: bogus-outcome backfill state
    "bogus_backfill_done": False,     # set True after _backfill_bogus_outcomes() runs once
    "bogus_backfill_count": None,     # how many rows re-settled with historical price
    "bogus_backfill_errors": None,    # how many rows we couldn't re-settle (no historical price)
''',
    '''    # AUD-063-R1: obsolete startup resettlement is an explicit zero-I/O quarantine.
    "legacy_startup_backfill_status": "QUARANTINED_NO_READ_NO_WRITE",
    "legacy_startup_backfill_reason": "SETTLED_ROW_REPAIR_IS_RECONCILER_ONLY",
''',
)
replace_once(
    "senecio_polymarket/backend/oracle_runner.py",
    '''WINDOW_15M_S = 900
WINDOW_1H_S = 3600
PRIMARY_WINDOW = "1h"   # gating source of truth per ACT XXIII directive 1
''',
    '''WINDOW_15M_S = 900
WINDOW_1H_S = 3600
PRIMARY_WINDOW = "1h"   # gating source of truth per ACT XXIII directive 1
SETTLEMENT_MATURITY_BUFFER_S = 60  # containing 1m candle must be closed before CAS
VERIFY_PAGE_SIZE = 100
VERIFY_MAX_PAGES = 2
VERIFY_MAX_ROWS_PER_INVOCATION = VERIFY_PAGE_SIZE * VERIFY_MAX_PAGES
''',
)
replace_once(
    "senecio_polymarket/backend/oracle_runner.py",
    '''async def _verify_pending_outcomes() -> int:
    """Settle one bounded, starvation-safe page of directional predictions."""
''',
    '''async def _verify_pending_outcomes() -> int:
    """Settle one bounded restart-safe keyset pass of directional predictions."""
''',
)
replace_once(
    "senecio_polymarket/backend/oracle_runner.py",
    '''        pending = await supabase_client.fetch_pending_outcomes(
            older_than_seconds=WINDOW_1H_S, limit=100
        )
''',
    '''        pending = await supabase_client.fetch_pending_outcomes(
            older_than_seconds=WINDOW_1H_S + SETTLEMENT_MATURITY_BUFFER_S,
            limit=VERIFY_PAGE_SIZE,
            max_pages=VERIFY_MAX_PAGES,
        )
''',
)
replace_once(
    "senecio_polymarket/backend/oracle_runner.py",
    '''    _state["last_verify_rows_scanned"] = scan.get("rows_scanned_last_pass", len(pending))
    _state["last_verify_scan_cap_hit"] = bool(scan.get("scan_cap_hit"))
    _state["last_verify_cursor"] = scan.get("cursor_after")
    _state["last_verify_scan_pass_complete"] = bool(scan.get("pass_complete"))
''',
    '''    _state["last_verify_rows_scanned"] = scan.get("rows_scanned_last_pass", len(pending))
    _state["last_verify_pages_scanned"] = scan.get("pages_scanned_last_pass")
    _state["last_verify_scan_cap_hit"] = bool(scan.get("scan_cap_hit"))
    _state["last_verify_cursor"] = scan.get("cursor_after")
    _state["last_verify_scan_pass_complete"] = bool(scan.get("pass_complete"))
    _state["last_verify_restart_safe_stateless"] = bool(scan.get("restart_safe_stateless"))
    _state["last_verify_fairness_bound_rows"] = scan.get("fairness_bound_rows_per_invocation")
    _state["last_verify_fairness_scope"] = scan.get("fairness_scope")
''',
)
replace_once(
    "senecio_polymarket/backend/oracle_runner.py",
    '''        "verifier aud063: scanned=%d settled=%d proof_unresolved=%d price_unresolved=%d cas=%d cap=%s",
        len(pending), settled, unresolved_proof, unresolved_price, cas_conflicts,
        bool(scan.get("scan_cap_hit")),
''',
    '''        "verifier aud063-r1: scanned=%d pages=%s settled=%d proof_unresolved=%d price_unresolved=%d cas=%d cap=%s fairness=%s",
        len(pending), scan.get("pages_scanned_last_pass"), settled, unresolved_proof, unresolved_price, cas_conflicts,
        bool(scan.get("scan_cap_hit")), scan.get("fairness_scope"),
''',
)
quarantine = '''def _quarantine_legacy_startup_backfill() -> None:
    """Truthful zero-I/O quarantine for the obsolete settled-row resettlement path.

    Existing WIN/LOSS evidence repair belongs exclusively to
    ``settlement_reconciler``. The primary writer remains NULL->settled CAS-only.
    This function intentionally performs no Supabase read, historical-price
    request, or database write, so startup reaches the primary verifier without
    legacy settled-row work in front of it.
    """
    _state["legacy_startup_backfill_status"] = "QUARANTINED_NO_READ_NO_WRITE"
    _state["legacy_startup_backfill_reason"] = "SETTLED_ROW_REPAIR_IS_RECONCILER_ONLY"


'''
regex_once(
    "senecio_polymarket/backend/oracle_runner.py",
    r'async def _backfill_bogus_outcomes\(\) -> int:.*?(?=async def _refresh_directional_stats\(\) -> None:)',
    quarantine,
)
replace_once(
    "senecio_polymarket/backend/oracle_runner.py",
    '''    # ACT-XXII-prereq: ONE-TIME backfill of bogus outcomes that were settled
    # with current-price instead of historical price. Runs once at startup
    # before the first prediction cycle, so the dashboard reflects correct
    # win rates as soon as possible.
    try:
        await _backfill_bogus_outcomes()
    except Exception as e:
        log.exception("backfill error (non-fatal, continuing): %s", e)

''',
    '''    # AUD-063-R1: explicit zero-I/O quarantine. No settled-row historical
    # reads or writes are allowed to delay the first primary verifier cycle.
    _quarantine_legacy_startup_backfill()

''',
)
replace_once(
    "senecio_polymarket/backend/oracle_runner.py",
    '''        # ACT XXI: Verify pending outcomes BEFORE producing new predictions.
        # This settles predictions whose 15min window elapsed in the previous cycle.
        # First cycle after boot will backfill all 200+ accumulated predictions.
''',
    '''        # Verify mature pending outcomes BEFORE producing new predictions.
        # Work is bounded to VERIFY_MAX_ROWS_PER_INVOCATION and uses stateless
        # intra-invocation keyset paging; no legacy settled-row backfill precedes it.
''',
)

# Update focused tests.
test_path = ROOT / "senecio_polymarket/tests/test_aud_063.py"
t = test_path.read_text(encoding="utf-8")
t = t.replace(
    '''    directional_outcome,
    price_evidence_from_candles,
    select_containing_candle,
    target_epoch_ms,
)
''',
    '''    CANDLE_INTERVAL_MS,
    directional_outcome,
    price_evidence_from_candles,
    select_containing_candle,
    target_epoch_ms,
    validate_price_evidence,
)
''',
    1,
)
old_t4 = '''    async def test_t04_more_than_100_directionals_drain_across_pages(self):
        rows = [pending_row(i + 1, "LONG") for i in range(250)]
        fake = FakePostgrest(rows)
        seen = []
        old = supabase_client._get_client
        supabase_client._get_client = lambda: fake
        try:
            for _ in range(3):
                seen += [r["id"] for r in await supabase_client.fetch_pending_outcomes(3600, 100)]
        finally:
            supabase_client._get_client = old
        self.assertEqual(len(seen), 250)
        self.assertEqual(len(set(seen)), 250)

'''
new_t4 = '''    async def test_t04_bounded_invocation_drains_two_keyset_pages(self):
        rows = [pending_row(i + 1, "LONG") for i in range(180)]
        fake = FakePostgrest(rows)
        seen = await self._with_client(fake, supabase_client.fetch_pending_outcomes(3600, 100, max_pages=2))
        self.assertEqual(len(seen), 180)
        self.assertEqual(len({r["id"] for r in seen}), 180)
        scan = supabase_client.get_pending_scan_diagnostics()
        self.assertTrue(scan["restart_safe_stateless"])
        self.assertEqual(scan["fairness_bound_rows_per_invocation"], 200)
        self.assertEqual(scan["fairness_scope"], "RESTART_SAFE_FULL_VISIBLE_BACKLOG")

'''
if old_t4 not in t:
    raise SystemExit("missing old T4")
t = t.replace(old_t4, new_t4, 1)
old_t5 = '''    async def test_t05_poison_row_does_not_starve_later_page(self):
        rows = [pending_row(1, "LONG", proof=False)] + [pending_row(i, "LONG") for i in range(2, 102)]
        fake = FakePostgrest(rows)
        old = supabase_client._get_client
        supabase_client._get_client = lambda: fake
        try:
            first = await supabase_client.fetch_pending_outcomes(3600, 100)
            second = await supabase_client.fetch_pending_outcomes(3600, 100)
        finally:
            supabase_client._get_client = old
        self.assertEqual(first[0]["id"], 1)
        self.assertEqual(second[-1]["id"], 101)

'''
new_t5 = '''    async def test_t05_poison_row_does_not_starve_later_page_within_bound(self):
        rows = [pending_row(1, "LONG", proof=False)] + [pending_row(i, "LONG") for i in range(2, 102)]
        fake = FakePostgrest(rows)
        seen = await self._with_client(fake, supabase_client.fetch_pending_outcomes(3600, 100, max_pages=2))
        self.assertEqual(seen[0]["id"], 1)
        self.assertEqual(seen[-1]["id"], 101)

'''
if old_t5 not in t:
    raise SystemExit("missing old T5")
t = t.replace(old_t5, new_t5, 1)
old_t7 = '''    async def test_t07_keyset_survives_prior_row_disappearance(self):
        fake = FakePostgrest([pending_row(i + 1) for i in range(150)])
        old = supabase_client._get_client
        supabase_client._get_client = lambda: fake
        try:
            first = await supabase_client.fetch_pending_outcomes(3600, 100)
            fake.rows = [r for r in fake.rows if r["id"] > 100]
            second = await supabase_client.fetch_pending_outcomes(3600, 100)
        finally:
            supabase_client._get_client = old
        self.assertEqual(first[-1]["id"], 100)
        self.assertEqual(second[0]["id"], 101)

'''
new_t7 = '''    async def test_t07_intra_invocation_second_page_is_keyset_not_offset(self):
        fake = FakePostgrest([pending_row(i + 1) for i in range(150)])
        seen = await self._with_client(fake, supabase_client.fetch_pending_outcomes(3600, 100, max_pages=2))
        page_calls = [p for kind, p in fake.calls if kind == "GET" and "confidence" in p.get("select", "")]
        self.assertEqual(len(seen), 150)
        self.assertEqual(len(page_calls), 2)
        self.assertNotIn("or", page_calls[0])
        self.assertIn("or", page_calls[1])
        self.assertNotIn("offset", page_calls[1])

'''
if old_t7 not in t:
    raise SystemExit("missing old T7")
t = t.replace(old_t7, new_t7, 1)
addition = r'''
    async def test_t28_open_candle_seconds_01_30_59_rejected_until_close(self):
        for second in (1, 30, 59):
            ts = f"2026-01-01T00:00:{second:02d}+00:00"
            target = target_epoch_ms(ts, WINDOW_1H_S)
            open_ms = target - (target % CANDLE_INTERVAL_MS)
            close_ms = open_ms + CANDLE_INTERVAL_MS
            before = datetime.fromtimestamp((close_ms - 1) / 1000, tz=timezone.utc).isoformat()
            at_close = datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc).isoformat()
            kwargs = dict(
                candles=[[open_ms, 102, 102, 102, 102, 1.0]],
                exchange="okx",
                symbol="BTCUSDT",
                ts_iso=ts,
                window_seconds=WINDOW_1H_S,
            )
            self.assertIsNone(price_evidence_from_candles(**kwargs, observed_at=before))
            mature = price_evidence_from_candles(**kwargs, observed_at=at_close)
            self.assertIsNotNone(mature)
            self.assertEqual(mature["candle_close_epoch_ms"], close_ms)
            self.assertEqual(mature["maturity_rule"], "OBSERVED_AT_GTE_CANDLE_CLOSE_EPOCH_MS")

    async def test_t29_proof_gate_rejects_premature_candle_observation(self):
        row = qualified_row(1)
        ev1 = row["audit"]["outcomes_dual"]["price_evidence_v1"]["1h"]
        close_ms = ev1["candle_close_epoch_ms"]
        ev1["observed_at"] = datetime.fromtimestamp((close_ms - 1) / 1000, tz=timezone.utc).isoformat()
        self.assertFalse(validate_price_evidence(
            ev1,
            expected_exchange=row["exchange_used"],
            expected_symbol=row["symbol"],
            expected_ts=row["ts"],
            expected_window_seconds=WINDOW_1H_S,
        ))
        self.assertFalse(settlement_proof.is_proof_qualified(row))
        self.assertEqual(settlement_proof.proof_status(row), "RAW_UNVERIFIED")

    async def test_t30_primary_cas_rejects_premature_historical_evidence(self):
        row = pending_row(1)
        fake = FakePostgrest([row])
        ev15 = evidence(row, WINDOW_15M_S, 101)
        ev1 = evidence(row, WINDOW_1H_S, 102)
        close_ms = ev1["candle_close_epoch_ms"]
        ev1["observed_at"] = datetime.fromtimestamp((close_ms - 1) / 1000, tz=timezone.utc).isoformat()
        ok = await self._with_client(
            fake,
            supabase_client.update_outcome_dual(
                1, "WIN", "WIN", 101, 102,
                price_evidence_15m=ev15,
                price_evidence_1h=ev1,
            ),
        )
        self.assertFalse(ok)
        self.assertIsNone(fake.rows[0]["outcome"])
        self.assertFalse(any(kind == "PATCH" for kind, _ in fake.calls))

    async def test_t31_restart_between_invocations_cannot_hide_row_within_bound(self):
        rows = [pending_row(i + 1, "LONG", proof=False) for i in range(125)]
        rows.append(pending_row(1000, "LONG"))
        fake = FakePostgrest(rows)
        old = supabase_client._get_client
        supabase_client._get_client = lambda: fake
        try:
            first = await supabase_client.fetch_pending_outcomes(3600, 100, max_pages=2)
            supabase_client.reset_pending_scan_cursor()
            second = await supabase_client.fetch_pending_outcomes(3600, 100, max_pages=2)
        finally:
            supabase_client._get_client = old
        self.assertIn(1000, {r["id"] for r in first})
        self.assertIn(1000, {r["id"] for r in second})
        self.assertEqual(supabase_client.get_pending_scan_diagnostics()["fairness_scope"], "RESTART_SAFE_FULL_VISIBLE_BACKLOG")

    async def test_t32_backlog_beyond_fairness_bound_is_explicitly_downgraded(self):
        rows = [pending_row(i + 1, "LONG", proof=False) for i in range(250)]
        fake = FakePostgrest(rows)
        seen = await self._with_client(fake, supabase_client.fetch_pending_outcomes(3600, 100, max_pages=2))
        scan = supabase_client.get_pending_scan_diagnostics()
        self.assertEqual(len(seen), 200)
        self.assertTrue(scan["scan_cap_hit"])
        self.assertEqual(scan["fairness_bound_rows_per_invocation"], 200)
        self.assertEqual(scan["fairness_scope"], "RESTART_SAFE_PREFIX_ONLY_EXPLICIT_CAP")

    async def test_t33_obsolete_startup_backfill_is_zero_io_quarantine(self):
        source = (Path(__file__).parents[1] / "backend" / "oracle_runner.py").read_text()
        self.assertNotIn("_backfill_bogus_outcomes", source)
        self.assertNotIn("bogus_backfill_", source)
        fetch = AsyncMock(return_value=[])
        update = AsyncMock(return_value=False)
        with (
            patch.object(supabase_client, "fetch_predictions", fetch),
            patch.object(supabase_client, "update_outcome_dual", update),
        ):
            oracle_runner._quarantine_legacy_startup_backfill()
        fetch.assert_not_awaited()
        update.assert_not_awaited()
        self.assertEqual(oracle_runner._state["legacy_startup_backfill_status"], "QUARANTINED_NO_READ_NO_WRITE")
'''
t = t.rstrip() + "\n" + addition + "\n"
test_path.write_text(t, encoding="utf-8")

replace_once(
    ".github/workflows/aud-063.yml",
    "      - name: Focused AUD-063 regression T1-T27\n",
    "      - name: Focused AUD-063/R1 regression T1-T33\n",
)
replace_once(
    ".github/workflows/aud-063.yml",
    "          grep -Eq 'Ran 27 tests' /tmp/aud063-focused.log\n",
    "          grep -Eq 'Ran 33 tests' /tmp/aud063-focused.log\n",
)

# Update deterministic evidence generator.
gen = ROOT / "senecio_polymarket/scripts/aud063_generate_evidence.py"
g = gen.read_text(encoding="utf-8")
g = g.replace('GENERATED_AT = "2026-08-15T03:15:00+00:00"', 'GENERATED_AT = "2026-08-15T04:05:00+00:00"', 1)
g = g.replace(
    '        "order": "AUD-063",\n        "base_sha": BASE_SHA,',
    '        "order": "AUD-063-R1",\n        "parent_order": "AUD-063",\n        "audited_parent_head": "0320f47657d4433bfc4dc3396fd0d31ffabe2270",\n        "base_sha": BASE_SHA,',
    1,
)
selection_block = '''    selection = {
        **common,
        "source_class": "SOURCE_CONTRACT",
        "server_side_filters": {"outcome": "is.null", "prediction": "in.(LONG,SHORT)", "horizon": "ts<=now-1h-60s"},
        "order": "ts.asc,id.asc",
        "pagination": "STATELESS_INTRA_INVOCATION_KEYSET_TS_ID",
        "page_size": 100,
        "max_pages_per_invocation": 2,
        "fairness_bound_rows_per_invocation": 200,
        "restart_dependency": "NONE_WITHIN_DECLARED_BOUND",
        "beyond_bound_claim": "PREFIX_ONLY_EXPLICIT_CAP_NO_UNIVERSAL_STARVATION_CLAIM",
        "flat_can_enter_verifier_page": False,
        "failed_row_retryable_next_invocation": True,
        "maturity_buffer_seconds": 60,
        "observability": [
            "eligible_directional_pending_count", "oldest_eligible_directional_pending_id",
            "oldest_eligible_directional_pending_ts", "oldest_eligible_directional_pending_age_seconds",
            "last_verify_rows_scanned", "last_verify_pages_scanned", "last_verify_count",
            "last_verify_unresolved_proof", "last_verify_unresolved_price", "last_verify_scan_cap_hit",
            "last_verify_cursor", "last_verify_scan_pass_complete", "last_verify_restart_safe_stateless",
            "last_verify_fairness_bound_rows", "last_verify_fairness_scope", "last_verify_no_progress_reason"
        ],
    }
    write_json(out, "aud-063-selection-contract.json", selection)
'''
g, n = re.subn(r'    selection = \{.*?    write_json\(out, "aud-063-selection-contract.json", selection\)\n', selection_block, g, count=1, flags=re.S)
if n != 1:
    raise SystemExit("generator selection block not found")
cursor_block = '''    cursor = {
        **common,
        "source_class": "DETERMINISTIC_ADVERSARIAL_SIMULATION",
        "mechanism": "LOCAL_KEYSET_CURSOR_LIVES_ONLY_INSIDE_ONE_BOUNDED_INVOCATION",
        "page_size": 100,
        "max_pages_per_invocation": 2,
        "fairness_bound_rows_per_invocation": 200,
        "cases": {
            "flat_head": {"old_flat": 125, "later_directional": 1, "post_fix_directional_seen": True},
            "restart_125_poison_then_healthy": {"eligible": 126, "cursor_reset_between_invocations": True, "healthy_seen_each_invocation": True},
            "healthy_180": {"eligible": 180, "pages": [100, 80], "unique_visited": 180},
            "over_bound_250": {"eligible": 250, "visited": 200, "scan_cap_hit": True, "fairness_scope": "RESTART_SAFE_PREFIX_ONLY_EXPLICIT_CAP"},
            "keyset": {"second_page_uses_ts_id_seek": True, "offset_pagination": False},
        },
        "invariant_within_bound": "RESTART_SAFE_LATER_ROWS_REACHABLE_WITHOUT_CROSS_CYCLE_MEMORY",
        "universal_starvation_claim": False,
    }
    write_json(out, "aud-063-cursor-fairness.json", cursor)
'''
g, n = re.subn(r'    cursor = \{.*?    write_json\(out, "aud-063-cursor-fairness.json", cursor\)\n', cursor_block, g, count=1, flags=re.S)
if n != 1:
    raise SystemExit("generator cursor block not found")
historical_block = '''    historical = {
        **common,
        "source_class": "SOURCE_AND_DETERMINISTIC_CANDLE_CONTRACT",
        "canonical_rule": "ONE_MINUTE_CANDLE_CONTAINING_EXACT_TARGET",
        "containment": "candle_open_ms <= target_ms < candle_close_ms",
        "candle_close_identity": "candle_open_epoch_ms + candle_interval_ms",
        "maturity_rule": "observed_at >= candle_close_epoch_ms",
        "open_or_incomplete_candle": "INADMISSIBLE_NO_CAS",
        "windows_seconds": [900, 3600],
        "both_windows_required_and_mature_before_primary_cas": True,
        "same_source_as_origin_required": True,
        "allowed_public_sources": ["okx", "kraken", "gate", "mexc", "bitget"],
        "unsupported_source_fallback": None,
        "live_current_price_fallback": False,
        "external_directional_market_price_source": False,
        "equal_price_rule": "LOSS_FOR_LONG_AND_SHORT",
        "legacy_without_maturity_or_historical_price_evidence": "RAW_UNVERIFIED",
    }
    write_json(out, "aud-063-historical-price-contract.json", historical)
'''
g, n = re.subn(r'    historical = \{.*?    write_json\(out, "aud-063-historical-price-contract.json", historical\)\n', historical_block, g, count=1, flags=re.S)
if n != 1:
    raise SystemExit("generator historical block not found")
g = g.replace(
    '        "reconciler_null_writer": False,\n',
    '        "reconciler_null_writer": False,\n        "maturity_validation_before_cas": True,\n',
    1,
)
findings_block = '''    findings = {
        **common,
        "source_class": "AUD063_R1_SOURCE_FORENSICS_AND_REGRESSION_EVIDENCE",
        "findings": [
            {"id":"AUD063-F001","severity":"HIGH","status":"CLOSED","fix":"server-side LONG/SHORT filter + stable ts,id keyset"},
            {"id":"AUD063-F002","severity":"HIGH","status":"CLOSED","fix":"same-source historical evidence tied to origin witness; unsupported source fails closed"},
            {"id":"AUD063-F003","severity":"MEDIUM","status":"CLOSED","fix":"exact 1m containing candle identity"},
            {"id":"AUD063-F004","severity":"HIGH","status":"CLOSED_FAIL_CLOSED_LEGACY","fix":"both price evidence records required by proof gate"},
            {"id":"AUD063-F005","severity":"MEDIUM","status":"CLOSED","fix":"reconciler repair-only; never NULL writer"},
            {"id":"AUD063-F006","severity":"MEDIUM","status":"CLOSED","fix":"backlog/scan/no-progress observability"},
            {"id":"AUD063-F007","severity":"HIGH","status":"CLOSED","fix":"actual persistence observation time governs causal learning"},
            {"id":"R1-F001","severity":"HIGH","status":"CLOSED","root_cause":"containing 1m candle could be current/incomplete at evidence observation","fix":"persist candle_close_epoch_ms; writer and validator reject observed_at before candle close; 60s eligibility buffer","regression":"T28-T30"},
            {"id":"R1-F002","severity":"MEDIUM","status":"CLOSED_BOUNDED","root_cause":"fairness cursor existed only in process memory across verifier cycles","fix":"stateless per-invocation two-page ts,id keyset traversal; restart-safe for first 200 eligible rows each invocation; explicit cap/no universal claim beyond bound","regression":"T4-T7,T31-T32"},
            {"id":"R1-F003","severity":"MEDIUM","status":"CLOSED","root_cause":"obsolete startup resettlement read historical data then called a NULL-only CAS that could never mutate settled rows","fix":"remove obsolete resettlement loop; explicit synchronous zero-read/zero-write quarantine; reconciler remains sole settled-row repair path","regression":"T33"}
        ]
    }
    write_json(out, "aud-063-findings.json", findings)
'''
g, n = re.subn(r'    findings = \{.*?    write_json\(out, "aud-063-findings.json", findings\)\n', findings_block, g, count=1, flags=re.S)
if n != 1:
    raise SystemExit("generator findings block not found")
report_block = '''    report = f"""# AUD-063-R1 — Settlement starvation remediation hardening

Generated: {GENERATED_AT}
Parent AUD-063 head: `0320f47657d4433bfc4dc3396fd0d31ffabe2270`

## Preserved AUD-063 corrections

Server-side LONG/SHORT filtering, stable `(ts,id)` keyset semantics, same-origin public historical evidence, both windows, NULL-only CAS, repair-only reconciler, actual recovery observation time, independent authority N, temporal learning cutoffs, symbol isolation, PAPER/live-capital locks, and external-directional OFF remain intact.

## R1-F001 — closed candle maturity

Historical evidence now persists `candle_close_epoch_ms` and is invalid unless `observed_at >= candle_close_epoch_ms`. The primary selector waits an additional 60 seconds beyond the one-hour horizon, and the validator independently rejects open/incomplete candles. No current ticker, nearest-candle, or alternate-venue fallback is authoritative.

## R1-F002 — restart-safe bounded fairness

The cross-cycle process-memory cursor was removed. Each verifier invocation starts from the oldest eligible directional row and performs at most two 100-row pages using local `(ts,id)` keyset seek. This is restart-safe for the first 200 eligible rows in every invocation. If the visible backlog exceeds 200, diagnostics explicitly report `RESTART_SAFE_PREFIX_ONLY_EXPLICIT_CAP`; there is no universal no-starvation claim beyond that bound. Failed rows remain retryable because scan progress is not persisted.

## R1-F003 — obsolete startup backfill quarantined

The legacy startup resettlement loop was removed. Startup performs only a synchronous zero-read/zero-write quarantine marker, then reaches the primary verifier. Existing settled-row evidence repair remains exclusively in `settlement_reconciler`, which cannot create NULL->WIN/LOSS authority.

## Safety

No production/database/Northflank/GitHub-settings/RUNTIME017 mutation. No merge or deploy. No threshold or weight tuning. External directional activation remains off. Production backlog recovery remains unexecuted and no edge claim is made.
"""
'''
g, n = re.subn(r'    report = f""".*?"""\n(?=    \(out / "\.\./AUD-063-REPORT\.md"\))', report_block, g, count=1, flags=re.S)
if n != 1:
    raise SystemExit("generator report block not found")
gen.write_text(g, encoding="utf-8")

print("AUD063_R1_PATCH_APPLIED")
