from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..version import version_info


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("version", help="Print Ophelia version information")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    payload = version_info()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        version = payload.get("version") or "unknown"
        source = payload.get("source") or "unknown"
        print(f"ophelia {version} ({source})")
    return 0


register_cli_descriptor(
    CommandDescriptor(
        command="ship version",
        operation="version",
        summary="Print Ophelia version information.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.version",
        args_schema={
            "type": "object",
            "properties": {"json": {"type": "boolean"}},
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.version.v1",
        artifacts=[],
        safety_notes=["Read-only metadata command. Does not inspect env values or mutate state."],
    )
)
