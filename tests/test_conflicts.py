from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.conflicts import scan_conflicts
from ophelia.manifest import load_manifest
from ophelia.runtime import deploy_bundle


class ConflictTests(unittest.TestCase):
    def test_reports_no_conflicts_for_distinct_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "one.ophelia.yml").write_text(_manifest("one", "one.example.com", 3101))
            (root / "two.ophelia.yml").write_text(_manifest("two", "two.example.com", 3102))

            report = scan_conflicts(root)

            self.assertTrue(report["ok"])
            self.assertEqual([], report["conflicts"])
            self.assertEqual(2, report["manifest_count"])

    def test_reports_duplicate_domain_port_alias_and_app(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "one.ophelia.yml").write_text(_manifest("same", "same.example.com", 3101))
            (root / "two.ophelia.yml").write_text(_manifest("same", "same.example.com", 3101))

            report = scan_conflicts(root)

            self.assertFalse(report["ok"])
            conflict_types = {item["type"] for item in report["conflicts"]}
            self.assertIn("duplicate_app_id", conflict_types)
            self.assertIn("duplicate_domain", conflict_types)
            self.assertIn("duplicate_host_port", conflict_types)
            self.assertIn("duplicate_docker_alias", conflict_types)

    def test_runtime_lock_for_same_app_is_not_reported_as_route_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "one.ophelia.yml"
            manifest_path.write_text(_manifest("one", "one.example.com", 3101))
            manifest = load_manifest(manifest_path)
            app_root = deploy_bundle(manifest, manifest_path, runtime_root)
            (app_root / "active_release.json").write_text((app_root / "release.json").read_text())

            report = scan_conflicts(root, runtime_root=runtime_root)

        self.assertTrue(report["ok"])
        self.assertEqual([], report["conflicts"])

    def test_current_manifests_have_verification_checks(self) -> None:
        manifest_dir = Path(__file__).resolve().parents[1] / "manifests"
        report = scan_conflicts(manifest_dir)

        warning_types = {item["type"] for item in report["warnings"]}
        self.assertNotIn("missing_verification_checks", warning_types)


def _manifest(app: str, domain: str, host_port: int) -> str:
    return f"""
version: 1
app: {app}
kind: service
image: ghcr.io/example/{app}:latest
services:
  web:
    port: 3000
    host_port: {host_port}
routes:
  - domain: {domain}
    service: web
verify:
  - name: health
    url: https://{domain}/health
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
