"""Regression test for the Batch 5 (phase 13) review fix.

A Cloudflare rollback must not report a delete it does not perform: created
records are intentionally never live-DELETEd, so the executor records them as
skipped and warns, rather than implying the deletion happened.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ophelia.portability as portability


class CloudflareRollbackHonestyTests(unittest.TestCase):
    def test_delete_record_is_skipped_and_warned_not_silently_succeeded(self) -> None:
        calls = []

        def _fake_request(method, url, token, body=None):
            calls.append((method, url))
            return {"success": True, "result": {"id": "r1", "type": "A", "name": "x", "content": "1.2.3.4"}}

        original = portability._cloudflare_request
        portability._cloudflare_request = _fake_request  # type: ignore[assignment]
        os.environ["CF_ROLLBACK_TEST_TOKEN"] = "tok-not-a-secret-in-test"
        try:
            change = {
                "zone_id": "z1",
                "api_token_env": "CF_ROLLBACK_TEST_TOKEN",
                "actions": [
                    {"delete_record": {"id": "created-1"}},
                    {"restore_record": {"id": "r1", "type": "A", "name": "x", "content": "1.2.3.4"}},
                ],
            }
            result = portability._rollback_cloudflare_dns_provider(change)
        finally:
            portability._cloudflare_request = original  # type: ignore[assignment]
            os.environ.pop("CF_ROLLBACK_TEST_TOKEN", None)

        # The restore happened via PATCH; no DELETE was ever issued.
        self.assertTrue(all(method != "DELETE" for method, _ in calls))
        self.assertTrue(any(method == "PATCH" for method, _ in calls))
        # The delete was recorded as skipped, with a warning — not silently dropped.
        self.assertIn("skipped_actions", result)
        self.assertEqual("cloudflare_delete_not_supported", result["skipped_actions"][0]["reason"])
        self.assertTrue(result.get("warnings"))


if __name__ == "__main__":
    unittest.main()
