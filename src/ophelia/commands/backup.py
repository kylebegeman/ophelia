from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..backup import apply_restore, backup_plan, create_backup, restore_plan
from ..config import DEFAULT_RUNTIME_ROOT
from ..portability import backup_status_report


def register(subparsers: _SubParsersAction) -> None:
    backup_parser = subparsers.add_parser("backup", help="Plan or create app backups")
    backup_subparsers = backup_parser.add_subparsers(dest="backup_command")
    plan_parser = backup_subparsers.add_parser("plan", help="Plan an app backup")
    plan_parser.add_argument("app", help="App id")
    plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    plan_parser.set_defaults(handler=run_backup_plan)

    status_parser = backup_subparsers.add_parser("status", help="Report backup freshness and coverage")
    status_parser.add_argument("app", help="App id")
    status_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    status_parser.add_argument("--manifest", type=Path, help="Path to app .ophelia manifest")
    status_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    status_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    status_parser.set_defaults(handler=run_backup_status)

    create_parser = backup_subparsers.add_parser("create", help="Create an app backup")
    create_parser.add_argument("app", help="App id")
    create_parser.add_argument("--confirm", required=True, help="Confirmation token from backup plan")
    create_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    create_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    create_parser.set_defaults(handler=run_backup_create)

    restore_parser = subparsers.add_parser("restore", help="Plan or apply restore previews")
    restore_subparsers = restore_parser.add_subparsers(dest="restore_command")
    restore_plan_parser = restore_subparsers.add_parser("plan", help="Plan a restore preview")
    restore_plan_parser.add_argument("app", help="App id")
    restore_plan_parser.add_argument("backup_id", help="Backup id")
    restore_plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    restore_plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    restore_plan_parser.set_defaults(handler=run_restore_plan)

    restore_apply_parser = restore_subparsers.add_parser("apply", help="Create a restore preview")
    restore_apply_parser.add_argument("app", help="App id")
    restore_apply_parser.add_argument("backup_id", help="Backup id")
    restore_apply_parser.add_argument("--confirm", required=True, help="Confirmation token from restore plan")
    restore_apply_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    restore_apply_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    restore_apply_parser.set_defaults(handler=run_restore_apply)


def run_backup_plan(args: Namespace) -> int:
    plan = backup_plan(args.runtime_root, args.app)
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0 if plan["can_apply"] else 1


def run_backup_status(args: Namespace) -> int:
    report = backup_status_report(
        args.app,
        environment=args.environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        latest = report.get("latest_backup")
        print(f"Latest backup: {latest.get('backup_id') if isinstance(latest, dict) else 'none'}")
        if report["blockers"]:
            print("Blockers:")
            for blocker in report["blockers"]:
                print(f"  - {blocker['message']}")
    return 0 if not report["blockers"] else 1


def run_backup_create(args: Namespace) -> int:
    try:
        report = create_backup(args.runtime_root, args.app, args.confirm)
    except RuntimeError as exc:
        print(f"Backup failed: {exc}")
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def run_restore_plan(args: Namespace) -> int:
    plan = restore_plan(args.runtime_root, args.app, args.backup_id)
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0 if plan["can_apply"] else 1


def run_restore_apply(args: Namespace) -> int:
    try:
        report = apply_restore(args.runtime_root, args.app, args.backup_id, args.confirm)
    except RuntimeError as exc:
        print(f"Restore failed: {exc}")
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
