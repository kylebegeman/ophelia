from __future__ import annotations

from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..operations import list_operations, run_operation
from ._output import print_error, print_json


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("operations", help="List or run explicit operation templates")
    operations_subparsers = parser.add_subparsers(dest="operations_command")
    list_parser = operations_subparsers.add_parser("list")
    list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    list_parser.set_defaults(handler=run_list)

    run_parser = operations_subparsers.add_parser("run")
    run_parser.add_argument("name")
    run_parser.add_argument("--manifest-dir", type=Path, default=REPO_ROOT / "manifests")
    run_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--confirm")
    run_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    run_parser.set_defaults(handler=run_template)


def run_list(args: Namespace) -> int:
    report = list_operations()
    if args.json:
        print_json(report)
    else:
        print(report["summary"])
        for item in report["operations"]:
            print(f"  {item['name']}: {len(item['steps'])} step(s)")
    return 0


def run_template(args: Namespace) -> int:
    try:
        report = run_operation(
            args.name,
            None if args.dry_run or not args.confirm else args.confirm,
            args.manifest_dir,
            args.runtime_root,
        )
    except ValueError as exc:
        print_error(str(exc), "operation_failed", json_output=args.json)
        return 1
    if args.json:
        print_json(report)
    else:
        print(report.get("summary") or f"Operation {args.name} ready.")
        token = report.get("confirmation_token")
        if token:
            print(f"Confirmation token: {token}")
        report_path = report.get("report_path")
        if report_path:
            print(f"Report: {report_path}")
    return 0
