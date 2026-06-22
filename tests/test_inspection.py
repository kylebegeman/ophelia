from __future__ import annotations

import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.inspection import doctor_report, status_report
from ophelia.manifest import load_manifest
from ophelia.runtime import deploy_bundle


class InspectionTests(unittest.TestCase):
    def test_status_reports_runtime_apps_without_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest())
            manifest = load_manifest(manifest_path)
            deploy_bundle(manifest, manifest_path, runtime_root)

            with patch.dict("os.environ", {"OPHELIA_SKIP_DOCKER_STATUS": "1"}):
                report = status_report(runtime_root, root, root / "manifests")

            self.assertTrue(report["runtime_root_exists"])
            self.assertEqual("inspect-test", report["known_apps"][0]["app"])
            self.assertFalse(report["known_apps"][0]["caddy"]["synced"])
            self.assertFalse(report["known_apps"][0]["applied"])
            self.assertIsNone(report["known_apps"][0]["verified"])
            self.assertIn("disk_usage", report)
            self.assertIn("warnings", report)

    def test_doctor_reports_missing_runtime_root_as_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "platform" / "shared").mkdir(parents=True)
            (root / "platform" / "shared" / "compose.yml").write_text("services: {}\n")
            with patch.dict("os.environ", {"OPHELIA_SKIP_DOCKER_STATUS": "1", "OPHELIA_SKIP_GH_STATUS": "1"}):
                report = doctor_report(root / "missing-runtime", root, root / "manifests")

            self.assertTrue(report["ok"])
            self.assertEqual(1, report["schema_version"])
            self.assertEqual("ophelia.doctor", report["kind"])
            self.assertEqual("ok", report["status"])
            self.assertTrue(any("missing-runtime" in warning for warning in report["warnings"]))
            check_names = {str(check["name"]) for check in report["checks"]}
            self.assertIn("state.index.current", check_names)
            self.assertIn("command_catalog.complete", check_names)
            self.assertIn("redaction.smoke", check_names)


def _manifest() -> str:
    return """
version: 1
app: inspect-test
kind: service
image: ghcr.io/example/inspect-test:latest
services:
  web:
    port: 3000
routes:
  - domain: inspect-test.example.com
    service: web
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
