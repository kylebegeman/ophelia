from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import (
    REPORT_SCHEMA_REF,
    CommandDescriptor,
    register_cli_descriptor,
)
from ..config import DEFAULT_RUNTIME_ROOT
from ..manifest import ManifestError, load_manifest
from ..operator_reports import secrets_required
from ..secret_providers import secret_provider_report, secret_provider_status
from ..secrets_audit import secrets_audit


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("secrets", help="Inspect secret requirements")
    secret_subparsers = parser.add_subparsers(dest="secrets_command")
    required = secret_subparsers.add_parser("required", help="Report required keys for a manifest")
    required.add_argument("manifest", type=Path)
    required.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    required.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    required.set_defaults(handler=run_required)

    audit = secret_subparsers.add_parser("audit", help="Names-only audit of env/secret requirements and presence")
    audit.add_argument("manifest_or_app", help="Path to a .ophelia.yml manifest OR an app id")
    audit.add_argument("--environment", choices=["dev", "staging", "production"])
    audit.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    audit.add_argument(
        "--include-process-env",
        action="store_true",
        help="Also check os.environ for key presence (names/booleans only, never values)",
    )
    audit.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    audit.set_defaults(handler=run_audit)

    providers = secret_subparsers.add_parser("providers", help="Names-only secret provider presence report")
    providers.add_argument("manifest_or_app", nargs="?", help="Path to a .ophelia.yml manifest OR an app id")
    providers.add_argument("--environment", choices=["dev", "staging", "production"])
    providers.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    providers.add_argument("--config", type=Path, help="Path to ophelia integrations config")
    providers.add_argument(
        "--include-process-env",
        action="store_true",
        help="Also check os.environ for key presence (names/booleans only, never values)",
    )
    providers.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    providers.set_defaults(handler=run_providers)


def run_required(args: Namespace) -> int:
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        print(f"Manifest invalid: {exc}")
        return 1
    report = secrets_required(manifest, args.manifest, args.runtime_root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for item in report["requirements"]:
            print(
                f"{item['key']}\tpresent={item['present']}\t"
                f"missing={item['missing']}\tplaceholder={item['placeholder_detected']}"
            )
    return 0


def run_audit(args: Namespace) -> int:
    report = secrets_audit(
        args.manifest_or_app,
        environment=args.environment,
        runtime_root=args.runtime_root,
        include_process_env=args.include_process_env,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        print(f"Status: {report['status']}")
        for item in report["keys"]:
            required = "required" if item["required"] else "optional"
            print(
                f"  {item['name']}\tpresent={item['present']}\t{required}\t"
                f"source={item['source']}\tstatus={item['status']}"
            )
    return 0 if report["status"] != "blocked" else 1


def run_providers(args: Namespace) -> int:
    if args.manifest_or_app:
        report = secret_provider_report(
            args.manifest_or_app,
            environment=args.environment,
            runtime_root=args.runtime_root,
            config_path=args.config,
            include_process_env=args.include_process_env,
        )
    else:
        report = secret_provider_status(runtime_root=args.runtime_root, config_path=args.config)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report.get("summary", "Secret provider report."))
        print(f"Status: {report.get('status')}")
        for item in report.get("keys", []):
            if isinstance(item, dict):
                print(f"  {item.get('name')}\tpresent_any={item.get('present_any')}\tstatus={item.get('status')}")
        for blocker in report.get("blockers", []):
            print(f"  blocker {blocker.get('code')}: {blocker.get('message')}")
        for warning in report.get("warnings", []):
            print(f"  warning {warning.get('code')}: {warning.get('message')}")
    return 0 if report.get("status") != "blocked" else 1


register_cli_descriptor(
    CommandDescriptor(
        command="ship secrets audit",
        operation="secrets.audit",
        summary="Names-only audit of an app's env/secret requirements and presence.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.secrets_audit",
        args_schema={
            "type": "object",
            "properties": {
                "manifest_or_app": {"type": "string"},
                "environment": {"type": "string", "enum": ["dev", "staging", "production"]},
                "runtime_root": {"type": "string"},
                "include_process_env": {"type": "boolean"},
                "json": {"type": "boolean"},
            },
            "required": ["manifest_or_app"],
            "additionalProperties": False,
        },
        output_schema_ref=REPORT_SCHEMA_REF,
        artifacts=[],
        safety_notes=[
            "Read-only. Reports env key names and presence booleans only; never a value.",
            "--include-process-env only sets presence booleans; it does not read or emit values.",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship secrets providers",
        operation="secrets.providers",
        summary="Names-only secret provider presence report across runtime env, GitHub environment observations, and SOPS refs.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.secret_provider_report",
        args_schema={
            "type": "object",
            "properties": {
                "manifest_or_app": {"type": "string"},
                "environment": {"type": "string", "enum": ["dev", "staging", "production"]},
                "runtime_root": {"type": "string"},
                "config": {"type": "string"},
                "include_process_env": {"type": "boolean"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref=REPORT_SCHEMA_REF,
        artifacts=[],
        safety_notes=[
            "Read-only. Emits key names, provider locations, presence booleans, and freshness metadata only.",
            "Never reads or emits secret values from runtime env, GitHub, SOPS, or process env.",
        ],
    )
)
