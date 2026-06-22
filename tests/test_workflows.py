from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.command_catalog import command_registry
from ophelia.workflows import (
    WORKFLOW_PLAN_KIND,
    WORKFLOW_PREVIEW_KIND,
    WORKFLOW_TEMPLATES_KIND,
    cancel_workflow,
    command_has_shell_metacharacters,
    list_workflow_templates,
    pause_workflow,
    plan_workflow,
    preview_workflow,
    resume_workflow,
    run_workflow,
    show_workflow,
)


EXPECTED_MOVE_APP_ORDER = [
    "validate-manifest",
    "validate-provider-config",
    "audit-secrets",
    "check-route-conflicts",
    "check-backups",
    "readiness",
    "placement",
    "export-plan",
    "export-create",
    "import-plan",
    "import-apply",
    "restore-drill-plan",
    "restore-drill-apply",
    "traffic-plan",
    "traffic-apply",
]

_SHELL_METACHARS = ["&&", "||", "|", ";", "`", "$(", ">", "<"]


def _plan(runtime_root: Path) -> dict:
    return plan_workflow(
        "move-app",
        app="demo-service",
        environment="production",
        source="source-host",
        target="target-host",
        target_origin="https://origin.example.com",
        runtime_root=runtime_root,
    )


class MoveAppGraphTests(unittest.TestCase):
    def test_node_ids_in_dependency_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = _plan(Path(tmp))
        self.assertEqual(WORKFLOW_PLAN_KIND, plan["kind"])
        self.assertTrue(plan["dry_run"])
        node_ids = [node["id"] for node in plan["nodes"]]
        self.assertEqual(EXPECTED_MOVE_APP_ORDER, node_ids)

    def test_every_dependency_references_an_earlier_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = _plan(Path(tmp))
        seen: set[str] = set()
        for node in plan["nodes"]:
            for dependency in node["depends_on"]:
                self.assertIn(
                    dependency,
                    seen,
                    f"node {node['id']} depends on {dependency} which is not an earlier node",
                )
            seen.add(node["id"])
        # No top-level blockers for a well-formed template.
        self.assertEqual([], plan["blockers"])

    def test_node_commands_are_arg_arrays_without_shell_metacharacters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = _plan(Path(tmp))
        commands_by_id = {node["id"]: node["command"] for node in plan["nodes"]}
        self.assertEqual(
            ["ship", "validate", "MANIFEST_PATH", "--json"],
            commands_by_id["validate-manifest"],
        )
        self.assertEqual(
            ["ship", "inspect", "conflicts", "--manifest-dir", "MANIFEST_DIR", "--json"],
            commands_by_id["check-route-conflicts"],
        )
        self.assertEqual(
            [
                "ship",
                "app",
                "placement",
                "demo-service",
                "--from",
                "source-host",
                "--to",
                "target-host",
                "--environment",
                "production",
                "--json",
            ],
            commands_by_id["placement"],
        )
        self.assertEqual(
            [
                "ship",
                "app",
                "traffic",
                "plan",
                "demo-service",
                "--from",
                "source-host",
                "--to",
                "target-host",
                "--target-origin",
                "https://origin.example.com",
                "--environment",
                "production",
                "--json",
            ],
            commands_by_id["traffic-plan"],
        )
        for node in plan["nodes"]:
            command = node["command"]
            self.assertIsInstance(command, list)
            for token in command:
                self.assertIsInstance(token, str)
                for meta in _SHELL_METACHARS:
                    self.assertNotIn(meta, token, f"node {node['id']} token {token!r} has {meta!r}")
            self.assertFalse(command_has_shell_metacharacters(command))
            # Every node command targets the typed `ship` CLI and is read-only JSON.
            self.assertEqual("ship", command[0])
            self.assertIn("--json", command)

    def test_plan_is_read_only_no_node_executed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            plan = _plan(runtime_root)
            for node in plan["nodes"]:
                self.assertEqual("planned", node["status"])
                self.assertIsNone(node["receipt_id"])
                self.assertIsNone(node["started_at"])
                self.assertIsNone(node["completed_at"])
            # The ONLY file written is the graph artifact under workflows/.
            written = sorted(p for p in runtime_root.rglob("*") if p.is_file())
            self.assertEqual(1, len(written), f"unexpected files written: {written}")
            self.assertEqual(runtime_root / "workflows", written[0].parent)
            self.assertTrue(written[0].name.endswith(".json"))
            # No receipts directory or receipt files created.
            self.assertFalse((runtime_root / "receipts").exists())

    def test_every_node_operation_is_in_command_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = _plan(Path(tmp))
        catalog_operations = {descriptor.operation for descriptor in command_registry()}
        for node in plan["nodes"]:
            self.assertIn(
                node["operation"],
                catalog_operations,
                f"node {node['id']} operation {node['operation']} not in command catalog",
            )

    def test_move_app_mutating_nodes_are_confirmation_gated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = _plan(Path(tmp))
        mutating = [node for node in plan["nodes"] if node["mutates_state"]]
        self.assertEqual(
            ["export-create", "import-apply", "restore-drill-apply", "traffic-apply"],
            [node["id"] for node in mutating],
        )
        self.assertTrue(all(node["requires_confirmation"] for node in mutating))
        self.assertTrue(all(node["plan_command"] for node in mutating))
        self.assertTrue(all(node["apply_command"] for node in mutating))

    def test_artifact_is_referenced_and_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            plan = _plan(runtime_root)
            self.assertEqual(WORKFLOW_PLAN_KIND, plan["kind"])
            self.assertEqual("workflow.plan", plan["operation"])
            self.assertFalse(plan["confirmation_required"])
            self.assertIsNone(plan["exact_apply_input"])
            self.assertEqual(1, len(plan["artifacts"]))
            artifact_path = Path(plan["artifacts"][0]["path"])
            self.assertTrue(artifact_path.exists())
            self.assertEqual(runtime_root / "workflows", artifact_path.parent)


class UnknownTemplateTests(unittest.TestCase):
    def test_unknown_template_name_returns_blockers_no_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_workflow("does-not-exist", app="demo-service", runtime_root=Path(tmp))
        self.assertEqual([], plan["nodes"])
        self.assertTrue(plan["blockers"])
        codes = {blocker["code"] for blocker in plan["blockers"]}
        self.assertIn("workflow_template_not_found", codes)
        # No artifact written for an unknown template.
        written = sorted(p for p in Path(tmp).rglob("*") if p.is_file())
        self.assertEqual([], written)


class ShowWorkflowTests(unittest.TestCase):
    def test_show_round_trips_a_planned_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            plan = _plan(runtime_root)
            report = show_workflow(plan["workflow_id"], runtime_root=runtime_root)
        self.assertEqual("ok", report["status"])
        self.assertEqual([], report["blockers"])
        self.assertIsNotNone(report["workflow"])
        node_ids = [node["id"] for node in report["workflow"]["nodes"]]
        self.assertEqual(EXPECTED_MOVE_APP_ORDER, node_ids)
        self.assertEqual(plan["workflow_id"], report["workflow"]["workflow_id"])

    def test_show_unknown_id_returns_not_found_blocker_no_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = show_workflow("workflow.move-app.missing", runtime_root=Path(tmp))
        self.assertEqual("not_found", report["status"])
        self.assertIsNone(report["workflow"])
        codes = {blocker["code"] for blocker in report["blockers"]}
        self.assertIn("workflow_not_found", codes)


class RunWorkflowTests(unittest.TestCase):
    def test_run_pauses_at_first_mutating_node_and_writes_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            plan = _plan(runtime_root)
            calls: list[list[str]] = []

            def _runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "ophelia.report",
                            "operation": "test.node",
                            "operation_id": f"node-output-{len(calls)}",
                            "status": "ok",
                            "blockers": [],
                            "warnings": [],
                            "artifacts": [],
                        }
                    ),
                    stderr="",
                )

            receipt = run_workflow(
                plan["workflow_id"],
                runtime_root=runtime_root,
                substitutions={
                    "MANIFEST_PATH": "manifests/demo-service.ophelia.yml",
                    "PROVIDER_CONFIG": "providers.json",
                    "MANIFEST_DIR": "manifests",
                    "EXPORT_BUNDLE": "exports/demo-service",
                },
                command_runner=_runner,
            )

            self.assertEqual("paused", receipt["status"])
            self.assertEqual("workflow.run", receipt["operation"])
            self.assertEqual(8, len(calls))
            self.assertEqual(8, receipt["node_counts"]["succeeded"])
            self.assertEqual(1, receipt["node_counts"]["paused_for_confirmation"])
            self.assertEqual("paused_for_confirmation", receipt["nodes"][8]["status"])
            self.assertEqual("export-create", receipt["nodes"][8]["id"])
            self.assertEqual("node-output-1", receipt["nodes"][0]["receipt_id"])
            self.assertEqual("ship", Path(calls[0][0]).name)
            self.assertTrue((runtime_root / "workflows" / "receipts").exists())
            self.assertTrue((runtime_root / "apps" / "demo-service" / "receipts").exists())
            stored = show_workflow(plan["workflow_id"], runtime_root=runtime_root)
            self.assertEqual("paused", stored["workflow"]["last_run"]["status"])
            self.assertEqual("paused", stored["workflow"]["workflow_status"])

    def test_resume_skips_succeeded_nodes_and_runs_confirmed_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            plan = _plan(runtime_root)
            calls: list[list[str]] = []

            def _runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                kind = "ophelia.receipt" if "--confirm" in command else "ophelia.report"
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "schema_version": 1,
                            "kind": kind,
                            "operation": "test.node",
                            "operation_id": f"node-output-{len(calls)}",
                            "status": "succeeded" if kind == "ophelia.receipt" else "ok",
                            "blockers": [],
                            "warnings": [],
                            "artifacts": [{"path": "artifact.json", "kind": "test"}],
                            "rollback": {"available": False},
                        }
                    ),
                    stderr="",
                )

            substitutions = {
                "MANIFEST_PATH": "manifests/demo-service.ophelia.yml",
                "PROVIDER_CONFIG": "providers.json",
                "MANIFEST_DIR": "manifests",
                "EXPORT_BUNDLE": "exports/demo-service",
            }
            first = run_workflow(
                plan["workflow_id"],
                runtime_root=runtime_root,
                substitutions=substitutions,
                command_runner=_runner,
            )
            second = resume_workflow(
                plan["workflow_id"],
                runtime_root=runtime_root,
                substitutions=substitutions,
                confirmations={"export-create": "token-from-export-plan"},
                command_runner=_runner,
            )

            self.assertEqual("paused", first["status"])
            self.assertEqual("paused", second["status"])
            # Eight read-only nodes from the first run, then export-create and
            # import-plan on resume. Already-succeeded nodes were not rerun.
            self.assertEqual(10, len(calls))
            self.assertEqual("succeeded", second["nodes"][8]["status"])
            self.assertEqual("node-output-9", second["nodes"][8]["receipt_id"])
            self.assertEqual("succeeded", second["nodes"][9]["status"])
            self.assertEqual("paused_for_confirmation", second["nodes"][10]["status"])

    def test_run_blocks_unresolved_placeholders_without_executing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            plan = _plan(runtime_root)

            def _runner(_command: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
                raise AssertionError("unresolved workflow should not execute a command")

            receipt = run_workflow(
                plan["workflow_id"],
                runtime_root=runtime_root,
                command_runner=_runner,
            )

            self.assertEqual("blocked", receipt["status"])
            self.assertEqual("blocked", receipt["nodes"][0]["status"])
            self.assertEqual("skipped", receipt["nodes"][1]["status"])
            codes = {blocker["code"] for blocker in receipt["blockers"]}
            self.assertIn("workflow_unresolved_placeholder", codes)

    def test_run_honors_blocked_child_payload_even_with_zero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            plan = _plan(runtime_root)
            calls: list[list[str]] = []

            def _runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "ophelia.report",
                            "operation": "manifest.validate",
                            "operation_id": "blocked-child",
                            "status": "blocked",
                            "blockers": [{"code": "fixture_blocked", "message": "fixture blocker"}],
                            "warnings": [],
                            "artifacts": [],
                        }
                    ),
                    stderr="",
                )

            receipt = run_workflow(
                plan["workflow_id"],
                runtime_root=runtime_root,
                substitutions={
                    "MANIFEST_PATH": "manifests/demo-service.ophelia.yml",
                    "PROVIDER_CONFIG": "providers.json",
                    "MANIFEST_DIR": "manifests",
                    "EXPORT_BUNDLE": "exports/demo-service",
                },
                command_runner=_runner,
            )

        self.assertEqual("blocked", receipt["status"])
        self.assertEqual(1, len(calls))
        self.assertEqual("blocked", receipt["nodes"][0]["status"])
        self.assertEqual("skipped", receipt["nodes"][1]["status"])
        codes = {blocker["code"] for blocker in receipt["blockers"]}
        self.assertIn("fixture_blocked", codes)


class PreviewWorkflowTests(unittest.TestCase):
    def test_preview_resolves_nodes_without_executing_or_writing_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            plan = _plan(runtime_root)

            preview = preview_workflow(
                "latest:demo-service",
                runtime_root=runtime_root,
                substitutions={
                    "MANIFEST_PATH": "manifests/demo-service.ophelia.yml",
                    "PROVIDER_CONFIG": "providers.json",
                    "MANIFEST_DIR": "manifests",
                    "EXPORT_BUNDLE": "exports/demo-service",
                },
            )

            self.assertEqual(WORKFLOW_PREVIEW_KIND, preview["kind"])
            self.assertEqual("paused", preview["status"])
            self.assertEqual(plan["workflow_id"], preview["workflow_id"])
            self.assertEqual(8, preview["node_counts"]["ready"])
            self.assertEqual(1, preview["node_counts"]["paused_for_confirmation"])
            self.assertEqual(6, preview["node_counts"]["skipped"])
            self.assertEqual("paused_for_confirmation", preview["nodes"][8]["status"])
            self.assertTrue(all("resolved_command" in node for node in preview["nodes"][:9]))
            self.assertTrue(all("executable_path" in node for node in preview["nodes"][:9]))
            self.assertFalse((runtime_root / "workflows" / "receipts").exists())
            stored = show_workflow(plan["workflow_id"], runtime_root=runtime_root)
            self.assertNotIn("last_run", stored["workflow"])

    def test_preview_blocks_unresolved_placeholders_and_skips_dependents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            plan = _plan(runtime_root)

            preview = preview_workflow(plan["workflow_id"], runtime_root=runtime_root)

            self.assertEqual("blocked", preview["status"])
            self.assertEqual("blocked", preview["nodes"][0]["status"])
            self.assertEqual("skipped", preview["nodes"][1]["status"])
            codes = {blocker["code"] for blocker in preview["blockers"]}
            self.assertIn("workflow_unresolved_placeholder", codes)
            self.assertFalse((runtime_root / "workflows" / "receipts").exists())


class ListTemplatesTests(unittest.TestCase):
    def test_list_templates_includes_move_app(self) -> None:
        report = list_workflow_templates()
        self.assertEqual(WORKFLOW_TEMPLATES_KIND, report["kind"])
        names = {template["name"] for template in report["templates"]}
        self.assertIn("move-app", names)
        self.assertIn("incident-triage", names)
        self.assertIn("release-readiness", names)
        self.assertIn("github-provisioning", names)
        self.assertIn("restore-rehearsal", names)
        move_app = next(t for t in report["templates"] if t["name"] == "move-app")
        self.assertEqual(len(EXPECTED_MOVE_APP_ORDER), move_app["node_count"])
        self.assertIn("app", move_app["parameters"])


class WorkflowControlTests(unittest.TestCase):
    def test_pause_and_cancel_update_workflow_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            plan = _plan(runtime_root)

            paused = pause_workflow(plan["workflow_id"], runtime_root=runtime_root)
            stored_after_pause = show_workflow(plan["workflow_id"], runtime_root=runtime_root)
            cancelled = cancel_workflow(plan["workflow_id"], runtime_root=runtime_root)
            stored_after_cancel = show_workflow(plan["workflow_id"], runtime_root=runtime_root)

        self.assertEqual("succeeded", paused["status"])
        self.assertEqual("paused", stored_after_pause["workflow"]["workflow_status"])
        self.assertEqual("succeeded", cancelled["status"])
        self.assertEqual("cancelled", stored_after_cancel["workflow"]["workflow_status"])
        self.assertTrue(all(node["status"] == "cancelled" for node in stored_after_cancel["workflow"]["nodes"]))


if __name__ == "__main__":
    unittest.main()
