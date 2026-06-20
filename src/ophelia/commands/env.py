from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT
from ..portability import env_shape_diff_report


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("env", help="Inspect redacted env shape")
    env_subparsers = parser.add_subparsers(dest="env_command")

    diff = env_subparsers.add_parser("diff", help="Compare required and runtime env keys")
    diff.add_argument("app", help="App id")
    diff.add_argument("--environment", choices=["dev", "staging", "production"])
    diff.add_argument("--manifest", type=Path, help="Path to app .ophelia manifest")
    diff.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    diff.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    diff.set_defaults(handler=run_diff)


def run_diff(args: Namespace) -> int:
    report = env_shape_diff_report(
        args.app,
        environment=args.environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        for item in report["entries"]:
            print(f"  {item['key']}: {item['status']}")
    return 0 if not report["blockers"] else 1
