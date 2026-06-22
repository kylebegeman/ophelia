from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia import app_factory as af
from ophelia.github_providers import github_provider_status


class GitHubProviderContractTests(unittest.TestCase):
    def test_github_app_status_uses_env_presence_without_leaking_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "integrations.yml"
            config.write_text(
                """
version: 1
github:
  preferred_provider: github_app
  fallback_provider: gh
  app:
    enabled: true
    installation_id: "12345"
    app_id_env: OPHELIA_TEST_APP_ID
    private_key_env: OPHELIA_TEST_PRIVATE_KEY
    permissions:
      contents: write
  gh:
    enabled: true
  repositories:
    - app: demo-app
      repo: example/demo-app
      provider: github_app
""".strip()
                + "\n"
            )
            old_app_id = os.environ.get("OPHELIA_TEST_APP_ID")
            old_private = os.environ.get("OPHELIA_TEST_PRIVATE_KEY")
            os.environ["OPHELIA_TEST_APP_ID"] = "123"
            os.environ["OPHELIA_TEST_PRIVATE_KEY"] = "-----BEGIN PRIVATE KEY-----\nFAKESECRET\n-----END PRIVATE KEY-----"
            try:
                report = github_provider_status(runtime_root=root / "runtime", config_path=config)
            finally:
                _restore_env("OPHELIA_TEST_APP_ID", old_app_id)
                _restore_env("OPHELIA_TEST_PRIVATE_KEY", old_private)

        self.assertEqual("github_app", report["selected_provider"])
        app_provider = next(provider for provider in report["providers"] if provider["name"] == "github_app")
        self.assertTrue(app_provider["usable"])
        encoded = json.dumps(report)
        self.assertNotIn("FAKESECRET", encoded)
        self.assertNotIn("BEGIN PRIVATE KEY", encoded)

    def test_github_app_plan_and_apply_share_token_gated_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "integrations.yml"
            config.write_text(
                """
version: 1
github:
  preferred_provider: github_app
  fallback_provider: gh
  app:
    enabled: true
    installation_id: "12345"
    app_id_env: OPHELIA_TEST_APP_ID
    private_key_env: OPHELIA_TEST_PRIVATE_KEY
""".strip()
                + "\n"
            )
            old_app_id = os.environ.get("OPHELIA_TEST_APP_ID")
            old_private = os.environ.get("OPHELIA_TEST_PRIVATE_KEY")
            os.environ["OPHELIA_TEST_APP_ID"] = "123"
            os.environ["OPHELIA_TEST_PRIVATE_KEY"] = "fake-private-key-value"
            calls: list[dict] = []

            def _runner(command: dict, timeout: float) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                return subprocess.CompletedProcess([command["id"]], 0, stdout="", stderr="")

            try:
                plan = af.github_provision_plan(
                    "demo-app",
                    "static-site",
                    owner="example",
                    repo="example/demo-app",
                    phase="repo",
                    runtime_root=root / "runtime",
                    github_provider="github-app",
                    provider_config=config,
                )
                receipt = af.github_provision_apply(
                    "demo-app",
                    "static-site",
                    owner="example",
                    repo="example/demo-app",
                    phase="repo",
                    runtime_root=root / "runtime",
                    github_provider="github-app",
                    provider_config=config,
                    confirm=plan["confirmation_token"],
                    command_runner=_runner,
                )
            finally:
                _restore_env("OPHELIA_TEST_APP_ID", old_app_id)
                _restore_env("OPHELIA_TEST_PRIVATE_KEY", old_private)

        self.assertEqual("github_app", plan["github_provider"])
        self.assertEqual("github_app", plan["commands"][0]["provider"])
        self.assertIn("api", plan["commands"][0])
        self.assertEqual("succeeded", receipt["status"])
        self.assertEqual(["create_repo"], [call["id"] for call in calls])


def _restore_env(key: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
