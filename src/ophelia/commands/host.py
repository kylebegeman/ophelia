from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..operator_reports import host_inventory


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("host", help="Inspect host facts")
    host_subparsers = parser.add_subparsers(dest="host_command")
    inventory = host_subparsers.add_parser("inventory", help="Show host inventory")
    inventory.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    inventory.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    inventory.set_defaults(handler=run_inventory)


def run_inventory(args: Namespace) -> int:
    report = host_inventory(args.runtime_root, REPO_ROOT)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Runtime root: {report['runtime_root']}")
        print(f"Ophelia commit: {report['ophelia_commit'] or 'unknown'}")
        print("Networks: " + (", ".join(report["networks"]) or "unavailable"))
    return 0
