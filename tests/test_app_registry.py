from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.app_registry import app_health, load_app_registry, registry_conflicts


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
