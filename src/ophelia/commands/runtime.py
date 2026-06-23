from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT
from ..operator_reports import runtime_ownership


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("runtime", help="Inspect runtime ownership")
    runtime_subparsers = parser.add_subparsers(dest="runtime_command")
    ownership = runtime_subparsers.add_parser("ownership", help="Report file ownership for an app")
    ownership.add_argument("app")
    ownership.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    ownership.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    ownership.set_defaults(handler=run_ownership)


def run_ownership(args: Namespace) -> int:
    report = runtime_ownership(args.runtime_root, args.app)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Runtime ownership for {args.app}: {report['runtime_path']}")
        print("Generated files:")
        for item in report["ophelia_generated_files"]:
            print(f"  - {item}")
    return 0
