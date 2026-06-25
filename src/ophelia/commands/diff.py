from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT
from ..manifest import ManifestError, load_manifest
from ..planning import bundle_diff, deploy_plan


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("diff", help="Compare desired rendered state with local runtime state")
    parser.add_argument("manifest", type=Path, help="Path to the .ophelia manifest")
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT,
        help="Runtime root to inspect",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        print(f"Manifest invalid: {exc}")
        return 1

    diff = bundle_diff(manifest, args.runtime_root)
    plan = deploy_plan(manifest, args.manifest, args.runtime_root)
    payload = {"summary": plan["summary"], **diff}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(plan["summary"])
        print(f"Runtime path: {diff['runtime_path']}")
        print("Changed files:")
        for item in diff["changed_files"]:
            print(f"  - {item['path']} ({item['change']})")
        if not diff["changed_files"]:
            print("  none")
        print("Removed files:")
        for item in diff["removed_files"]:
            print(f"  - {item['path']}")
        if not diff["removed_files"]:
            print("  none")
    return 0
