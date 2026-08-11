from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from backend import settlement_reconciler as reconciler
from backend.settlement_proof import filter_proof_qualified, is_proof_qualified, proof_status, score_qualified_rows


TS = "2026-08-10T00:00:00+00:00"


class FakeResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.content = b"1" if body is not None else b""
        self.text = str(body)

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, rows, patch_status=200, patch_body=None):
        self.rows = rows
        self.patch_status = patch_status
        self.patch_body = patch_body if patch_body is not None else [{"id": 1}]
        self.get_calls = []
        self.patch_calls = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True

    async def get(self, url, params=None):
        self.get_calls.append((url, dict(params or {})))
        return FakeResponse(200, list(self.rows))

    async def patch(self, url, params=None, json=None):
        self.patch_calls.append((url, dict(params or {}), dict(json or {})))
        return FakeResponse(self.patch_status, self.patch_body)


class FakeExchange:
    def close(self):
        return None


def run_reconcile(rows, patch_status=200, patch_body=None):
    client = FakeClient(rows, patch_status=patch_status, patch_body=patch_body)
    old_url, old_key, old_limit = reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT
    reconciler.SUPABASE_URL = "https://example.invalid"
    reconciler.SUPABASE_KEY = "test-key"
    reconciler.BATCH_LIMIT = 50
    try:
        with patch.object(reconciler.httpx, "AsyncClient", return_value=client), patch.object(
            reconciler.ccxt, "okx", return_value=FakeExchange()
        ), patch.object(reconciler, "_price_at", side_effect=lambda *args: 101.0 if args[-1] == 900 else 102.0):
            result = asyncio.run(reconciler.reconcile_once())
    finally:
        reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT = old_url, old_key, old_limit
    return result, client


class SettlementReconcilerTests(unittest.TestCase):
    def test_null_row_is_never_touched_even_if_query_response_is_malformed(self):
        rows = [{"id": 1, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": None, "audit": None, "exchange_used": "okx"}]
        result, client = run_reconcile(rows)
        self.assertEqual(result["repaired"], 0)
        self.assertEqual(len(client.patch_calls), 0)
        self.assertEqual(result["skipped"], 1)
        params = client.get_calls[0][1]
        self.assertEqual(params["outcome"], "in.(WIN,LOSS)")
        self.assertEqual(params["audit->outcomes_dual"], "is.null")

    def test_win_without_dual_is_repaired_with_1h_primary(self):
        rows = [{"id": 2, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"}]
        result, client = run_reconcile(rows)
        self.assertEqual(result["repaired"], 1)
        self.assertEqual(result["errors"], 0)
        patch = client.patch_calls[0][2]
        self.assertNotIn("outcome", patch)
        self.assertEqual(client.patch_calls[0][1]["audit->outcomes_dual"], "is.null")
        self.assertEqual(client.patch_calls[0][1]["outcome"], "eq.WIN")
        self.assertEqual(patch["audit"]["outcomes_dual"]["primary_window"], "1h")
        self.assertEqual(patch["audit"]["outcomes_dual"]["outcome_15m"], "WIN")
        self.assertEqual(patch["audit"]["outcomes_dual"]["outcome_1h"], "WIN")

    def test_win_with_existing_dual_is_not_touched(self):
        rows = [{"id": 3, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": {"outcomes_dual": {"outcome_15m": "WIN", "outcome_1h": "WIN", "primary_window": "1h"}}, "exchange_used": "okx"}]
        result, client = run_reconcile(rows)
        self.assertEqual(result["repaired"], 0)
        self.assertEqual(len(client.patch_calls), 0)
        self.assertEqual(result["skipped"], 1)

    def test_inconsistent_existing_dual_is_not_overwritten(self):
        rows = [{"id": 4, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": {"outcomes_dual": {"outcome_15m": "LOSS", "outcome_1h": "LOSS", "primary_window": "1h"}}, "exchange_used": "okx"}]
        result, client = run_reconcile(rows)
        self.assertEqual(result["repaired"], 0)
        self.assertEqual(len(client.patch_calls), 0)
        self.assertEqual(result["skipped"], 1)

    def test_patch_failure_never_counts_as_repaired(self):
        rows = [{"id": 5, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"}]
        result, client = run_reconcile(rows, patch_status=500)
        self.assertEqual(result["repaired"], 0)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(len(client.patch_calls), 1)


class ProofQualificationTests(unittest.TestCase):
    def test_raw_win_loss_is_never_proof_qualified(self):
        row = {"ts": TS, "prediction": "LONG", "outcome": "WIN", "audit": None}
        self.assertFalse(is_proof_qualified(row))
        self.assertEqual(proof_status(row), "RAW_UNVERIFIED")

    def test_dual_without_origin_proof_is_not_qualified(self):
        row = {
            "ts": TS,
            "prediction": "LONG",
            "outcome": "WIN",
            "audit": {"outcomes_dual": {"outcome_15m": "WIN", "outcome_1h": "WIN", "price_15m_later": 101, "price_1h_later": 102, "primary_window": "1h"}},
        }
        self.assertFalse(is_proof_qualified(row))
        self.assertEqual(proof_status(row), "RAW_UNVERIFIED")

    def test_complete_chain_is_proof_qualified(self):
        row = {
            "ts": TS,
            "prediction": "LONG",
            "outcome": "WIN",
            "audit": {
                "origin_price_v1": {"version": "origin-price-v1", "price": 100, "timestamp": TS, "source": "okx"},
                "outcomes_dual": {
                    "outcome_15m": "WIN",
                    "outcome_1h": "WIN",
                    "price_15m_later": 101,
                    "price_1h_later": 102,
                    "primary_window": "1h",
                },
            },
        }
        self.assertTrue(is_proof_qualified(row))
        self.assertEqual(proof_status(row), "PROOF_QUALIFIED")

    def test_wrong_1h_outcome_cannot_be_qualified(self):
        row = {
            "ts": TS,
            "prediction": "LONG",
            "outcome": "WIN",
            "audit": {
                "origin_price_v1": {"version": "origin-price-v1", "price": 100, "timestamp": TS, "source": "okx"},
                "outcomes_dual": {
                    "outcome_15m": "WIN",
                    "outcome_1h": "LOSS",
                    "price_15m_later": 101,
                    "price_1h_later": 99,
                    "primary_window": "1h",
                },
            },
        }
        self.assertFalse(is_proof_qualified(row))
        self.assertEqual(proof_status(row), "RAW_UNVERIFIED")
\n\nclass AuthoritativeBoundaryTests(unittest.TestCase):\n    def qualified(self):\n        return {"ts": TS, "prediction": "LONG", "outcome": "WIN", "audit": {"origin_price_v1": {"version": "origin-price-v1", "price": 100, "timestamp": TS, "source": "okx"}, "outcomes_dual": {"outcome_15m": "WIN", "outcome_1h": "WIN", "price_15m_later": 101, "price_1h_later": 102, "primary_window": "1h"}}}\n\n    def test_scorer_counts_only_proof_qualified(self):\n        q = self.qualified()\n        raw = {"ts": TS, "prediction": "LONG", "outcome": "WIN", "audit": None}\n        dual = {"ts": TS, "prediction": "LONG", "outcome": "LOSS", "audit": {"outcomes_dual": {"outcome_15m": "LOSS", "outcome_1h": "LOSS", "price_15m_later": 99, "price_1h_later": 98, "primary_window": "1h"}}}\n        score = score_qualified_rows([q, raw, dual])\n        self.assertEqual((score["verified"], score["wins"], score["losses"], score["win_rate_pct"]), (1, 1, 0, 100.0))\n        self.assertEqual(len(filter_proof_qualified([q, raw, dual])), 1)\n\n    def test_missing_origin_is_raw_unverified(self):\n        row = self.qualified()\n        row["audit"].pop("origin_price_v1")\n        self.assertFalse(is_proof_qualified(row))\n        self.assertEqual(proof_status(row), "RAW_UNVERIFIED")\n\n    def test_conflict_does_not_overwrite_outcome(self):\n        rows = [{"id": 6, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"}]\n        client = FakeClient(rows)\n        old = reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT\n        reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT = "https://example.invalid", "test-key", 50\n        try:\n            with patch.object(reconciler.httpx, "AsyncClient", return_value=client), patch.object(reconciler.ccxt, "okx", return_value=FakeExchange()), patch.object(reconciler, "_price_at", side_effect=lambda *args: 101.0 if args[-1] == reconciler.WINDOW_15M_S else 99.0):\n                result = asyncio.run(reconciler.reconcile_once())\n        finally:\n            reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT = old\n        self.assertEqual(result["repaired"], 0)\n        self.assertEqual(result["conflicts"], 1)\n        payload = client.patch_calls[0][2]\n        self.assertNotIn("outcome", payload)\n        self.assertEqual(payload["audit"]["reconciliation_conflict"]["action"], "NO_OUTCOME_OVERWRITE")\n\n    def test_race_conditional_noop_is_not_repaired(self):\n        rows = [{"id": 7, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"}]\n        result, client = run_reconcile(rows, patch_status=200, patch_body=[])\n        self.assertEqual(result["repaired"], 0)\n        self.assertEqual(result["errors"], 1)\n        self.assertEqual(client.patch_calls[0][1]["audit->outcomes_dual"], "is.null")\n\n    def test_patch_failure_retries_next_cycle(self):\n        rows = [{"id": 8, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"}]\n        first, first_client = run_reconcile(rows, patch_status=500)\n        second, second_client = run_reconcile(rows, patch_status=500)\n        self.assertEqual((first["repaired"], second["repaired"], first["errors"], second["errors"]), (0, 0, 1, 1))\n        self.assertEqual((len(first_client.patch_calls), len(second_client.patch_calls)), (1, 1))\n\n    def test_multi_batch_repairs_all_eligible_rows(self):\n        rows = [{"id": i, "ts": f"2026-08-10T00:{i:02d}:00+00:00", "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"} for i in range(51)]\n        pages = [rows[:50], rows[50:]]\n        class PagedClient(FakeClient):\n            def __init__(self, pages):\n                super().__init__([], patch_status=200)\n                self.pages, self.page_index = pages, 0\n            async def get(self, url, params=None):\n                self.get_calls.append((url, dict(params or {})))\n                page = self.pages[self.page_index] if self.page_index < len(self.pages) else []\n                self.page_index += 1\n                return FakeResponse(200, list(page))\n        client = PagedClient(pages)\n        old = reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT\n        reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT = "https://example.invalid", "test-key", 50\n        try:\n            with patch.object(reconciler.httpx, "AsyncClient", return_value=client), patch.object(reconciler.ccxt, "okx", return_value=FakeExchange()), patch.object(reconciler, "_price_at", return_value=101.0):\n                result = asyncio.run(reconciler.reconcile_once())\n        finally:\n            reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT = old\n        self.assertEqual(result["repaired"], 51)\n        self.assertEqual(len(client.get_calls), 3)\n\n

if __name__ == "__main__":
    unittest.main()
