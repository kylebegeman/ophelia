from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.command_catalog import command_registry
from ophelia.workflows import (
    WORKFLOW_PLAN_KIND,
    WORKFLOW_TEMPLATES_KIND,
    command_has_shell_metacharacters,
    list_workflow_templates,
    plan_workflow,
    show_workflow,
)


EXPECTED_MOVE_APP_ORDER = [
    "validate-manifest",
    "validate-provider-config",
    "audit-secrets",
    "check-route-conflicts",
    "check-backups",
    "readiness",
    "export-plan",
    "import-plan",
    "restore-drill-plan",
    "traffic-plan",
]

_SHELL_METACHARS = ["&&", "||", "|", ";", "`", "$(", ">", "<"]


def _plan(runtime_root: Path) -> dict:
    return plan_workflow(
        "move-app",
        app="dragon-writer",
        environment="production",
        source="spaceship",
        target="ovh",
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

    def test_all_move_app_nodes_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = _plan(Path(tmp))
        mutating = [node["id"] for node in plan["nodes"] if node["mutates_state"]]
        self.assertEqual([], mutating, f"move-app nodes should be read/plan only: {mutating}")

    def test_artifact_is_referenced_and_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            plan = _plan(runtime_root)
            self.assertEqual(1, len(plan["artifacts"]))
            artifact_path = Path(plan["artifacts"][0]["path"])
            self.assertTrue(artifact_path.exists())
            self.assertEqual(runtime_root / "workflows", artifact_path.parent)


class UnknownTemplateTests(unittest.TestCase):
    def test_unknown_template_name_returns_blockers_no_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_workflow("does-not-exist", app="dragon-writer", runtime_root=Path(tmp))
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


class ListTemplatesTests(unittest.TestCase):
    def test_list_templates_includes_move_app(self) -> None:
        report = list_workflow_templates()
        self.assertEqual(WORKFLOW_TEMPLATES_KIND, report["kind"])
        names = {template["name"] for template in report["templates"]}
        self.assertIn("move-app", names)
        move_app = next(t for t in report["templates"] if t["name"] == "move-app")
        self.assertEqual(len(EXPECTED_MOVE_APP_ORDER), move_app["node_count"])
        self.assertIn("app", move_app["parameters"])


if __name__ == "__main__":
    unittest.main()
