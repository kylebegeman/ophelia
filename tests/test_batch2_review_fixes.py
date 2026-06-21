"""Regression tests for Batch 2 (phases 8-9) review fixes."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.lumen_adapter import dashboard_data
from ophelia.manifest import ManifestError, load_manifest


class MalformedYamlIsolationTests(unittest.TestCase):
    def test_load_manifest_raises_manifest_error_on_bad_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bad = Path(temp_dir) / "bad.ophelia.yml"
            bad.write_text("version: 1\napp: x\n  : : oops\n][\n")
            with self.assertRaises(ManifestError):
                load_manifest(bad)

    def test_one_bad_manifest_does_not_blank_the_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifests = Path(temp_dir) / "manifests"
            manifests.mkdir()
            (manifests / "good.ophelia.yml").write_text(
                "version: 1\n"
                "app: goodapp\n"
                "kind: service\n"
                "image: ghcr.io/example/goodapp@sha256:abc\n"
                "services:\n"
                "  web:\n"
                "    port: 3000\n"
                "routes:\n"
                "  - domain: good.example.com\n"
                "    service: web\n"
            )
            (manifests / "broken.ophelia.yml").write_text("version: 1\napp: broken\n  : : nope\n][\n")
            data = dashboard_data(runtime_root=Path(temp_dir) / "rt", manifests_dir=manifests)
            apps = {row.get("app") for row in data.get("apps", [])}
            # The good app still renders despite a malformed sibling.
            self.assertIn("goodapp", apps)


class SafeCallNoSecretLeakTests(unittest.TestCase):
    def test_exception_message_records_type_not_value(self) -> None:
        from ophelia import lumen_adapter

        warnings: list = []

        def boom():
            raise RuntimeError("connection failed for postgres://u:SAFECALLLEAK@h/db")

        lumen_adapter._safe_call(boom, warnings, "readiness_unavailable", "app1")
        blob = json.dumps(warnings)
        self.assertNotIn("SAFECALLLEAK", blob)
        self.assertIn("RuntimeError", blob)


if __name__ == "__main__":
    unittest.main()
