from __future__ import annotations

from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT
from ..notes import add_note, list_notes
from ._output import print_error, print_json


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
    add_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    add_parser.set_defaults(handler=run_add)

    list_parser = notes_subparsers.add_parser("list")
    target_list = list_parser.add_mutually_exclusive_group(required=True)
    target_list.add_argument("--job")
    target_list.add_argument("--release")
    list_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    list_parser.set_defaults(handler=run_list)


def run_add(args: Namespace) -> int:
    target_type, target_id = ("job", args.job) if args.job else ("release", args.release)
    try:
        note = add_note(args.runtime_root, target_type, target_id, args.text, args.author, args.source)
    except ValueError as exc:
        print_error(str(exc), "notes_unreadable", json_output=args.json)
        return 1
    if args.json:
        print_json(
            {
                "schema_version": 1,
                "kind": "ophelia.note",
                "note": note,
                "summary": f"Added note for {target_type} {target_id}.",
            }
        )
    else:
        print(f"Added note for {target_type} {target_id}.")
    return 0


def run_list(args: Namespace) -> int:
    target_type, target_id = ("job", args.job) if args.job else ("release", args.release)
    try:
        notes = list_notes(args.runtime_root, target_type, target_id)
    except ValueError as exc:
        print_error(str(exc), "notes_unreadable", json_output=args.json)
        return 1
    if args.json:
        print_json(
            {
                "schema_version": 1,
                "kind": "ophelia.notes",
                "target_type": target_type,
                "target_id": target_id,
                "notes": notes,
                "summary": f"{len(notes)} note(s) for {target_type} {target_id}.",
            }
        )
    else:
        print(f"{len(notes)} note(s) for {target_type} {target_id}.")
        for note in notes:
            print(f"  - {note.get('timestamp')} {note.get('author')}: {note.get('text')}")
    return 0
