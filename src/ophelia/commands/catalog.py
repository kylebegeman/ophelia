from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction

from ..command_catalog import catalog


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("commands", help="Discover Ophelia commands and their safety metadata")
    commands_subparsers = parser.add_subparsers(dest="commands_command")

    catalog_parser = commands_subparsers.add_parser("catalog", help="List every agent-facing command")
    catalog_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON (default)")
    catalog_parser.add_argument(
        "--human",
        action="store_true",
        help="Print an aligned human table instead of JSON",
    )
    catalog_parser.set_defaults(handler=run_catalog)


def run_catalog(args: Namespace) -> int:
    commands = catalog()
    # Default to JSON. Only an explicit --human (and not --json) prints the table.
    if getattr(args, "human", False) and not getattr(args, "json", False):
        _print_table(commands)
        return 0

    print(
        json.dumps(
            {"schema_version": 1, "kind": "ophelia.command_catalog", "commands": commands},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _print_table(commands: list) -> None:
    rows = [("COMMAND", "OPERATION", "RISK", "MUTATES")]
    for item in commands:
        rows.append(
            (
                item["command"],
                item["operation"],
                item["risk"],
                "yes" if item["mutates_state"] else "no",
            )
        )
    widths = [max(len(row[col]) for row in rows) for col in range(4)]
    for row in rows:
        print("  ".join(value.ljust(widths[col]) for col, value in enumerate(row)).rstrip())
