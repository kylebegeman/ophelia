from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.command_catalog import command_registry  # noqa: E402
from ophelia.live_readiness import LIVE_READINESS_KIND, live_readiness_report  # noqa: E402
from ophelia.manifest import load_manifest  # noqa: E402
from ophelia.runtime import deploy_bundle  # noqa: E402


class LiveReadinessTests(unittest.TestCase):
    def test_live_readiness_lane_is_read_only_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifests_dir = root / "manifests"
            manifests_dir.mkdir()
            manifest_path = manifests_dir / "live-app.ophelia.yml"
            manifest_path.write_text(_manifest())
            manifest = load_manifest(manifest_path)
            app_root = deploy_bundle(manifest, manifest_path, runtime_root)
            (app_root / "env").write_text("API_TOKEN=runtime-secret-value\nDATABASE_URL=postgres://u:secret@db/app\n")
            host_config = root / "hosts.yml"
            host_config.write_text(_host_config())
            provider_config = root / "integrations.yml"
            provider_config.write_text(_provider_config())
            github_obs = runtime_root / "github" / "secret-observations"
            github_obs.mkdir(parents=True)
            (github_obs / "live-app.staging.json").write_text(
                json.dumps({"environments": {"staging": {"secrets": [{"name": "API_TOKEN"}]}}})
            )
            before = _file_set(runtime_root)

            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("OPHELIA_SKIP_DOCKER_STATUS", None)
                report = live_readiness_report(
                    runtime_root=runtime_root,
                    manifests_dir=manifests_dir,
                    ophelia_root=root,
                    app="live-app",
                    environment="staging",
                    host_config=host_config,
                    provider_config=provider_config,
                    target_host="target-eu",
                )
                self.assertNotIn("OPHELIA_SKIP_DOCKER_STATUS", os.environ)

            after = _file_set(runtime_root)

        self.assertEqual(LIVE_READINESS_KIND, report["kind"])
        self.assertTrue(report["read_only"])
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["mutates_state"])
        self.assertFalse(report["confirmation_required"])
        self.assertFalse(report["probe_policy"]["http"]["enabled"])
        self.assertFalse(report["probe_policy"]["docker"]["enabled"])
        self.assertEqual([], report["mutation_guard"]["mutating_operations_called"])
        self.assertEqual(before, after)
        self.assertEqual(1, report["totals"]["app_count"])
        self.assertEqual("live-app", report["apps"][0]["app"])
        self.assertIn("placement", report["apps"][0]["checks"])
        self.assertEqual("ophelia.secret_provider_status", report["secret_provider_status"]["kind"])
        self.assertEqual("ophelia.secret_provider_report", report["apps"][0]["checks"]["secrets"]["kind"])
        self.assertIsInstance(report["apps"][0]["checks"]["secrets"]["key_count"], int)
        self.assertNotIn("runtime-secret-value", json.dumps(report))
        self.assertNotIn("postgres://u:secret", json.dumps(report))
        self.assertNotIn("super-secret-token", json.dumps(report))

    def test_live_readiness_command_is_in_catalog(self) -> None:
        operations = {descriptor.operation for descriptor in command_registry()}
        self.assertIn("live.readiness.run", operations)


def _file_set(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}


def _manifest() -> str:
    return """
version: 1
app: live-app
environment: staging
kind: service
image: ghcr.io/example/live-app:latest
env:
  API_TOKEN: replace-me
services:
  web:
    port: 3000
routes:
  - domain: live-app.example.com
    service: web
observability:
  health:
    url: https://live-app.example.com/health
    expect_status: 200
""".strip() + "\n"


def _host_config() -> str:
    return """
version: 1
hosts:
  - id: target-eu
    provider: target-host
    region: gra
    capabilities:
      docker: true
      docker_compose: true
      caddy: true
      edge: true
      network: true
      backups: true
      postgres: true
      redis: true
    capacity:
      memory: 8gb
      disk_free: 100gb
""".strip() + "\n"


def _provider_config() -> str:
    return """
version: 1
github:
  preferred_provider: gh
  fallback_provider: gh
secrets:
  providers:
    local_runtime_env:
      enabled: true
    github_environments:
      enabled: true
    sops:
      enabled: true
      search_roots:
        - ./secrets
metadata:
  api_token: super-secret-token
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
