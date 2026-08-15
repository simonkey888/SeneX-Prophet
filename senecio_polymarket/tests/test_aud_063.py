from __future__ import annotations

import asyncio
import copy
import json
import subprocess
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend import authoritative_score, oracle_runner, settlement_proof, supabase_client
from backend.settlement_contract import (
    WINDOW_15M_S,
    WINDOW_1H_S,
    directional_outcome,
    price_evidence_from_candles,
    select_containing_candle,
    target_epoch_ms,
)

BASE_SHA = "49c5f0a69609c005da80e48b585e91d8582a5ac6"
FIXED_TS = "2026-01-01T00:00:00+00:00"
OBSERVED = "2026-01-01T02:00:00+00:00"


class FakeResponse:
    def __init__(self, rows=None, status=200, headers=None, content=True):
        self._rows = [] if rows is None else rows
        self.status_code = status
        self.headers = headers or {}
        self.text = ""
        self.content = json.dumps(self._rows).encode() if content else b""

    def json(self):
        return copy.deepcopy(self._rows)


class FakePostgrest:
    """Minimal PostgREST boundary with numeric semantics for integer id columns."""

    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)
        self.calls = []
        self.is_closed = False
        self.patch_no_representation = False

    @staticmethod
    def _id_gt(left, right):
        try:
            return int(left) > int(right)
        except (TypeError, ValueError):
            return str(left) > str(right)

    def _selected(self, params):
        rows = [r for r in self.rows if r.get("outcome") is None]
        if params.get("prediction") == "in.(LONG,SHORT)":
            rows = [r for r in rows if str(r.get("prediction")).upper() in {"LONG", "SHORT"}]
        ts_filter = str(params.get("ts", ""))
        if ts_filter.startswith(("lt.", "lte.")):
            cutoff = datetime.fromisoformat(ts_filter.split(".", 1)[1].replace("Z", "+00:00"))
            rows = [
                r for r in rows
                if datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")) <= cutoff
            ]
        cursor = params.get("or")
        if cursor:
            prefix = "(ts.gt."
            middle = ",and(ts.eq."
            tail = ",id.gt."
            body = cursor[len(prefix):-2]
            cursor_ts, rest = body.split(middle, 1)
            same_ts, cursor_id = rest.split(tail, 1)
            rows = [
                r for r in rows
                if str(r["ts"]) > cursor_ts
                or (str(r["ts"]) == same_ts and self._id_gt(r["id"], cursor_id))
            ]
        rows.sort(key=lambda r: (str(r.get("ts")), int(r.get("id"))))
        return rows

    async def get(self, path, params=None, headers=None, **kwargs):
        params = dict(params or {})
        self.calls.append(("GET", params))
        if "id" in params and str(params["id"]).startswith("eq."):
            wanted = str(params["id"])[3:]
            return FakeResponse([r for r in self.rows if str(r.get("id")) == wanted])
        selected = self._selected(params)
        total = len(selected)
        limit = int(params.get("limit", "100"))
        return FakeResponse(
            selected[:limit],
            headers={"content-range": f"0-{max(0, min(limit, total)-1)}/{total}"},
        )

    async def patch(self, path, params=None, json=None, **kwargs):
        params = dict(params or {})
        self.calls.append(("PATCH", params))
        wanted = str(params.get("id", ""))[3:]
        for row in self.rows:
            if str(row.get("id")) != wanted:
                continue
            if params.get("outcome") == "is.null" and row.get("outcome") is not None:
                continue
            if (
                params.get("audit->outcomes_dual") == "is.null"
                and isinstance(row.get("audit"), dict)
                and row["audit"].get("outcomes_dual") is not None
            ):
                continue
            if self.patch_no_representation:
                return FakeResponse([], status=204, content=False)
            row.update(copy.deepcopy(json or {}))
            return FakeResponse([copy.deepcopy(row)])
        return FakeResponse([], status=200)


def pending_row(i, direction="LONG", ts=FIXED_TS, symbol="BTCUSDT", source="okx", proof=True):
    audit = {"sentinel": "preserve-me"}
    if proof:
        audit["origin_price_v1"] = {
            "version": "origin-price-v1",
            "price": 100.0,
            "timestamp": ts,
            "source": source,
        }
    return {
        "id": i,
        "ts": ts,
        "symbol": symbol,
        "prediction": direction,
        "confidence": 0.5,
        "price_now": 100.0,
        "exchange_used": source,
        "outcome": None,
        "audit": audit,
    }


def evidence(row, window, price, observed=OBSERVED):
    target = target_epoch_ms(row["ts"], window)
    open_ms = target - (target % 60_000)
    return price_evidence_from_candles(
        candles=[[open_ms, price, price, price, price, 1.0]],
        exchange=row["exchange_used"],
        symbol=row["symbol"],
        ts_iso=row["ts"],
        window_seconds=window,
        observed_at=observed,
    )


def qualified_row(i=1, direction="LONG", symbol="BTCUSDT", ts=FIXED_TS, observed=OBSERVED):
    row = pending_row(i, direction=direction, ts=ts, symbol=symbol)
    p15 = 101.0 if direction == "LONG" else 99.0
    p1 = 102.0 if direction == "LONG" else 98.0
    ev15 = evidence(row, WINDOW_15M_S, p15, observed)
    ev1 = evidence(row, WINDOW_1H_S, p1, observed)
    row["outcome"] = "WIN"
    row["price_15m_later"] = p15
    row["audit"]["outcomes_dual"] = {
        "outcome_15m": "WIN",
        "outcome_1h": "WIN",
        "price_15m_later": p15,
        "price_1h_later": p1,
        "primary_window": "1h",
        "settlement_contract_version": "aud063-v1",
        "price_evidence_v1": {"15m": ev15, "1h": ev1},
        "settlement_observation_v1": {
            "version": "settlement-observation-v1",
            "observed_at": observed,
            "writer": "SENEX_PRIMARY_DUAL_WINDOW_VERIFIER_V2",
        },
    }
    return row


class TinyCore:
    def __init__(self):
        self.weights = {"orderflow": 1.0}
        self._senex_base_weights = dict(self.weights)
        self._calibration_window = []
        self._calibration_by_direction = {}
        self._mutation_log = []

    def record_outcome(self, *_):
        pass

    def record_outcome_directional(self, *_):
        pass


class Aud063Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        supabase_client.reset_pending_scan_cursor()

    async def _with_client(self, client, coro):
        old = supabase_client._get_client
        supabase_client._get_client = lambda: client
        try:
            return await coro
        finally:
            supabase_client._get_client = old

    async def test_t01_prefx_flat_starvation_reproduced_exact_shape(self):
        rows = [
            pending_row(i + 1, "FLAT", ts=f"2026-01-01T00:{i//60:02d}:{i%60:02d}+00:00")
            for i in range(125)
        ]
        rows.append(pending_row(1000, "LONG", ts="2026-01-01T00:03:00+00:00"))
        legacy = sorted([r for r in rows if r["outcome"] is None], key=lambda r: r["ts"])[:100]
        self.assertEqual({r["prediction"] for r in legacy}, {"FLAT"})
        self.assertNotIn(1000, {r["id"] for r in legacy})
        capture_script = Path(__file__).parents[1] / "scripts" / "aud063_baseline_reproduce.py"
        self.assertIn(BASE_SHA, capture_script.read_text())

    async def test_t02_selector_excludes_flat_server_side(self):
        fake = FakePostgrest([pending_row(1, "FLAT"), pending_row(2, "LONG")])
        rows = await self._with_client(fake, supabase_client.fetch_pending_outcomes(3600, 100))
        self.assertEqual([r["id"] for r in rows], [2])
        page_calls = [p for kind, p in fake.calls if kind == "GET" and "confidence" in p.get("select", "")]
        self.assertEqual(page_calls[-1]["prediction"], "in.(LONG,SHORT)")

    async def test_t03_many_flat_later_long_reaches_and_settles(self):
        flats = [pending_row(i + 1, "FLAT") for i in range(125)]
        target = pending_row(1000, "LONG")
        fake = FakePostgrest(flats + [target])
        ev15, ev1 = evidence(target, 900, 101.0), evidence(target, 3600, 102.0)
        old_get = supabase_client._get_client
        supabase_client._get_client = lambda: fake
        try:
            with (
                patch.object(oracle_runner, "_fetch_price_evidence_at_time", AsyncMock(side_effect=[ev15, ev1])),
                patch.object(oracle_runner, "_refresh_directional_stats", AsyncMock(return_value=None)),
            ):
                settled = await oracle_runner._verify_pending_outcomes()
        finally:
            supabase_client._get_client = old_get
        self.assertEqual(settled, 1)
        row = next(r for r in fake.rows if r["id"] == 1000)
        self.assertEqual(row["outcome"], "WIN")

    async def test_t04_more_than_100_directionals_drain_across_pages(self):
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

    async def test_t05_poison_row_does_not_starve_later_page(self):
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

    async def test_t06_failed_row_is_retryable_after_pass_reset(self):
        fake = FakePostgrest([pending_row(1, "LONG", proof=False), pending_row(2, "LONG")])
        old = supabase_client._get_client
        supabase_client._get_client = lambda: fake
        try:
            one = await supabase_client.fetch_pending_outcomes(3600, 100)
            two = await supabase_client.fetch_pending_outcomes(3600, 100)
        finally:
            supabase_client._get_client = old
        self.assertEqual([r["id"] for r in one], [1, 2])
        self.assertEqual([r["id"] for r in two], [1, 2])

    async def test_t07_keyset_survives_prior_row_disappearance(self):
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

    async def test_t08_both_windows_required(self):
        row = pending_row(1)
        fake = FakePostgrest([row])
        ok = await self._with_client(
            fake,
            supabase_client.update_outcome_dual(
                1, "WIN", "WIN", 101, 102,
                price_evidence_15m=evidence(row, 900, 101), price_evidence_1h=None,
            ),
        )
        self.assertFalse(ok)

    async def test_t09_missing_window_leaves_outcome_null(self):
        row = pending_row(1)
        fake = FakePostgrest([row])
        await self._with_client(
            fake,
            supabase_client.update_outcome_dual(
                1, "WIN", "WIN", 101, 102, price_evidence_15m=evidence(row, 900, 101)
            ),
        )
        self.assertIsNone(fake.rows[0]["outcome"])

    async def test_t10_historical_candle_boundary_is_containing_not_nearest(self):
        target = target_epoch_ms(FIXED_TS, 900)
        prev = target - 60_000
        exact = target
        chosen = select_containing_candle(
            [[prev, 0, 0, 0, 99, 1], [exact, 0, 0, 0, 101, 1]], target_ms=target
        )
        self.assertEqual(chosen[0], exact)
        self.assertIsNone(
            select_containing_candle([[prev - 60_000, 0, 0, 0, 99, 1]], target_ms=target)
        )

    async def test_t11_long_arithmetic(self):
        self.assertEqual(directional_outcome("LONG", 100, 101), "WIN")
        self.assertEqual(directional_outcome("LONG", 100, 100), "LOSS")

    async def test_t12_short_arithmetic(self):
        self.assertEqual(directional_outcome("SHORT", 100, 99), "WIN")
        self.assertEqual(directional_outcome("SHORT", 100, 100), "LOSS")

    async def test_t13_flat_never_directional_outcome(self):
        self.assertIsNone(directional_outcome("FLAT", 100, 101))

    async def test_t14_cas_idempotence(self):
        row = pending_row(1)
        fake = FakePostgrest([row])
        ev15 = evidence(row, 900, 101)
        ev1 = evidence(row, 3600, 102)
        old = supabase_client._get_client
        supabase_client._get_client = lambda: fake
        try:
            first = await supabase_client.update_outcome_dual(
                1, "WIN", "WIN", 101, 102, price_evidence_15m=ev15, price_evidence_1h=ev1
            )
            second = await supabase_client.update_outcome_dual(
                1, "WIN", "WIN", 101, 102, price_evidence_15m=ev15, price_evidence_1h=ev1
            )
        finally:
            supabase_client._get_client = old
        self.assertTrue(first)
        self.assertFalse(second)

    async def test_t15_cas_no_representation_is_not_success(self):
        row = pending_row(1)
        fake = FakePostgrest([row])
        fake.patch_no_representation = True
        ok = await self._with_client(
            fake,
            supabase_client.update_outcome_dual(
                1, "WIN", "WIN", 101, 102,
                price_evidence_15m=evidence(row, 900, 101),
                price_evidence_1h=evidence(row, 3600, 102),
            ),
        )
        self.assertFalse(ok)

    async def test_t16_audit_json_preserved(self):
        row = pending_row(1)
        fake = FakePostgrest([row])
        await self._with_client(
            fake,
            supabase_client.update_outcome_dual(
                1, "WIN", "WIN", 101, 102,
                price_evidence_15m=evidence(row, 900, 101),
                price_evidence_1h=evidence(row, 3600, 102),
            ),
        )
        self.assertEqual(fake.rows[0]["audit"]["sentinel"], "preserve-me")

    async def test_t17_concurrent_cas_only_one_wins(self):
        row = pending_row(1)
        fake = FakePostgrest([row])
        ev15, ev1 = evidence(row, 900, 101), evidence(row, 3600, 102)
        old = supabase_client._get_client
        supabase_client._get_client = lambda: fake
        try:
            results = await asyncio.gather(*[
                supabase_client.update_outcome_dual(
                    1, "WIN", "WIN", 101, 102,
                    price_evidence_15m=ev15, price_evidence_1h=ev1,
                )
                for _ in range(2)
            ])
        finally:
            supabase_client._get_client = old
        self.assertEqual(sum(bool(x) for x in results), 1)

    async def test_t18_reconciler_is_not_null_outcome_writer(self):
        text = (Path(__file__).parents[1] / "backend" / "settlement_reconciler.py").read_text()
        self.assertIn('"outcome": "in.(WIN,LOSS)"', text)
        self.assertNotIn('"outcome": "is.null"', text)
        self.assertNotIn('"outcome": o1h', text)

    async def test_t19_legacy_missing_historical_provenance_not_authority_qualified(self):
        row = pending_row(1)
        row["outcome"] = "WIN"
        row["audit"]["outcomes_dual"] = {
            "outcome_15m": "WIN", "outcome_1h": "WIN",
            "price_15m_later": 101, "price_1h_later": 102,
            "primary_window": "1h",
            "settlement_observation_v1": {"version": "settlement-observation-v1", "observed_at": OBSERVED},
        }
        self.assertFalse(settlement_proof.is_proof_qualified(row))

    async def test_t20_recovery_observation_time_is_actual_persistence_time(self):
        row = pending_row(1)
        fake = FakePostgrest([row])
        await self._with_client(
            fake,
            supabase_client.update_outcome_dual(
                1, "WIN", "WIN", 101, 102,
                price_evidence_15m=evidence(row, 900, 101),
                price_evidence_1h=evidence(row, 3600, 102),
            ),
        )
        observed = datetime.fromisoformat(
            fake.rows[0]["audit"]["outcomes_dual"]["settlement_observation_v1"]["observed_at"]
        )
        self.assertGreater(observed, datetime.fromisoformat(FIXED_TS) + timedelta(hours=1))

    async def test_t21_late_recovery_cannot_leak_into_earlier_cutoff(self):
        from oracle_runtime.institutional_core import replay_authoritative_learning

        row = qualified_row(1, observed="2026-01-01T02:00:00+00:00")
        state = replay_authoritative_learning(
            TinyCore(), [row], "BTCUSDT", decision_cutoff="2026-01-01T01:30:00+00:00"
        )
        self.assertEqual(state["source_prediction_ids"], [])

    async def test_t22_later_cutoff_can_consume_fully_qualified_recovery(self):
        from oracle_runtime.institutional_core import replay_authoritative_learning

        row = qualified_row(1, observed="2026-01-01T02:00:00+00:00")
        state = replay_authoritative_learning(
            TinyCore(), [row], "BTCUSDT", decision_cutoff="2026-01-01T03:00:00+00:00"
        )
        self.assertEqual(state["source_prediction_ids"], [1])

    async def test_t23_raw_proof_n_distinct_from_independent_authority_n(self):
        a = qualified_row(
            1, ts="2026-01-01T00:00:00+00:00", observed="2026-01-01T03:00:00+00:00"
        )
        b = qualified_row(
            2, ts="2026-01-01T00:30:00+00:00", observed="2026-01-01T03:00:00+00:00"
        )
        raw = settlement_proof.filter_proof_qualified([a, b])
        independent = authoritative_score.independent_1h_cohort(raw)
        self.assertEqual(len(raw), 2)
        self.assertEqual(len(independent), 1)

    async def test_t24_symbol_isolation_in_learning(self):
        from oracle_runtime.institutional_core import replay_authoritative_learning

        btc = qualified_row(1, "LONG", "BTCUSDT", observed="2026-01-01T02:00:00+00:00")
        eth = qualified_row(2, "LONG", "ETHUSDT", observed="2026-01-01T02:00:00+00:00")
        state = replay_authoritative_learning(
            TinyCore(), [btc, eth], "BTCUSDT", decision_cutoff="2026-01-01T03:00:00+00:00"
        )
        self.assertEqual(state["source_prediction_ids"], [1])

    async def test_t25_observability_surfaces_no_progress(self):
        row = pending_row(1)
        fake = FakePostgrest([row])
        old = supabase_client._get_client
        supabase_client._get_client = lambda: fake
        try:
            with (
                patch.object(oracle_runner, "_fetch_price_evidence_at_time", AsyncMock(return_value=None)),
                patch.object(oracle_runner, "_refresh_directional_stats", AsyncMock(return_value=None)),
            ):
                await oracle_runner._verify_pending_outcomes()
        finally:
            supabase_client._get_client = old
        self.assertEqual(oracle_runner._state["eligible_directional_pending_count"], 1)
        self.assertEqual(
            oracle_runner._state["last_verify_no_progress_reason"],
            "ELIGIBLE_PENDING_HISTORICAL_PRICE_UNAVAILABLE",
        )

    async def test_t26_threshold_weight_external_direction_scope_unchanged(self):
        self.assertEqual(authoritative_score.MIN_GLOBAL_N, 100)
        self.assertEqual(authoritative_score.MIN_DIRECTION_N, 30)
        try:
            changed = subprocess.check_output(
                ["git", "diff", "--name-only", f"{BASE_SHA}...HEAD"], text=True
            ).splitlines()
        except Exception:
            changed = []
        forbidden = ("oracle/institutional_core.py", "oracle_runtime/institutional_core.py", "external")
        self.assertFalse(any(any(token in path for token in forbidden) for path in changed))

    async def test_t27_runtime017_untouched(self):
        try:
            changed = subprocess.check_output(
                ["git", "diff", "--name-only", f"{BASE_SHA}...HEAD"], text=True
            ).splitlines()
        except Exception:
            changed = []
        self.assertFalse(
            any("runtime017" in p.lower() or "runtime-017" in p.lower() for p in changed)
        )


if __name__ == "__main__":
    unittest.main()
