from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.manifest import load_manifest
from ophelia.operator_reports import manifest_registry, preflight_report, release_registry, runtime_ownership, secrets_required
from ophelia.runtime import deploy_bundle


class OperatorReportTests(unittest.TestCase):
    def test_registry_preflight_secrets_and_ownership_reports(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest())
            manifest = load_manifest(manifest_path)
            deploy_bundle(manifest, manifest_path, runtime_root)

            registry = manifest_registry(root, runtime_root)
            releases = release_registry(runtime_root)
            preflight = preflight_report(manifest_path, runtime_root, root)
            secrets = secrets_required(manifest, manifest_path, runtime_root)
            ownership = runtime_ownership(runtime_root, "operator-test")

            self.assertEqual("operator-test", registry["manifests"][0]["app"])
            self.assertEqual(1, len(releases["releases"]))
            self.assertTrue(preflight["ok"])
            self.assertIn("API_TOKEN", secrets["blocking_keys"])
            self.assertTrue(ownership["ophelia_generated_files"])

            (runtime_root / "apps" / "operator-test" / "env").write_text("API_TOKEN=real-token\n")
            resolved = secrets_required(manifest, manifest_path, runtime_root)
            self.assertEqual([], resolved["blocking_keys"])

            env = {**os.environ, "OPHELIA_SKIP_DOCKER_STATUS": "1"}
            result = subprocess.run(
                [
                    str(repo / "cli" / "ship"),
                    "host",
                    "inventory",
                    "--runtime-root",
                    str(runtime_root),
                    "--json",
                ],
                text=True,
                capture_output=True,
                check=True,
                env=env,
            )
            json.loads(result.stdout)


def _manifest() -> str:
    return """
version: 1
app: operator-test
kind: static
static_root: /tmp/operator-test
env:
  API_TOKEN: replace-me
routes:
  - domain: operator-test.example.com
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
