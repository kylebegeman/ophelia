from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..operations import list_operations, run_operation


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("operations", help="List or run explicit operation templates")
    operations_subparsers = parser.add_subparsers(dest="operations_command")
    list_parser = operations_subparsers.add_parser("list")
    list_parser.add_argument("--json", action="store_true", help="Accepted for command consistency; output is JSON.")
    list_parser.set_defaults(handler=run_list)

    run_parser = operations_subparsers.add_parser("run")
    run_parser.add_argument("name")
    run_parser.add_argument("--manifest-dir", type=Path, default=REPO_ROOT / "manifests")
    run_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--confirm")
    run_parser.add_argument("--json", action="store_true", help="Accepted for command consistency; output is JSON.")
    run_parser.set_defaults(handler=run_template)


def run_list(args: Namespace) -> int:
    print(json.dumps(list_operations(), indent=2, sort_keys=True))
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
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
