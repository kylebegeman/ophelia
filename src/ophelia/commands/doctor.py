from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..inspection import doctor_report


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("doctor", help="Check local Ophelia host readiness")
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT,
        help="Runtime root to inspect",
    )
    parser.add_argument(
        "--ophelia-root",
        type=Path,
        default=REPO_ROOT,
        help="Ophelia checkout root",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    report = doctor_report(args.runtime_root, args.ophelia_root, args.ophelia_root / "manifests")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1
    print(f"Doctor: {'ok' if report['ok'] else 'blocked'}")
    for check in report["checks"]:
        marker = "ok" if check["ok"] else "fail"
        if check.get("warning"):
            marker = "warn"
        print(f"  [{marker}] {check['name']}: {check['message']}")

    if report["warnings"]:
        print("Warnings:")
        for warning in report["warnings"]:
            print(f"  - {warning}")
    if report["blockers"]:
        print("Blockers:")
        for blocker in report["blockers"]:
            print(f"  - {blocker}")
    return 0 if report["ok"] else 1
