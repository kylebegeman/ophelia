from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..config import DEFAULT_RUNTIME_ROOT
from ..workflows import list_workflow_templates, plan_workflow, show_workflow


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("workflow", help="Inspect plan-only operation graphs")
    workflow_subparsers = parser.add_subparsers(dest="workflow_command")

    plan_parser = workflow_subparsers.add_parser(
        "plan", help="Build a plan-only operation graph for a template"
    )
    plan_parser.add_argument("name", help="Workflow template name (e.g. move-app)")
    plan_parser.add_argument("--app", required=True, help="App id")
    plan_parser.add_argument("--from", dest="source", help="Source host id")
    plan_parser.add_argument("--to", dest="target", help="Target host id")
    plan_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    plan_parser.add_argument("--target-origin", help="Target DNS/Caddy origin host or IP")
    plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    plan_parser.set_defaults(handler=run_plan)

    show_parser = workflow_subparsers.add_parser("show", help="Show a stored operation graph")
    show_parser.add_argument("workflow_id", help="Workflow id from `ship workflow plan`")
    show_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    show_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    show_parser.set_defaults(handler=run_show)

    list_parser = workflow_subparsers.add_parser("list", help="List available workflow templates")
    list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    list_parser.set_defaults(handler=run_list)


def _print_nodes(nodes: list) -> None:
    for node in nodes:
        depends = ", ".join(node.get("depends_on") or []) or "(none)"
        mutates = "mutates" if node.get("mutates_state") else "read-only"
        print(f"  {node['id']}\t{node.get('operation')}\tdepends_on=[{depends}]\t{mutates}")


def run_plan(args: Namespace) -> int:
    plan = plan_workflow(
        args.name,
        app=args.app,
        environment=args.environment,
        source=args.source,
        target=args.target,
        target_origin=args.target_origin,
        runtime_root=args.runtime_root,
    )
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print(plan["summary"])
        print(f"  workflow_id: {plan['workflow_id']}")
        _print_nodes(plan["nodes"])
        for blocker in plan["blockers"]:
            print(f"  ! {blocker.get('code')}: {blocker.get('message')}")
        for warning in plan["warnings"]:
            print(f"  ~ {warning.get('code')}: {warning.get('message')}")
    return 1 if plan["blockers"] else 0


def run_show(args: Namespace) -> int:
    report = show_workflow(args.workflow_id, runtime_root=args.runtime_root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        workflow = report.get("workflow")
        if workflow:
            _print_nodes(workflow.get("nodes") or [])
        for blocker in report["blockers"]:
            print(f"  ! {blocker.get('code')}: {blocker.get('message')}")
    return 1 if report["blockers"] else 0


def run_list(args: Namespace) -> int:
    report = list_workflow_templates()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"{len(report['templates'])} workflow template(s):")
        for template in report["templates"]:
            print(f"  {template['name']}\t{template['node_count']} node(s)\t{template['summary']}")
    return 0


register_cli_descriptor(
    CommandDescriptor(
        command="ship workflow plan",
        operation="workflow.plan",
        summary="Build a plan-only, inspectable operation graph for a workflow template.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.workflow_plan",
        args_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "app": {"type": "string"},
                "source": {"type": "string"},
                "target": {"type": "string"},
                "environment": {"type": "string"},
                "target_origin": {"type": "string"},
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["name", "app"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.workflow_plan.v1",
        artifacts=["stored operation graph JSON under runtime_root/workflows/"],
        safety_notes=[
            "Plan-only operation graph. No node is executed; nothing touches the VPS.",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship workflow show",
        operation="workflow.show",
        summary="Show a stored plan-only operation graph by workflow id.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.workflow_report",
        args_schema={
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["workflow_id"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.workflow_report.v1",
        artifacts=[],
        safety_notes=["Read-only graph inspection. No mutation."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship workflow list",
        operation="workflow.list",
        summary="List available plan-only workflow templates.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.workflow_templates",
        args_schema={
            "type": "object",
            "properties": {"json": {"type": "boolean"}},
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.workflow_templates.v1",
        artifacts=[],
        safety_notes=["Read-only discovery surface. No mutation."],
    )
)
