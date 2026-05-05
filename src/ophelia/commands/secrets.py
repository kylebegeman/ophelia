from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT
from ..manifest import ManifestError, load_manifest
from ..operator_reports import secrets_required


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("secrets", help="Inspect secret requirements")
    secret_subparsers = parser.add_subparsers(dest="secrets_command")
    required = secret_subparsers.add_parser("required", help="Report required keys for a manifest")
    required.add_argument("manifest", type=Path)
    required.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    required.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    required.set_defaults(handler=run_required)


def run_required(args: Namespace) -> int:
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        print(f"Manifest invalid: {exc}")
        return 1
    report = secrets_required(manifest, args.manifest, args.runtime_root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for item in report["requirements"]:
            print(
                f"{item['key']}\tpresent={item['present']}\t"
                f"missing={item['missing']}\tplaceholder={item['placeholder_detected']}"
            )
    return 0
