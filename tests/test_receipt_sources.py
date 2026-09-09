from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Optional, Tuple


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.operation_refs import resolve_receipt_ref
from ophelia.main import main
from ophelia.portability import receipt_list_report, receipt_show_report
from ophelia.receipt_index import receipt_timeline
from ophelia.state_db import rebuild_state, state_db_path


class ReceiptSourceTests(unittest.TestCase):
    def test_browser_unifies_file_product_and_kernel_journal_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            _write_legacy_receipt(runtime_root)
            _write_product_receipt(runtime_root)
            database = _write_kernel_receipt(runtime_root)

            report = receipt_list_report(runtime_root)
            timeline = receipt_timeline(runtime_root)
            shown = receipt_show_report("receipt-kernel", runtime_root)
            resolved = resolve_receipt_ref("latest:kernel-app", runtime_root=runtime_root)
            locator_resolution = resolve_receipt_ref(
                str(next(item["path"] for item in report["receipts"] if item["receipt_id"] == "receipt-kernel")),
                runtime_root=runtime_root,
            )
            shown_by_locator = receipt_show_report(str(locator_resolution["path"]), runtime_root)
            state_report = rebuild_state(runtime_root, Path(temp_dir) / "manifests")
            state_connection = sqlite3.connect(state_db_path(runtime_root))
            try:
                kernel_checks = state_connection.execute(
                    "SELECT name, ok, message FROM checks WHERE receipt_id = ?",
                    ("receipt-kernel",),
                ).fetchall()
            finally:
                state_connection.close()

        records = {item["receipt_id"]: item for item in report["receipts"]}
        self.assertEqual({"receipt-legacy", "receipt-product", "receipt-kernel"}, set(records))
        self.assertEqual("file", records["receipt-legacy"]["source"])
        self.assertEqual("product", records["receipt-product"]["source"])
        self.assertEqual("journal", records["receipt-kernel"]["source"])
        self.assertEqual("kernel-app", records["receipt-kernel"]["app"])
        self.assertEqual("succeeded", records["receipt-kernel"]["status"])
        self.assertTrue(str(records["receipt-kernel"]["path"]).startswith("sqlite:"))
        self.assertIn(str(database), records["receipt-kernel"]["path"])

        timeline_by_id = {item["receipt_id"]: item for item in timeline["receipts"]}
        self.assertEqual(["/tmp/safe-artifact"], timeline_by_id["receipt-legacy"]["artifact_paths"])
        self.assertTrue(timeline_by_id["receipt-kernel"]["rollback_available"])
        self.assertEqual([], timeline["warnings"])

        self.assertEqual("ophelia.kernel.terminal_receipt", shown["receipt"]["kind"])
        self.assertEqual("receipt-kernel", shown["receipt_id"])
        self.assertEqual("journal", shown["receipt_source"])
        self.assertTrue(resolved["ok"])
        self.assertEqual("receipt-kernel", resolved["resolved_id"])
        self.assertEqual("ophelia.kernel.terminal_receipt", resolved["payload"]["kind"])
        self.assertTrue(locator_resolution["ok"])
        self.assertEqual("path", locator_resolution["strategy"])
        self.assertEqual("receipt-kernel", locator_resolution["resolved_id"])
        self.assertEqual("receipt-kernel", shown_by_locator["receipt_id"])
        self.assertEqual("ok", state_report["status"])
        self.assertEqual([], state_report["warnings"])
        self.assertEqual(3, state_report["counts"]["receipts"])
        self.assertEqual([("runtime_health", 1, "healthy")], kernel_checks)

    def test_product_wrapper_uses_nested_kernel_identity_and_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            _write_product_receipt(runtime_root)

            matching = receipt_list_report(runtime_root, app="product-app", environment="production")
            excluded = receipt_list_report(runtime_root, app="other-app")
            shown = receipt_show_report("receipt-product", runtime_root)

        self.assertEqual(1, len(matching["receipts"]))
        self.assertEqual([], excluded["receipts"])
        self.assertEqual("product", matching["receipts"][0]["source"])
        self.assertEqual("product-app", matching["receipts"][0]["app"])
        self.assertEqual("ophelia.product-operation-receipt", shown["receipt"]["kind"])

    def test_correlated_product_and_journal_receipt_stays_rich_without_false_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            product_path = _write_product_receipt(
                runtime_root,
                previous_revision_id="rev-before-backup",
            )
            wrapper = json.loads(product_path.read_text())
            _write_journal_payload(
                runtime_root,
                "receipt-product",
                wrapper["ophelia_receipt"],
            )

            report = receipt_list_report(runtime_root)
            timeline = receipt_timeline(runtime_root)
            shown = receipt_show_report("receipt-product", runtime_root)
            record = report["receipts"][0]
            shown_journal = receipt_show_report(str(record["journal_locator"]), runtime_root)

        self.assertEqual([], report["warnings"])
        self.assertEqual(1, len(report["receipts"]))
        self.assertEqual("product", record["source"])
        self.assertEqual(["journal", "product"], record["sources"])
        self.assertEqual(str(product_path), record["product_receipt_path"])
        self.assertTrue(str(record["journal_locator"]).startswith("sqlite:"))
        self.assertEqual("ophelia.product-operation-receipt", shown["receipt"]["kind"])
        self.assertEqual("production", shown["environment"])
        self.assertEqual(["journal", "product"], shown["receipt_sources"])
        self.assertEqual(record["journal_locator"], shown["journal_locator"])
        self.assertEqual(str(product_path), shown["product_receipt_path"])
        self.assertEqual("ophelia.kernel.terminal_receipt", shown_journal["receipt"]["kind"])
        self.assertFalse(timeline["receipts"][0]["rollback_available"])

    def test_correlated_product_without_digest_keeps_authoritative_journal_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            product_path = _write_product_receipt(runtime_root)
            wrapper = json.loads(product_path.read_text())
            wrapper.pop("ophelia_receipt_digest")
            product_path.write_text(json.dumps(wrapper, sort_keys=True) + "\n")
            _write_journal_payload(
                runtime_root,
                "receipt-product",
                wrapper["ophelia_receipt"],
            )

            report = receipt_list_report(runtime_root)
            shown = receipt_show_report("receipt-product", runtime_root)

        self.assertEqual(
            ["receipt_product_integrity_failed"],
            [item["code"] for item in report["warnings"]],
        )
        self.assertEqual("journal", report["receipts"][0]["source"])
        self.assertEqual("ophelia.kernel.terminal_receipt", shown["receipt"]["kind"])

    def test_tampered_product_correlation_keeps_authoritative_journal_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            product_path = _write_product_receipt(runtime_root)
            wrapper = json.loads(product_path.read_text())
            journal_payload = dict(wrapper["ophelia_receipt"])
            journal_payload["outcome"] = "failed_compensated"
            _write_journal_payload(runtime_root, "receipt-product", journal_payload)

            report = receipt_list_report(runtime_root)
            shown = receipt_show_report("receipt-product", runtime_root)
            resolved = resolve_receipt_ref("receipt-product", runtime_root=runtime_root)
            cli_code, cli_report = _run_cli_show("receipt-product", runtime_root)

        self.assertEqual(
            ["receipt_correlation_integrity_failed"],
            [item["code"] for item in report["warnings"]],
        )
        self.assertEqual("journal", report["receipts"][0]["source"])
        self.assertEqual(["journal", "product"], report["receipts"][0]["sources"])
        self.assertEqual("failed_compensated", report["receipts"][0]["status"])
        self.assertEqual("ophelia.kernel.terminal_receipt", shown["receipt"]["kind"])
        self.assertEqual("failed_compensated", resolved["payload"]["outcome"])
        self.assertIn(
            "receipt_correlation_integrity_failed",
            {item["code"] for item in shown["warnings"]},
        )
        self.assertIn(
            "receipt_correlation_integrity_failed",
            {item["code"] for item in resolved["warnings"]},
        )
        self.assertEqual(0, cli_code)
        self.assertEqual(len(cli_report["warnings"]), cli_report["digest"]["counts"]["warnings"])
        self.assertIn(
            "receipt_correlation_integrity_failed",
            cli_report["digest"]["warning_codes"],
        )

    def test_tampered_product_app_cannot_bypass_correlation_through_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            product_path = _write_product_receipt(runtime_root)
            wrapper = json.loads(product_path.read_text())
            journal_payload = dict(wrapper["ophelia_receipt"])
            wrapper["ophelia_receipt"]["app"] = "spoofed-app"
            wrapper["product_id"] = "spoofed-app"
            wrapper["ophelia_receipt_digest"] = _canonical_payload_digest(
                wrapper["ophelia_receipt"]
            )
            product_path.write_text(json.dumps(wrapper, sort_keys=True) + "\n")
            _write_journal_payload(runtime_root, "receipt-product", journal_payload)

            spoofed = receipt_list_report(runtime_root, app="spoofed-app")
            authoritative = receipt_list_report(runtime_root, app="product-app")

        self.assertEqual([], spoofed["receipts"])
        self.assertIn(
            "receipt_correlation_integrity_failed",
            {item["code"] for item in spoofed["warnings"]},
        )
        self.assertEqual(1, len(authoritative["receipts"]))
        self.assertEqual("journal", authoritative["receipts"][0]["source"])

    def test_corrupt_journal_payload_is_retained_as_a_warning_and_unreadable_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            _write_raw_journal_payload(runtime_root, "receipt-corrupt", "{not-json")

            report = receipt_list_report(runtime_root)
            timeline = receipt_timeline(runtime_root)
            shown = receipt_show_report("receipt-corrupt", runtime_root)
            resolved = resolve_receipt_ref("receipt-corrupt", runtime_root=runtime_root)
            state_report = rebuild_state(runtime_root, Path(temp_dir) / "manifests")

        self.assertEqual(["receipt_journal_payload_invalid"], [item["code"] for item in report["warnings"]])
        self.assertFalse(next(item for item in report["checks"] if item["name"] == "receipt_scan")["ok"])
        self.assertEqual("invalid", report["receipts"][0]["status"])
        self.assertEqual(["receipt_journal_payload_invalid"], [item["code"] for item in timeline["warnings"]])
        self.assertEqual("invalid", timeline["receipts"][0]["status"])
        self.assertIn("receipt_unreadable", {item["code"] for item in shown["blockers"]})
        self.assertFalse(resolved["ok"])
        self.assertIn("operation_ref_unreadable", {item["code"] for item in resolved["blockers"]})
        self.assertEqual(["receipt_journal_payload_invalid"], [item["code"] for item in state_report["warnings"]])

    def test_journal_identity_mismatch_is_invalid_and_cannot_be_shown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            payload = _kernel_payload("receipt-payload", "identity-app", "deploy.apply")
            database = _write_journal_payload(runtime_root, "receipt-row", payload)
            locator = f"sqlite:{database.resolve()}#terminal_receipts/receipt-row"

            report = receipt_list_report(runtime_root)
            shown = receipt_show_report("receipt-row", runtime_root)
            shown_by_locator = receipt_show_report(locator, runtime_root)

        self.assertEqual(
            ["receipt_journal_identity_mismatch"],
            [item["code"] for item in report["warnings"]],
        )
        self.assertEqual("invalid", report["receipts"][0]["status"])
        self.assertIn("receipt_unreadable", {item["code"] for item in shown["blockers"]})
        self.assertIn("receipt_unreadable", {item["code"] for item in shown_by_locator["blockers"]})

    def test_actual_journal_schema_rejects_digest_and_authoritative_column_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            digest_payload = _kernel_payload(
                "receipt-digest",
                "digest-app",
                "deploy.apply",
            )
            operation_payload = _kernel_payload(
                "receipt-operation",
                "operation-app",
                "backup.apply",
            )
            outcome_payload = _kernel_payload(
                "receipt-outcome",
                "outcome-app",
                "restore-drill.apply",
            )
            database = _write_actual_schema_journal_payloads(
                runtime_root,
                [digest_payload, operation_payload, outcome_payload],
            )

            baseline = receipt_list_report(runtime_root)
            tampered_digest_payload = dict(digest_payload)
            tampered_digest_payload["app"] = "spoofed-app"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "UPDATE terminal_receipts SET payload_json = ? WHERE receipt_id = ?",
                    (
                        json.dumps(tampered_digest_payload, sort_keys=True),
                        "receipt-digest",
                    ),
                )
                connection.execute(
                    "UPDATE terminal_receipts SET operation_id = ? WHERE receipt_id = ?",
                    ("operation-wrong", "receipt-operation"),
                )
                connection.execute(
                    "UPDATE terminal_receipts SET outcome = ? WHERE receipt_id = ?",
                    ("failed", "receipt-outcome"),
                )
                connection.commit()
            finally:
                connection.close()

            tampered = receipt_list_report(runtime_root)
            shown = receipt_show_report("receipt-digest", runtime_root)

        self.assertEqual([], baseline["warnings"])
        self.assertEqual(3, len(baseline["receipts"]))
        self.assertEqual(
            ["receipt_journal_integrity_failed"] * 3,
            [item["code"] for item in tampered["warnings"]],
        )
        self.assertEqual(
            {"receipt-digest", "receipt-operation", "receipt-outcome"},
            {item["receipt_id"] for item in tampered["receipts"]},
        )
        self.assertEqual(
            {"invalid"},
            {item["status"] for item in tampered["receipts"]},
        )
        self.assertIn("receipt_unreadable", {item["code"] for item in shown["blockers"]})

    def test_invalid_utf8_file_and_journal_blob_degrade_to_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            file_path = runtime_root / "apps" / "binary-app" / "receipts" / "binary.json"
            file_path.parent.mkdir(parents=True)
            file_path.write_bytes(b"\xff\xfe\x00")
            _write_raw_journal_payload(runtime_root, "receipt-binary", b"\xff\xfe")

            report = receipt_list_report(runtime_root)
            timeline = receipt_timeline(runtime_root)

        codes = {item["code"] for item in report["warnings"]}
        self.assertEqual({"receipt_unreadable", "receipt_journal_payload_invalid"}, codes)
        self.assertEqual(2, len(report["receipts"]))
        self.assertEqual(codes, {item["code"] for item in timeline["warnings"]})

    def test_symlinked_journal_is_not_opened_as_authoritative_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            target_root = root / "target-runtime"
            target_database = _write_journal_payload(
                target_root,
                "receipt-target",
                _kernel_payload("receipt-target", "target-app", "deploy.apply"),
            )
            before = target_database.read_bytes()
            database = runtime_root / "host-state" / "operations.db"
            database.parent.mkdir(parents=True)
            database.symlink_to(target_database)

            report = receipt_list_report(runtime_root)
            after = target_database.read_bytes()

        self.assertEqual(before, after)
        self.assertEqual([], report["receipts"])
        self.assertEqual(
            ["receipt_journal_unsafe_path"],
            [item["code"] for item in report["warnings"]],
        )

    def test_symlinked_journal_parent_is_not_opened_as_authoritative_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            target_root = root / "target-runtime"
            target_database = _write_journal_payload(
                target_root,
                "receipt-target",
                _kernel_payload("receipt-target", "target-app", "deploy.apply"),
            )
            runtime_root.mkdir(parents=True)
            (runtime_root / "host-state").symlink_to(target_database.parent)

            report = receipt_list_report(runtime_root)

        self.assertEqual([], report["receipts"])
        self.assertEqual(
            ["receipt_journal_unsafe_path"],
            [item["code"] for item in report["warnings"]],
        )

    def test_implicit_file_scan_rejects_symlinked_receipt_file_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            external_root = root / "external"
            external_root.mkdir()
            outside_file = external_root / "outside.json"
            outside_file.write_text(
                json.dumps(
                    {
                        "kind": "ophelia.receipt",
                        "receipt_id": "receipt-outside-file",
                        "operation": "outside.apply",
                        "status": "succeeded",
                        "app": "file-app",
                        "environment": "production",
                        "inputs_redacted": True,
                        "innocuous_note": "CANARY-OUTSIDE-FILE",
                    }
                )
                + "\n"
            )
            file_receipts = runtime_root / "apps" / "file-app" / "receipts"
            file_receipts.mkdir(parents=True)
            (file_receipts / "linked.json").symlink_to(outside_file)

            outside_directory = external_root / "receipt-directory"
            outside_directory.mkdir()
            (outside_directory / "outside.json").write_text(
                outside_file.read_text().replace(
                    "receipt-outside-file",
                    "receipt-outside-directory",
                )
            )
            directory_app = runtime_root / "apps" / "directory-app"
            directory_app.mkdir(parents=True)
            (directory_app / "receipts").symlink_to(outside_directory)

            report = receipt_list_report(runtime_root)

        serialized = json.dumps(report, sort_keys=True)
        self.assertEqual([], report["receipts"])
        self.assertNotIn("CANARY-OUTSIDE-FILE", serialized)
        self.assertEqual(
            {"receipt_unsafe_path"},
            {item["code"] for item in report["warnings"]},
        )

    def test_backup_application_json_is_never_indexed_as_a_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            private_path = (
                runtime_root
                / "backups"
                / "apps"
                / "victim"
                / "backup-1"
                / "runtime"
                / "user-data.json"
            )
            private_path.parent.mkdir(parents=True)
            private_path.write_text(
                json.dumps({"customer_note": "CANARY-PRIVATE-BACKUP-DATA"}) + "\n"
            )

            report = receipt_list_report(runtime_root)

        self.assertEqual([], report["receipts"])
        self.assertNotIn("CANARY-PRIVATE-BACKUP-DATA", json.dumps(report, sort_keys=True))

    def test_incomplete_journal_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            database = runtime_root / "host-state" / "operations.db"
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE terminal_receipts (receipt_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)"
                )
                connection.commit()
            finally:
                connection.close()

            report = receipt_list_report(runtime_root)

        self.assertEqual([], report["receipts"])
        self.assertEqual(
            ["receipt_journal_schema_invalid"],
            [item["code"] for item in report["warnings"]],
        )

    def test_duplicate_identity_with_different_payloads_emits_collision_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            _write_file_receipt(runtime_root, "alpha-app", "receipt-shared", "deploy.apply")
            _write_file_receipt(runtime_root, "beta-app", "receipt-shared", "backup.apply")

            report = receipt_list_report(runtime_root)

        self.assertEqual(1, len(report["receipts"]))
        self.assertEqual(2, len(report["receipts"][0]["receipt_locators"]))
        self.assertEqual(
            ["receipt_identity_collision"],
            [item["code"] for item in report["warnings"]],
        )

    def test_show_defensively_redacts_receipt_that_disclaims_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            path = runtime_root / "apps" / "unsafe-app" / "receipts" / "unsafe.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "kind": "ophelia.receipt",
                        "receipt_id": "receipt-unsafe",
                        "operation": "unsafe.apply",
                        "status": "failed",
                        "app": "unsafe-app",
                        "environment": "staging",
                        "inputs_redacted": False,
                        "api_token": "never-print-receipt-token",
                    }
                )
                + "\n"
            )

            shown = receipt_show_report("receipt-unsafe", runtime_root)

        serialized = json.dumps(shown, sort_keys=True)
        self.assertNotIn("never-print-receipt-token", serialized)
        self.assertIn("receipt_inputs_not_redacted", {item["code"] for item in shown["warnings"]})

    def test_explicit_product_recovery_wrapper_is_classified_as_product(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            path = _write_product_receipt(runtime_root)
            payload = json.loads(path.read_text())
            payload["kind"] = "ophelia.product-recovery-receipt"
            path.write_text(json.dumps(payload, sort_keys=True) + "\n")

            shown = receipt_show_report(str(path), runtime_root)

        self.assertEqual("product", shown["receipt_source"])
        self.assertEqual("receipt-product", shown["receipt_id"])

    def test_standalone_product_wrapper_without_digest_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            path = _write_product_receipt(runtime_root)
            payload = json.loads(path.read_text())
            payload.pop("ophelia_receipt_digest")
            path.write_text(json.dumps(payload, sort_keys=True) + "\n")

            report = receipt_list_report(runtime_root)
            shown = receipt_show_report("receipt-product", runtime_root)
            shown_by_path = receipt_show_report(str(path), runtime_root)

        self.assertEqual(
            ["receipt_product_integrity_failed"],
            [item["code"] for item in report["warnings"]],
        )
        self.assertEqual("invalid", report["receipts"][0]["status"])
        self.assertIn("receipt_unreadable", {item["code"] for item in shown["blockers"]})
        self.assertIn("receipt_unreadable", {item["code"] for item in shown_by_path["blockers"]})

    def test_cli_show_supports_exact_prefix_locator_and_explicit_product_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            product_path = _write_product_receipt(runtime_root)
            database = _write_kernel_receipt(runtime_root)
            locator = f"sqlite:{database.resolve()}#terminal_receipts/receipt-kernel"

            exact_code, exact = _run_cli_show("receipt-kernel", runtime_root)
            prefix_code, prefix = _run_cli_show("receipt-kern", runtime_root)
            locator_code, locator_report = _run_cli_show(locator, runtime_root)
            product_code, product = _run_cli_show(str(product_path), runtime_root)

        self.assertEqual((0, 0, 0, 0), (exact_code, prefix_code, locator_code, product_code))
        self.assertEqual("receipt-kernel", exact["receipt_id"])
        self.assertEqual("receipt-kernel", prefix["receipt_id"])
        self.assertEqual("receipt-kernel", locator_report["receipt_id"])
        self.assertEqual("receipt-product", product["receipt_id"])
        self.assertEqual("product-app", product["app"])
        self.assertEqual("production", product["environment"])
        self.assertEqual("product", product["receipt_source"])

    def test_restore_drills_cli_show_supports_journal_exact_and_latest_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            drill_payload = _kernel_payload(
                "receipt-drill",
                "kernel-app",
                "restore-drill.apply",
            )
            _write_journal_payload(runtime_root, "receipt-drill", drill_payload)

            exact_code, exact = _run_restore_drills_show(
                "receipt-drill",
                runtime_root,
            )
            alias_code, alias = _run_restore_drills_show(
                "latest:kernel-app",
                runtime_root,
            )

        self.assertEqual((0, 0), (exact_code, alias_code))
        self.assertEqual("receipt-drill", exact["drill_id"])
        self.assertEqual("receipt-drill", alias["drill_id"])
        self.assertEqual("ophelia.kernel.terminal_receipt", exact["drill"]["kind"])
        self.assertEqual("ophelia.kernel.terminal_receipt", alias["drill"]["kind"])
        self.assertTrue(str(exact["receipt_locator"]).startswith("sqlite:"))

    def test_restore_drills_cli_resolves_legacy_verification_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            path = (
                runtime_root
                / "apps"
                / "legacy-verify-app"
                / "restore-drills"
                / "verification.json"
            )
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "kind": "ophelia.receipt",
                        "operation": "backup.verify.apply",
                        "operation_id": "operation-legacy-verification",
                        "verify_id": "verify-legacy",
                        "status": "succeeded",
                        "app": "legacy-verify-app",
                        "environment": "production",
                        "inputs_redacted": True,
                    }
                )
                + "\n"
            )

            code, report = _run_restore_drills_show("verify-legacy", runtime_root)

        self.assertEqual(0, code)
        self.assertEqual("verify-legacy", report["drill_id"])

    def test_restore_drills_cli_rejects_non_drill_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            _write_kernel_receipt(runtime_root)

            code, report = _run_restore_drills_show("receipt-kernel", runtime_root)

        self.assertEqual(1, code)
        self.assertNotIn("drill", report)
        self.assertIn(
            "operation_ref_not_found",
            {item["code"] for item in report["blockers"]},
        )

    def test_restore_drills_cli_normalizes_product_scope_and_refreshes_warning_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            product_path = _write_product_receipt(runtime_root)
            wrapper = json.loads(product_path.read_text())
            wrapper["operation"] = "restore-drill.apply"
            wrapper["ophelia_receipt"]["operation"] = "restore-drill.apply"
            wrapper["ophelia_receipt_digest"] = _canonical_payload_digest(
                wrapper["ophelia_receipt"]
            )
            product_path.write_text(json.dumps(wrapper, sort_keys=True) + "\n")

            standalone_code, standalone = _run_restore_drills_show(
                "receipt-product",
                runtime_root,
            )

            journal_payload = dict(wrapper["ophelia_receipt"])
            journal_payload["outcome"] = "failed_compensated"
            _write_journal_payload(runtime_root, "receipt-product", journal_payload)
            warned_code, warned = _run_restore_drills_show(
                "receipt-product",
                runtime_root,
            )

        self.assertEqual(0, standalone_code)
        self.assertEqual("product-app", standalone["app"])
        self.assertEqual("production", standalone["environment"])
        self.assertEqual("ophelia.product-operation-receipt", standalone["drill"]["kind"])
        self.assertEqual(0, warned_code)
        self.assertIn(
            "receipt_correlation_integrity_failed",
            {item["code"] for item in warned["warnings"]},
        )
        self.assertEqual(len(warned["warnings"]), warned["digest"]["counts"]["warnings"])
        self.assertIn(
            "receipt_correlation_integrity_failed",
            warned["digest"]["warning_codes"],
        )

    def test_read_only_scan_does_not_create_a_missing_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            database = runtime_root / "host-state" / "operations.db"

            report = receipt_list_report(runtime_root)

            self.assertEqual([], report["receipts"])
            self.assertFalse(database.exists())


def _write_legacy_receipt(runtime_root: Path) -> None:
    path = runtime_root / "apps" / "legacy-app" / "receipts" / "legacy.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "kind": "ophelia.receipt",
                "receipt_id": "receipt-legacy",
                "operation": "app.export.create",
                "status": "succeeded",
                "app": "legacy-app",
                "environment": "staging",
                "started_at": "2026-08-28T10:00:00Z",
                "completed_at": "2026-08-28T10:01:00Z",
                "artifacts": [{"path": "/tmp/safe-artifact", "kind": "fixture"}],
                "rollback": {"available": False},
                "inputs_redacted": True,
            }
        )
        + "\n"
    )


def _write_product_receipt(
    runtime_root: Path,
    previous_revision_id: Optional[str] = None,
) -> Path:
    path = runtime_root / "receipts" / "product" / "receipt-product.json"
    path.parent.mkdir(parents=True)
    terminal = {
        "kind": "ophelia.kernel.terminal_receipt",
        "receipt_id": "receipt-product",
        "operation_id": "operation-product",
        "operation": "backup.apply",
        "outcome": "succeeded",
        "app": "product-app",
        "environment": "production",
        "started_at": "2026-08-29T10:00:00Z",
        "completed_at": "2026-08-29T10:01:00Z",
        "previous_revision_id": previous_revision_id,
        "verification": {
            "status": "passed",
            "checks": [
                {
                    "name": "backup_integrity",
                    "status": "passed",
                    "summary": "verified",
                }
            ],
        },
        "inputs_redacted": True,
    }
    payload = {
        "schema_version": 1,
        "kind": "ophelia.product-operation-receipt",
        "status": "succeeded",
        "operation": "backup.apply",
        "product_id": "product-app",
        "inputs_redacted": True,
        "ophelia_receipt_digest": _canonical_payload_digest(terminal),
        "ophelia_receipt": terminal,
    }
    path.write_text(json.dumps(payload, sort_keys=True) + "\n")
    return path


def _write_kernel_receipt(runtime_root: Path) -> Path:
    database = runtime_root / "host-state" / "operations.db"
    database.parent.mkdir(parents=True)
    payload = {
        "kind": "ophelia.kernel.terminal_receipt",
        "receipt_id": "receipt-kernel",
        "operation_id": "operation-kernel",
        "operation": "deploy.apply",
        "outcome": "succeeded",
        "app": "kernel-app",
        "environment": "production",
        "started_at": "2026-08-30T10:00:00Z",
        "completed_at": "2026-08-30T10:01:00Z",
        "previous_revision_id": "rev-previous",
        "verification": {
            "status": "passed",
            "checks": [
                {
                    "name": "runtime_health",
                    "status": "passed",
                    "summary": "healthy",
                }
            ],
        },
        "inputs_redacted": True,
    }
    _write_journal_payload(runtime_root, "receipt-kernel", payload)
    return database


def _write_journal_payload(
    runtime_root: Path,
    receipt_id: str,
    payload: Dict[str, object],
) -> Path:
    return _write_raw_journal_payload(runtime_root, receipt_id, json.dumps(payload, sort_keys=True))


def _write_raw_journal_payload(runtime_root: Path, receipt_id: str, raw_payload: object) -> Path:
    database = runtime_root / "host-state" / "operations.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS terminal_receipts (
                receipt_id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL UNIQUE,
                outcome TEXT NOT NULL,
                receipt_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        try:
            parsed = json.loads(raw_payload)
        except (TypeError, ValueError, UnicodeDecodeError):
            parsed = None
        payload = parsed if isinstance(parsed, dict) else {}
        connection.execute(
            """
            INSERT INTO terminal_receipts(
                receipt_id, operation_id, outcome, receipt_digest, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                payload.get("operation_id") or f"operation-{receipt_id}",
                payload.get("outcome") or "succeeded",
                (
                    _canonical_payload_digest(payload)
                    if payload
                    else "sha256:" + "0" * 64
                ),
                raw_payload,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return database


def _write_actual_schema_journal_payloads(
    runtime_root: Path,
    payloads: list,
) -> Path:
    database = runtime_root / "host-state" / "operations.db"
    database.parent.mkdir(parents=True, exist_ok=True)
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
        for payload in payloads:
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
                    _canonical_payload_digest(payload),
                    json.dumps(payload, sort_keys=True),
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return database


def _run_cli_show(reference: str, runtime_root: Path) -> Tuple[int, Dict[str, object]]:
    output = StringIO()
    with redirect_stdout(output):
        result = main(
            [
                "receipts",
                "show",
                reference,
                "--runtime-root",
                str(runtime_root),
                "--json",
            ]
        )
    return result, json.loads(output.getvalue())


def _run_restore_drills_show(
    reference: str,
    runtime_root: Path,
) -> Tuple[int, Dict[str, object]]:
    output = StringIO()
    with redirect_stdout(output):
        result = main(
            [
                "restore-drills",
                "show",
                reference,
                "--runtime-root",
                str(runtime_root),
                "--json",
            ]
        )
    return result, json.loads(output.getvalue())


def _kernel_payload(receipt_id: str, app: str, operation: str) -> Dict[str, object]:
    return {
        "kind": "ophelia.kernel.terminal_receipt",
        "receipt_id": receipt_id,
        "operation_id": "operation-" + receipt_id,
        "operation": operation,
        "outcome": "succeeded",
        "app": app,
        "environment": "production",
        "started_at": "2026-08-30T10:00:00Z",
        "completed_at": "2026-08-30T10:01:00Z",
        "previous_revision_id": "rev-previous",
        "inputs_redacted": True,
    }


def _write_file_receipt(
    runtime_root: Path,
    app: str,
    receipt_id: str,
    operation: str,
) -> Path:
    path = runtime_root / "apps" / app / "receipts" / f"{receipt_id}.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "kind": "ophelia.receipt",
                "receipt_id": receipt_id,
                "operation": operation,
                "status": "succeeded",
                "app": app,
                "environment": "production",
                "inputs_redacted": True,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return path


def _canonical_payload_digest(payload: Dict[str, object]) -> str:
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
