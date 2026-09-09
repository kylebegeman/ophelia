from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..config import DEFAULT_RUNTIME_ROOT
from ..operation_refs import public_resolution, resolve_receipt_ref
from ..operation_schema import operation_digest, report_envelope
from ..restore_verification import (
    is_restore_drill_receipt_operation,
    restore_drills_list,
    restore_drills_show,
)


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("restore-drills", help="List or show restore drill / backup-verification receipts")
    drill_subparsers = parser.add_subparsers(dest="restore_drills_command")

    list_parser = drill_subparsers.add_parser("list", help="List restore drill / verification receipts for an app")
    list_parser.add_argument("--app", required=True, help="App id")
    list_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    list_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    list_parser.set_defaults(handler=run_restore_drills_list)

    show_parser = drill_subparsers.add_parser("show", help="Show one restore drill / verification receipt")
    show_parser.add_argument("drill_id", help="Restore drill / verification id")
    show_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    show_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    show_parser.set_defaults(handler=run_restore_drills_show)


def run_restore_drills_list(args: Namespace) -> int:
    report = restore_drills_list(args.app, environment=args.environment, runtime_root=args.runtime_root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        for drill in report.get("drills", []) if isinstance(report.get("drills"), list) else []:
            if isinstance(drill, dict):
                print(f"  - {drill.get('receipt_id')} [{drill.get('kind')}] status={drill.get('status')}")
    return 0


def run_restore_drills_show(args: Namespace) -> int:
    resolution = resolve_receipt_ref(
        args.drill_id,
        runtime_root=args.runtime_root,
        operation_filter=is_restore_drill_receipt_operation,
    )
    if resolution.get("ok"):
        resolved_id = str(resolution.get("resolved_id") or args.drill_id)
        report = restore_drills_show(
            resolved_id,
            runtime_root=args.runtime_root,
            resolved_payload=(
                resolution.get("payload")
                if isinstance(resolution.get("payload"), dict)
                else None
            ),
            receipt_locator=(
                str(resolution.get("path")) if resolution.get("path") else None
            ),
        )
        report["requested_ref"] = args.drill_id
        report["resolved_ref"] = public_resolution(resolution)
        report["warnings"] = _dedupe_warnings(
            [
                *(
                    report.get("warnings", [])
                    if isinstance(report.get("warnings"), list)
                    else []
                ),
                *(
                    resolution.get("warnings", [])
                    if isinstance(resolution.get("warnings"), list)
                    else []
                ),
            ]
        )
        report["digest"] = operation_digest(report)
    else:
        report = report_envelope(
            "restore.drills.show",
            None,
            None,
            f"Restore drill / verification receipt reference unresolved: {args.drill_id}.",
            blockers=list(resolution.get("blockers", [])) if isinstance(resolution.get("blockers"), list) else [],
            warnings=list(resolution.get("warnings", [])) if isinstance(resolution.get("warnings"), list) else [],
            checks=[],
            artifacts=[],
            kind="ophelia.restore_drill",
            drill_id=args.drill_id,
            requested_ref=args.drill_id,
            resolved_ref=public_resolution(resolution),
        )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
    return 0 if not report.get("blockers") else 1


def _dedupe_warnings(items: list) -> list:
    result = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        identity = (
            str(item.get("code") or ""),
            str(item.get("message") or ""),
            str(item.get("path") or ""),
        )
        if identity in seen:
            continue
        seen.add(identity)
        result.append(item)
    return result


register_cli_descriptor(
    CommandDescriptor(
        command="ship restore-drills list",
        operation="restore.drills.list",
        summary="List restore drill and backup-verification receipts for an app (read-only).",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.restore_drills",
        args_schema={
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "environment": {"type": "string"},
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["app"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.restore_drills.v1",
        artifacts=[],
        safety_notes=["Read-only receipt browsing. No mutation; secret values redacted."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship restore-drills show",
        operation="restore.drills.show",
        summary="Show one restore drill / backup-verification receipt (read-only).",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.restore_drill",
        args_schema={
            "type": "object",
            "properties": {
                "drill_id": {"type": "string"},
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["drill_id"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.restore_drill.v1",
        artifacts=[],
        safety_notes=["Read-only receipt browsing. No mutation; secret values redacted."],
    )
)
