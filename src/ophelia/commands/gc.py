from __future__ import annotations

from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT
from ..gc import apply_gc, gc_plan
from ._output import print_error, print_json


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("gc", help="Plan or apply safe runtime cleanup")
    gc_subparsers = parser.add_subparsers(dest="gc_command")
    plan_parser = gc_subparsers.add_parser("plan")
    plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    plan_parser.set_defaults(handler=run_plan)

    apply_parser = gc_subparsers.add_parser("apply")
    apply_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    apply_parser.add_argument("--confirm", required=True)
    apply_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    apply_parser.set_defaults(handler=run_apply)


def run_plan(args: Namespace) -> int:
    plan = gc_plan(args.runtime_root)
    if args.json:
        print_json(plan)
    else:
        print(plan["summary"])
        print(f"Confirmation token: {plan['confirmation_token']}")
        for item in plan["candidates"]:
            print(f"  - {item['path']} ({item['reason']})")
    return 0


def run_apply(args: Namespace) -> int:
    try:
        report = apply_gc(args.runtime_root, args.confirm)
    except ValueError as exc:
        print_error(str(exc), "gc_confirmation_failed", json_output=args.json)
        return 1
    if args.json:
        print_json(report)
    else:
        print(report["summary"])
        for path in report["deleted"]:
            print(f"  - {path}")
    return 0
