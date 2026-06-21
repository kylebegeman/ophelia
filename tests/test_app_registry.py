from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.app_registry import HealthURL, _check_url, app_health, load_app_registry, registry_conflicts


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size: int = -1) -> bytes:
        return b"ok"


class AppRegistryTests(unittest.TestCase):
    def test_loads_registry_and_detects_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_path = Path(temp_dir) / "apps.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "apps": [
                            _app("apollo-staging", ["ops-staging.begam.in"], ["apollo-host-staging"]),
                            _app("duplicate", ["ops-staging.begam.in"], ["other"]),
                        ]
                    }
                )
            )

            entries = load_app_registry(registry_path)
            conflicts = registry_conflicts(entries)

            self.assertEqual(["apollo-staging", "duplicate"], [entry.name for entry in entries])
            self.assertEqual(["ops-staging.begam.in"], conflicts["domains"])

    def test_loads_direct_list_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_path = Path(temp_dir) / "apps.json"
            registry_path.write_text(json.dumps([_app("apollo-staging", [], [])]))

            entries = load_app_registry(registry_path)

            self.assertEqual(["apollo-staging"], [entry.name for entry in entries])

    def test_invalid_registry_json_has_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_path = Path(temp_dir) / "apps.json"
            registry_path.write_text("{")

            with self.assertRaisesRegex(ValueError, "App registry JSON is invalid"):
                load_app_registry(registry_path)

    def test_health_without_urls_and_docker_is_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_path = Path(temp_dir) / "apps.json"
            registry_path.write_text(json.dumps({"apps": [_app("apollo-staging", [], [])]}))

            entry = load_app_registry(registry_path)[0]
            report = app_health(entry, skip_docker=True)

            self.assertTrue(report["ok"])
            self.assertEqual([], report["containers"])
            self.assertEqual([], report["health_urls"])

    def test_cli_apps_and_app_health_emit_json(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_path = Path(temp_dir) / "apps.json"
            registry_path.write_text(json.dumps({"apps": [_app("apollo-staging", [], [])]}))
            env = {**os.environ, "OPHELIA_SKIP_DOCKER_STATUS": "1"}

            for command in [
                ["apps", "--registry", str(registry_path), "--json"],
                ["app", "health", "apollo-staging", "--registry", str(registry_path), "--skip-docker", "--json"],
            ]:
                result = subprocess.run(
                    [str(repo / "cli" / "ophelia"), *command],
                    text=True,
                    capture_output=True,
                    check=True,
                    env=env,
                    cwd=repo,
                )
                json.loads(result.stdout)

    def test_registry_rejects_invalid_health_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_path = Path(temp_dir) / "apps.json"
            app = _app("apollo-staging", [], [])
            app["health_urls"] = [{"name": "bad", "url": "ftp://example.com/health"}]
            registry_path.write_text(json.dumps({"apps": [app]}))

            with self.assertRaises(ValueError):
                load_app_registry(registry_path)

    def test_health_result_redacts_secret_shaped_url(self) -> None:
        url = "https://user:secret@example.com/health?token=abc#frag"
        with mock.patch("ophelia.app_registry.urllib.request.urlopen", return_value=_FakeResponse()):
            result = _check_url(HealthURL(name=url, url=url), timeout=1)

        self.assertEqual("https://<redacted>@example.com/health?<redacted>#<redacted>", result["url"])
        self.assertEqual(result["url"], result["name"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("token=abc", json.dumps(result))

    def test_health_url_validation_redacts_secret_shaped_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_path = Path(temp_dir) / "apps.json"
            app = _app("apollo-staging", [], [])
            app["health_urls"] = [
                {
                    "name": "bad",
                    "url": "https://user:secret@example.com/health?token=abc",
                    "expect_status": "nope",
                }
            ]
            registry_path.write_text(json.dumps({"apps": [app]}))

            with self.assertRaises(ValueError) as context:
                load_app_registry(registry_path)

            message = str(context.exception)
            self.assertIn("https://<redacted>@example.com/health?<redacted>", message)
            self.assertNotIn("secret", message)
            self.assertNotIn("token=abc", message)

    def test_registry_rejects_malformed_entries_with_clear_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_path = Path(temp_dir) / "apps.json"
            app = _app("apollo-staging", [], [])
            app.pop("compose_project")
            registry_path.write_text(json.dumps({"apps": [app]}))

            with self.assertRaisesRegex(ValueError, "compose_project"):
                load_app_registry(registry_path)

            app = _app("apollo-staging", [], [])
            app["domains"] = "ops-staging.begam.in"
            registry_path.write_text(json.dumps({"apps": [app]}))

            with self.assertRaisesRegex(ValueError, "domains"):
                load_app_registry(registry_path)

    def test_app_cli_reports_unknown_app_without_traceback(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_path = Path(temp_dir) / "apps.json"
            registry_path.write_text(json.dumps({"apps": [_app("apollo-staging", [], [])]}))

            result = subprocess.run(
                [
                    str(repo / "cli" / "ophelia"),
                    "app",
                    "health",
                    "missing",
                    "--registry",
                    str(registry_path),
                    "--json",
                ],
                text=True,
                capture_output=True,
                cwd=repo,
            )

            self.assertNotEqual(0, result.returncode)
            payload = json.loads(result.stdout)
            self.assertIn("Unknown app", payload["error"])
            self.assertEqual("", result.stderr)


def _app(name: str, domains: list[str], containers: list[str]) -> dict:
    return {
        "name": name,
        "environment": "staging",
        "compose_project": name,
        "root_path": f"/opt/{name}",
        "domains": domains,
        "caddy_site_file": f"/home/kyle/ophelia-runtime/caddy/sites.d/{name}.caddy",
        "container_names": containers,
        "health_urls": [],
        "public_docker_network": "ophelia-edge",
    }


if __name__ == "__main__":
    unittest.main()
