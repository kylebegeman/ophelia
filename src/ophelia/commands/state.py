from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..state_db import query_receipts, rebuild_state, refresh_state, state_status, state_summary


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("state", help="Inspect or rebuild the local runtime-state index")
    state_subparsers = parser.add_subparsers(dest="state_command")

    status_parser = state_subparsers.add_parser("status", help="Report local state index status")
    status_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    status_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    status_parser.set_defaults(handler=run_status)

    rebuild_parser = state_subparsers.add_parser(
        "rebuild", help="Rebuild the local state index from runtime-root files"
    )
    rebuild_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    rebuild_parser.add_argument("--manifests-dir", type=Path, default=REPO_ROOT / "manifests")
    rebuild_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    rebuild_parser.set_defaults(handler=run_rebuild)

    refresh_parser = state_subparsers.add_parser(
        "refresh", help="Refresh the local state service index from runtime-root files"
    )
    refresh_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    refresh_parser.add_argument("--manifests-dir", type=Path, default=REPO_ROOT / "manifests")
    refresh_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    refresh_parser.set_defaults(handler=run_refresh)

    summary_parser = state_subparsers.add_parser("summary", help="Read app aggregates from the local state service")
    summary_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    summary_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    summary_parser.set_defaults(handler=run_summary)

    query_parser = state_subparsers.add_parser("query", help="Query the local state index")
    query_subparsers = query_parser.add_subparsers(dest="state_query_command")
    receipts_parser = query_subparsers.add_parser("receipts", help="Query indexed receipts")
    receipts_parser.add_argument("--app", help="Filter by app id")
    receipts_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    receipts_parser.add_argument("--operation", help="Filter by exact operation id")
    receipts_parser.add_argument("--status", help="Filter by exact status")
    receipts_parser.add_argument("--ref", help="Resolve one receipt by id, prefix, path, latest, latest:<app>, or latest:<operation>")
    receipts_parser.add_argument("--limit", type=int, help="Limit the number of results")
    receipts_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    receipts_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    receipts_parser.set_defaults(handler=run_query_receipts)


def run_status(args: Namespace) -> int:
    report = state_status(args.runtime_root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report.get("summary", ""))
        print(f"  available: {report['available']}")
        print(f"  db_path: {report['db_path']}")
        print(f"  exists: {report['exists']}")
        print(f"  schema_version_code: {report['schema_version_code']}")
        print(f"  schema_version_db: {report['schema_version_db']}")
        print(f"  needs_rebuild: {report['needs_rebuild']}")
        print(f"  needs_refresh: {report.get('needs_refresh')}")
        freshness = report.get("freshness") if isinstance(report.get("freshness"), dict) else {}
        print(f"  freshness: {freshness.get('status')}")
        counts = report.get("counts")
        if isinstance(counts, dict):
            for name, value in sorted(counts.items()):
                print(f"    {name}: {value}")
    return 0 if not report.get("needs_rebuild") else 1


def run_rebuild(args: Namespace) -> int:
    report = rebuild_state(args.runtime_root, args.manifests_dir)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("Rebuilding the local state index (writes only the local SQLite index under the runtime root; never mutates VPS state).")
        print(report.get("summary") or f"status: {report['status']}")
        print(f"  db_path: {report['db_path']}")
        for name, value in sorted(report.get("counts", {}).items()):
            print(f"    {name}: {value}")
        for warning in report.get("warnings", []):
            print(f"  ! {warning.get('code')}: {warning.get('message')}")
        for blocker in report.get("blockers", []):
            print(f"  x {blocker.get('code')}: {blocker.get('message')}")
    return 0 if report.get("status") == "ok" else 1


def run_refresh(args: Namespace) -> int:
    report = refresh_state(args.runtime_root, args.manifests_dir)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report.get("summary") or f"status: {report['status']}")
        print(f"  db_path: {report['db_path']}")
        freshness = report.get("freshness") if isinstance(report.get("freshness"), dict) else {}
        print(f"  refreshed_at: {freshness.get('refreshed_at')}")
        for name, value in sorted(report.get("counts", {}).items()):
            print(f"    {name}: {value}")
    return 0 if report.get("status") == "ok" else 1


def run_summary(args: Namespace) -> int:
    report = state_summary(args.runtime_root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report.get("summary", ""))
        freshness = report.get("freshness") if isinstance(report.get("freshness"), dict) else {}
        print(f"  freshness: {freshness.get('status')}")
        for item in report.get("apps", []) if isinstance(report.get("apps"), list) else []:
            print(
                f"  {item['app']}\troutes={item.get('route_count')}\t"
                f"receipts={item.get('receipt_count')}\tbackups={item.get('backup_count')}"
            )
    return 0 if not report.get("needs_rebuild") else 1


def run_query_receipts(args: Namespace) -> int:
    report = query_receipts(
        args.runtime_root,
        app=args.app,
        environment=args.environment,
        operation=args.operation,
        status=args.status,
        ref=args.ref,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report.get("summary", ""))
        for item in report.get("receipts", []):
            rollback = "rollback" if item.get("rollback_available") else "no-rollback"
            print(
                f"  {item['receipt_id']}\t{item.get('operation')}\t{item.get('status')}"
                f"\t{item.get('started_at')}\t{rollback}"
            )
    return 0 if not report.get("needs_rebuild") else 1


register_cli_descriptor(
    CommandDescriptor(
        command="ship state status",
        operation="state.status",
        summary="Report local runtime-state index status (read-only).",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.state_status",
        args_schema={
            "type": "object",
            "properties": {
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.state_status.v1",
        artifacts=[],
        safety_notes=["Read-only local index inspection. No mutation."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship state rebuild",
        operation="state.rebuild",
        summary="Rebuild the local runtime-state SQLite index from runtime-root files.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.state_rebuild",
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
        output_schema_ref="ophelia.state_rebuild.v1",
        artifacts=["local SQLite state index"],
        safety_notes=[
            "Writes only the local SQLite index under the runtime root; never mutates VPS state.",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship state refresh",
        operation="state.refresh",
        summary="Refresh the local state service index from runtime-root files.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.state_refresh",
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
        output_schema_ref="ophelia.state_refresh.v1",
        artifacts=["local SQLite state index"],
        safety_notes=[
            "Writes only the local SQLite index under the runtime root; never mutates VPS state.",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship state summary",
        operation="state.summary",
        summary="Read app aggregates from the local state service.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.state_summary",
        args_schema={
            "type": "object",
            "properties": {
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.state_summary.v1",
        artifacts=[],
        safety_notes=["Read-only local state service query. No file-system scan; never auto-refreshes."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship state query receipts",
        operation="state.query.receipts",
        summary="Query indexed receipts from the local runtime-state index (read-only).",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.state_query",
        args_schema={
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "environment": {"type": "string"},
                "operation": {"type": "string"},
                "status": {"type": "string"},
                "ref": {"type": "string"},
                "limit": {"type": "integer"},
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.state_query.v1",
        artifacts=[],
        safety_notes=["Read-only local index query. No mutation; never auto-rebuilds."],
    )
)
