from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..drift import all_drift, manifest_drift
from ..manifest import ManifestError, load_manifest


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("drift", help="Detect runtime drift from desired manifest-rendered state")
    parser.add_argument("target", help="Manifest path or 'all'")
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--manifest-dir", type=Path, default=REPO_ROOT / "manifests")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    if args.target == "all":
        report = all_drift(args.manifest_dir, args.runtime_root)
    else:
        manifest_path = Path(args.target)
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError as exc:
            print(f"Manifest invalid: {exc}")
            return 1
        report = manifest_drift(manifest, manifest_path, args.runtime_root)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif args.target == "all":
        print(f"Drift: {'yes' if report['drift'] else 'no'}")
        for item in report["reports"]:
            print(f"  - {item['summary']}")
        for error in report["errors"]:
            print(f"  - {error['path']}: {error['error']}")
    else:
        print(report["summary"])
        print("Changed files:")
        for item in report["rendered"]["changed_files"]:
            print(f"  - {item['path']} ({item['change']})")
        if not report["rendered"]["changed_files"]:
            print("  none")
        print("Missing env keys: " + (", ".join(report["env"]["missing_keys"]) or "none"))
        print("Placeholder env keys: " + (", ".join(report["env"]["placeholder_keys"]) or "none"))

    return 1 if report["drift"] else 0
