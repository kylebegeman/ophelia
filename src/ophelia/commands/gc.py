from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT
from ..gc import apply_gc, gc_plan


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("gc", help="Plan or apply safe runtime cleanup")
    gc_subparsers = parser.add_subparsers(dest="gc_command")
    plan_parser = gc_subparsers.add_parser("plan")
    plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    plan_parser.add_argument("--json", action="store_true", help="Accepted for command consistency; output is JSON.")
    plan_parser.set_defaults(handler=run_plan)

    apply_parser = gc_subparsers.add_parser("apply")
    apply_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    apply_parser.add_argument("--confirm", required=True)
    apply_parser.add_argument("--json", action="store_true", help="Accepted for command consistency; output is JSON.")
    apply_parser.set_defaults(handler=run_apply)


def run_plan(args: Namespace) -> int:
    print(json.dumps(gc_plan(args.runtime_root), indent=2, sort_keys=True))
    return 0


def run_apply(args: Namespace) -> int:
    try:
        report = apply_gc(args.runtime_root, args.confirm)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
