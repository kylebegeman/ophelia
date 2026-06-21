from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..actions import action_catalog


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("actions", help="List allowlisted Ophelia actions")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    catalog = action_catalog()
    if args.json:
        print(json.dumps({"actions": catalog}, indent=2, sort_keys=True))
    else:
        for action in catalog:
            print(f"{action['id']}\t{action['mutation_level']}\t{action['description']}")
    return 0
