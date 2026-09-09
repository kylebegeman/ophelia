from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..config import DEFAULT_RUNTIME_ROOT
from ..operation_refs import public_resolution, resolve_receipt_ref
from ..operation_schema import operation_digest, report_envelope
from ..portability import receipt_list_report, receipt_show_report
from ..receipt_index import receipt_timeline


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("receipts", help="Browse Ophelia operation receipts")
    receipt_subparsers = parser.add_subparsers(dest="receipts_command")

    list_parser = receipt_subparsers.add_parser("list", help="List receipts")
    list_parser.add_argument("--app", help="Filter by app id")
    list_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    list_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    list_parser.set_defaults(handler=run_list)

    show_parser = receipt_subparsers.add_parser("show", help="Show one receipt")
    show_parser.add_argument("receipt_id")
    show_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    show_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    show_parser.set_defaults(handler=run_show)

    timeline_parser = receipt_subparsers.add_parser(
        "timeline", help="Newest-first, filtered timeline of receipts"
    )
    timeline_parser.add_argument("--app", help="Filter by app id")
    timeline_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    timeline_parser.add_argument("--operation", help="Filter by exact operation id")
    timeline_parser.add_argument("--status", help="Filter by exact status")
    timeline_parser.add_argument("--since", help="Include receipts at or after this ISO date/datetime")
    timeline_parser.add_argument("--until", help="Include receipts at or before this ISO date/datetime")
    timeline_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    timeline_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    timeline_parser.set_defaults(handler=run_timeline)


def run_list(args: Namespace) -> int:
    report = receipt_list_report(args.runtime_root, app=args.app, environment=args.environment)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        for item in report["receipts"]:
            print(f"  {item['receipt_id']}\t{item.get('operation')}\t{item.get('status')}\t{item.get('path')}")
    return 0


def run_show(args: Namespace) -> int:
    resolution = resolve_receipt_ref(args.receipt_id, runtime_root=args.runtime_root)
    if resolution.get("ok"):
        lookup = (
            resolution.get("path")
            if resolution.get("strategy") == "path"
            else resolution.get("resolved_id")
        )
        report = receipt_show_report(str(lookup), runtime_root=args.runtime_root)
        report["requested_ref"] = args.receipt_id
        report["resolved_ref"] = public_resolution(resolution)
        report["warnings"] = _dedupe_warnings(
            [
                *(report.get("warnings", []) if isinstance(report.get("warnings"), list) else []),
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
            "receipts.show",
            None,
            None,
            f"Receipt reference unresolved: {args.receipt_id}.",
            blockers=list(resolution.get("blockers", [])) if isinstance(resolution.get("blockers"), list) else [],
            warnings=list(resolution.get("warnings", [])) if isinstance(resolution.get("warnings"), list) else [],
            checks=[],
            artifacts=[],
            receipt_id=args.receipt_id,
            requested_ref=args.receipt_id,
            resolved_ref=public_resolution(resolution),
        )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        if report["blockers"]:
            for blocker in report["blockers"]:
                print(f"  - {blocker['message']}")
        else:
            print(json.dumps(report["receipt"], indent=2, sort_keys=True))
    return 0 if not report["blockers"] else 1


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


def run_timeline(args: Namespace) -> int:
    report = receipt_timeline(
        args.runtime_root,
        app=args.app,
        environment=args.environment,
        operation=args.operation,
        status=args.status,
        since=args.since,
        until=args.until,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        for item in report["receipts"]:
            rollback = "rollback" if item.get("rollback_available") else "no-rollback"
            print(
                f"  {item['receipt_id']}\t{item.get('operation')}\t{item.get('status')}"
                f"\t{item.get('started_at')}\t{rollback}"
            )
        for warning in report["warnings"]:
            print(f"  ! {warning.get('code')}: {warning.get('message')}")
    return 0


register_cli_descriptor(
    CommandDescriptor(
        command="ship receipts timeline",
        operation="receipts.timeline",
        summary="Newest-first, filtered timeline of operation receipts.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.receipt_timeline",
        args_schema={
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "environment": {"type": "string"},
                "operation": {"type": "string"},
                "status": {"type": "string"},
                "since": {"type": "string"},
                "until": {"type": "string"},
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.receipt_timeline.v1",
        artifacts=[],
        safety_notes=["Read-only receipt timeline. No mutation."],
    )
)
