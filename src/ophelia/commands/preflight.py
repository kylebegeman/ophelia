from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..operator_reports import preflight_report


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("preflight", help="Run a high-level preflight report")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--manifest-dir", type=Path, default=REPO_ROOT / "manifests")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    report = preflight_report(args.manifest, args.runtime_root, args.manifest_dir)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Preflight: {'pass' if report['ok'] else 'blocked'}")
        print(report.get("summary", ""))
        if report["warnings"]:
            print("Warnings:")
            for warning in report["warnings"]:
                print(f"  - {warning}")
        if report["blockers"]:
            print("Blockers:")
            for blocker in report["blockers"]:
                print(f"  - {blocker}")
        print(f"Recommended next command: {report['recommended_next_command']}")
    return 0 if report["ok"] else 1
