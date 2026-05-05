from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.conflicts import scan_conflicts


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
