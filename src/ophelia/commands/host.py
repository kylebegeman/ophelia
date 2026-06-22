from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..host_inventory import collect_host_inventory, host_readiness


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("host", help="Inspect host facts")
    host_subparsers = parser.add_subparsers(dest="host_command")
    inventory = host_subparsers.add_parser("inventory", help="Show host inventory")
    inventory.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    inventory.add_argument("--manifests-dir", type=Path, default=REPO_ROOT / "manifests")
    inventory.add_argument("--config", type=Path, help="Optional host inventory config JSON/YAML")
    inventory.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    inventory.set_defaults(handler=run_inventory)

    readiness = host_subparsers.add_parser("readiness", help="Check host readiness from inventory")
    readiness.add_argument("host_id", nargs="?", help="Optional host id to check")
    readiness.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    readiness.add_argument("--manifests-dir", type=Path, default=REPO_ROOT / "manifests")
    readiness.add_argument("--config", type=Path, help="Optional host inventory config JSON/YAML")
    readiness.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    readiness.set_defaults(handler=run_readiness)


def run_inventory(args: Namespace) -> int:
    report = collect_host_inventory(args.runtime_root, REPO_ROOT, args.manifests_dir, args.config)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Runtime root: {report['runtime_root']}")
        print(f"Hosts: {len(report.get('hosts', []))}")
        for host in report.get("hosts", []):
            if isinstance(host, dict):
                print(f"  - {host.get('id')}: {host.get('provider')} {host.get('region')} ({host.get('architecture')})")
        _print_items("Blockers", report.get("blockers", []))
        _print_items("Warnings", report.get("warnings", []))
    return 0 if not report.get("blockers") else 1


def run_readiness(args: Namespace) -> int:
    report = host_readiness(args.host_id, args.runtime_root, REPO_ROOT, args.manifests_dir, args.config)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        for host in report.get("hosts", []):
            if isinstance(host, dict):
                print(f"  - {host.get('host_id')}: {host.get('status')}")
        _print_items("Blockers", report.get("blockers", []))
        _print_items("Warnings", report.get("warnings", []))
    return 0 if not report.get("blockers") else 1


def _print_items(label: str, value: object) -> None:
    items = value if isinstance(value, list) else []
    if not items:
        print(f"{label}: none")
        return
    print(f"{label}:")
    for item in items:
        if isinstance(item, dict):
            print(f"  - {item.get('message') or item}")
        else:
            print(f"  - {item}")


register_cli_descriptor(
    CommandDescriptor(
        command="ship host inventory",
        operation="host.inventory",
        summary="Collect read-only host inventory with capability, capacity, runtime, and foundation-service metadata.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.host_inventory",
        args_schema={
            "type": "object",
            "properties": {
                "runtime_root": {"type": "string"},
                "manifests_dir": {"type": "string"},
                "config": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.host_inventory.v1",
        artifacts=[],
        safety_notes=["Read-only inventory collection. Does not create or mutate host records."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship host readiness",
        operation="host.readiness",
        summary="Evaluate host capability, capacity, network, and backup readiness from read-only inventory.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.host_readiness",
        args_schema={
            "type": "object",
            "properties": {
                "host_id": {"type": "string"},
                "runtime_root": {"type": "string"},
                "manifests_dir": {"type": "string"},
                "config": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.host_readiness.v1",
        artifacts=[],
        safety_notes=["Read-only readiness evaluation. Does not mutate host state."],
    )
)
