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
        self.assertIn("ship pack init --app demo-app --environment staging", json.dumps(plan["next_commands"]))
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


if __name__ == "__main__":
    unittest.main()
