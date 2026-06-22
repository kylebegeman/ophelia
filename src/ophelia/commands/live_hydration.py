from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..live_drills import DEFAULT_LOCAL_LIVE_DRILL_PROFILES
from ..live_hydration import (
    LIVE_HYDRATION_EVIDENCE_KIND,
    LIVE_HYDRATION_KIND,
    LIVE_HYDRATION_PROBE_GATE_KIND,
    LIVE_HYDRATION_SCAFFOLD_KIND,
    live_hydration_evidence_validate,
    live_hydration_probe_gate,
    live_hydration_report,
    live_hydration_scaffold,
)


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("live-hydration", help="Report missing evidence for one live app baseline")
    hydration_subparsers = parser.add_subparsers(dest="live_hydration_command")

    report_parser = hydration_subparsers.add_parser(
        "report",
        help="Build a read-only hydration report for one app or live drill profile",
    )
    report_parser.add_argument("--profile", help="Live drill profile id")
    report_parser.add_argument("--profiles", type=Path, default=DEFAULT_LOCAL_LIVE_DRILL_PROFILES)
    report_parser.add_argument("--app", help="App id when not using a profile")
    report_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    report_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    report_parser.add_argument("--manifests-dir", type=Path, default=REPO_ROOT / "manifests")
    report_parser.add_argument("--ophelia-root", type=Path, default=REPO_ROOT)
    report_parser.add_argument("--manifest", type=Path, help="Explicit app manifest path")
    report_parser.add_argument("--host-config", type=Path, help="Optional host inventory config JSON/YAML")
    report_parser.add_argument("--provider-config", type=Path, help="Optional integrations config JSON/YAML")
    report_parser.add_argument("--target-host", help="Optional target host id for placement hydration")
    report_parser.add_argument(
        "--allow-blocked",
        action="store_true",
        help="Exit zero after printing a blocked report. Useful for expected live-baseline audits.",
    )
    report_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    report_parser.set_defaults(handler=run_report)

    scaffold_parser = hydration_subparsers.add_parser(
        "scaffold",
        help="Build or write non-secret evidence templates for one hydration baseline",
    )
    _add_common_args(scaffold_parser)
    scaffold_parser.add_argument("--output-dir", type=Path, help="Template output directory. Defaults under <runtime_root>/hydration/<app>/<environment>.")
    scaffold_parser.add_argument("--write", action="store_true", help="Write scaffold template files. Without this, only print the plan.")
    scaffold_parser.add_argument("--force", action="store_true", help="Overwrite existing scaffold files when used with --write.")
    scaffold_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    scaffold_parser.set_defaults(handler=run_scaffold)

    validate_parser = hydration_subparsers.add_parser(
        "validate-evidence",
        help="Validate a hydration evidence directory without promoting it",
    )
    _add_common_args(validate_parser)
    validate_parser.add_argument("--input-dir", type=Path, help="Evidence directory. Defaults to the scaffold output path.")
    validate_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    validate_parser.set_defaults(handler=run_validate_evidence)

    gate_parser = hydration_subparsers.add_parser(
        "probe-gate",
        help="Decide whether opt-in live probes are allowed later without running them",
    )
    _add_common_args(gate_parser)
    gate_parser.add_argument("--input-dir", type=Path, help="Evidence directory. Defaults to the scaffold output path.")
    gate_parser.add_argument(
        "--allow-blocked",
        action="store_true",
        help="Exit zero after printing a blocked gate report. Useful for expected live-baseline audits.",
    )
    gate_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    gate_parser.set_defaults(handler=run_probe_gate)


def _add_common_args(parser) -> None:
    parser.add_argument("--profile", help="Live drill profile id")
    parser.add_argument("--profiles", type=Path, default=DEFAULT_LOCAL_LIVE_DRILL_PROFILES)
    parser.add_argument("--app", help="App id when not using a profile")
    parser.add_argument("--environment", choices=["dev", "staging", "production"])
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--manifests-dir", type=Path, default=REPO_ROOT / "manifests")
    parser.add_argument("--ophelia-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--manifest", type=Path, help="Explicit app manifest path")
    parser.add_argument("--host-config", type=Path, help="Optional host inventory config JSON/YAML")
    parser.add_argument("--provider-config", type=Path, help="Optional integrations config JSON/YAML")
    parser.add_argument("--target-host", help="Optional target host id for placement hydration")


def run_report(args: Namespace) -> int:
    report = live_hydration_report(
        app=args.app,
        environment=args.environment,
        profile=args.profile,
        profiles_path=args.profiles,
        runtime_root=args.runtime_root,
        manifests_dir=args.manifests_dir,
        ophelia_root=args.ophelia_root,
        manifest_path=args.manifest,
        host_config=args.host_config,
        provider_config=args.provider_config,
        target_host=args.target_host,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report.get("summary", "Live hydration report."))
        print(f"Status: {report.get('status')}")
        for step in report.get("hydration_steps", []) if isinstance(report.get("hydration_steps"), list) else []:
            if isinstance(step, dict):
                print(f"  - {step.get('code')}: {step.get('summary')}")
        _print_items("Blockers", report.get("blockers", []))
        _print_items("Warnings", report.get("warnings", []))
    return 0 if report.get("status") != "blocked" or args.allow_blocked else 1


def run_scaffold(args: Namespace) -> int:
    report = live_hydration_scaffold(
        app=args.app,
        environment=args.environment,
        profile=args.profile,
        profiles_path=args.profiles,
        runtime_root=args.runtime_root,
        manifests_dir=args.manifests_dir,
        ophelia_root=args.ophelia_root,
        manifest_path=args.manifest,
        host_config=args.host_config,
        provider_config=args.provider_config,
        target_host=args.target_host,
        output_dir=args.output_dir,
        write=args.write,
        force=args.force,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report.get("summary", "Live hydration scaffold."))
        print(f"Status: {report.get('status')}")
        print(f"Output: {report.get('output_dir')}")
        for item in report.get("files", []) if isinstance(report.get("files"), list) else []:
            if isinstance(item, dict):
                state = "written" if item.get("written") else "planned"
                print(f"  - {item.get('kind')}: {item.get('path')} ({state})")
        _print_items("Blockers", report.get("blockers", []))
        _print_items("Warnings", report.get("warnings", []))
    return 0 if report.get("status") != "blocked" else 1


def run_validate_evidence(args: Namespace) -> int:
    report = live_hydration_evidence_validate(
        app=args.app,
        environment=args.environment,
        profile=args.profile,
        profiles_path=args.profiles,
        runtime_root=args.runtime_root,
        manifests_dir=args.manifests_dir,
        ophelia_root=args.ophelia_root,
        manifest_path=args.manifest,
        host_config=args.host_config,
        provider_config=args.provider_config,
        target_host=args.target_host,
        input_dir=args.input_dir,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report.get("summary", "Live hydration evidence validation."))
        print(f"Status: {report.get('status')}")
        print(f"Input: {report.get('input_dir')}")
        for item in report.get("file_checks", []) if isinstance(report.get("file_checks"), list) else []:
            if isinstance(item, dict):
                print(f"  - {item.get('kind')}: {item.get('status')}")
        _print_items("Blockers", report.get("blockers", []))
        _print_items("Warnings", report.get("warnings", []))
    return 0 if report.get("status") != "blocked" else 1


def run_probe_gate(args: Namespace) -> int:
    report = live_hydration_probe_gate(
        app=args.app,
        environment=args.environment,
        profile=args.profile,
        profiles_path=args.profiles,
        runtime_root=args.runtime_root,
        manifests_dir=args.manifests_dir,
        ophelia_root=args.ophelia_root,
        manifest_path=args.manifest,
        host_config=args.host_config,
        provider_config=args.provider_config,
        target_host=args.target_host,
        input_dir=args.input_dir,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report.get("summary", "Live hydration probe gate."))
        print(f"Status: {report.get('status')} go_no_go={report.get('go_no_go')}")
        for item in report.get("probe_commands", []) if isinstance(report.get("probe_commands"), list) else []:
            if isinstance(item, dict):
                print(f"  - {item.get('name')}: {item.get('command')}")
        _print_items("Blockers", report.get("blockers", []))
        _print_items("Warnings", report.get("warnings", []))
    return 0 if report.get("status") != "blocked" or args.allow_blocked else 1


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
        command="ship live-hydration report",
        operation="live_hydration.report",
        summary="Report missing runtime/env/secret-name/release/host evidence for one live app baseline.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind=LIVE_HYDRATION_KIND,
        args_schema={
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "profiles": {"type": "string"},
                "app": {"type": "string"},
                "environment": {"enum": ["dev", "staging", "production"]},
                "runtime_root": {"type": "string"},
                "manifests_dir": {"type": "string"},
                "ophelia_root": {"type": "string"},
                "manifest": {"type": "string"},
                "host_config": {"type": "string"},
                "provider_config": {"type": "string"},
                "target_host": {"type": "string"},
                "allow_blocked": {"type": "boolean"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.live_hydration_report.v1",
        artifacts=[],
        examples=[
            "ship live-hydration report --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --allow-blocked --json",
            "ship live-hydration report --app quark-ops --environment production --host-config config/ophelia-hosts.yml --provider-config config/ophelia-integrations.yml --json",
        ],
        safety_notes=[
            "Read-only report. Does not create runtime files, run probes, refresh state, or mutate providers.",
            "Secret values are never emitted; secret provider sections report names, booleans, paths, and counts only.",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship live-hydration probe-gate",
        operation="live_hydration.probe_gate",
        summary="Gate opt-in live probes using hydration and evidence validation without running probes.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind=LIVE_HYDRATION_PROBE_GATE_KIND,
        args_schema={
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "profiles": {"type": "string"},
                "app": {"type": "string"},
                "environment": {"enum": ["dev", "staging", "production"]},
                "runtime_root": {"type": "string"},
                "manifests_dir": {"type": "string"},
                "ophelia_root": {"type": "string"},
                "manifest": {"type": "string"},
                "host_config": {"type": "string"},
                "provider_config": {"type": "string"},
                "target_host": {"type": "string"},
                "input_dir": {"type": "string"},
                "allow_blocked": {"type": "boolean"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.live_hydration_probe_gate.v1",
        artifacts=[],
        examples=[
            "ship live-hydration probe-gate --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --allow-blocked --json",
            "ship live-hydration probe-gate --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --input-dir ~/ophelia-runtime/hydration/quark-ops-staging/staging --json",
        ],
        safety_notes=[
            "Read-only. Does not run HTTP/Docker probes or provider calls.",
            "Probe commands are emitted only as explicit follow-up commands for operator review.",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship live-hydration validate-evidence",
        operation="live_hydration.evidence.validate",
        summary="Validate a live hydration evidence directory without promoting runtime files.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind=LIVE_HYDRATION_EVIDENCE_KIND,
        args_schema={
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "profiles": {"type": "string"},
                "app": {"type": "string"},
                "environment": {"enum": ["dev", "staging", "production"]},
                "runtime_root": {"type": "string"},
                "manifests_dir": {"type": "string"},
                "ophelia_root": {"type": "string"},
                "manifest": {"type": "string"},
                "host_config": {"type": "string"},
                "provider_config": {"type": "string"},
                "target_host": {"type": "string"},
                "input_dir": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.live_hydration_evidence_validation.v1",
        artifacts=[],
        examples=[
            "ship live-hydration validate-evidence --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --json",
            "ship live-hydration validate-evidence --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --input-dir ~/ophelia-runtime/hydration/quark-ops-staging/staging --json",
        ],
        safety_notes=[
            "Read-only validation. Does not copy, promote, or mutate runtime/provider files.",
            "Reports key names and issue counts only; evidence values are not emitted.",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship live-hydration scaffold",
        operation="live_hydration.scaffold",
        summary="Create non-secret evidence templates for one live hydration baseline.",
        risk="low",
        mutates_state=True,
        requires_confirmation=False,
        plan_command="ship live-hydration scaffold --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --json",
        apply_command="ship live-hydration scaffold --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --write --json",
        json_kind=LIVE_HYDRATION_SCAFFOLD_KIND,
        args_schema={
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "profiles": {"type": "string"},
                "app": {"type": "string"},
                "environment": {"enum": ["dev", "staging", "production"]},
                "runtime_root": {"type": "string"},
                "manifests_dir": {"type": "string"},
                "ophelia_root": {"type": "string"},
                "manifest": {"type": "string"},
                "host_config": {"type": "string"},
                "provider_config": {"type": "string"},
                "target_host": {"type": "string"},
                "output_dir": {"type": "string"},
                "write": {"type": "boolean"},
                "force": {"type": "boolean"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.live_hydration_scaffold.v1",
        artifacts=[],
        examples=[
            "ship live-hydration scaffold --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --json",
            "ship live-hydration scaffold --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --output-dir ~/ophelia-runtime/hydration/quark-ops-staging/staging --write --json",
        ],
        safety_notes=[
            "Default mode is dry-run and read-only.",
            "With --write, only template files are written under the scaffold output directory; live env, release, and provider observation paths are not modified.",
            "Templates contain names and placeholders only, never secret values.",
        ],
    )
)
