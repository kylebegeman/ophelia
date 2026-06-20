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
from ophelia.portability import export_plan, import_plan, pack_validation_report
from ophelia.runtime import deploy_bundle


class PortabilityTests(unittest.TestCase):
    def test_pack_validation_warns_for_noncritical_inferred_addon_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "app.ophelia.yml"
            manifest_path.write_text(_addon_manifest(portability=None))
            manifest = load_manifest(manifest_path)

            report = pack_validation_report(manifest, manifest_path)

        self.assertTrue(report["ok"])
        warning_codes = {item["code"] for item in report["warnings"]}
        self.assertIn("postgres_data_contract_inferred", warning_codes)

    def test_pack_validation_blocks_critical_inferred_addon_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "app.ophelia.yml"
            manifest_path.write_text(_addon_manifest(portability="critical"))
            manifest = load_manifest(manifest_path)

            report = pack_validation_report(manifest, manifest_path)

        self.assertFalse(report["ok"])
        error_codes = {item["code"] for item in report["errors"]}
        self.assertIn("postgres_data_contract_inferred", error_codes)

    def test_export_plan_is_read_only_and_redacts_env_shape(self) -> None:
        with self._skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = root / "dragonwriter.ophelia.yml"
                manifest_path.write_text(_portable_manifest())
                manifest = load_manifest(manifest_path)
                deploy_bundle(manifest, manifest_path, runtime_root)
                (runtime_root / "apps" / "dragon-writer" / "env").write_text(
                    "OPHELIA_APP=dragon-writer\nDATABASE_URL=super-secret\n"
                )

                plan = export_plan(
                    "dragon-writer",
                    environment="production",
                    runtime_root=runtime_root,
                    manifest_path=manifest_path,
                    ophelia_root=root,
                )

        self.assertEqual("app.export.plan", plan["receipt_type"])
        self.assertTrue(plan["read_only"])
        self.assertTrue(plan["can_create"])
        self.assertEqual([], plan["blockers"])
        self.assertIn("postgres", plan["data_dependencies"])
        self.assertIn("export-plan.json", plan["artifact_paths"]["export_plan_receipt"])
        self.assertNotIn("super-secret", json.dumps(plan))

    def test_import_plan_reads_export_metadata_json(self) -> None:
        with self._skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                metadata_path = root / "export-plan.json"
                metadata_path.write_text(
                    json.dumps(
                        {
                            "receipt_type": "app.export.plan",
                            "app": "dragon-writer",
                            "environment": "production",
                            "domains": ["dragonwriter.begam.in"],
                            "routes": [{"domain": "dragonwriter.begam.in", "service": "web"}],
                            "data_dependencies": {
                                "postgres": {
                                    "mode": "shared-postgres-database",
                                    "import": {"command": "pg_restore"},
                                },
                                "volumes": [
                                    {
                                        "name": "uploads",
                                        "mount": "/app/uploads",
                                        "class": "critical",
                                    }
                                ],
                            },
                            "env_shape": [{"key": "DATABASE_URL", "secret_value_redacted": True}],
                            "verification_checks": [
                                {
                                    "name": "health",
                                    "url": "https://dragonwriter.begam.in/health",
                                    "expect_status": 200,
                                }
                            ],
                        }
                    )
                    + "\n"
                )

                plan = import_plan(metadata_path, runtime_root=root / "runtime", ophelia_root=root)

        self.assertEqual("app.import.plan", plan["receipt_type"])
        self.assertEqual("dragon-writer", plan["app"])
        self.assertEqual("production", plan["environment"])
        self.assertEqual([], plan["blockers"])
        self.assertFalse(plan["apply_supported"])
        self.assertIn("postgres.import", {item["name"] for item in plan["data_restore_commands"]})

    def test_pack_and_app_plan_commands_emit_json(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        env = {**os.environ, "OPHELIA_SKIP_DOCKER_STATUS": "1"}
        commands = [
            ["pack", "validate", "examples/dragonwriter.ophelia.yml", "--json"],
            ["pack", "explain", "examples/dragonwriter.ophelia.yml", "--json"],
            [
                "app",
                "export",
                "plan",
                "dragon-writer",
                "--environment",
                "production",
                "--manifest",
                "examples/dragonwriter.ophelia.yml",
                "--runtime-root",
                str(Path(tempfile.gettempdir()) / "ophelia-portability-empty-runtime"),
                "--json",
            ],
        ]

        for command in commands:
            result = subprocess.run(
                [str(repo / "cli" / "ship"), *command],
                text=True,
                capture_output=True,
                env=env,
                cwd=repo,
            )
            self.assertIn(result.returncode, {0, 1})
            json.loads(result.stdout)

    def _skip_docker_status(self):
        return _EnvPatch("OPHELIA_SKIP_DOCKER_STATUS", "1")


class _EnvPatch:
    def __init__(self, key: str, value: str):
        self.key = key
        self.value = value
        self.previous = None

    def __enter__(self):
        self.previous = os.environ.get(self.key)
        os.environ[self.key] = self.value

    def __exit__(self, exc_type, exc, tb):
        if self.previous is None:
            os.environ.pop(self.key, None)
        else:
            os.environ[self.key] = self.previous


def _addon_manifest(portability: str | None) -> str:
    pack = ""
    if portability is not None:
        pack = f"""
pack:
  portability: {portability}
"""
    return f"""
version: 1
app: addon-portable
environment: production
kind: service
image: ghcr.io/example/addon-portable:latest
{pack}
services:
  web:
    port: 3000
routes:
  - domain: addon-portable.example.com
    service: web
addons:
  postgres: true
  redis: false
verify:
  - name: health
    url: https://addon-portable.example.com/health
""".strip() + "\n"


def _portable_manifest() -> str:
    return """
version: 1
app: dragon-writer
environment: production
kind: service
image: ghcr.io/example/dragon-writer@sha256:aaaaaaaa
pack:
  portability: critical
  owner: personal
host_requirements:
  min_disk_free: 1b
services:
  web:
    port: 3000
routes:
  - domain: dragonwriter.begam.in
    service: web
data:
  postgres:
    mode: shared-postgres-database
    database: dragon_writer
    export:
      format: custom
      command: pg_dump
    import:
      command: pg_restore
    verify:
      command: ophelia/checks/data-verify.sh
  volumes:
    - name: uploads
      mount: /app/uploads
      class: critical
      export: tar-zstd
      import: tar-zstd
  backups:
    required: true
    restore_drill_required: true
    offsite_required: true
verify:
  - name: health
    url: https://dragonwriter.begam.in/health
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
