from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT
from ..runtime import list_releases, load_release


def register(subparsers: _SubParsersAction) -> None:
    releases_parser = subparsers.add_parser("releases", help="List release records for an app")
    releases_parser.add_argument("app", help="App id")
    releases_parser.add_argument(
        "--runtime-root",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT,
        help="Runtime root to inspect",
    )
    releases_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    releases_parser.set_defaults(handler=run_releases)

    release_parser = subparsers.add_parser("release", help="Inspect one release record")
    release_subparsers = release_parser.add_subparsers(dest="release_command")
    show_parser = release_subparsers.add_parser("show", help="Show a release record")
    show_parser.add_argument("app", help="App id")
    show_parser.add_argument("release_id", help="Release id or 'current'")
    show_parser.add_argument(
        "--runtime-root",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT,
        help="Runtime root to inspect",
    )
    show_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    show_parser.set_defaults(handler=run_release_show)


def run_releases(args: Namespace) -> int:
    records = list_releases(args.runtime_root, args.app)
    if args.json:
        print(json.dumps({"app": args.app, "releases": records}, indent=2, sort_keys=True))
        return 0
    if not records:
        print(f"No releases found for {args.app} in {args.runtime_root}")
        return 0

    print("RELEASE ID\tDEPLOYED AT\tENVIRONMENT\tVERIFY\tMANIFEST HASH")
    for record in records:
        verification = record.get("verification")
        if isinstance(verification, dict):
            verify_status = str(verification.get("status") or verification.get("ok") or "unknown")
        else:
            verify_status = "unknown"
        print(
            "\t".join(
                [
                    str(record.get("release_id", "")),
                    str(record.get("deployed_at", "")),
                    str(record.get("environment") or "unknown"),
                    verify_status,
                    str(record.get("manifest_hash") or "")[:12],
                ]
            )
        )
    return 0


def run_release_show(args: Namespace) -> int:
    try:
        record = load_release(args.runtime_root, args.app, args.release_id)
    except FileNotFoundError as exc:
        print(str(exc))
        return 1

    print(json.dumps(record, indent=2, sort_keys=True))
    return 0
