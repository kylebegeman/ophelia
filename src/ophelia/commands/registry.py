from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..operator_reports import manifest_registry, release_registry


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("registry", help="Inspect manifest and release registries")
    registry_subparsers = parser.add_subparsers(dest="registry_command")

    manifests = registry_subparsers.add_parser("manifests", help="List known manifests")
    manifests.add_argument("--manifest-dir", type=Path, default=REPO_ROOT / "manifests")
    manifests.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    manifests.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    manifests.set_defaults(handler=run_manifests)

    releases = registry_subparsers.add_parser("releases", help="List release history across apps")
    releases.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    releases.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    releases.set_defaults(handler=run_releases)


def run_manifests(args: Namespace) -> int:
    report = manifest_registry(args.manifest_dir, args.runtime_root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for item in report["manifests"]:
            print(f"{item['app']}\t{item['environment'] or 'unknown'}\t{item['kind']}\t{item['manifest_path']}")
    return 0 if not report["errors"] else 1


def run_releases(args: Namespace) -> int:
    report = release_registry(args.runtime_root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for item in report["releases"]:
            print(f"{item['app']}\t{item['environment'] or 'unknown'}\t{item['release_id']}")
    return 0
