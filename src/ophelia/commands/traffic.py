from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..config import DEFAULT_RUNTIME_ROOT
from ..portability import traffic_status


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("traffic", help="Read-only production traffic state")
    traffic_subparsers = parser.add_subparsers(dest="traffic_command")

    status_parser = traffic_subparsers.add_parser(
        "status",
        help="Summarize an app's traffic state from receipts and local files (no provider credentials)",
    )
    status_parser.add_argument("--app", required=True, help="App id")
    status_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    status_parser.add_argument("--manifest", type=Path, help="Path to app .ophelia manifest")
    status_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    status_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    status_parser.set_defaults(handler=run_traffic_status)


def run_traffic_status(args: Namespace) -> int:
    report = traffic_status(
        args.app,
        environment=args.environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if not report.get("blockers") else 1
    print(report["summary"])
    latest_apply = report.get("latest_apply")
    if isinstance(latest_apply, dict):
        print(
            "  latest apply: "
            f"{latest_apply.get('receipt_id')} status={latest_apply.get('status')} "
            f"{latest_apply.get('source_host')} -> {latest_apply.get('target_host')} "
            f"origin={latest_apply.get('target_origin')}"
        )
    latest_rollback = report.get("latest_rollback")
    if isinstance(latest_rollback, dict):
        print(f"  latest rollback: {latest_rollback.get('receipt_id')} status={latest_rollback.get('status')}")
    for route in report.get("route_ownership", []) if isinstance(report.get("route_ownership"), list) else []:
        if isinstance(route, dict):
            print(f"  route: {route.get('domain')} owned by {route.get('app')}")
    for blocker in report.get("blockers", []) if isinstance(report.get("blockers"), list) else []:
        if isinstance(blocker, dict):
            print(f"  blocker: {blocker.get('code')} - {blocker.get('message')}")
    return 0 if not report.get("blockers") else 1


register_cli_descriptor(
    CommandDescriptor(
        command="ship traffic status",
        operation="app.traffic.status",
        summary="Summarize an app's traffic state from receipts and local files (read-only, no provider credentials).",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.traffic_status",
        args_schema={
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "environment": {"type": "string"},
                "manifest": {"type": "string"},
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["app"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.traffic_status.v1",
        artifacts=[],
        safety_notes=[
            "Read-only. Reads receipts and local files only; never probes the network or reads provider credentials.",
            "Secret values are redacted from all output.",
        ],
    )
)
