from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT
from ..rollback import apply_rollback, rollback_plan


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("rollback", help="Plan or apply a release rollback")
    rollback_subparsers = parser.add_subparsers(dest="rollback_command")

    plan_parser = rollback_subparsers.add_parser("plan", help="Plan a rollback without mutating runtime state")
    plan_parser.add_argument("app", help="App id")
    plan_parser.add_argument("release_id", help="Release id to restore")
    plan_parser.add_argument(
        "--runtime-root",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT,
        help="Runtime root to inspect",
    )
    plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    plan_parser.set_defaults(handler=run_plan)

    apply_parser = rollback_subparsers.add_parser("apply", help="Apply a planned rollback")
    apply_parser.add_argument("app", help="App id")
    apply_parser.add_argument("release_id", help="Release id to restore")
    apply_parser.add_argument("--confirm", required=True, help="Confirmation token from rollback plan")
    apply_parser.add_argument(
        "--runtime-root",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT,
        help="Runtime root to mutate",
    )
    apply_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    apply_parser.set_defaults(handler=run_apply)


def run_plan(args: Namespace) -> int:
    try:
        plan = rollback_plan(args.runtime_root, args.app, args.release_id)
    except FileNotFoundError as exc:
        print(str(exc))
        return 1

    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0 if plan["can_apply"] else 1


def run_apply(args: Namespace) -> int:
    try:
        report = apply_rollback(args.runtime_root, args.app, args.release_id, args.confirm)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"Rollback failed: {exc}")
        return 1

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
