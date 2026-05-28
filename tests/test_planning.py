from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.manifest import load_manifest
from ophelia.planning import bundle_diff, deploy_plan
from ophelia.runtime import deploy_bundle


class PlanningTests(unittest.TestCase):
    def test_deploy_plan_reports_changes_without_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest("super-secret-value"))
            manifest = load_manifest(manifest_path)

            plan = deploy_plan(manifest, manifest_path, runtime_root)

            self.assertEqual("plan-test", plan["app"])
            self.assertTrue(plan["changed_files"])
            self.assertIn("SECRET_TOKEN", {item["key"] for item in plan["env_requirements"]})
            self.assertNotIn("super-secret-value", json.dumps(plan))

    def test_bundle_diff_is_clean_after_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest("replace-me"))
            manifest = load_manifest(manifest_path)
            deploy_bundle(manifest, manifest_path, runtime_root)

            diff = bundle_diff(manifest, runtime_root)

            self.assertTrue(diff["clean"])
            self.assertEqual([], diff["changed_files"])

    def test_diff_json_cli_is_parseable(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                str(repo / "cli" / "ship"),
                "diff",
                str(repo / "examples" / "dragonwriter.ophelia.yml"),
                "--runtime-root",
                str(Path(tempfile.gettempdir()) / "ophelia-plan-test-runtime"),
                "--json",
            ],
            text=True,
            capture_output=True,
            check=True,
        )

        payload = json.loads(result.stdout)
        self.assertEqual("dragon-writer", payload["app"])
        self.assertIn("changed_files", payload)


def _manifest(secret_value: str) -> str:
    return f"""
version: 1
app: plan-test
kind: service
image: ghcr.io/example/plan-test:latest
env:
  SECRET_TOKEN: {secret_value}
services:
  web:
    port: 3000
routes:
  - domain: plan-test.example.com
    service: web
verify:
  - name: health
    url: https://plan-test.example.com/health
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
