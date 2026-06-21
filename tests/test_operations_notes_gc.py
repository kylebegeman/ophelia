from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.gc import apply_gc, gc_plan
from ophelia.manifest import load_manifest
from ophelia.notes import add_note, list_notes
from ophelia.operations import operation_plan, run_operation
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
            self.assertEqual("validate-all-manifests", plan["operation"])
            confirmed = run_operation("validate-all-manifests", str(plan["confirmation_token"]), root, runtime_root)
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
