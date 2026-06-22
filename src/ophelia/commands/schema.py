from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..schema_export import SCHEMA_VERSION, manifest_json_schema, manifest_json_schema_text


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("schema", help="Export Ophelia JSON schemas")
    schema_subparsers = parser.add_subparsers(dest="schema_command")

    manifest_parser = schema_subparsers.add_parser(
        "manifest", help="Print the manifest JSON schema (draft 2020-12)"
    )
    manifest_parser.add_argument(
        "--json", action="store_true", help="Emit the schema as JSON (default)"
    )
    manifest_parser.add_argument(
        "--output", type=Path, help="Write the schema to PATH instead of stdout"
    )
    manifest_parser.set_defaults(handler=run_manifest)


def run_manifest(args: Namespace) -> int:
    if args.output is not None:
        text = manifest_json_schema_text()
        output: Path = args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "ophelia.schema_export",
                    "output": str(output),
                    "bytes": len(text) + 1,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(json.dumps(manifest_json_schema(), indent=2, sort_keys=True))
    return 0


register_cli_descriptor(
    CommandDescriptor(
        command="ship schema manifest",
        operation="schema.manifest",
        summary="Export the Ophelia manifest JSON schema (draft 2020-12).",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.schema_export",
        args_schema={
            "type": "object",
            "properties": {
                "json": {"type": "boolean"},
                "output": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.manifest_schema.v1",
        artifacts=["manifest JSON schema file (with --output)"],
        safety_notes=[
            "Read-only schema export. Writes only the file named by --output.",
        ],
    )
)
