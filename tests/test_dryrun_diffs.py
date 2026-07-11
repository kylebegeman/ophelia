from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.execution.staging import StagingError
from ophelia.manifest import load_manifest
from ophelia.planning import deploy_plan
from ophelia.portability import traffic_plan
from ophelia.runtime import deploy_bundle


def _service_manifest(secret_value: str) -> str:
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


def _static_manifest(static_root: Path) -> str:
    return f"""
version: 1
app: static-portable
environment: staging
kind: static
static_root: {static_root}
routes:
  - domain: static-portable.example.com
pack:
  portability: static
  owner: personal
""".strip() + "\n"


class DeployDiffArtifactTests(unittest.TestCase):
    def test_compose_diff_artifact_is_present_and_secret_free(self) -> None:
        secret = "super-secret-value"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_service_manifest(secret))
            manifest = load_manifest(manifest_path)

            plan = deploy_plan(manifest, manifest_path, runtime_root)

            # compose changed (no prior runtime), so an artifact must be present.
            self.assertTrue(plan["compose_changes"])
            artifacts = plan["artifacts"]
            self.assertTrue(artifacts)
            compose_artifact = next(a for a in artifacts if a["name"] == "compose-diff")
            self.assertEqual("ophelia.artifact.diff", compose_artifact["kind"])
            self.assertTrue(compose_artifact["redacted"])

            artifact_path = Path(compose_artifact["path"])
            self.assertTrue(artifact_path.exists())
            file_text = artifact_path.read_text()
            self.assertNotIn(secret, file_text)
            # The redaction marker proves the env line was diffed but masked.
            self.assertIn("SECRET_TOKEN", file_text)
            self.assertIn("<redacted>", file_text)

            # The plan JSON carries only the path, never inline diff content.
            self.assertNotIn(secret, json.dumps(plan))

    def test_changes_list_describes_targets_without_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_service_manifest("super-secret-value"))
            manifest = load_manifest(manifest_path)

            plan = deploy_plan(manifest, manifest_path, runtime_root)
            targets = {change["target"] for change in plan["changes"]}
            self.assertIn("compose", targets)
            self.assertTrue(all("change" in change and "path" in change for change in plan["changes"]))
            self.assertNotIn("super-secret-value", json.dumps(plan["changes"]))

    def test_artifacts_dir_override_cannot_escape_operation_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            artifacts_dir = root / "custom-artifacts"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_service_manifest("super-secret-value"))
            manifest = load_manifest(manifest_path)

            with self.assertRaisesRegex(StagingError, "restricted to the operation staging tree"):
                deploy_plan(manifest, manifest_path, runtime_root, artifacts_dir=artifacts_dir)
            self.assertFalse(artifacts_dir.exists())

    def test_no_compose_diff_when_compose_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_service_manifest("replace-me"))
            manifest = load_manifest(manifest_path)
            deploy_bundle(manifest, manifest_path, runtime_root)

            plan = deploy_plan(manifest, manifest_path, runtime_root)
            self.assertEqual([], plan["compose_changes"])
            self.assertEqual([], plan["artifacts"])


class TrafficProviderChangeTests(unittest.TestCase):
    def test_traffic_plan_emits_redacted_provider_changes(self) -> None:
        token = "super-secret-cf-token"
        previous = os.environ.get("OPHELIA_TEST_CF_TOKEN")
        os.environ["OPHELIA_SKIP_DOCKER_STATUS"] = "1"
        os.environ["OPHELIA_TEST_CF_TOKEN"] = token
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                static_root = root / "site"
                static_root.mkdir()
                (static_root / "index.html").write_text("<h1>Static</h1>\n")
                manifest_path = root / "static.ophelia.yml"
                manifest_path.write_text(_static_manifest(static_root))
                manifest = load_manifest(manifest_path)
                app_root = deploy_bundle(manifest, manifest_path, runtime_root)
                (app_root / "active_release.json").write_text((app_root / "release.json").read_text())

                provider_config = root / "providers.json"
                provider_config.write_text(
                    json.dumps(
                        {
                            "dns": {
                                "provider": "cloudflare",
                                "zone_id": "zone-fixture",
                                "api_token_env": "OPHELIA_TEST_CF_TOKEN",
                                "allow_mutation": True,
                            }
                        }
                    )
                )

                plan = traffic_plan(
                    "static-portable",
                    "source-host",
                    "target-host",
                    "target.example.net",
                    "staging",
                    runtime_root,
                    manifest_path,
                    dns_provider="cloudflare",
                    ttl=300,
                    provider_config=provider_config,
                    execute_provider_mutation=True,
                )
        finally:
            os.environ.pop("OPHELIA_SKIP_DOCKER_STATUS", None)
            if previous is None:
                os.environ.pop("OPHELIA_TEST_CF_TOKEN", None)
            else:
                os.environ["OPHELIA_TEST_CF_TOKEN"] = previous

        changes = plan["changes"]
        kinds = {change["kind"] for change in changes}
        self.assertIn("dns_record", kinds)
        self.assertIn("caddy_site", kinds)
        self.assertIn("provider_execution", kinds)

        dns_change = next(c for c in changes if c["kind"] == "dns_record")
        self.assertEqual("source-host", dns_change["before"]["owner"])
        self.assertEqual("target-host", dns_change["after"]["owner"])
        self.assertEqual("target.example.net", dns_change["after"]["value"])

        provider_change = next(c for c in changes if c["kind"] == "provider_execution")
        # The env-ref name is acceptable to surface; the token VALUE must never appear.
        self.assertNotEqual(token, provider_change["dns"].get("api_token_env"))

        # No fixture token value anywhere in the serialized plan.
        self.assertNotIn(token, json.dumps(plan))


if __name__ == "__main__":
    unittest.main()
