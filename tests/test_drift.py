from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.drift import manifest_drift
from ophelia.manifest import load_manifest
from ophelia.runtime import deploy_bundle
from ophelia.state_db import refresh_state


class DriftTests(unittest.TestCase):
    def test_clean_rendered_state_reports_no_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest())
            manifest = load_manifest(manifest_path)
            deploy_bundle(manifest, manifest_path, runtime_root)
            refresh_state(runtime_root, root)

            report = manifest_drift(manifest, manifest_path, runtime_root)

            self.assertFalse(report["drift"])
            self.assertEqual("ophelia.drift_report", report["kind"])
            self.assertEqual("info", report["severity"])
            self.assertEqual([], report["rendered"]["changed_files"])

    def test_modified_runtime_file_reports_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest())
            manifest = load_manifest(manifest_path)
            app_root = deploy_bundle(manifest, manifest_path, runtime_root)
            refresh_state(runtime_root, root)
            (app_root / "caddy" / "drift-test.caddy").write_text("# modified\n")

            report = manifest_drift(manifest, manifest_path, runtime_root)

            self.assertTrue(report["drift"])
            self.assertEqual("drift", report["status"])
            self.assertEqual("medium", report["severity"])
            self.assertEqual(["caddy/drift-test.caddy"], [item["path"] for item in report["rendered"]["changed_files"]])
            self.assertIn("rendered_file_changed", {item["code"] for item in report["findings"]})

    def test_missing_state_index_is_reported_as_drift_finding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest())
            manifest = load_manifest(manifest_path)
            deploy_bundle(manifest, manifest_path, runtime_root)

            report = manifest_drift(manifest, manifest_path, runtime_root)

            codes = {item["code"] for item in report["findings"]}
            self.assertTrue(report["drift"])
            self.assertIn("state_index_missing", codes)

    def test_findings_are_sorted_by_severity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest_with_env())
            manifest = load_manifest(manifest_path)
            app_root = deploy_bundle(manifest, manifest_path, runtime_root)
            (app_root / "env").write_text("OPHELIA_APP=drift-test\n")
            (app_root / "caddy" / "drift-test.caddy").write_text("# modified\n")

            report = manifest_drift(manifest, manifest_path, runtime_root)

            ranks = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
            severities = [ranks[item["severity"]] for item in report["findings"]]
            self.assertEqual(sorted(severities, reverse=True), severities)
            self.assertEqual("high", report["severity"])
            self.assertEqual("env_key_missing", report["findings"][0]["code"])

    def test_github_observation_reports_missing_environment_secret_as_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest_with_env())
            manifest = load_manifest(manifest_path)
            deploy_bundle(manifest, manifest_path, runtime_root)
            observations = runtime_root / "github" / "observations"
            observations.mkdir(parents=True)
            (observations / "drift-test.json").write_text(
                """
{
  "repo": "example/drift-test",
  "environments": {
    "staging": {"secrets": ["OPHELIA_DEPLOY_HOST", "OPHELIA_DEPLOY_PORT", "OPHELIA_DEPLOY_USER", "OPHELIA_DEPLOY_KEY"]},
    "production": {"secrets": []}
  },
  "branch_protection": {
    "next": {"protected": true, "required_status_checks": ["validate", "tests"]},
    "master": {"protected": true, "required_status_checks": ["validate", "tests"]}
  },
  "labels": ["release:patch", "release:minor", "release:major"],
  "workflows": [".github/workflows/ophelia-staging.yml", ".github/workflows/ophelia-release.yml"]
}
""".strip()
                + "\n"
            )

            report = manifest_drift(manifest, manifest_path, runtime_root)

        self.assertTrue(report["drift"])
        codes = {item["code"] for item in report["findings"]}
        self.assertIn("github_environment_secret_missing", codes)
        github_snapshot = next(item for item in report["snapshots"] if item["owner"] == "github")
        self.assertEqual("drift", github_snapshot["status"])


def _manifest() -> str:
    return """
version: 1
app: drift-test
kind: static
static_root: /tmp/drift-test-static
routes:
  - domain: drift-test.example.com
""".strip() + "\n"


def _manifest_with_env() -> str:
    return """
version: 1
app: drift-test
kind: static
static_root: /tmp/drift-test-static
env:
  API_TOKEN: replace-me
routes:
  - domain: drift-test.example.com
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
