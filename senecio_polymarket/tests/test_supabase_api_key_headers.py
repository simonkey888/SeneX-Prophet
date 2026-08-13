import unittest
from unittest.mock import patch

from backend import settlement_reconciler as reconciler
from backend.supabase_client import build_supabase_headers


class SupabaseApiKeyHeaderTests(unittest.TestCase):
    def test_secret_key_uses_apikey_only(self):
        key = "sb_secret_example_for_test_only"
        headers = build_supabase_headers(key)
        self.assertEqual(headers["apikey"], key)
        self.assertNotIn("Authorization", headers)

    def test_publishable_key_uses_apikey_only(self):
        key = "sb_publishable_example_for_test_only"
        headers = build_supabase_headers(key)
        self.assertEqual(headers["apikey"], key)
        self.assertNotIn("Authorization", headers)

    def test_legacy_jwt_key_keeps_bearer_compatibility(self):
        key = "eyJlegacy.header.signature"
        headers = build_supabase_headers(key)
        self.assertEqual(headers["apikey"], key)
        self.assertEqual(headers["Authorization"], f"Bearer {key}")

    def test_reconciler_uses_secret_key_safe_headers(self):
        key = "sb_secret_reconciler_test_only"
        with (
            patch.object(reconciler, "SUPABASE_URL", "https://example.supabase.co"),
            patch.object(reconciler, "SUPABASE_KEY", key),
        ):
            headers = reconciler._headers()
        self.assertEqual(headers["apikey"], key)
        self.assertNotIn("Authorization", headers)


if __name__ == "__main__":
    unittest.main()
