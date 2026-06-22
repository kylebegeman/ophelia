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
from ..github_providers import github_provider_status
from ..provider_config import explain_provider_config, validate_provider_config
from ._output import print_error


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("providers", help="Validate and explain traffic provider configs")
    provider_subparsers = parser.add_subparsers(dest="providers_command")

    validate = provider_subparsers.add_parser("validate", help="Validate a traffic provider config")
    validate.add_argument("--config", help="Path to the provider config JSON")
    validate.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    validate.set_defaults(handler=run_validate)

    explain = provider_subparsers.add_parser("explain", help="Explain what a provider config would do")
    explain.add_argument("--config", help="Path to the provider config JSON")
    explain.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    explain.set_defaults(handler=run_explain)

    github = provider_subparsers.add_parser("github", help="Inspect GitHub provider contracts")
    github_subparsers = github.add_subparsers(dest="providers_github_command")
    github_status = github_subparsers.add_parser("status", help="Show GitHub App/gh provider readiness")
    github_status.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    github_status.add_argument("--config", type=Path, help="Path to ophelia integrations config")
    github_status.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    github_status.set_defaults(handler=run_github_status)


def run_validate(args: Namespace) -> int:
    config_path = _require_config(args)
    if config_path is None:
        return 1
    report = validate_provider_config(Path(config_path))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_validation(report)
    return 0 if report.get("status") != "blocked" else 1


def run_explain(args: Namespace) -> int:
    config_path = _require_config(args)
    if config_path is None:
        return 1
    report = explain_provider_config(Path(config_path))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_explanation(report)
    return 0 if report.get("status") != "blocked" and report.get("kind") != "ophelia.error" else 1


def run_github_status(args: Namespace) -> int:
    report = github_provider_status(runtime_root=args.runtime_root, config_path=args.config)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report.get("summary", "GitHub provider status."))
        print(f"Status: {report.get('status')}")
        print(f"Selected provider: {report.get('selected_provider')}")
        for provider in report.get("providers", []):
            if isinstance(provider, dict):
                print(f"  {provider.get('name')}: usable={provider.get('usable')} configured={provider.get('configured')}")
        for blocker in report.get("blockers", []):
            print(f"  blocker {blocker.get('code')}: {blocker.get('message')}")
        for warning in report.get("warnings", []):
            print(f"  warning {warning.get('code')}: {warning.get('message')}")
    return 0 if report.get("status") != "blocked" else 1


def _require_config(args: Namespace) -> str | None:
    config = getattr(args, "config", None)
    if not config:
        _print_error("Missing required --config PATH.", "provider_config_arg_missing", args.json)
        return None
    return config


def _print_error(message: str, code: str, emit_json: bool) -> None:
    print_error(message, code, json_output=emit_json)


def _print_validation(report: dict) -> None:
    if report.get("kind") == "ophelia.error":
        print(report.get("error", "Provider config error."))
        return
    print(report.get("summary", "Provider config validated."))
    print(f"Status: {report.get('status')}")
    for provider in report.get("providers", []):
        print(f"  {provider.get('group')}.{provider.get('type')}: {provider.get('status')}")
        for blocker in provider.get("blockers", []):
            print(f"    blocker {blocker.get('code')}: {blocker.get('message')}")
        for warning in provider.get("warnings", []):
            print(f"    warning {warning.get('code')}: {warning.get('message')}")


def _print_explanation(report: dict) -> None:
    if report.get("kind") == "ophelia.error":
        print(report.get("error", "Provider config error."))
        return
    print(report.get("summary", "Provider config explanation."))
    for provider in report.get("providers", []):
        print(f"  {provider.get('group')}.{provider.get('type')}: {provider.get('would')}")
        env_refs = provider.get("env_refs") or []
        if env_refs:
            print(f"    env refs (names): {', '.join(env_refs)}")


register_cli_descriptor(
    CommandDescriptor(
        command="ship providers validate",
        operation="providers.validate",
        summary="Validate a traffic provider config (DNS/Caddy) without mutation.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.provider_config.validation",
        args_schema={
            "type": "object",
            "properties": {
                "config": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["config"],
            "additionalProperties": False,
        },
        output_schema_ref=REPORT_SCHEMA_REF,
        artifacts=[],
        safety_notes=[
            "Read-only provider config validation. No VPS, network, or secret value is touched.",
            "Blocks literal provider secrets; requires *_env references instead.",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship providers explain",
        operation="providers.explain",
        summary="Explain what a traffic provider config would do (read-only).",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.provider_config.explanation",
        args_schema={
            "type": "object",
            "properties": {
                "config": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["config"],
            "additionalProperties": False,
        },
        output_schema_ref=REPORT_SCHEMA_REF,
        artifacts=[],
        safety_notes=["Read-only explanation. Reports env-var references by name only; no secret values."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship providers github status",
        operation="providers.github.status",
        summary="Inspect GitHub App and gh provider readiness without mutation.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.github_provider_status",
        args_schema={
            "type": "object",
            "properties": {
                "runtime_root": {"type": "string"},
                "config": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref=REPORT_SCHEMA_REF,
        artifacts=[],
        safety_notes=[
            "Read-only. Checks provider config shape, executable presence, and env-var presence booleans only.",
            "Never reads or emits GitHub App private key values.",
        ],
    )
)
