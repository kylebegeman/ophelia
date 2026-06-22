from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..app_registry import load_app_registry, registry_conflicts
from ._output import print_error


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("apps", help="List registered apps")
    parser.add_argument("--registry", type=Path, help="Path to app registry JSON")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    try:
        entries = load_app_registry(args.registry)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc), "app_registry_invalid", json_output=args.json)
        return 1
    conflicts = registry_conflicts(entries)
    payload = {
        "apps": [
            {
                "name": entry.name,
                "environment": entry.environment,
                "compose_project": entry.compose_project,
                "root_path": entry.root_path,
                "domains": entry.domains,
                "caddy_site_file": entry.caddy_site_file,
                "container_names": entry.container_names,
                "health_urls": [check.__dict__ for check in entry.health_urls],
                "public_docker_network": entry.public_docker_network,
                "protected": entry.protected,
                "notes": entry.notes,
            }
            for entry in entries
        ],
        "conflicts": conflicts,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for entry in entries:
            protected = " protected" if entry.protected else ""
            print(f"{entry.name}\t{entry.environment}\t{entry.compose_project}\t{entry.root_path}{protected}")
        if any(conflicts.values()):
            print("Conflicts:")
            for kind, values in conflicts.items():
                for value in values:
                    print(f"  {kind}: {value}")
    return 1 if any(conflicts.values()) else 0
