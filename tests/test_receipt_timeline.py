from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.portability import receipt_list_report, receipt_show_report
from ophelia.receipt_index import receipt_timeline


def _write_receipt(runtime_root: Path, app: str, environment: str, payload: dict) -> Path:
    receipts_dir = runtime_root / "apps" / app / environment / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    path = receipts_dir / f"{payload['receipt_id']}.json"
    path.write_text(json.dumps(payload))
    return path


def _receipt(
    receipt_id: str,
    operation: str,
    status: str,
    app: str,
    environment: str,
    started_at: str,
    *,
    artifacts=None,
    rollback=None,
) -> dict:
    payload = {
        "schema_version": 1,
        "kind": "ophelia.receipt",
        "receipt_id": receipt_id,
        "operation": operation,
        "status": status,
        "app": app,
        "environment": environment,
        "started_at": started_at,
        "completed_at": started_at,
    }
    if artifacts is not None:
        payload["artifacts"] = artifacts
    if rollback is not None:
        payload["rollback"] = rollback
    return payload


class ReceiptTimelineTests(unittest.TestCase):
    def _fixture(self, root: Path) -> Path:
        runtime_root = root / "runtime"
        _write_receipt(
            runtime_root,
            "alpha",
            "staging",
            _receipt(
                "r1",
                "deploy.apply",
                "succeeded",
                "alpha",
                "staging",
                "2026-06-19T08:00:00Z",
                artifacts=[{"path": "plans/compose-diff.diff", "kind": "ophelia.artifact.diff"}],
                rollback={"available": True, "note": "rollback ok"},
            ),
        )
        _write_receipt(
            runtime_root,
            "alpha",
            "production",
            _receipt(
                "r2",
                "deploy.apply",
                "failed",
                "alpha",
                "production",
                "2026-06-20T09:30:00Z",
                rollback={"available": False},
            ),
        )
        _write_receipt(
            runtime_root,
            "beta",
            "staging",
            _receipt(
                "r3",
                "app.traffic.apply",
                "succeeded",
                "beta",
                "staging",
                "2026-06-21T12:00:00Z",
            ),
        )
        return runtime_root

    def test_timeline_is_newest_first_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = self._fixture(Path(temp_dir))
            report = receipt_timeline(runtime_root)
            ids = [item["receipt_id"] for item in report["receipts"]]
            self.assertEqual(["r3", "r2", "r1"], ids)
            self.assertEqual("ophelia.receipt_timeline", report["kind"])
            # Re-running yields the identical order.
            self.assertEqual(ids, [item["receipt_id"] for item in receipt_timeline(runtime_root)["receipts"]])

    def test_per_entry_facets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = self._fixture(Path(temp_dir))
            report = receipt_timeline(runtime_root)
            by_id = {item["receipt_id"]: item for item in report["receipts"]}
            self.assertEqual(["plans/compose-diff.diff"], by_id["r1"]["artifact_paths"])
            self.assertTrue(by_id["r1"]["rollback_available"])
            self.assertFalse(by_id["r2"]["rollback_available"])
            self.assertEqual([], by_id["r3"]["artifact_paths"])

    def test_filter_by_app(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = self._fixture(Path(temp_dir))
            report = receipt_timeline(runtime_root, app="alpha")
            self.assertEqual({"alpha"}, {item["app"] for item in report["receipts"]})
            self.assertEqual(["r2", "r1"], [item["receipt_id"] for item in report["receipts"]])

    def test_filter_by_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = self._fixture(Path(temp_dir))
            report = receipt_timeline(runtime_root, environment="staging")
            self.assertEqual({"r3", "r1"}, {item["receipt_id"] for item in report["receipts"]})

    def test_filter_by_operation_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = self._fixture(Path(temp_dir))
            op = receipt_timeline(runtime_root, operation="deploy.apply")
            self.assertEqual({"r1", "r2"}, {item["receipt_id"] for item in op["receipts"]})
            failed = receipt_timeline(runtime_root, status="failed")
            self.assertEqual(["r2"], [item["receipt_id"] for item in failed["receipts"]])

    def test_filter_by_since_and_until(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = self._fixture(Path(temp_dir))
            since = receipt_timeline(runtime_root, since="2026-06-20")
            self.assertEqual(["r3", "r2"], [item["receipt_id"] for item in since["receipts"]])
            until = receipt_timeline(runtime_root, until="2026-06-20")
            # until=date is inclusive of the whole 2026-06-20 day.
            self.assertEqual(["r2", "r1"], [item["receipt_id"] for item in until["receipts"]])

    def test_filters_compose_with_and_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = self._fixture(Path(temp_dir))
            report = receipt_timeline(
                runtime_root,
                app="alpha",
                operation="deploy.apply",
                status="succeeded",
                since="2026-06-18",
                until="2026-06-19",
            )
            self.assertEqual(["r1"], [item["receipt_id"] for item in report["receipts"]])

    def test_malformed_receipt_becomes_warning_and_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = self._fixture(Path(temp_dir))
            bad_dir = runtime_root / "apps" / "alpha" / "staging" / "receipts"
            bad_path = bad_dir / "broken.json"
            bad_path.write_text("{ this is not valid json ")

            report = receipt_timeline(runtime_root)
            codes = {w["code"] for w in report["warnings"]}
            self.assertIn("receipt_unreadable", codes)
            self.assertTrue(any(w.get("path") == str(bad_path) for w in report["warnings"]))
            # The malformed receipt still must not crash list/show.
            self.assertTrue(receipt_list_report(runtime_root)["receipts"])

    def test_existing_receipt_reports_still_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = self._fixture(Path(temp_dir))
            listing = receipt_list_report(runtime_root)
            self.assertEqual("ophelia.report", listing["kind"])
            self.assertEqual(3, len(listing["receipts"]))
            shown = receipt_show_report("r1", runtime_root=runtime_root)
            self.assertEqual("r1", shown["receipt_id"])
            self.assertEqual([], shown["blockers"])

    def test_timeline_is_json_serializable_and_records_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = self._fixture(Path(temp_dir))
            report = receipt_timeline(runtime_root, app="beta", status="succeeded")
            blob = json.dumps(report)
            self.assertEqual(report, json.loads(blob))
            self.assertEqual("beta", report["filters"]["app"])
            self.assertEqual("succeeded", report["filters"]["status"])


if __name__ == "__main__":
    unittest.main()
