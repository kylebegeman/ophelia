from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..conflicts import scan_conflicts


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("inspect", help="Inspect platform-level manifest state")
    inspect_subparsers = parser.add_subparsers(dest="inspect_command")

    conflicts_parser = inspect_subparsers.add_parser("conflicts", help="Scan manifests for platform conflicts")
    conflicts_parser.add_argument("--manifest-dir", type=Path, default=REPO_ROOT / "manifests")
    conflicts_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    conflicts_parser.add_argument("--include-runtime", action="store_true", help="Include active runtime manifest locks")
    conflicts_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    conflicts_parser.set_defaults(handler=run_conflicts)


def run_conflicts(args: Namespace) -> int:
    report = scan_conflicts(args.manifest_dir, runtime_root=args.runtime_root if args.include_runtime else None)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        if report["conflicts"]:
            print("Conflicts:")
            for conflict in report["conflicts"]:
                print(f"  - {conflict['type']}: {conflict}")
        if report["warnings"]:
            print("Warnings:")
            for warning in report["warnings"]:
                print(f"  - {warning['type']}: {warning}")
    return 0 if report["ok"] else 1
