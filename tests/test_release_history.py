from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.manifest import load_manifest
from ophelia.runtime import DeployMetadata, apply_local_bundle, current_release_id, deploy_bundle, list_deployments, list_releases, load_release


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

    def test_direct_deploy_rejects_release_id_path_components_before_app_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(
                """
version: 1
app: release-boundary
kind: static
static_root: /tmp/release-boundary
routes:
  - domain: release-boundary.example.com
""".strip()
                + "\n"
            )

            with self.assertRaisesRegex(ValueError, "safe filename component"):
                deploy_bundle(
                    load_manifest(manifest_path),
                    manifest_path,
                    runtime_root,
                    deploy_metadata=DeployMetadata(release_id="../escape"),
                )

            self.assertFalse((runtime_root / "apps" / "release-boundary").exists())

    def test_corrupt_release_records_do_not_break_deployment_listing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            bad_app = runtime_root / "apps" / "bad"
            incomplete_app = runtime_root / "apps" / "incomplete"
            bad_app.mkdir(parents=True)
            incomplete_app.mkdir(parents=True)
            (bad_app / "release.json").write_text("{")
            (incomplete_app / "release.json").write_text(json.dumps({"app": "incomplete"}))

            self.assertEqual([], list_deployments(runtime_root))

    def test_copied_lock_manifest_deploy_uses_app_release_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_runtime = root / "source-runtime"
            target_runtime = root / "target-runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(
                """
version: 1
app: copied-release
environment: staging
kind: service
image: ghcr.io/example/copied-release:next
services:
  web:
    port: 3000
routes:
  - domain: copied-release.example.com
    service: web
""".strip()
                + "\n"
            )

            source_root = deploy_bundle(load_manifest(manifest_path), manifest_path, source_runtime)
            lock_path = source_root / "manifest.lock.json"
            copied = deploy_bundle(
                load_manifest(lock_path),
                lock_path,
                target_runtime,
                deploy_metadata=DeployMetadata(
                    release_id="lumen-v0.9.0",
                    commit_sha="abc123app",
                    build_time="2026-06-25T12:00:00Z",
                ),
            )

            release = json.loads((copied / "release.json").read_text())
            compose = (copied / "compose.yml").read_text()
            self.assertEqual("lumen-v0.9.0", release["release_id"])
            self.assertEqual("abc123app", release["commit_sha"])
            self.assertEqual("abc123app", release["git_sha"])
            self.assertEqual("2026-06-25T12:00:00Z", release["build_time"])
            self.assertIn('      OPHELIA_RELEASE_ID: "lumen-v0.9.0"', compose)
            self.assertIn('      OPHELIA_COMMIT_SHA: "abc123app"', compose)
            self.assertIn('      OPHELIA_BUILD_TIME: "2026-06-25T12:00:00Z"', compose)

    def test_deploy_metadata_env_fallback_uses_app_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(
                """
version: 1
app: env-release
environment: staging
kind: service
image: ghcr.io/example/env-release:next
services:
  web:
    port: 3000
routes:
  - domain: env-release.example.com
    service: web
""".strip()
                + "\n"
            )

            with mock.patch.dict(
                "os.environ",
                {
                    "OPHELIA_DEPLOY_RELEASE_ID": "env-release-id",
                    "OPHELIA_DEPLOY_COMMIT_SHA": "env-app-sha",
                    "OPHELIA_DEPLOY_BUILD_TIME": "2026-06-25T13:00:00Z",
                },
            ):
                app_root = deploy_bundle(load_manifest(manifest_path), manifest_path, runtime_root)

            release = json.loads((app_root / "release.json").read_text())
            self.assertEqual("env-release-id", release["release_id"])
            self.assertEqual("env-app-sha", release["commit_sha"])
            self.assertEqual("2026-06-25T13:00:00Z", release["build_time"])

    def test_load_release_reports_corrupt_record_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            releases_root = runtime_root / "apps" / "release-test" / "releases"
            releases_root.mkdir(parents=True)
            (releases_root / "bad.json").write_text("[1, 2, 3]")

            with self.assertRaisesRegex(ValueError, "Release record must be a JSON object"):
                load_release(runtime_root, "release-test", "bad")

    def test_active_release_tracks_successful_apply_not_latest_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"

            manifest_path.write_text(_static_manifest("active-test", "/tmp/active-one"))
            first_root = apply_local_bundle(load_manifest(manifest_path), manifest_path, runtime_root, root)
            first = json.loads((first_root / "release.json").read_text())
            self.assertTrue((first_root / "active_release.json").exists())
            self.assertEqual(first["release_id"], current_release_id(runtime_root, "active-test"))

            manifest_path.write_text(_static_manifest("active-test", "/tmp/active-two"))
            deploy_bundle(load_manifest(manifest_path), manifest_path, runtime_root)
            latest = json.loads((first_root / "release.json").read_text())
            active = json.loads((first_root / "active_release.json").read_text())

            self.assertNotEqual(first["release_id"], latest["release_id"])
            self.assertEqual(first["release_id"], active["release_id"])
            self.assertEqual(first["release_id"], current_release_id(runtime_root, "active-test"))
            releases = list_releases(runtime_root, "active-test")
            self.assertEqual(1, sum(1 for release in releases if release["active"]))
            self.assertEqual(1, sum(1 for release in releases if release["latest"]))


def _static_manifest(app: str, static_root: str) -> str:
    return f"""
version: 1
app: {app}
kind: static
static_root: {static_root}
routes:
  - domain: {app}.example.com
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
