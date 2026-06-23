from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..lumen_adapter import action_descriptors, capabilities, console_data, dashboard_data


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("lumen", help="Read-only operator-console adapter views")
    lumen_subparsers = parser.add_subparsers(dest="lumen_command")

    cap_parser = lumen_subparsers.add_parser(
        "capabilities", help="Manifest of commands, endpoints, and surfaces Ophelia exposes"
    )
    cap_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    cap_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    cap_parser.set_defaults(handler=run_capabilities)

    dash_parser = lumen_subparsers.add_parser(
        "dashboard-data", help="Aggregate readiness/receipts/conflicts/backups per app"
    )
    dash_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    dash_parser.add_argument("--manifests-dir", type=Path, default=REPO_ROOT / "manifests")
    dash_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    dash_parser.set_defaults(handler=run_dashboard_data)

    ad_parser = lumen_subparsers.add_parser(
        "action-descriptors", help="The shared command catalog framed as operator-console action descriptors"
    )
    ad_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    ad_parser.set_defaults(handler=run_action_descriptors)

    console_parser = lumen_subparsers.add_parser(
        "console-data", help="Single bounded payload for an operator console"
    )
    console_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    console_parser.add_argument("--manifests-dir", type=Path, default=REPO_ROOT / "manifests")
    console_parser.add_argument("--plugins-dir", type=Path, default=REPO_ROOT / "plugins")
    console_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    console_parser.set_defaults(handler=run_console_data)


def run_capabilities(args: Namespace) -> int:
    report = capabilities(runtime_root=args.runtime_root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report.get("summary", ""))
        print(f"  commands: {report['commands']['count']}")
        print(f"  http_endpoints: {len(report['http_endpoints'])}")
        print(f"  surfaces: {', '.join(report['surfaces'])}")
    return 0


def run_dashboard_data(args: Namespace) -> int:
    report = dashboard_data(runtime_root=args.runtime_root, manifests_dir=args.manifests_dir)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report.get("summary", ""))
        for entry in report.get("apps", []):
            score = entry.get("portability_score") or {}
            print(
                f"  {entry.get('app')}/{entry.get('environment')}\t"
                f"{entry.get('readiness_level')}\tscore={score.get('score')}\t"
                f"blockers={entry['blockers']['count']}\t"
                f"backup={entry.get('backup_freshness')}"
            )
        for warning in report.get("warnings", []):
            print(f"  ~ {warning.get('code')}: {warning.get('message')}")
    return 0


def run_action_descriptors(args: Namespace) -> int:
    report = action_descriptors()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"{len(report['descriptors'])} action descriptor(s) from the shared command catalog:")
        for descriptor in report["descriptors"]:
            print(f"  {descriptor['command']}\t{descriptor['operation']}\t{descriptor['risk']}")
    return 0


def run_console_data(args: Namespace) -> int:
    report = console_data(runtime_root=args.runtime_root, manifests_dir=args.manifests_dir, plugins_dir=args.plugins_dir)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        overview = report.get("overview") if isinstance(report.get("overview"), dict) else {}
        cards = overview.get("cards") if isinstance(overview.get("cards"), list) else []
        print(report.get("summary", ""))
        for card in cards:
            if isinstance(card, dict):
                print(f"  {card.get('label')}: {card.get('value')} ({card.get('status')})")
    return 0


register_cli_descriptor(
    CommandDescriptor(
        command="ship lumen capabilities",
        operation="lumen.capabilities",
        summary="Manifest of commands, HTTP endpoints, and surfaces Ophelia exposes to operator UIs.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.lumen.capabilities",
        args_schema={
            "type": "object",
            "properties": {
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.lumen.capabilities.v1",
        artifacts=[],
        safety_notes=["Read-only discovery surface. No mutation; no secret values."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship lumen console-data",
        operation="lumen.console",
        summary="Single bounded read-only payload for an operator console.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.lumen.console",
        args_schema={
            "type": "object",
            "properties": {
                "runtime_root": {"type": "string"},
                "manifests_dir": {"type": "string"},
                "plugins_dir": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.lumen.console.v1",
        artifacts=[],
        safety_notes=[
            "Read-only aggregate for UI rendering. Does not execute workflows, commands, plugins, or provider operations.",
            "Approval queue entries are metadata only; mutation still runs through each command's own plan/confirm/apply contract.",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship lumen dashboard-data",
        operation="lumen.dashboard-data",
        summary="Aggregate readiness, receipts, route conflicts, and backup freshness per app/environment.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.lumen.dashboard",
        args_schema={
            "type": "object",
            "properties": {
                "runtime_root": {"type": "string"},
                "manifests_dir": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.lumen.dashboard.v1",
        artifacts=[],
        safety_notes=[
            "Read-only aggregate of existing reports. No mutation; all secret values redacted.",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship lumen action-descriptors",
        operation="lumen.action-descriptors",
        summary="The shared command catalog framed as operator-console action descriptors (read-only).",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.lumen.action_descriptors",
        args_schema={
            "type": "object",
            "properties": {"json": {"type": "boolean"}},
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.lumen.action_descriptors.v1",
        artifacts=[],
        safety_notes=["Read-only discovery surface sourced from the shared catalog. No mutation."],
    )
)
