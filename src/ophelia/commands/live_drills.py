from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..live_drills import (
    DEFAULT_LIVE_DRILL_PROFILES,
    LIVE_DRILL_PROFILES_KIND,
    LIVE_DRILL_RESULT_KIND,
    LIVE_DRILL_RUNS_KIND,
    list_live_drill_profiles,
    run_all_live_drills,
    run_live_drill,
)


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("live-drills", help="Run read-only live-readiness drill profiles")
    drill_subparsers = parser.add_subparsers(dest="live_drills_command")

    list_parser = drill_subparsers.add_parser("list", help="List live drill profiles")
    list_parser.add_argument("--profiles", type=Path, default=DEFAULT_LIVE_DRILL_PROFILES)
    list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    list_parser.set_defaults(handler=run_list)

    run_parser = drill_subparsers.add_parser("run", help="Run one live drill profile")
    run_parser.add_argument("profile", help="Profile id")
    run_parser.add_argument("--profiles", type=Path, default=DEFAULT_LIVE_DRILL_PROFILES)
    run_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    run_parser.set_defaults(handler=run_one)

    run_all_parser = drill_subparsers.add_parser("run-all", help="Run every live drill profile")
    run_all_parser.add_argument("--profiles", type=Path, default=DEFAULT_LIVE_DRILL_PROFILES)
    run_all_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    run_all_parser.set_defaults(handler=run_all)


def run_list(args: Namespace) -> int:
    report = list_live_drill_profiles(args.profiles)
    _print_report(report, json_output=args.json)
    return 0 if report.get("status") != "blocked" else 1


def run_one(args: Namespace) -> int:
    report = run_live_drill(args.profile, args.profiles)
    _print_report(report, json_output=args.json)
    return 0 if report.get("status") != "blocked" else 1


def run_all(args: Namespace) -> int:
    report = run_all_live_drills(args.profiles)
    _print_report(report, json_output=args.json)
    return 0 if report.get("status") != "blocked" else 1


def _print_report(report: dict, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    print(report.get("summary", "Live drills report."))
    print(f"Status: {report.get('status')}")
    profiles = report.get("profiles") if isinstance(report.get("profiles"), list) else []
    for profile in profiles:
        if isinstance(profile, dict):
            print(f"  - {profile.get('id')}: {profile.get('summary') or profile.get('title')}")
    results = report.get("results") if isinstance(report.get("results"), list) else []
    for result in results:
        if isinstance(result, dict):
            profile = result.get("profile") if isinstance(result.get("profile"), dict) else {}
            print(f"  - {profile.get('id')}: {result.get('status')}")
    _print_items("Blockers", report.get("blockers", []))
    _print_items("Warnings", report.get("warnings", []))


def _print_items(label: str, value: object) -> None:
    items = value if isinstance(value, list) else []
    if not items:
        print(f"{label}: none")
        return
    print(f"{label}:")
    for item in items:
        if isinstance(item, dict):
            print(f"  - {item.get('code')}: {item.get('message')}")
        else:
            print(f"  - {item}")


register_cli_descriptor(
    CommandDescriptor(
        command="ship live-drills list",
        operation="live_drills.list",
        summary="List read-only live-readiness drill profiles from a profile file.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind=LIVE_DRILL_PROFILES_KIND,
        args_schema={
            "type": "object",
            "properties": {
                "profiles": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.live_drill_profiles.v1",
        artifacts=[],
        examples=[
            "ship live-drills list --json",
            "ship live-drills list --profiles fixtures/app-suite/live-drills.yml --json",
        ],
        safety_notes=["Read-only profile metadata. Does not run probes or child reports."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship live-drills run",
        operation="live_drills.run",
        summary="Run one read-only live-readiness drill profile and validate its expected state.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind=LIVE_DRILL_RESULT_KIND,
        args_schema={
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "profiles": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["profile"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.live_drill_result.v1",
        artifacts=[],
        examples=[
            "ship live-drills run fixture-suite-review --json",
            "ship live-drills run fixture-postgres-focused --profiles fixtures/app-suite/live-drills.yml --json",
        ],
        safety_notes=[
            "Read-only drill. Uses live-readiness and optional hardening reports without mutation.",
            "Profiles must opt into HTTP or Docker probes; fixture profiles leave them disabled.",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship live-drills run-all",
        operation="live_drills.run_all",
        summary="Run every read-only live-readiness drill profile and aggregate expected-state results.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind=LIVE_DRILL_RUNS_KIND,
        args_schema={
            "type": "object",
            "properties": {
                "profiles": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.live_drill_run_results.v1",
        artifacts=[],
        examples=[
            "ship live-drills run-all --json",
            "ship live-drills run-all --profiles fixtures/app-suite/live-drills.yml --json",
        ],
        safety_notes=["Read-only drill aggregate. Does not mutate runtime, providers, workflows, or state indexes."],
    )
)
