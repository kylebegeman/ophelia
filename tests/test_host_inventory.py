from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.host_inventory import (  # noqa: E402
    APP_PLACEMENT_PLAN_KIND,
    HOST_INVENTORY_KIND,
    HOST_READINESS_KIND,
    app_placement_plan,
    collect_host_inventory,
    host_readiness,
)


class HostInventoryTests(unittest.TestCase):
    def test_inventory_merges_configured_hosts_without_writing_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            config_path = root / "hosts.yml"
            config_path.write_text(_host_config())

            with patch.dict("os.environ", {"OPHELIA_SKIP_DOCKER_STATUS": "1"}):
                report = collect_host_inventory(
                    runtime_root,
                    root,
                    root / "manifests",
                    config_path,
                )

            self.assertEqual(HOST_INVENTORY_KIND, report["kind"])
            self.assertTrue(report["read_only"])
            self.assertEqual(str(config_path), report["config_path"])
            host_ids = {host["id"] for host in report["hosts"]}
            self.assertIn("local", host_ids)
            self.assertIn("ovh-gra", host_ids)
            ovh = next(host for host in report["hosts"] if host["id"] == "ovh-gra")
            self.assertEqual("ovh", ovh["provider"])
            self.assertEqual(102400, ovh["capacity"]["disk_free_mb"])
            self.assertTrue(ovh["capabilities"]["postgres"])
            self.assertNotIn("super-secret", json.dumps(report))
            self.assertFalse(runtime_root.exists(), "inventory collection must not create runtime state")

    def test_host_readiness_checks_selected_configured_host(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "hosts.yml"
            config_path.write_text(_host_config())

            with patch.dict("os.environ", {"OPHELIA_SKIP_DOCKER_STATUS": "1"}):
                report = host_readiness(
                    "ovh-gra",
                    root / "runtime",
                    root,
                    root / "manifests",
                    config_path,
                )

        self.assertEqual(HOST_READINESS_KIND, report["kind"])
        self.assertEqual("ok", report["status"])
        self.assertEqual("ovh-gra", report["hosts"][0]["host_id"])
        self.assertEqual([], report["blockers"])

    def test_app_placement_recommends_host_with_required_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "dragon.ophelia.yml"
            manifest_path.write_text(_postgres_manifest())
            config_path = root / "hosts.yml"
            config_path.write_text(_host_config())

            with patch.dict("os.environ", {"OPHELIA_SKIP_DOCKER_STATUS": "1"}):
                plan = app_placement_plan(
                    "demo-service",
                    environment="production",
                    runtime_root=root / "runtime",
                    manifest_path=manifest_path,
                    ophelia_root=root,
                    config_path=config_path,
                    source_host="local",
                    target_host="ovh-gra",
                )

        self.assertEqual(APP_PLACEMENT_PLAN_KIND, plan["kind"])
        self.assertTrue(plan["read_only"])
        self.assertEqual("ready", plan["status"])
        self.assertEqual("ovh-gra", plan["recommended_host"])
        recommended = next(item for item in plan["placements"] if item["host_id"] == "ovh-gra")
        self.assertEqual("recommended", recommended["recommendation"])
        self.assertEqual([], recommended["blockers"])
        self.assertTrue(plan["requirements"]["required_capabilities"]["postgres"])
        self.assertNotIn("postgres://user:secret", json.dumps(plan))

    def test_app_placement_blocks_when_required_database_capability_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "dragon.ophelia.yml"
            manifest_path.write_text(_postgres_manifest())
            config_path = root / "hosts.yml"
            config_path.write_text(_host_config(postgres=False))

            with patch.dict("os.environ", {"OPHELIA_SKIP_DOCKER_STATUS": "1"}):
                plan = app_placement_plan(
                    "demo-service",
                    environment="production",
                    runtime_root=root / "runtime",
                    manifest_path=manifest_path,
                    ophelia_root=root,
                    config_path=config_path,
                    target_host="ovh-gra",
                )

        self.assertEqual("blocked", plan["status"])
        codes = {blocker["code"] for placement in plan["placements"] for blocker in placement["blockers"]}
        self.assertIn("host_postgres_unavailable", codes)
        self.assertIn("no_eligible_host", {blocker["code"] for blocker in plan["blockers"]})


def _host_config(postgres: bool = True) -> str:
    postgres_value = "true" if postgres else "false"
    return f"""
version: 1
hosts:
  - id: ovh-gra
    name: OVH GRA runtime
    provider: ovh
    region: gra
    arch: amd64
    roles:
      - runtime
    labels:
      - production
    capacity:
      memory: 8gb
      disk_free: 100gb
    capabilities:
      docker: true
      docker_compose: true
      caddy: true
      edge: true
      network: true
      backups: true
      postgres: {postgres_value}
      redis: true
    metadata:
      api_token: super-secret
""".strip() + "\n"


def _postgres_manifest() -> str:
    return """
version: 1
app: demo-service
kind: service
environment: production
image: ghcr.io/example/demo-service:latest
resources:
  memory: 512m
services:
  web:
    port: 3000
routes:
  - domain: dragon.example.com
    service: web
addons:
  postgres: true
env:
  DATABASE_URL: postgres://user:secret@example.com/db
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
