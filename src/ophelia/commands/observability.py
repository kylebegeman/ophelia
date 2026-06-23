from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..config import DEFAULT_RUNTIME_ROOT
from ..observability import (
    observability_export,
    observability_plan,
    observability_schedule_run,
    observability_status,
)


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "observability", help="Read-only observability and telemetry views"
    )
    obs_subparsers = parser.add_subparsers(dest="observability_command")

    plan_parser = obs_subparsers.add_parser(
        "plan", help="Describe what would be monitored and surface config blockers"
    )
    _add_common_args(plan_parser)
    plan_parser.set_defaults(handler=run_plan)

    status_parser = obs_subparsers.add_parser(
        "status", help="Read-only observability status from local files (no probe by default)"
    )
    _add_common_args(status_parser)
    status_parser.add_argument(
        "--probe-http",
        action="store_true",
        help="Opt in to a single, timeout-bounded HTTP health probe (degrades to a warning).",
    )
    status_parser.add_argument(
        "--check-docker",
        action="store_true",
        help="Opt in to a timeout-bounded read-only `docker ps` check (degrades to a warning).",
    )
    status_parser.add_argument(
        "--http-timeout",
        type=float,
        default=5.0,
        help="HTTP probe timeout in seconds (bounded; only used with --probe-http).",
    )
    status_parser.set_defaults(handler=run_status)

    export_parser = obs_subparsers.add_parser(
        "export", help="Compact, redacted observability snapshot for operator UIs/state"
    )
    _add_common_args(export_parser)
    export_parser.set_defaults(handler=run_export)

    schedule_parser = obs_subparsers.add_parser(
        "schedule", help="Cron-friendly observability sweep commands"
    )
    schedule_subparsers = schedule_parser.add_subparsers(dest="observability_schedule_command")
    schedule_run_parser = schedule_subparsers.add_parser(
        "run", help="Run observability status for every registered manifest and write latest/run artifacts"
    )
    schedule_run_parser.add_argument("--manifests-dir", type=Path, default=None, help="Directory containing *.ophelia.yml manifests")
    schedule_run_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    schedule_run_parser.add_argument("--probe-http", action="store_true", help="Opt in to bounded HTTP health probes for configured apps")
    schedule_run_parser.add_argument("--check-docker", action="store_true", help="Opt in to bounded read-only docker ps checks")
    schedule_run_parser.add_argument("--http-timeout", type=float, default=5.0)
    schedule_run_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    schedule_run_parser.set_defaults(handler=run_schedule_run)


def _add_common_args(parser) -> None:
    parser.add_argument("--app", required=True, help="App name")
    parser.add_argument("--environment", help="Environment (dev/staging/production)")
    parser.add_argument("--manifest", type=Path, default=None, help="Explicit manifest path")
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")


def run_plan(args: Namespace) -> int:
    report = observability_plan(
        args.app,
        environment=args.environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
    )
    return _emit(report, args.json, _print_plan)


def run_status(args: Namespace) -> int:
    report = observability_status(
        args.app,
        environment=args.environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
        probe_http=args.probe_http,
        check_docker=args.check_docker,
        http_timeout=args.http_timeout,
    )
    return _emit(report, args.json, _print_status)


def run_export(args: Namespace) -> int:
    report = observability_export(
        args.app,
        environment=args.environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
    )
    return _emit(report, args.json, _print_export)


def run_schedule_run(args: Namespace) -> int:
    from ..config import REPO_ROOT

    report = observability_schedule_run(
        runtime_root=args.runtime_root,
        manifests_dir=args.manifests_dir or REPO_ROOT / "manifests",
        probe_http=args.probe_http,
        check_docker=args.check_docker,
        http_timeout=args.http_timeout,
    )
    return _emit(report, args.json, _print_schedule_run)


def _emit(report, as_json: bool, printer) -> int:
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        printer(report)
    return 0 if report.get("status") != "blocked" else 1


def _print_status(report) -> None:
    print(report.get("summary", ""))
    health = report.get("health", {})
    metrics = report.get("metrics", {})
    backups = report.get("backups", {})
    receipts = report.get("receipts_failures", {})
    print(f"  health: configured={health.get('configured')} probe={health.get('probe')}")
    print(f"  metrics: configured={metrics.get('configured')} format={metrics.get('format')} auth={metrics.get('auth')}")
    print(f"  backups: freshness={backups.get('freshness')} count={backups.get('backup_count')}")
    print(f"  receipt_failures: {receipts.get('count')}")
    for blocker in report.get("blockers", []):
        print(f"  ! {blocker.get('code')}: {blocker.get('message')}")
    for warning in report.get("warnings", []):
        print(f"  ~ {warning.get('code')}: {warning.get('message')}")


def _print_plan(report) -> None:
    print(report.get("summary", ""))
    for monitor in report.get("monitors", []):
        print(f"  monitor: {monitor.get('type')}")
    for blocker in report.get("blockers", []):
        print(f"  ! {blocker.get('code')}: {blocker.get('message')}")
    for warning in report.get("warnings", []):
        print(f"  ~ {warning.get('code')}: {warning.get('message')}")


def _print_export(report) -> None:
    print(report.get("summary", ""))
    snapshot = report.get("snapshot", {})
    for key in sorted(snapshot):
        print(f"  {key}: {snapshot[key]}")


def _print_schedule_run(report) -> None:
    print(report.get("summary", ""))
    totals = report.get("totals") if isinstance(report.get("totals"), dict) else {}
    print(
        "  apps: "
        f"{totals.get('app_count', 0)} checked, "
        f"{totals.get('blocked', 0)} blocked, "
        f"{totals.get('warning', 0)} warning"
    )
    for blocker in report.get("blockers", []):
        print(f"  ! {blocker.get('code')}: {blocker.get('message')}")
    for warning in report.get("warnings", []):
        print(f"  ~ {warning.get('code')}: {warning.get('message')}")


_COMMON_ARGS_SCHEMA = {
    "app": {"type": "string"},
    "environment": {"type": "string"},
    "manifest": {"type": "string"},
    "runtime_root": {"type": "string"},
    "json": {"type": "boolean"},
}


register_cli_descriptor(
    CommandDescriptor(
        command="ship observability plan",
        operation="observability.plan",
        summary="Describe what would be monitored for an app and surface observability config blockers (read-only).",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.observability_plan",
        args_schema={
            "type": "object",
            "properties": dict(_COMMON_ARGS_SCHEMA),
            "required": ["app"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.observability_plan.v1",
        artifacts=[],
        safety_notes=["Read-only. No network/Docker call and no mutation; secret values redacted."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship observability status",
        operation="observability.status",
        summary="Read-only observability status from local files; HTTP/Docker probes are opt-in and timeout-bounded.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.observability_status",
        args_schema={
            "type": "object",
            "properties": {
                **_COMMON_ARGS_SCHEMA,
                "probe_http": {"type": "boolean"},
                "check_docker": {"type": "boolean"},
                "http_timeout": {"type": "number"},
            },
            "required": ["app"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.observability_status.v1",
        artifacts=[],
        safety_notes=[
            "Read-only. No network/Docker call by default; --probe-http/--check-docker are opt-in, "
            "timeout-bounded, and degrade to warnings. Only summaries (never logs) are emitted; secrets redacted.",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship observability export",
        operation="observability.export",
        summary="Compact, redacted observability snapshot suitable for operator UI/state (no unbounded logs).",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.observability_export",
        args_schema={
            "type": "object",
            "properties": dict(_COMMON_ARGS_SCHEMA),
            "required": ["app"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.observability_export.v1",
        artifacts=[],
        safety_notes=["Read-only summary snapshot. No mutation; summaries only; secrets redacted."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship observability schedule run",
        operation="observability.schedule.run",
        summary="Run a cron-friendly observability sweep across registered manifests and write latest/run artifacts.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.observability_schedule_run",
        args_schema={
            "type": "object",
            "properties": {
                "manifests_dir": {"type": "string"},
                "runtime_root": {"type": "string"},
                "probe_http": {"type": "boolean"},
                "check_docker": {"type": "boolean"},
                "http_timeout": {"type": "number"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.observability_schedule_run.v1",
        artifacts=["runtime_root/observability/runs/*.json", "runtime_root/observability/latest.json"],
        safety_notes=[
            "Writes local observability run artifacts only.",
            "No network/Docker call by default; probes are opt-in and timeout-bounded.",
        ],
    )
)
