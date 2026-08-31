from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.portability import app_runbook_report


class AppRunbookTests(unittest.TestCase):
    def test_runbook_uses_one_redacted_model_for_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "demo-service.ophelia.yml"
            manifest_path.write_text(_manifest())
            before = sorted(path.relative_to(root) for path in root.rglob("*"))

            with patch(
                "ophelia.portability.run_app_owned_verifications",
                return_value={"ok": True, "status": "ok", "count": 1, "checks": []},
            ):
                report = app_runbook_report(
                    "demo-service",
                    "production",
                    runtime_root,
                    manifest_path,
                )

            after = sorted(path.relative_to(root) for path in root.rglob("*"))

        self.assertEqual(before, after, "generating a runbook must remain read-only")
        model = report["runbook"]
        markdown = report["markdown"]

        self.assertEqual("demo-service", model["app"])
        self.assertEqual("production", model["environment"])
        self.assertEqual(str(manifest_path), model["sources"]["manifest_path"])
        self.assertEqual(str(runtime_root / "apps" / "demo-service"), model["sources"]["runtime_path"])
        self.assertEqual(["api", "worker"], [item["name"] for item in model["services"]])
        self.assertEqual(
            ["a.demo-service.example.net", "z.demo-service.example.net"],
            [item["domain"] for item in model["routes"]],
        )
        self.assertEqual(
            ["postgres:shared-postgres-database", "volume:alpha", "volume:zeta"],
            [item["id"] for item in model["data_dependencies"]],
        )
        self.assertIn("deploy_plan", model["operations"])
        self.assertIn("restore_drill_plan", model["operations"])
        self.assertIn("receipts", model["operations"])
        self.assertEqual(model["generated_at"], report["generated_at"])

        for section in (
            "## Sources",
            "## Routes",
            "## Services",
            "## Data Dependencies",
            "## Backup And Restore",
            "## Release",
            "## Operations",
            "## Risks And Next Actions",
            "## Rollback And Recovery",
        ):
            self.assertIn(section, markdown)
        self.assertIn(model["generated_at"], markdown)
        self.assertIn(model["operations"]["deploy_plan"], markdown)
        self.assertIn("a.demo-service.example.net", markdown)
        self.assertIn("shared-postgres-database", markdown)

        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("never-print-this-token", serialized)
        self.assertNotIn("postgres://operator:never-print-this-password", serialized)
        self.assertNotIn("secret-command-argument", serialized)
        self.assertTrue(model["values_redacted"])

    def test_unresolved_manifest_still_returns_a_stable_runbook(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            missing_manifest = root / "missing.ophelia.yml"

            report = app_runbook_report(
                "missing-app",
                "staging",
                runtime_root,
                missing_manifest,
            )

            self.assertFalse(runtime_root.exists())

        model = report["runbook"]
        self.assertEqual("missing-app", model["app"])
        self.assertEqual([], model["routes"])
        self.assertEqual([], model["services"])
        self.assertEqual([], model["data_dependencies"])
        self.assertIsNone(model["sources"]["manifest_path"])
        self.assertTrue(report["blockers"])
        self.assertIn("Manifest unresolved", report["markdown"])


def _manifest() -> str:
    return """
version: 1
app: demo-service
environment: production
kind: service
image: ghcr.io/example/demo-service@sha256:aaaaaaaa
pack:
  portability: critical
  owner: personal
env:
  API_TOKEN: never-print-this-token
required_env:
  - DATABASE_URL
services:
  worker:
    port: 4000
    env:
      DATABASE_URL: postgres://operator:never-print-this-password@example.invalid/app
  api:
    port: 3000
routes:
  - domain: z.demo-service.example.net
    service: api
  - domain: a.demo-service.example.net
    service: api
    path_prefix: /api
data:
  postgres:
    mode: shared-postgres-database
    database: demo_service
    export:
      format: custom
      command: [pg_dump, secret-command-argument]
    import:
      command: [pg_restore, secret-command-argument]
  volumes:
    - name: zeta
      mount: /app/zeta
      source: zeta
      class: critical
      export: tar-zstd
      import: tar-zstd
    - name: alpha
      mount: /app/alpha
      source: alpha
      class: standard
      export: tar-zstd
      import: tar-zstd
  backups:
    required: true
    restore_drill_required: true
    offsite_required: true
verify:
  - name: health
    url: https://a.demo-service.example.net/health
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
