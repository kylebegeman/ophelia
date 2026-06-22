from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..config import DEFAULT_RUNTIME_ROOT
from ..workflows import (
    cancel_workflow,
    list_workflow_templates,
    pause_workflow,
    plan_workflow,
    preview_workflow,
    resume_workflow,
    run_workflow,
    show_workflow,
)


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("workflow", help="Plan, preview, and resume operation graphs")
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
    plan_parser.add_argument("--template", help="App scaffold/GitHub template name for template-aware workflows")
    plan_parser.add_argument("--owner", help="Owner/org for GitHub provisioning workflows")
    plan_parser.add_argument("--repo", help="Repository OWNER/REPO for GitHub provisioning workflows")
    plan_parser.add_argument("--phase", choices=["all", "repo", "environments", "protection"], help="GitHub provisioning phase")
    plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    plan_parser.set_defaults(handler=run_plan)

    show_parser = workflow_subparsers.add_parser("show", help="Show a stored operation graph")
    show_parser.add_argument("workflow_id", help="Workflow id from `ship workflow plan`")
    show_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    show_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    show_parser.set_defaults(handler=run_show)

    run_parser = workflow_subparsers.add_parser(
        "run", help="Execute runnable nodes in a stored operation graph"
    )
    run_parser.add_argument("workflow_id", help="Workflow id from `ship workflow plan`")
    run_parser.add_argument("--preview", action="store_true", help="Preview resolved nodes without executing anything")
    run_parser.add_argument(
        "--set",
        dest="substitutions",
        action="append",
        default=[],
        metavar="TOKEN=VALUE",
        help="Substitute a TOKEN_CASE placeholder in node commands; repeatable.",
    )
    run_parser.add_argument(
        "--confirm-node",
        dest="confirmations",
        action="append",
        default=[],
        metavar="NODE_ID=TOKEN",
        help="Supply a confirmation token for one mutating node; repeatable.",
    )
    run_parser.add_argument("--timeout", type=float, default=300.0, help="Per-node timeout in seconds")
    run_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    run_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    run_parser.set_defaults(handler=run_run)

    pause_parser = workflow_subparsers.add_parser("pause", help="Pause a stored workflow before the next run/resume")
    pause_parser.add_argument("workflow_id", help="Workflow id or alias")
    pause_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    pause_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    pause_parser.set_defaults(handler=run_pause)

    resume_parser = workflow_subparsers.add_parser("resume", help="Resume a paused or confirmation-waiting workflow")
    resume_parser.add_argument("workflow_id", help="Workflow id or alias")
    resume_parser.add_argument(
        "--set",
        dest="substitutions",
        action="append",
        default=[],
        metavar="TOKEN=VALUE",
        help="Substitute a TOKEN_CASE placeholder in node commands; repeatable.",
    )
    resume_parser.add_argument(
        "--confirm-node",
        dest="confirmations",
        action="append",
        default=[],
        metavar="NODE_ID=TOKEN",
        help="Supply a confirmation token for one mutating node; repeatable.",
    )
    resume_parser.add_argument("--timeout", type=float, default=300.0, help="Per-node timeout in seconds")
    resume_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    resume_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    resume_parser.set_defaults(handler=run_resume)

    cancel_parser = workflow_subparsers.add_parser("cancel", help="Cancel a stored workflow")
    cancel_parser.add_argument("workflow_id", help="Workflow id or alias")
    cancel_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    cancel_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    cancel_parser.set_defaults(handler=run_cancel)

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
        template=args.template,
        owner=args.owner,
        repo=args.repo,
        phase=args.phase,
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


def run_run(args: Namespace) -> int:
    substitutions, errors = _parse_substitutions(args.substitutions)
    confirmations, confirmation_errors = _parse_substitutions(args.confirmations, label="--confirm-node")
    errors.extend(confirmation_errors)
    if errors:
        report = {
            "schema_version": 1,
            "kind": "ophelia.error",
            "status": "failed",
            "error": "Invalid workflow substitution.",
            "blockers": errors,
            "warnings": [],
        }
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(report["error"])
            for blocker in errors:
                print(f"  ! {blocker.get('code')}: {blocker.get('message')}")
        return 1

    receipt = (
        preview_workflow(
            args.workflow_id,
            runtime_root=args.runtime_root,
            substitutions=substitutions,
        )
        if args.preview
        else run_workflow(
            args.workflow_id,
            runtime_root=args.runtime_root,
            substitutions=substitutions,
            confirmations=confirmations,
            timeout=args.timeout,
        )
    )
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(receipt.get("summary") or f"Workflow {'preview' if args.preview else 'run'} {receipt.get('status')}.")
        counts = receipt.get("node_counts") if isinstance(receipt.get("node_counts"), dict) else {}
        if args.preview:
            print(
                "  nodes: "
                f"{counts.get('ready', 0)} ready, "
                f"{counts.get('blocked', 0)} blocked, "
                f"{counts.get('skipped', 0)} skipped"
            )
        else:
            print(
                "  nodes: "
                f"{counts.get('succeeded', 0)} succeeded, "
                f"{counts.get('failed', 0)} failed, "
                f"{counts.get('paused_for_confirmation', 0)} paused, "
                f"{counts.get('blocked', 0)} blocked, "
                f"{counts.get('skipped', 0)} skipped"
            )
        for blocker in receipt.get("blockers", []):
            print(f"  ! {blocker.get('code')}: {blocker.get('message')}")
        for warning in receipt.get("warnings", []):
            print(f"  ~ {warning.get('code')}: {warning.get('message')}")
    success_statuses = {"ready", "paused"} if args.preview else {"succeeded", "paused"}
    return 0 if receipt.get("status") in success_statuses else 1


def run_pause(args: Namespace) -> int:
    receipt = pause_workflow(args.workflow_id, runtime_root=args.runtime_root)
    return _emit_workflow_control(receipt, args.json)


def run_resume(args: Namespace) -> int:
    substitutions, errors = _parse_substitutions(args.substitutions)
    confirmations, confirmation_errors = _parse_substitutions(args.confirmations, label="--confirm-node")
    errors.extend(confirmation_errors)
    if errors:
        report = {
            "schema_version": 1,
            "kind": "ophelia.error",
            "status": "failed",
            "error": "Invalid workflow resume input.",
            "blockers": errors,
            "warnings": [],
        }
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(report["error"])
            for blocker in errors:
                print(f"  ! {blocker.get('code')}: {blocker.get('message')}")
        return 1
    receipt = resume_workflow(
        args.workflow_id,
        runtime_root=args.runtime_root,
        substitutions=substitutions,
        confirmations=confirmations,
        timeout=args.timeout,
    )
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(receipt.get("summary") or f"Workflow resume {receipt.get('status')}.")
        counts = receipt.get("node_counts") if isinstance(receipt.get("node_counts"), dict) else {}
        print(
            "  nodes: "
            f"{counts.get('succeeded', 0)} succeeded, "
            f"{counts.get('failed', 0)} failed, "
            f"{counts.get('paused_for_confirmation', 0)} paused, "
            f"{counts.get('blocked', 0)} blocked, "
            f"{counts.get('skipped', 0)} skipped"
        )
        for blocker in receipt.get("blockers", []):
            print(f"  ! {blocker.get('code')}: {blocker.get('message')}")
    return 0 if receipt.get("status") in {"succeeded", "paused"} else 1


def run_cancel(args: Namespace) -> int:
    receipt = cancel_workflow(args.workflow_id, runtime_root=args.runtime_root)
    return _emit_workflow_control(receipt, args.json)


def run_list(args: Namespace) -> int:
    report = list_workflow_templates()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"{len(report['templates'])} workflow template(s):")
        for template in report["templates"]:
            print(f"  {template['name']}\t{template['node_count']} node(s)\t{template['summary']}")
    return 0


def _emit_workflow_control(receipt: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(receipt.get("summary") or f"Workflow control {receipt.get('status')}.")
        for blocker in receipt.get("blockers", []):
            print(f"  ! {blocker.get('code')}: {blocker.get('message')}")
    return 0 if receipt.get("status") == "succeeded" else 1


def _parse_substitutions(values: list[str], *, label: str = "--set") -> tuple[dict[str, str], list[dict[str, str]]]:
    substitutions: dict[str, str] = {}
    errors: list[dict[str, str]] = []
    for raw in values:
        key, sep, value = str(raw).partition("=")
        if not sep or not key or not value:
            errors.append(
                {
                    "code": "workflow_substitution_invalid",
                    "message": f"{label} values must use TOKEN=VALUE with both sides present.",
                }
            )
            continue
        substitutions[key] = value
    return substitutions, errors


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
                "template": {"type": "string"},
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "phase": {"type": "string"},
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
        command="ship workflow run",
        operation="workflow.run",
        summary="Execute runnable nodes in a stored operation graph, pausing at mutating nodes until confirmed.",
        risk="low",
        mutates_state=True,
        requires_confirmation=False,
        plan_command="ship workflow plan",
        apply_command=None,
        json_kind="ophelia.receipt",
        args_schema={
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "substitutions": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "confirmations": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "preview": {"type": "boolean"},
                "timeout": {"type": "number"},
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["workflow_id"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.receipt.v1",
        artifacts=["updated workflow graph JSON", "workflow run receipt"],
        safety_notes=[
            "Executes read-only nodes automatically; mutating nodes pause until --confirm-node supplies that node's plan token.",
            "Commands are argv arrays, never shell strings; raw stdout/stderr is not stored.",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship workflow pause",
        operation="workflow.pause",
        summary="Pause a stored workflow by updating local workflow state and writing a receipt.",
        risk="low",
        mutates_state=True,
        requires_confirmation=False,
        plan_command="ship workflow plan",
        apply_command=None,
        json_kind="ophelia.workflow_state",
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
        output_schema_ref="ophelia.workflow_state.v1",
        artifacts=["updated workflow graph JSON", "workflow control receipt"],
        safety_notes=["Only local workflow state is changed; app/runtime infrastructure is not mutated."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship workflow resume",
        operation="workflow.resume",
        summary="Resume a stored workflow from durable state, optionally supplying per-node confirmation tokens.",
        risk="low",
        mutates_state=True,
        requires_confirmation=False,
        plan_command="ship workflow plan",
        apply_command=None,
        json_kind="ophelia.receipt",
        args_schema={
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "substitutions": {"type": "array", "items": {"type": "string"}},
                "confirmations": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "number"},
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["workflow_id"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.receipt.v1",
        artifacts=["updated workflow graph JSON", "workflow run receipt"],
        safety_notes=[
            "Skips already-succeeded nodes and resumes from stored workflow state.",
            "Mutating nodes run only when their node id has an explicit --confirm-node token.",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship workflow cancel",
        operation="workflow.cancel",
        summary="Cancel a stored workflow by updating local workflow state and writing a receipt.",
        risk="low",
        mutates_state=True,
        requires_confirmation=False,
        plan_command="ship workflow plan",
        apply_command=None,
        json_kind="ophelia.workflow_state",
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
        output_schema_ref="ophelia.workflow_state.v1",
        artifacts=["updated workflow graph JSON", "workflow control receipt"],
        safety_notes=["Only local workflow state is changed; app/runtime infrastructure is not mutated."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship workflow run --preview",
        operation="workflow.preview",
        summary="Preview resolved workflow run nodes without executing them.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command="ship workflow plan",
        apply_command="ship workflow run",
        json_kind="ophelia.workflow_preview",
        args_schema={
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "substitutions": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["workflow_id"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.workflow_preview.v1",
        artifacts=["referenced workflow graph JSON"],
        safety_notes=[
            "Preview-only. Resolves substitutions, dependencies, and local executables without running nodes.",
        ],
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
