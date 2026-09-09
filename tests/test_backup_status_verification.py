from __future__ import annotations

import json
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.portability import backup_status_report
from ophelia.restore_verification import restore_drills_list


class BackupStatusVerificationTests(unittest.TestCase):
    def test_status_surfaces_latest_successful_backup_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "demo-service.ophelia.yml"
            manifest_path.write_text(_manifest())
            receipt_root = runtime_root / "apps" / "demo-service" / "receipts"
            receipt_root.mkdir(parents=True)
            (receipt_root / "older.json").write_text(
                json.dumps(
                    {
                        "operation": "backup.verify.apply",
                        "operation_id": "backup.verify.apply.demo-service.production.older",
                        "verify_id": "verify-older",
                        "app": "demo-service",
                        "environment": "production",
                        "backup_id": "backup-older",
                        "status": "succeeded",
                        "completed_at": "2026-08-28T10:00:00Z",
                    }
                )
                + "\n"
            )
            latest_path = receipt_root / "latest.json"
            latest_path.write_text(
                json.dumps(
                    {
                        "operation": "backup.verify.apply",
                        "operation_id": "backup.verify.apply.demo-service.production.latest",
                        "verify_id": "verify-latest",
                        "app": "demo-service",
                        "environment": "production",
                        "backup_id": "backup-latest",
                        "status": "succeeded",
                        "completed_at": "2026-08-29T10:00:00Z",
                    }
                )
                + "\n"
            )
            (receipt_root / "failed.json").write_text(
                json.dumps(
                    {
                        "operation": "backup.verify.apply",
                        "operation_id": "backup.verify.apply.demo-service.production.failed",
                        "verify_id": "verify-failed",
                        "app": "demo-service",
                        "environment": "production",
                        "backup_id": "backup-failed",
                        "status": "failed",
                        "completed_at": "2026-08-30T10:00:00Z",
                    }
                )
                + "\n"
            )

            report = backup_status_report(
                "demo-service",
                "production",
                runtime_root,
                manifest_path,
            )

        self.assertEqual("verified", report["validation"]["status"])
        self.assertEqual("verify-latest", report["validation"]["verify_id"])
        self.assertEqual("backup-latest", report["validation"]["backup_id"])
        self.assertEqual(str(latest_path), report["validation"]["receipt_path"])
        check = next(item for item in report["checks"] if item["name"] == "backup_verification")
        self.assertTrue(check["ok"])
        self.assertIn("verify-latest", check["message"])

    def test_status_explicitly_reports_metadata_only_without_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "demo-service.ophelia.yml"
            manifest_path.write_text(_manifest())

            report = backup_status_report(
                "demo-service",
                "production",
                runtime_root,
                manifest_path,
            )
        self.assertEqual("metadata-only", report["validation"]["status"])
        self.assertIsNone(report["validation"]["verify_id"])
        check = next(item for item in report["checks"] if item["name"] == "backup_verification")
        self.assertFalse(check["ok"])
        self.assertEqual("no successful verification receipt", check["message"])

    def test_status_consumes_successful_kernel_journal_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "demo-service.ophelia.yml"
            manifest_path.write_text(_manifest())
            payload = {
                "kind": "ophelia.kernel.terminal_receipt",
                "receipt_id": "receipt-journal-verification",
                "operation_id": "operation-journal-verification",
                "operation": "backup.verify.apply",
                "outcome": "succeeded",
                "app": "demo-service",
                "environment": "production",
                "backup_id": "backup-journal",
                "verify_id": "verify-journal",
                "started_at": "2026-08-30T10:00:00Z",
                "completed_at": "2026-08-30T10:01:00Z",
                "inputs_redacted": True,
            }
            database = runtime_root / "host-state" / "operations.db"
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    """
                    CREATE TABLE terminal_receipts (
                        receipt_id TEXT PRIMARY KEY,
                        operation_id TEXT NOT NULL UNIQUE,
                        outcome TEXT NOT NULL,
                        receipt_digest TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO terminal_receipts(
                        receipt_id, operation_id, outcome, receipt_digest, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        payload["receipt_id"],
                        payload["operation_id"],
                        payload["outcome"],
                        _digest(payload),
                        json.dumps(payload, sort_keys=True),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            report = backup_status_report(
                "demo-service",
                "production",
                runtime_root,
                manifest_path,
            )
            listing = restore_drills_list(
                "demo-service",
                "production",
                runtime_root,
            )

        self.assertEqual("verified", report["validation"]["status"])
        self.assertEqual("verify-journal", report["validation"]["verify_id"])
        self.assertEqual("receipt-journal-verification", report["validation"]["receipt_id"])
        self.assertTrue(str(report["validation"]["receipt_path"]).startswith("sqlite:"))
        self.assertEqual(
            ["receipt-journal-verification"],
            [item["receipt_id"] for item in listing["drills"]],
        )


def _manifest() -> str:
    return """
version: 1
app: demo-service
environment: production
kind: service
image: ghcr.io/example/demo-service@sha256:aaaaaaaa
pack:
  portability: critical
services:
  web:
    port: 3000
routes:
  - domain: demo-service.example.net
    service: web
data:
  backups:
    required: true
    restore_drill_required: true
verify:
  - name: health
    url: https://demo-service.example.net/health
""".strip() + "\n"


def _digest(payload: Dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


if __name__ == "__main__":
    unittest.main()
