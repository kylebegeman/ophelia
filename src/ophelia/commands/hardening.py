from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..hardening import DEFAULT_FIXTURE_ROOT, PRODUCTION_HARDENING_KIND, production_hardening_report
from ..plugin_contracts import DEFAULT_PLUGINS_DIR


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("hardening", help="Read-only production hardening reports")
    hardening_subparsers = parser.add_subparsers(dest="hardening_command")

    readiness_parser = hardening_subparsers.add_parser(
        "production-readiness",
        help="Compose live readiness, console, plugin, workflow, state, and fixture drills into one report",
    )
    readiness_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    readiness_parser.add_argument("--manifests-dir", type=Path, default=REPO_ROOT / "manifests")
    readiness_parser.add_argument("--ophelia-root", type=Path, default=REPO_ROOT)
    readiness_parser.add_argument("--host-config", type=Path, help="Optional host inventory config JSON/YAML")
    readiness_parser.add_argument("--provider-config", type=Path, help="Optional integrations config JSON/YAML")
    readiness_parser.add_argument("--plugins-dir", type=Path, default=DEFAULT_PLUGINS_DIR)
    readiness_parser.add_argument(
        "--include-fixture-suite",
        action="store_true",
        help="Run the committed fixture app suite as an expected-state drill",
    )
    readiness_parser.add_argument("--fixture-root", type=Path, default=DEFAULT_FIXTURE_ROOT)
    readiness_parser.add_argument(
        "--allow-blocked-live-readiness",
        action="store_true",
        help="Treat a blocked live-readiness sub-report as a review warning instead of a hard blocker",
    )
    readiness_parser.add_argument("--expected-fixture-blocked-app", default="fixture-incomplete-app")
    readiness_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    readiness_parser.set_defaults(handler=run_production_readiness)


def run_production_readiness(args: Namespace) -> int:
    report = production_hardening_report(
        runtime_root=args.runtime_root,
        manifests_dir=args.manifests_dir,
        ophelia_root=args.ophelia_root,
        host_config=args.host_config,
        provider_config=args.provider_config,
        plugins_dir=args.plugins_dir,
        include_fixture_suite=args.include_fixture_suite,
        fixture_root=args.fixture_root,
        allow_blocked_live_readiness=args.allow_blocked_live_readiness,
        expected_fixture_blocked_app=args.expected_fixture_blocked_app,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report.get("summary", "Production hardening report."))
        print(f"Status: {report.get('status')} ({report.get('go_no_go')})")
        for check in report.get("checks", []) if isinstance(report.get("checks"), list) else []:
            if isinstance(check, dict):
                marker = "ok" if check.get("ok") else "blocked"
                print(f"  - {check.get('name')}: {marker} ({check.get('message')})")
        _print_items("Blockers", report.get("blockers", []))
        _print_items("Warnings", report.get("warnings", []))
    return 0 if report.get("status") != "blocked" else 1


def _print_items(label: str, value: object) -> None:
    items = value if isinstance(value, list) else []
    if not items:
        print(f"{label}: none")
        return
    print(f"{label}:")
    for item in items:
        if isinstance(item, dict):
            print(f"  - {item.get('code')}: {item.get('message')}")
        else:
            print(f"  - {item}")


register_cli_descriptor(
    CommandDescriptor(
        command="ship hardening production-readiness",
        operation="production.hardening",
        summary=(
            "Compose live readiness, Lumen console data, plugin validation, workflow availability, "
            "state freshness, catalog safety, and optional fixture drills into a read-only go/no-go report."
        ),
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind=PRODUCTION_HARDENING_KIND,
        args_schema={
            "type": "object",
            "properties": {
                "runtime_root": {"type": "string"},
                "manifests_dir": {"type": "string"},
                "ophelia_root": {"type": "string"},
                "host_config": {"type": "string"},
                "provider_config": {"type": "string"},
                "plugins_dir": {"type": "string"},
                "include_fixture_suite": {"type": "boolean"},
                "fixture_root": {"type": "string"},
                "allow_blocked_live_readiness": {"type": "boolean"},
                "expected_fixture_blocked_app": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.production_hardening_report.v1",
        artifacts=[],
        examples=[
            "ship hardening production-readiness --json",
            (
                "ship hardening production-readiness --runtime-root fixtures/app-suite/runtime "
                "--manifests-dir fixtures/app-suite/manifests --host-config fixtures/app-suite/host-inventory.yml "
                "--provider-config fixtures/app-suite/integrations.yml --plugins-dir fixtures/app-suite/plugins "
                "--include-fixture-suite --allow-blocked-live-readiness --json"
            ),
        ],
        safety_notes=[
            "Read-only aggregate. Does not execute workflow nodes, provider mutations, plugin code, or shell commands.",
            "HTTP and Docker probes remain disabled because the report consumes the default live-readiness lane.",
            "Use --include-fixture-suite to prove the deterministic fixture suite still matches its expected mixed state.",
        ],
    )
)
