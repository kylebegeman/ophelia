from __future__ import annotations

import sys
import tempfile
import unittest
import json
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.manifest import load_manifest
from ophelia.rollback import apply_rollback, rollback_plan
from ophelia.runtime import deploy_bundle


class RollbackTests(unittest.TestCase):
    def test_plans_and_applies_release_bundle_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"

            manifest_path.write_text(_manifest("ghcr.io/example/rollback-test:v1"))
            first = load_manifest(manifest_path)
            first_root = deploy_bundle(first, manifest_path, runtime_root)
            first_release = (first_root / "release.json").read_text()
            first_release_id = __import__("json").loads(first_release)["release_id"]

            manifest_path.write_text(_manifest("ghcr.io/example/rollback-test:v2"))
            second = load_manifest(manifest_path)
            deploy_bundle(second, manifest_path, runtime_root)
            self.assertIn("rollback-test:v2", (first_root / "compose.yml").read_text())

            plan = rollback_plan(runtime_root, "rollback-test", first_release_id)
            self.assertTrue(plan["can_apply"])
            self.assertTrue(plan["confirmation_token"])
            self.assertTrue(any(item["path"] == "compose.yml" for item in plan["changes"]))
            self.assertTrue(plan["post_apply_verification"]["available"])
            self.assertFalse(plan["traffic_switching"]["managed_by_ophelia"])

            report = apply_rollback(
                runtime_root,
                "rollback-test",
                first_release_id,
                str(plan["confirmation_token"]),
            )

            self.assertIn("rollback-test:v1", (first_root / "compose.yml").read_text())
            self.assertEqual(first_release_id, report["target_release_id"])
            self.assertEqual([], report["deleted_files"])
            self.assertEqual("not_run", report["verification"]["status"])
            self.assertEqual("health", report["verification"]["checks"][0]["name"])
            self.assertFalse(report["traffic_switching"]["managed_by_ophelia"])
            self.assertTrue((first_root / "rollback-reports" / f"{report['report_id']}.json").exists())

    def test_apply_requires_matching_confirmation_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest("ghcr.io/example/rollback-test:v1"))
            manifest = load_manifest(manifest_path)
            app_root = deploy_bundle(manifest, manifest_path, runtime_root)
            release_id = __import__("json").loads((app_root / "release.json").read_text())["release_id"]

            with self.assertRaises(RuntimeError):
                apply_rollback(runtime_root, "rollback-test", release_id, "wrong-token")

    def test_plan_blocks_external_symlink_in_release_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest("ghcr.io/example/rollback-test:v1"))
            app_root = deploy_bundle(load_manifest(manifest_path), manifest_path, runtime_root)
            release = json.loads((app_root / "release.json").read_text())
            bundle_root = Path(release["bundle_path"])
            external_compose = root / "outside-compose.yml"
            external_compose.write_text("services: {}\n")
            (bundle_root / "compose.yml").unlink()
            (bundle_root / "compose.yml").symlink_to(external_compose)

            plan = rollback_plan(runtime_root, "rollback-test", release["release_id"])

            self.assertFalse(plan["can_apply"])
            self.assertIn("External symlink", str(plan["blockers"]))


def _manifest(image: str) -> str:
    return f"""
version: 1
app: rollback-test
kind: service
image: {image}
services:
  web:
    port: 3000
routes:
  - domain: rollback-test.example.com
    service: web
verify:
  - name: health
    url: https://rollback-test.example.com/health
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
