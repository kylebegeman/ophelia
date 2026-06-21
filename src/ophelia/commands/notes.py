from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT
from ..notes import add_note, list_notes


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("notes", help="Add or list operator notes")
    notes_subparsers = parser.add_subparsers(dest="notes_command")
    add_parser = notes_subparsers.add_parser("add")
    target = add_parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--job")
    target.add_argument("--release")
    add_parser.add_argument("text")
    add_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    add_parser.add_argument("--author")
    add_parser.add_argument("--source", default="cli")
    add_parser.add_argument("--json", action="store_true", help="Accepted for command consistency; output is JSON.")
    add_parser.set_defaults(handler=run_add)

    list_parser = notes_subparsers.add_parser("list")
    target_list = list_parser.add_mutually_exclusive_group(required=True)
    target_list.add_argument("--job")
    target_list.add_argument("--release")
    list_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    list_parser.add_argument("--json", action="store_true", help="Accepted for command consistency; output is JSON.")
    list_parser.set_defaults(handler=run_list)


def run_add(args: Namespace) -> int:
    target_type, target_id = ("job", args.job) if args.job else ("release", args.release)
    note = add_note(args.runtime_root, target_type, target_id, args.text, args.author, args.source)
    print(json.dumps(note, indent=2, sort_keys=True))
    return 0


def run_list(args: Namespace) -> int:
    target_type, target_id = ("job", args.job) if args.job else ("release", args.release)
    print(json.dumps({"notes": list_notes(args.runtime_root, target_type, target_id)}, indent=2, sort_keys=True))
    return 0
