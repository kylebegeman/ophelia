from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.manifest import load_manifest
from ophelia.runtime import deploy_bundle, list_releases, load_release


class ReleaseHistoryTests(unittest.TestCase):
    def test_deploy_writes_current_pointer_and_release_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(
                """
version: 1
app: release-test
kind: service
image: ghcr.io/example/release-test:latest
services:
  web:
    port: 3000
routes:
  - domain: release-test.example.com
    service: web
""".strip()
                + "\n"
            )

            manifest = load_manifest(manifest_path)
            app_root = deploy_bundle(manifest, manifest_path, runtime_root)

            current = json.loads((app_root / "release.json").read_text())
            releases = list_releases(runtime_root, "release-test")

            self.assertEqual(current["release_id"], releases[0]["release_id"])
            self.assertEqual("release-test", current["app"])
            self.assertEqual(str(app_root.resolve()), current["runtime_path"])
            self.assertIn("manifest_hash", current)
            self.assertIn("rendered_bundle_hash", current)
            self.assertEqual({"default": "ghcr.io/example/release-test:latest", "web": "ghcr.io/example/release-test:latest"}, current["images"])
            self.assertEqual("not_run", current["verification"]["status"])
            self.assertEqual(current["release_id"], load_release(runtime_root, "release-test", current["release_id"])["release_id"])

    def test_repeated_deploys_create_distinct_release_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(
                """
version: 1
app: release-test
kind: static
static_root: /tmp/release-test
routes:
  - domain: release-test.example.com
""".strip()
                + "\n"
            )
            manifest = load_manifest(manifest_path)

            first = deploy_bundle(manifest, manifest_path, runtime_root)
            first_id = json.loads((first / "release.json").read_text())["release_id"]
            second = deploy_bundle(manifest, manifest_path, runtime_root)
            second_id = json.loads((second / "release.json").read_text())["release_id"]

            self.assertNotEqual(first_id, second_id)


if __name__ == "__main__":
    unittest.main()
