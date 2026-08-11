from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from backend import settlement_reconciler as reconciler
from backend.settlement_proof import filter_proof_qualified, is_proof_qualified, proof_status, score_qualified_rows

TS = "2026-08-10T00:00:00+00:00"

class FakeResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code; self._body = body; self.content = b"1" if body is not None else b""; self.text = str(body)
    def json(self): return self._body

class FakeClient:
    def __init__(self, rows, patch_status=200, patch_body=None):
        self.rows = rows; self.patch_status = patch_status; self.patch_body = patch_body if patch_body is not None else [{"id": 1}]; self.get_calls = []; self.patch_calls = []
    async def __aenter__(self): return self
    async def __aexit__(self, exc_type, exc, tb): return None
    async def get(self, url, params=None): self.get_calls.append((url, dict(params or {}))); return FakeResponse(200, list(self.rows))
    async def patch(self, url, params=None, json=None): self.patch_calls.append((url, dict(params or {}), dict(json or {}))); return FakeResponse(self.patch_status, self.patch_body)

class FakeExchange:
    def close(self): return None

def run_reconcile(rows, patch_status=200, patch_body=None):
    client = FakeClient(rows, patch_status=patch_status, patch_body=patch_body); old = reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT
    reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT = "https://example.invalid", "test-key", 50
    try:
        with patch.object(reconciler.httpx, "AsyncClient", return_value=client), patch.object(reconciler.ccxt, "okx", return_value=FakeExchange()), patch.object(reconciler, "_price_at", side_effect=lambda *args: 101.0 if args[-1] == 900 else 102.0): result = asyncio.run(reconciler.reconcile_once())
    finally: reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT = old
    return result, client

class SettlementReconcilerTests(unittest.TestCase):
    def test_null_row_is_never_touched(self):
        rows = [{"id": 1, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": None, "audit": None, "exchange_used": "okx"}]; result, client = run_reconcile(rows)
        self.assertEqual((result["repaired"], result["skipped"], len(client.patch_calls)), (0, 1, 0)); self.assertEqual(client.get_calls[0][1]["outcome"], "in.(WIN,LOSS)")

    def test_win_without_dual_repairs_evidence_only(self):
        rows = [{"id": 2, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"}]; result, client = run_reconcile(rows); payload = client.patch_calls[0][2]
        self.assertEqual(result["repaired"], 1); self.assertNotIn("outcome", payload); self.assertEqual(client.patch_calls[0][1]["outcome"], "eq.WIN"); self.assertEqual(client.patch_calls[0][1]["audit->outcomes_dual"], "is.null"); self.assertEqual(payload["audit"]["outcomes_dual"]["primary_window"], "1h")

    def test_existing_dual_is_not_touched_even_if_inconsistent(self):
        rows = [{"id": 3, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": {"outcomes_dual": {"outcome_15m": "LOSS", "outcome_1h": "LOSS", "primary_window": "1h"}}, "exchange_used": "okx"}]; result, client = run_reconcile(rows)
        self.assertEqual((result["repaired"], result["skipped"], len(client.patch_calls)), (0, 1, 0))

    def test_conflict_does_not_overwrite_primary_outcome(self):
        rows = [{"id": 4, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"}]; client = FakeClient(rows); old = reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT; reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT = "https://example.invalid", "test-key", 50
        try:
            with patch.object(reconciler.httpx, "AsyncClient", return_value=client), patch.object(reconciler.ccxt, "okx", return_value=FakeExchange()), patch.object(reconciler, "_price_at", side_effect=lambda *args: 101.0 if args[-1] == reconciler.WINDOW_15M_S else 99.0): result = asyncio.run(reconciler.reconcile_once())
        finally: reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT = old
        self.assertEqual((result["repaired"], result["conflicts"]), (0, 1)); self.assertNotIn("outcome", client.patch_calls[0][2]); self.assertEqual(client.patch_calls[0][2]["audit"]["reconciliation_conflict"]["action"], "NO_OUTCOME_OVERWRITE")

    def test_patch_failure_is_not_repaired(self):
        rows = [{"id": 5, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"}]; result, client = run_reconcile(rows, patch_status=500)
        self.assertEqual((result["repaired"], result["errors"], len(client.patch_calls)), (0, 1, 1))

    def test_patch_failure_is_retryable(self):
        rows = [{"id": 6, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"}]; a, ca = run_reconcile(rows, patch_status=500); b, cb = run_reconcile(rows, patch_status=500)
        self.assertEqual((a["repaired"], b["repaired"], a["errors"], b["errors"]), (0, 0, 1, 1)); self.assertEqual((len(ca.patch_calls), len(cb.patch_calls)), (1, 1))

    def test_conditional_patch_noop_is_not_repaired(self):
        rows = [{"id": 7, "ts": TS, "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"}]; result, client = run_reconcile(rows, patch_status=200, patch_body=[])
        self.assertEqual((result["repaired"], result["errors"]), (0, 1)); self.assertEqual(client.patch_calls[0][1]["audit->outcomes_dual"], "is.null")

    def test_multi_batch_repairs_all_eligible_rows(self):
        rows = [{"id": i, "ts": f"2026-08-10T00:{i:02d}:00+00:00", "symbol": "BTCUSDT", "prediction": "LONG", "price_now": 100, "outcome": "WIN", "audit": None, "exchange_used": "okx"} for i in range(51)]; pages = [rows[:50], rows[50:]]
        class PagedClient(FakeClient):
            def __init__(self, pages): super().__init__([]); self.pages, self.page_index = pages, 0
            async def get(self, url, params=None): self.get_calls.append((url, dict(params or {}))); page = self.pages[self.page_index] if self.page_index < len(self.pages) else []; self.page_index += 1; return FakeResponse(200, list(page))
        client = PagedClient(pages); old = reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT; reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT = "https://example.invalid", "test-key", 50
        try:
            with patch.object(reconciler.httpx, "AsyncClient", return_value=client), patch.object(reconciler.ccxt, "okx", return_value=FakeExchange()), patch.object(reconciler, "_price_at", return_value=101.0): result = asyncio.run(reconciler.reconcile_once())
        finally: reconciler.SUPABASE_URL, reconciler.SUPABASE_KEY, reconciler.BATCH_LIMIT = old
        self.assertEqual(result["repaired"], 51); self.assertEqual(len(client.get_calls), 2)

class ProofQualificationTests(unittest.TestCase):
    def qualified(self): return {"ts": TS, "prediction": "LONG", "outcome": "WIN", "audit": {"origin_price_v1": {"version": "origin-price-v1", "price": 100, "timestamp": TS, "source": "okx"}, "outcomes_dual": {"outcome_15m": "WIN", "outcome_1h": "WIN", "price_15m_later": 101, "price_1h_later": 102, "primary_window": "1h"}}}
    def test_raw_win_loss_is_raw_unverified(self):
        row = {"ts": TS, "prediction": "LONG", "outcome": "WIN", "audit": None}; self.assertFalse(is_proof_qualified(row)); self.assertEqual(proof_status(row), "RAW_UNVERIFIED")
    def test_missing_origin_is_raw_unverified(self):
        row = self.qualified(); row["audit"].pop("origin_price_v1"); self.assertFalse(is_proof_qualified(row))
    def test_complete_chain_is_proof_qualified(self):
        row = self.qualified(); self.assertTrue(is_proof_qualified(row)); self.assertEqual(proof_status(row), "PROOF_QUALIFIED")
    def test_inconsistent_1h_evidence_is_fail_closed(self):
        row = self.qualified(); row["audit"]["outcomes_dual"]["outcome_1h"] = "LOSS"; self.assertFalse(is_proof_qualified(row)); self.assertEqual(proof_status(row), "RAW_UNVERIFIED")
    def test_scorer_counts_only_proof_qualified_rows(self):
        q = self.qualified(); raw = {"ts": TS, "prediction": "LONG", "outcome": "WIN", "audit": None}; dual = {"ts": TS, "prediction": "LONG", "outcome": "LOSS", "audit": {"outcomes_dual": {"outcome_15m": "LOSS", "outcome_1h": "LOSS", "price_15m_later": 99, "price_1h_later": 98, "primary_window": "1h"}}}; score = score_qualified_rows([q, raw, dual])
        self.assertEqual((score["verified"], score["wins"], score["losses"], score["win_rate_pct"]), (1, 1, 0, 100.0)); self.assertEqual(len(filter_proof_qualified([q, raw, dual])), 1)

if __name__ == "__main__": unittest.main()
