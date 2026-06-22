from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.gc import apply_gc, gc_plan
from ophelia.manifest import load_manifest
from ophelia.notes import add_note, list_notes
from ophelia.operations import operation_plan, list_operations, run_operation
from ophelia.runtime import deploy_bundle


class OperationsNotesGCTests(unittest.TestCase):
    def test_operation_plan_notes_and_gc(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "app.ophelia.yml"
            runtime_root = root / "runtime"
            manifest_path.write_text(_manifest("v1"))
            deploy_bundle(load_manifest(manifest_path), manifest_path, runtime_root)
            manifest_path.write_text(_manifest("v2"))
            deploy_bundle(load_manifest(manifest_path), manifest_path, runtime_root)
            manifest_path.write_text(_manifest("v3"))
            deploy_bundle(load_manifest(manifest_path), manifest_path, runtime_root)
            manifest_path.write_text(_manifest("v4"))
            deploy_bundle(load_manifest(manifest_path), manifest_path, runtime_root)
            manifest_path.write_text(_manifest("v5"))
            deploy_bundle(load_manifest(manifest_path), manifest_path, runtime_root)
            manifest_path.write_text(_manifest("v6"))
            deploy_bundle(load_manifest(manifest_path), manifest_path, runtime_root)

            plan = operation_plan("validate-all-manifests", root, runtime_root)
            self.assertEqual(1, plan["schema_version"])
            self.assertEqual("ophelia.operation_plan", plan["kind"])
            self.assertEqual("validate-all-manifests", plan["operation"])
            confirmed = run_operation("validate-all-manifests", str(plan["confirmation_token"]), root, runtime_root)
            self.assertEqual(1, confirmed["schema_version"])
            self.assertEqual("ophelia.operation_report", confirmed["kind"])
            self.assertFalse(confirmed["executed_autonomously"])
            self.assertTrue(Path(str(confirmed["report_path"])).exists())
            self.assertIn("result_artifact", confirmed)

            note = add_note(runtime_root, "release", "rel-1", "rolled back during test", author="tester")
            self.assertEqual("tester", note["author"])
            self.assertEqual(1, len(list_notes(runtime_root, "release", "rel-1")))

            old_static_version = runtime_root / "static" / "gc-test" / "versions" / "20250101T000000Z"
            old_static_version.mkdir(parents=True)
            (old_static_version / "index.html").write_text("old")
            for index in range(5):
                version = runtime_root / "static" / "gc-test" / "versions" / f"2025010{index + 2}T000000Z"
                version.mkdir(parents=True)

            cleanup = gc_plan(runtime_root)
            self.assertTrue(cleanup["candidates"])
            self.assertIn(str(old_static_version), {item["path"] for item in cleanup["candidates"]})
            report = apply_gc(runtime_root, str(cleanup["confirmation_token"]))
            self.assertTrue(report["deleted"])
            self.assertFalse(old_static_version.exists())
            self.assertTrue((runtime_root / "apps" / "gc-test" / "env").exists())

    def test_list_operations_has_envelope_keys(self) -> None:
        report = list_operations()
        self.assertEqual(1, report["schema_version"])
        self.assertEqual("ophelia.operations", report["kind"])
        self.assertTrue(report["operations"])

    def test_notes_reject_corrupt_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            note_path = runtime_root / "notes" / "release" / "rel-1.json"
            note_path.parent.mkdir(parents=True)
            note_path.write_text("{")

            with self.assertRaisesRegex(ValueError, "Notes file is unreadable"):
                list_notes(runtime_root, "release", "rel-1")

    def test_notes_reject_non_list_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            note_path = runtime_root / "notes" / "release" / "rel-1.json"
            note_path.parent.mkdir(parents=True)
            note_path.write_text(json.dumps({"text": "not a list"}))

            with self.assertRaisesRegex(ValueError, "list of note objects"):
                add_note(runtime_root, "release", "rel-1", "new note")


def _manifest(tag: str) -> str:
    return f"""
version: 1
app: gc-test
kind: service
image: ghcr.io/example/gc-test:{tag}
deployment_order: 10
depends_on:
  - platform-db
verify_before_next: true
services:
  web:
    port: 3000
routes:
  - domain: gc-test.example.com
    service: web
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
