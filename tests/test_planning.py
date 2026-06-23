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
from ophelia.runtime import apply_local_bundle, deploy_bundle


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
            self.assertEqual("deploy.plan", plan["digest"]["operation"])
            self.assertEqual("ready", plan["digest"]["status"])
            self.assertEqual("medium", plan["digest"]["risk"])
            self.assertIn("SECRET_TOKEN", {item["key"] for item in plan["env_requirements"]})
            self.assertNotIn("super-secret-value", json.dumps(plan))

    def test_deploy_plan_reports_required_env_without_compose_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(
                """
version: 1
app: required-env-plan
kind: service
image: ghcr.io/example/required-env-plan:latest
required_env:
  - REQUIRED_API_TOKEN
env:
  NODE_ENV: production
services:
  web:
    port: 3000
routes:
  - domain: required-env-plan.example.com
    service: web
""".strip()
                + "\n"
            )
            manifest = load_manifest(manifest_path)

            plan = deploy_plan(manifest, manifest_path, runtime_root)
            bundle = plan["env_requirements"]

            required = {item["key"]: item for item in bundle}
            self.assertTrue(required["REQUIRED_API_TOKEN"]["required_for_apply"])
            self.assertTrue(required["REQUIRED_API_TOKEN"]["placeholder"])
            self.assertFalse(required["NODE_ENV"]["required_for_apply"])
            compose_diff = json.dumps(plan)
            self.assertNotIn("REQUIRED_API_TOKEN:", compose_diff)

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

    def test_deploy_removes_stale_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest("replace-me"))
            deploy_bundle(load_manifest(manifest_path), manifest_path, runtime_root)

            manifest_path.write_text(
                """
version: 1
app: plan-test
kind: static
static_root: /tmp/plan-test
routes:
  - domain: plan-test.example.com
verify:
  - name: health
    url: https://plan-test.example.com/health
""".strip()
                + "\n"
            )
            manifest = load_manifest(manifest_path)
            app_root = deploy_bundle(manifest, manifest_path, runtime_root)

            self.assertFalse((app_root / "compose.yml").exists())
            self.assertTrue(bundle_diff(manifest, runtime_root)["clean"])

    def test_deploy_from_manifest_lock_uses_staged_support_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            env_dir = root / "env"
            env_dir.mkdir()
            (env_dir / "shared.env").write_text("FEATURE_FLAG=true\n")
            checks_dir = root / "ophelia" / "checks"
            hooks_dir = root / "ophelia" / "hooks"
            checks_dir.mkdir(parents=True)
            hooks_dir.mkdir(parents=True)
            (checks_dir / "data-verify.sh").write_text("#!/usr/bin/env sh\nexit 0\n")
            (hooks_dir / "pre-export.sh").write_text("#!/usr/bin/env sh\nexit 0\n")

            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(
                """
version: 1
app: support-test
kind: service
image: ghcr.io/example/support-test:latest
env_files:
  - env/shared.env
services:
  web:
    port: 3000
routes:
  - domain: support-test.example.com
    service: web
data:
  volumes:
    - name: runtime-data
      mount: /app/data
      export: tar-zstd
      import: tar-zstd
      verify:
        command: ophelia/checks/data-verify.sh
hooks:
  pre_export: ophelia/hooks/pre-export.sh
""".strip()
                + "\n"
            )
            app_root = deploy_bundle(load_manifest(manifest_path), manifest_path, runtime_root)
            staged_env = app_root / "env.d" / "01-shared.env"
            staged_check = app_root / "ophelia" / "checks" / "data-verify.sh"
            staged_hook = app_root / "ophelia" / "hooks" / "pre-export.sh"
            self.assertEqual("FEATURE_FLAG=true\n", staged_env.read_text())
            self.assertEqual("#!/usr/bin/env sh\nexit 0\n", staged_check.read_text())
            self.assertEqual("#!/usr/bin/env sh\nexit 0\n", staged_hook.read_text())

            lock_path = app_root / "manifest.lock.json"
            deploy_bundle(load_manifest(lock_path), lock_path, runtime_root)

            self.assertEqual("FEATURE_FLAG=true\n", staged_env.read_text())
            self.assertEqual("#!/usr/bin/env sh\nexit 0\n", staged_check.read_text())
            self.assertEqual("#!/usr/bin/env sh\nexit 0\n", staged_hook.read_text())

    def test_support_file_cleanup_preserves_active_until_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            env_dir = root / "env"
            env_dir.mkdir()
            (env_dir / "old.env").write_text("OLD=true\n")
            (env_dir / "new.env").write_text("NEW=true\n")
            manifest_path = root / "app.ophelia.yml"

            manifest_path.write_text(_static_manifest_with_env("support-cleanup", "env/old.env"))
            app_root = apply_local_bundle(load_manifest(manifest_path), manifest_path, runtime_root, root)
            old_support = app_root / "env.d" / "01-old.env"
            self.assertTrue(old_support.exists())
            active = json.loads((app_root / "active_release.json").read_text())
            self.assertTrue((Path(active["bundle_path"]) / "env.d" / "01-old.env").exists())

            manifest_path.write_text(_static_manifest_with_env("support-cleanup", "env/new.env"))
            deploy_bundle(load_manifest(manifest_path), manifest_path, runtime_root)
            self.assertTrue(old_support.exists())
            self.assertTrue((app_root / "env.d" / "01-new.env").exists())

            apply_local_bundle(load_manifest(manifest_path), manifest_path, runtime_root, root)
            self.assertFalse(old_support.exists())
            self.assertTrue((app_root / "env.d" / "01-new.env").exists())

    def test_diff_json_cli_is_parseable(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                str(repo / "cli" / "ship"),
                "diff",
                str(repo / "examples" / "service-app.ophelia.yml"),
                "--runtime-root",
                str(Path(tempfile.gettempdir()) / "ophelia-plan-test-runtime"),
                "--json",
            ],
            text=True,
            capture_output=True,
            check=True,
        )

        payload = json.loads(result.stdout)
        self.assertEqual("demo-service", payload["app"])
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


def _static_manifest_with_env(app: str, env_file: str) -> str:
    return f"""
version: 1
app: {app}
kind: static
static_root: /tmp/{app}
env_files:
  - {env_file}
routes:
  - domain: {app}.example.com
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
