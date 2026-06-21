from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT
from ..portability import receipt_list_report, receipt_show_report


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("receipts", help="Browse Ophelia operation receipts")
    receipt_subparsers = parser.add_subparsers(dest="receipts_command")

    list_parser = receipt_subparsers.add_parser("list", help="List receipts")
    list_parser.add_argument("--app", help="Filter by app id")
    list_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    list_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    list_parser.set_defaults(handler=run_list)

    show_parser = receipt_subparsers.add_parser("show", help="Show one receipt")
    show_parser.add_argument("receipt_id")
    show_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    show_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    show_parser.set_defaults(handler=run_show)


def run_list(args: Namespace) -> int:
    report = receipt_list_report(args.runtime_root, app=args.app, environment=args.environment)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        for item in report["receipts"]:
            print(f"  {item['receipt_id']}\t{item.get('operation')}\t{item.get('status')}\t{item.get('path')}")
    return 0


def run_show(args: Namespace) -> int:
    report = receipt_show_report(args.receipt_id, runtime_root=args.runtime_root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        if report["blockers"]:
            for blocker in report["blockers"]:
                print(f"  - {blocker['message']}")
        else:
            print(json.dumps(report["receipt"], indent=2, sort_keys=True))
    return 0 if not report["blockers"] else 1
