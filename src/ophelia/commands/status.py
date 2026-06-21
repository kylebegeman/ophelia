from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..inspection import status_report


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("status", help="Inspect the local Ophelia runtime")
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
    report = status_report(args.runtime_root, args.ophelia_root, args.ophelia_root / "manifests")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    print(f"Runtime root: {report['runtime_root']}")
    print(f"Runtime exists: {'yes' if report['runtime_root_exists'] else 'no'}")
    print(f"Docker: {report['docker'].get('version') or 'unavailable'}")
    print(f"Docker compose: {report['docker'].get('compose_version') or 'unavailable'}")

    disk = report["disk_usage"]
    print(
        "Disk: "
        f"{disk['percent_used']}% used, "
        f"{disk['free_bytes']} bytes free at {disk['path']}"
    )

    apps = report["known_apps"]
    if apps:
        print("Apps:")
        for app in apps:
            caddy = app["caddy"]
            print(
                "  "
                f"{app['app']} release={app.get('release_id') or 'unknown'} "
                f"kind={app['kind']} environment={app.get('environment') or 'unknown'} "
                f"caddy_active={'yes' if caddy['active'] else 'no'} "
                f"caddy_synced={'yes' if caddy['synced'] else 'no'}"
            )
    else:
        print("Apps: none")

    print("Docker networks: " + (", ".join(report["docker_networks"]) or "unavailable"))
    shared = report["shared_services"]
    print(f"Shared services: {'available' if shared.get('available') else shared.get('reason', 'unavailable')}")

    if report["warnings"]:
        print("Warnings:")
        for warning in report["warnings"]:
            print(f"  - {warning}")
    return 0
