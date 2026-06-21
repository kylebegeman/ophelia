from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.drift import manifest_drift
from ophelia.manifest import load_manifest
from ophelia.runtime import deploy_bundle


class DriftTests(unittest.TestCase):
    def test_clean_rendered_state_reports_no_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest())
            manifest = load_manifest(manifest_path)
            deploy_bundle(manifest, manifest_path, runtime_root)

            report = manifest_drift(manifest, manifest_path, runtime_root)

            self.assertFalse(report["drift"])
            self.assertEqual([], report["rendered"]["changed_files"])

    def test_modified_runtime_file_reports_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest())
            manifest = load_manifest(manifest_path)
            app_root = deploy_bundle(manifest, manifest_path, runtime_root)
            (app_root / "caddy" / "drift-test.caddy").write_text("# modified\n")

            report = manifest_drift(manifest, manifest_path, runtime_root)

            self.assertTrue(report["drift"])
            self.assertEqual(["caddy/drift-test.caddy"], [item["path"] for item in report["rendered"]["changed_files"]])


def _manifest() -> str:
    return """
version: 1
app: drift-test
kind: static
static_root: /tmp/drift-test-static
routes:
  - domain: drift-test.example.com
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
