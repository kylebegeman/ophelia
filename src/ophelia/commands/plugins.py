from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import REPORT_SCHEMA_REF, CommandDescriptor, register_cli_descriptor
from ..plugin_contracts import DEFAULT_PLUGINS_DIR, plugin_inventory, validate_plugin_manifest


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("plugins", help="Discover and validate trusted Ophelia plugin metadata")
    plugin_subparsers = parser.add_subparsers(dest="plugins_command")

    list_parser = plugin_subparsers.add_parser("list", help="List trusted plugin manifests")
    list_parser.add_argument("--plugins-dir", type=Path, default=DEFAULT_PLUGINS_DIR)
    list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    list_parser.set_defaults(handler=run_list)

    catalog_parser = plugin_subparsers.add_parser("catalog", help="Emit plugin capability and command metadata")
    catalog_parser.add_argument("--plugins-dir", type=Path, default=DEFAULT_PLUGINS_DIR)
    catalog_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    catalog_parser.set_defaults(handler=run_catalog)

    validate_parser = plugin_subparsers.add_parser("validate", help="Validate one plugin manifest")
    validate_parser.add_argument("manifest", type=Path)
    validate_parser.add_argument("--trusted-root", type=Path)
    validate_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    validate_parser.set_defaults(handler=run_validate)


def run_list(args: Namespace) -> int:
    report = plugin_inventory(args.plugins_dir)
    _print_report(report, json_output=args.json)
    return 0 if report.get("status") != "blocked" else 1


def run_catalog(args: Namespace) -> int:
    report = plugin_inventory(args.plugins_dir)
    _print_report(report, json_output=args.json)
    return 0 if report.get("status") != "blocked" else 1


def run_validate(args: Namespace) -> int:
    report = validate_plugin_manifest(args.manifest, trusted_root=args.trusted_root)
    _print_report(report, json_output=args.json)
    return 0 if report.get("status") != "blocked" else 1


def _print_report(report: dict, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    print(report.get("summary", "Plugin report."))
    print(f"Status: {report.get('status')}")
    plugins = report.get("plugins") if isinstance(report.get("plugins"), list) else []
    plugin = report.get("plugin") if isinstance(report.get("plugin"), dict) else None
    if plugin:
        plugins = [plugin]
    for item in plugins:
        if isinstance(item, dict):
            counts = item.get("capability_counts") if isinstance(item.get("capability_counts"), dict) else {}
            cap_summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "no capabilities"
            print(f"  - {item.get('name')} {item.get('version')}: {cap_summary}")
    for blocker in report.get("blockers", []) if isinstance(report.get("blockers"), list) else []:
        if isinstance(blocker, dict):
            print(f"  blocker {blocker.get('code')}: {blocker.get('message')}")
    for warning in report.get("warnings", []) if isinstance(report.get("warnings"), list) else []:
        if isinstance(warning, dict):
            print(f"  warning {warning.get('code')}: {warning.get('message')}")


register_cli_descriptor(
    CommandDescriptor(
        command="ship plugins list",
        operation="plugins.list",
        summary="List trusted Ophelia plugin manifests without loading plugin code.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.plugin_catalog",
        args_schema={
            "type": "object",
            "properties": {
                "plugins_dir": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref=REPORT_SCHEMA_REF,
        artifacts=[],
        safety_notes=[
            "Read-only metadata discovery. Does not import, install, or execute plugin code.",
            "Plugins remain disabled by default and are not injected into the executable command catalog.",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship plugins catalog",
        operation="plugins.catalog",
        summary="Emit plugin capability and command metadata from a trusted directory.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.plugin_catalog",
        args_schema={
            "type": "object",
            "properties": {
                "plugins_dir": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref=REPORT_SCHEMA_REF,
        artifacts=[],
        safety_notes=["Read-only metadata catalog. Plugin command descriptors are descriptive only."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship plugins validate",
        operation="plugins.validate",
        summary="Validate a single Ophelia plugin manifest.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.plugin_validation",
        args_schema={
            "type": "object",
            "properties": {
                "manifest": {"type": "string"},
                "trusted_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["manifest"],
            "additionalProperties": False,
        },
        output_schema_ref=REPORT_SCHEMA_REF,
        artifacts=[],
        safety_notes=[
            "Read-only validation. Rejects unbounded schemas, literal secrets, and unsafe mutating descriptors.",
        ],
    )
)

