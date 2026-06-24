from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.operation_schema import artifact, issue, plan_envelope, receipt_envelope


class OperationSchemaDigestTests(unittest.TestCase):
    def test_plan_envelope_includes_redacted_digest(self) -> None:
        plan = plan_envelope(
            operation="backup.create",
            app="demo-service",
            environment="production",
            summary="Create a production backup.",
            blockers=[],
            warnings=[issue("backup_window_open", "Backup window is currently open.")],
            checks=[{"name": "storage", "ok": True}],
            artifacts=[artifact("backups/demo-service", "directory")],
            confirmation_required=True,
            confirmation_token="non-secret-token",
            exact_apply_input={
                "command": (
                    "ship backup create demo-service "
                    "--token super-secret-value "
                    "--database-url postgres://user:password@db.example.com/app "
                    "--confirm CONFIRMATION_TOKEN"
                )
            },
            risk="high",
            changes=[{"path": "backups/demo-service", "change": "create"}],
        )

        digest = plan["digest"]
        encoded_digest = json.dumps(digest)
        self.assertEqual("backup.create", digest["operation"])
        self.assertEqual("demo-service", digest["app"])
        self.assertEqual("production", digest["environment"])
        self.assertEqual("high", digest["risk"])
        self.assertEqual("awaiting_confirmation", digest["status"])
        self.assertEqual(1, digest["counts"]["warnings"])
        self.assertEqual(1, digest["counts"]["checks"])
        self.assertEqual(1, digest["counts"]["artifacts"])
        self.assertEqual(1, digest["counts"]["changes"])
        self.assertTrue(digest["confirmation"]["required"])
        self.assertTrue(digest["confirmation"]["token_present"])
        self.assertIn("<redacted>", digest["confirmation"]["apply_command"])
        self.assertNotIn("super-secret-value", encoded_digest)
        self.assertNotIn("user:password", encoded_digest)

    def test_receipt_envelope_digest_summarizes_rollback(self) -> None:
        receipt = receipt_envelope(
            operation="app.traffic.apply",
            app="demo-service",
            environment="production",
            status="succeeded",
            started_at="2026-06-21T12:00:00Z",
            completed_at="2026-06-21T12:01:00Z",
            checks=[{"name": "provider", "ok": True}],
            rollback={"available": True, "note": "Use app.traffic.rollback.apply with this receipt."},
        )

        digest = receipt["digest"]
        self.assertEqual("app.traffic.apply", digest["operation"])
        self.assertEqual("succeeded", digest["status"])
        self.assertEqual(1, digest["counts"]["checks"])
        self.assertFalse(digest["confirmation"]["required"])
        self.assertTrue(digest["rollback"]["available"])
        self.assertEqual("Use app.traffic.rollback.apply with this receipt.", digest["rollback"]["note"])


if __name__ == "__main__":
    unittest.main()
