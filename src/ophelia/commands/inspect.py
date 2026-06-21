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
                print(f"  - {_describe_conflict(conflict)}")
        if report["warnings"]:
            print("Warnings:")
            for warning in report["warnings"]:
                print(f"  - {_describe_conflict(warning)}")
    return 0 if report["ok"] else 1


def _describe_conflict(item: dict) -> str:
    """Human one-liner for a conflict/warning item (never a raw dict repr)."""
    kind = item.get("type")
    identity = (
        item.get("domain")
        or item.get("app")
        or item.get("alias")
        or item.get("host_port")
        or item.get("path")
        or ""
    )
    owners = sorted({str(o.get("app")) for o in item.get("owners", []) if isinstance(o, dict) and o.get("app")})
    line = f"{kind}: {identity}".rstrip(": ")
    if owners:
        line = f"{line} claimed by {', '.join(owners)}"
    return line
