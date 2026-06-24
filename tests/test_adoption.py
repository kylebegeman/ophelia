from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.adoption import adoption_plan
from ophelia.command_catalog import command_registry
from ophelia.main import main


class AdoptionPlanTests(unittest.TestCase):
    def test_missing_manifest_blocks_but_stays_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "demo-app"
            repo.mkdir()
            before = _snapshot(repo)

            plan = adoption_plan(
                "demo-app",
                "staging",
                repo_path=repo,
                runtime_root=Path(temp_dir) / "runtime",
            )

            after = _snapshot(repo)

        self.assertEqual(before, after)
        self.assertEqual("ophelia.plan", plan["kind"])
        self.assertEqual("app.adoption.plan", plan["operation"])
        self.assertTrue(plan["read_only"])
        self.assertFalse(plan["mutates_state"])
        self.assertFalse(plan["live_values_collected"])
        self.assertIn("adoption_manifest_missing", {item["code"] for item in plan["blockers"]})
        command_ids = [item["id"] for item in plan["next_commands"]]
        self.assertEqual("preview-service-bootstrap", command_ids[0])
        self.assertEqual("preview-static-bootstrap", command_ids[1])
        self.assertIn("--include-manifest", plan["next_commands"][0]["argv"])
        self.assertEqual("ship", plan["next_commands"][0]["argv"][0])

    def test_valid_repo_reports_contract_and_missing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "demo-app"
            repo.mkdir()
            manifest = repo / ".ophelia.yml"
            manifest.write_text(_manifest("demo-app", "staging"))
            (repo / "ophelia").mkdir()
            (repo / "ophelia" / "runbook.md").write_text("# Demo Runbook\n")

            plan = adoption_plan(
                "demo-app",
                "staging",
                repo_path=repo,
                runtime_root=Path(temp_dir) / "runtime",
            )

        self.assertEqual([], plan["blockers"])
        self.assertEqual(str(manifest), plan["manifest_path"])
        self.assertTrue(plan["pack_validation"]["ok"])
        warning_codes = {item["code"] for item in plan["warnings"]}
        self.assertIn("adoption_artifacts_missing", warning_codes)
        gate_status = {item["id"]: item["status"] for item in plan["adoption_gates"]}
        self.assertEqual("passed", gate_status["manifest_contract"])
        self.assertEqual("passed", gate_status["pack_validation"])
        self.assertEqual("warning", gate_status["repo_artifacts"])
        self.assertEqual(["ship", "pack", "validate", str(manifest), "--json"], plan["next_commands"][1]["argv"])

    def test_present_non_executable_hook_is_adoption_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "demo-app"
            repo.mkdir()
            (repo / ".ophelia.yml").write_text(_manifest("demo-app", "staging"))
            _write_all_adoption_artifacts(repo, executable=False)

            plan = adoption_plan(
                "demo-app",
                "staging",
                repo_path=repo,
                runtime_root=Path(temp_dir) / "runtime",
            )

        warning_codes = {item["code"] for item in plan["warnings"]}
        self.assertIn("adoption_artifacts_not_executable", warning_codes)
        self.assertNotIn("adoption_artifacts_missing", warning_codes)
        gate_status = {item["id"]: item["status"] for item in plan["adoption_gates"]}
        self.assertEqual("warning", gate_status["repo_artifacts"])

    def test_embedded_pack_validation_redacts_command_secret_literals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "demo-app"
            repo.mkdir()
            (repo / ".ophelia.yml").write_text(_manifest_with_secret_command("demo-app", "staging"))

            plan = adoption_plan(
                "demo-app",
                "staging",
                repo_path=repo,
                runtime_root=Path(temp_dir) / "runtime",
            )

        blob = json.dumps(plan)
        self.assertNotIn("super-secret", blob)
        self.assertNotIn("postgres://user:password", blob)
        self.assertIn("<redacted>", blob)

    def test_app_owned_contract_recognizes_standard_endpoints_and_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "demo-app"
            repo.mkdir()
            (repo / ".ophelia.yml").write_text(_manifest_with_app_owned_checks("demo-app", "staging"))
            (repo / "package.json").write_text(
                json.dumps(
                    {
                        "scripts": {
                            "ophelia:health": "node scripts/ophelia-health.js",
                            "ophelia:data:verify": "node scripts/ophelia-data-verify.js",
                            "ophelia:release": "node scripts/ophelia-release.js",
                        }
                    }
                )
            )

            plan = adoption_plan(
                "demo-app",
                "staging",
                repo_path=repo,
                runtime_root=Path(temp_dir) / "runtime",
            )

        contract = plan["app_owned_contract"]
        self.assertEqual([], contract["missing"])
        self.assertTrue(contract["standard_endpoints"]["/ophelia/health"])
        self.assertTrue(contract["standard_endpoints"]["/ophelia/release"])
        check = next(item for item in plan["checks"] if item["name"] == "app_owned_runtime_contract")
        self.assertTrue(check["ok"])

    def test_app_owned_contract_recognizes_manifest_data_verifier_without_npm_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "demo-app"
            repo.mkdir()
            (repo / ".ophelia.yml").write_text(
                _manifest_with_compiled_data_verify_check("demo-app", "staging")
            )
            (repo / "package.json").write_text(
                json.dumps(
                    {
                        "scripts": {
                            "ophelia:health": "tsx scripts/ophelia-health.ts",
                            "ophelia:data:verify": "tsx scripts/ophelia-data-verify.ts",
                            "ophelia:release": "tsx scripts/ophelia-release.ts",
                        }
                    }
                )
            )

            plan = adoption_plan(
                "demo-app",
                "staging",
                repo_path=repo,
                runtime_root=Path(temp_dir) / "runtime",
            )

        contract = plan["app_owned_contract"]
        self.assertEqual([], contract["missing"])
        self.assertTrue(contract["manifest_data_verifier"])
        self.assertTrue(contract["runtime_checks"]["data_verify"])

    def test_cli_json_and_catalog_descriptor_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "demo-app"
            repo.mkdir()
            (repo / ".ophelia.yml").write_text(_manifest("demo-app", "staging"))
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(
                    [
                        "app",
                        "adoption",
                        "plan",
                        "demo-app",
                        "--repo-path",
                        str(repo),
                        "--environment",
                        "staging",
                        "--json",
                    ]
                )

        self.assertEqual(0, exit_code)
        payload = json.loads(buffer.getvalue())
        self.assertEqual("app.adoption.plan", payload["operation"])
        self.assertEqual("ophelia.plan", payload["kind"])
        descriptors = {descriptor.operation: descriptor for descriptor in command_registry()}
        self.assertIn("app.adoption.plan", descriptors)
        self.assertFalse(descriptors["app.adoption.plan"].mutates_state)


def _snapshot(root: Path) -> set[Path]:
    return {path for path in root.rglob("*")}


def _write_all_adoption_artifacts(repo: Path, *, executable: bool) -> None:
    for relative in [
        "ophelia/runbook.md",
        "ophelia/agent.md",
        "ophelia/checks/data-verify.sh",
        "ophelia/hooks/pre-export.sh",
        "ophelia/hooks/freeze.sh",
        "ophelia/hooks/unfreeze.sh",
        "ophelia/hooks/post-import.sh",
    ]:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n")
        if executable and path.suffix == ".sh":
            path.chmod(0o755)


def _manifest(app: str, environment: str) -> str:
    return f"""\
version: 1
app: {app}
environment: {environment}
kind: service

services:
  web:
    image: ghcr.io/ophelia-fixtures/{app}@sha256:1111111111111111111111111111111111111111111111111111111111111111
    port: 8080

routes:
  - domain: {app}.fixture.invalid
    service: web

pack:
  portability: standard
  owner: platform-fixtures
  description: Synthetic adoption test app.

data:
  backups:
    required: true
    restore_drill_required: true
    offsite_required: true
"""


def _manifest_with_secret_command(app: str, environment: str) -> str:
    return f"""\
version: 1
app: {app}
environment: {environment}
kind: service

services:
  web:
    image: ghcr.io/ophelia-fixtures/{app}@sha256:1111111111111111111111111111111111111111111111111111111111111111
    port: 8080

routes:
  - domain: {app}.fixture.invalid
    service: web

pack:
  portability: standard
  owner: platform-fixtures
  description: Synthetic adoption redaction test app.

data:
  postgres:
    mode: shared-postgres-database
    database: demo_app
    export:
      command: pg_dump --password super-secret postgres://user:password@db.example/demo_app
    import:
      command: pg_restore --dbname=demo_app demo_app.dump
    verify:
      command: psql demo_app -c "select 1"
  backups:
    required: true
    restore_drill_required: true
    offsite_required: true
"""


def _manifest_with_app_owned_checks(app: str, environment: str) -> str:
    return _manifest(app, environment) + """\
verify:
  - service: app
    path: /ophelia/health
    method: GET
    expect_status: 200
    json_assertions:
      - $.kind == "product.runtime.health"
      - $.ok == true
  - service: app
    path: /ophelia/release
    method: GET
    expect_status: 200
    json_assertions:
      - $.kind == "product.runtime.release"
  - type: command
    service: web
    command:
      - npm
      - run
      - ophelia:data:verify
"""


def _manifest_with_compiled_data_verify_check(app: str, environment: str) -> str:
    return _manifest(app, environment) + """\
verify:
  - service: app
    path: /ophelia/health
    method: GET
    expect_status: 200
  - service: app
    path: /ophelia/release
    method: GET
    expect_status: 200
  - type: command
    service: web
    command:
      - node
      - dist/ophelia-cli.cjs
      - data-verify
      - --json
"""


if __name__ == "__main__":
    unittest.main()
