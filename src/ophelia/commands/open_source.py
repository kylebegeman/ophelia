from __future__ import annotations

from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..config import REPO_ROOT
from ..open_source_readiness import OPEN_SOURCE_AUDIT_KIND, open_source_audit_report
from ._output import print_json


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("open-source", help="Open-source release readiness tools")
    open_source_subparsers = parser.add_subparsers(dest="open_source_command")

    audit_parser = open_source_subparsers.add_parser(
        "audit",
        help="Scan the tracked public surface for open-source release blockers",
    )
    audit_parser.add_argument("--root", type=Path, default=REPO_ROOT, help="Repository root to scan")
    audit_parser.add_argument("--max-findings", type=int, default=50, help="Maximum findings to include in output")
    audit_parser.add_argument("--allow-blocked", action="store_true", help="Return success even when blockers are present")
    audit_parser.add_argument("--fail-on-warnings", action="store_true", help="Return failure when warning findings are present")
    audit_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    audit_parser.set_defaults(handler=run_open_source_audit)


def run_open_source_audit(args: Namespace) -> int:
    report = open_source_audit_report(root=args.root, max_findings=args.max_findings)
    if args.json:
        print_json(report)
    else:
        print(report.get("summary", "Open-source readiness report."))
        print(f"Status: {report.get('status')} ({report.get('go_no_go')})")
        print(f"Files scanned: {report.get('file_count')}")
        print(f"Findings: {report.get('finding_count')} ({report.get('blocker_count')} blocker, {report.get('warning_count')} warning)")
        _print_findings("Blockers", report.get("blockers", []))
        _print_findings("Warnings", report.get("warnings", []))
        if report.get("findings_truncated"):
            print(f"Findings truncated at {report.get('max_findings')}; rerun with --max-findings for more detail.")
        print(str(report.get("recommendation") or ""))
    if report.get("status") == "blocked" and not args.allow_blocked:
        return 1
    if args.fail_on_warnings and report.get("warning_count"):
        return 1
    return 0


def _print_findings(label: str, value: object) -> None:
    items = value if isinstance(value, list) else []
    if not items:
        print(f"{label}: none")
        return
    print(f"{label}:")
    for item in items:
        if not isinstance(item, dict):
            print(f"  - {item}")
            continue
        location = str(item.get("path") or "unknown")
        if item.get("line") is not None:
            location = f"{location}:{item.get('line')}"
        print(f"  - {item.get('code')} at {location}: {item.get('message')}")


register_cli_descriptor(
    CommandDescriptor(
        command="ship open-source audit",
        operation="open_source.audit",
        summary="Scan the tracked public surface for open-source release blockers and governance gaps.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind=OPEN_SOURCE_AUDIT_KIND,
        args_schema={
            "type": "object",
            "properties": {
                "root": {"type": "string"},
                "max_findings": {"type": "integer"},
                "allow_blocked": {"type": "boolean"},
                "fail_on_warnings": {"type": "boolean"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.open_source_audit_report.v1",
        artifacts=[],
        examples=[
            "ship open-source audit --json",
            "ship open-source audit --allow-blocked --json",
            "ship open-source audit --fail-on-warnings --json",
        ],
        safety_notes=[
            "Read-only release hygiene scan. Does not mutate files, runtime state, providers, or Git history.",
            "Use --allow-blocked for current-state reporting before the repository is public-safe.",
        ],
    )
)
