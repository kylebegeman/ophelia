from __future__ import annotations

from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..backup import apply_restore, backup_plan, create_backup, restore_plan
from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..config import DEFAULT_RUNTIME_ROOT
from ..manifest import ManifestError, load_manifest
from ..portability import backup_status_report, restore_drill_apply, restore_drill_plan
from ..restore_verification import backup_verify_apply, backup_verify_plan
from ._output import print_error, print_issues, print_json


def register(subparsers: _SubParsersAction) -> None:
    backup_parser = subparsers.add_parser("backup", help="Plan or create app backups")
    backup_subparsers = backup_parser.add_subparsers(dest="backup_command")
    plan_parser = backup_subparsers.add_parser("plan", help="Plan an app backup")
    plan_parser.add_argument("app", help="App id")
    plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    plan_parser.set_defaults(handler=run_backup_plan)

    verify_parser = backup_subparsers.add_parser("verify", help="Plan or apply backup verification")
    verify_subparsers = verify_parser.add_subparsers(dest="backup_verify_command")
    verify_plan_parser = verify_subparsers.add_parser("plan", help="Plan a read-only backup verification")
    verify_plan_parser.add_argument("app", help="App id")
    verify_plan_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    verify_plan_parser.add_argument("--backup-id", help="Specific backup id (default: latest)")
    verify_plan_parser.add_argument("--manifest", type=Path, help="Path to app .ophelia manifest")
    verify_plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    verify_plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    verify_plan_parser.set_defaults(handler=run_backup_verify_plan)

    verify_apply_parser = verify_subparsers.add_parser("apply", help="Run an isolated, token-gated backup verification")
    verify_apply_parser.add_argument("app", help="App id")
    verify_apply_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    verify_apply_parser.add_argument("--confirm", required=True, help="Confirmation token from backup verify plan")
    verify_apply_parser.add_argument("--backup-id", help="Specific backup id (default: latest)")
    verify_apply_parser.add_argument("--manifest", type=Path, help="Path to app .ophelia manifest")
    verify_apply_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    verify_apply_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    verify_apply_parser.set_defaults(handler=run_backup_verify_apply)

    rehearse_parser = backup_subparsers.add_parser(
        "rehearse", help="Plan or apply an export-artifact rehearsal using the app manifest"
    )
    rehearse_parser.add_argument("mode_or_source", help="`plan`, `apply`, or export bundle directory/.tar/.tar.zst")
    rehearse_parser.add_argument("source", nargs="?", type=Path, help="Export bundle when using `plan` or `apply`")
    rehearse_parser.add_argument("--manifest", type=Path, required=True, help="Path to app .ophelia manifest")
    rehearse_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    rehearse_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    rehearse_parser.add_argument("--confirm", help="Confirmation token from backup rehearse plan when using `apply`")
    rehearse_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    rehearse_parser.set_defaults(handler=run_backup_rehearse)

    status_parser = backup_subparsers.add_parser("status", help="Report backup freshness and coverage")
    status_parser.add_argument("app", help="App id")
    status_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    status_parser.add_argument("--manifest", type=Path, help="Path to app .ophelia manifest")
    status_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    status_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    status_parser.set_defaults(handler=run_backup_status)

    create_parser = backup_subparsers.add_parser("create", help="Create an app backup")
    create_parser.add_argument("app", help="App id")
    create_parser.add_argument("--confirm", required=True, help="Confirmation token from backup plan")
    create_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    create_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    create_parser.set_defaults(handler=run_backup_create)

    restore_parser = subparsers.add_parser("restore", help="Plan or apply restore previews")
    restore_subparsers = restore_parser.add_subparsers(dest="restore_command")
    restore_plan_parser = restore_subparsers.add_parser("plan", help="Plan a restore preview")
    restore_plan_parser.add_argument("app", help="App id")
    restore_plan_parser.add_argument("backup_id", help="Backup id")
    restore_plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    restore_plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    restore_plan_parser.set_defaults(handler=run_restore_plan)

    restore_apply_parser = restore_subparsers.add_parser("apply", help="Create a restore preview")
    restore_apply_parser.add_argument("app", help="App id")
    restore_apply_parser.add_argument("backup_id", help="Backup id")
    restore_apply_parser.add_argument("--confirm", required=True, help="Confirmation token from restore plan")
    restore_apply_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    restore_apply_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    restore_apply_parser.set_defaults(handler=run_restore_apply)


def run_backup_plan(args: Namespace) -> int:
    plan = backup_plan(args.runtime_root, args.app)
    if args.json:
        print_json(plan)
    else:
        print(plan["summary"])
        if plan.get("confirmation_token"):
            print(f"Confirmation token: {plan['confirmation_token']}")
        print_issues("Blockers", plan.get("blockers", []))
        print_issues("Warnings", plan.get("warnings", []))
    return 0 if plan["can_apply"] else 1


def run_backup_status(args: Namespace) -> int:
    report = backup_status_report(
        args.app,
        environment=args.environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
    )
    if args.json:
        print_json(report)
    else:
        print(report["summary"])
        latest = report.get("latest_backup")
        print(f"Latest backup: {latest.get('backup_id') if isinstance(latest, dict) else 'none'}")
        if report["blockers"]:
            print("Blockers:")
            for blocker in report["blockers"]:
                print(f"  - {blocker['message']}")
    return 0 if not report["blockers"] else 1


def run_backup_create(args: Namespace) -> int:
    try:
        report = create_backup(args.runtime_root, args.app, args.confirm)
    except RuntimeError as exc:
        print_error(f"Backup failed: {exc}", "backup_create_failed", json_output=args.json)
        return 1
    if args.json:
        print_json(report)
    else:
        print(report.get("summary") or f"Backup create {report.get('status')}.")
        if report.get("backup_path"):
            print(f"Backup: {report['backup_path']}")
        print_issues("Blockers", report.get("blockers", []))
        print_issues("Warnings", report.get("warnings", []))
    return 0 if report.get("status") == "succeeded" else 1


def run_restore_plan(args: Namespace) -> int:
    plan = restore_plan(args.runtime_root, args.app, args.backup_id)
    if args.json:
        print_json(plan)
    else:
        print(plan["summary"])
        if plan.get("confirmation_token"):
            print(f"Confirmation token: {plan['confirmation_token']}")
        print_issues("Blockers", plan.get("blockers", []))
        print_issues("Warnings", plan.get("warnings", []))
    return 0 if plan["can_apply"] else 1


def run_restore_apply(args: Namespace) -> int:
    try:
        report = apply_restore(args.runtime_root, args.app, args.backup_id, args.confirm)
    except RuntimeError as exc:
        print_error(f"Restore failed: {exc}", "restore_apply_failed", json_output=args.json)
        return 1
    if args.json:
        print_json(report)
    else:
        print(report.get("summary") or f"Restore apply {report.get('status')}.")
        if report.get("preview_path"):
            print(f"Preview: {report['preview_path']}")
        print_issues("Blockers", report.get("blockers", []))
        print_issues("Warnings", report.get("warnings", []))
    return 0 if report.get("status") == "succeeded" else 1


def run_backup_verify_plan(args: Namespace) -> int:
    plan = backup_verify_plan(
        args.app,
        environment=args.environment,
        runtime_root=args.runtime_root,
        backup_id=args.backup_id,
        manifest_path=args.manifest,
    )
    if args.json:
        print_json(plan)
    else:
        print(plan["summary"])
        selected = plan.get("selected_backup") if isinstance(plan.get("selected_backup"), dict) else {}
        print(f"Selected backup: {selected.get('backup_id') or 'none'}")
        print(f"Rehearsal target: {plan.get('rehearsal_target') or 'n/a'}")
        if plan.get("confirmation_token"):
            print(f"Confirmation token: {plan['confirmation_token']}")
        if plan.get("blockers"):
            print("Blockers:")
            for blocker in plan["blockers"]:
                print(f"  - {blocker.get('message') if isinstance(blocker, dict) else blocker}")
    return 0 if not plan.get("blockers") else 1


def run_backup_verify_apply(args: Namespace) -> int:
    receipt = backup_verify_apply(
        args.app,
        environment=args.environment,
        runtime_root=args.runtime_root,
        backup_id=args.backup_id,
        confirm=args.confirm,
        manifest_path=args.manifest,
    )
    if args.json:
        print_json(receipt)
    else:
        print(receipt.get("summary") or receipt.get("error") or f"Backup verification {receipt.get('status')}.")
        for check in receipt.get("checks", []) if isinstance(receipt.get("checks"), list) else []:
            if isinstance(check, dict):
                print(f"  - [{'ok' if check.get('ok') else 'fail'}] {check.get('name')}: {check.get('message')}")
    status = receipt.get("status")
    return 0 if status == "succeeded" else 1


def run_backup_rehearse(args: Namespace) -> int:
    mode = str(args.mode_or_source)
    if mode in {"plan", "apply"}:
        if args.source is None:
            print_error(f"backup rehearse {mode} requires a source artifact.", "backup_rehearse_source_missing", json_output=args.json)
            return 1
        if mode == "plan":
            return run_backup_rehearse_plan(args)
        if not args.confirm:
            print_error("backup rehearse apply requires --confirm.", "confirmation_token_missing", json_output=args.json)
            return 1
        return run_backup_rehearse_apply(args)

    source = Path(mode)
    manifest = _load_rehearsal_manifest(args.manifest, args.json)
    if manifest is None:
        return 1
    environment = args.environment or manifest.environment
    plan = restore_drill_plan(
        manifest.app,
        environment=environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
        source=source,
        operation_base="backup.rehearse",
        readiness_gate=False,
    )
    if plan.get("blockers"):
        if args.json:
            print_json(plan)
        else:
            print(plan["summary"])
            print_issues("Blockers", plan.get("blockers", []))
            print_issues("Warnings", plan.get("warnings", []))
        return 1
    receipt = restore_drill_apply(
        manifest.app,
        environment=environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
        source=source,
        confirm=plan.get("confirmation_token") if isinstance(plan.get("confirmation_token"), str) else None,
        operation_base="backup.rehearse",
        readiness_gate=False,
        extra_receipt_fields={
            "one_shot": True,
            "planned_operation_id": plan.get("operation_id"),
        },
    )
    if args.json:
        print_json(receipt)
    else:
        print(receipt.get("summary") or f"Backup rehearsal {receipt.get('status')}.")
        if receipt.get("drill_path"):
            print(f"Drill: {receipt['drill_path']}")
        print_issues("Blockers", receipt.get("blockers", []))
        print_issues("Warnings", receipt.get("warnings", []))
    return 0 if receipt.get("status") == "succeeded" else 1


def run_backup_rehearse_plan(args: Namespace) -> int:
    manifest = _load_rehearsal_manifest(args.manifest, args.json)
    if manifest is None:
        return 1
    environment = args.environment or manifest.environment
    plan = restore_drill_plan(
        manifest.app,
        environment=environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
        source=args.source,
        operation_base="backup.rehearse",
        readiness_gate=False,
    )
    if args.json:
        print_json(plan)
    else:
        print(plan["summary"])
        if plan.get("confirmation_token"):
            print(f"Confirmation token: {plan['confirmation_token']}")
        print_issues("Blockers", plan.get("blockers", []))
        print_issues("Warnings", plan.get("warnings", []))
    return 0 if not plan.get("blockers") else 1


def run_backup_rehearse_apply(args: Namespace) -> int:
    manifest = _load_rehearsal_manifest(args.manifest, args.json)
    if manifest is None:
        return 1
    environment = args.environment or manifest.environment
    receipt = restore_drill_apply(
        manifest.app,
        environment=environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
        source=args.source,
        confirm=args.confirm,
        operation_base="backup.rehearse",
        readiness_gate=False,
    )
    if args.json:
        print_json(receipt)
    else:
        print(receipt.get("summary") or f"Backup rehearsal {receipt.get('status')}.")
        if receipt.get("drill_path"):
            print(f"Drill: {receipt['drill_path']}")
        print_issues("Blockers", receipt.get("blockers", []))
        print_issues("Warnings", receipt.get("warnings", []))
    return 0 if receipt.get("status") == "succeeded" else 1


def _load_rehearsal_manifest(path: Path, json_output: bool):
    try:
        return load_manifest(path)
    except ManifestError as exc:
        print_error(f"Manifest invalid: {exc}", "manifest_invalid", json_output=json_output)
        return None


register_cli_descriptor(
    CommandDescriptor(
        command="ship backup verify plan",
        operation="backup.verify.plan",
        summary="Read-only plan for verifying a backup: selection, freshness, isolated rehearsal target, and the checks it would run.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command="ship backup verify plan",
        apply_command="ship backup verify apply",
        json_kind="ophelia.plan",
        args_schema={
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "environment": {"type": "string"},
                "backup_id": {"type": "string"},
                "manifest": {"type": "string"},
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["app"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.plan.v1",
        artifacts=[],
        safety_notes=["Read-only. Writes nothing; secret values redacted; cleanup is a separate future plan."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship backup verify apply",
        operation="backup.verify.apply",
        summary="Run a token-gated backup verification in an isolated rehearsal target and write a receipt. Never overwrites production and never deletes.",
        risk="medium",
        mutates_state=True,
        requires_confirmation=True,
        plan_command="ship backup verify plan",
        apply_command="ship backup verify apply",
        json_kind="ophelia.receipt",
        args_schema={
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "environment": {"type": "string"},
                "confirm": {"type": "string"},
                "backup_id": {"type": "string"},
                "manifest": {"type": "string"},
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["app", "confirm"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.receipt.v1",
        artifacts=["restore verification receipt"],
        safety_notes=[
            "Token-gated. Refuses any rehearsal target resolving into a production app dir or the repo. "
            "Writes only an isolated rehearsal area and a receipt; never overwrites production data; never deletes.",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship backup rehearse",
        operation="backup.rehearse.apply",
        summary="One-shot native restore rehearsal for an Ophelia export artifact using the app manifest verifier.",
        risk="medium",
        mutates_state=True,
        requires_confirmation=False,
        plan_command="ship backup rehearse plan",
        apply_command="ship backup rehearse",
        json_kind="ophelia.receipt",
        args_schema={
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "manifest": {"type": "string"},
                "environment": {"type": "string"},
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["source", "manifest"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.receipt.v1",
        artifacts=["restore drill receipt", "rehearsal checks", "isolated extracted data"],
        safety_notes=[
            "Runs plan and apply in one command using the current plan token internally.",
            "Safely rejects archive paths that escape extraction and writes only under restore-drills.",
            "Never writes into live runtime directories.",
        ],
        examples=[
            "ship backup rehearse ./demo.production.export.tar --manifest .ophelia.staging.yml --environment staging --json",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship backup rehearse plan",
        operation="backup.rehearse.plan",
        summary="Read-only plan for rehearsing an export bundle artifact using the app manifest and restore-drill engine.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command="ship backup rehearse plan",
        apply_command="ship backup rehearse apply",
        json_kind="ophelia.plan",
        args_schema={
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "manifest": {"type": "string"},
                "environment": {"type": "string"},
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["source", "manifest"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.plan.v1",
        artifacts=[],
        safety_notes=["Read-only. Derives app/environment from --manifest and emits the token for backup rehearse apply."],
        examples=[
            "ship backup rehearse plan ./demo.production.export.tar --manifest .ophelia.yml --json",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship backup rehearse apply",
        operation="backup.rehearse.apply",
        summary="Run a token-gated export-artifact rehearsal, safely extract data into an isolated drill directory, and run app-owned volume verifier commands.",
        risk="medium",
        mutates_state=True,
        requires_confirmation=True,
        plan_command="ship backup rehearse plan",
        apply_command="ship backup rehearse apply",
        json_kind="ophelia.receipt",
        args_schema={
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "manifest": {"type": "string"},
                "environment": {"type": "string"},
                "runtime_root": {"type": "string"},
                "confirm": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["source", "manifest", "confirm"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.receipt.v1",
        artifacts=["restore drill receipt", "rehearsal checks", "isolated extracted data"],
        safety_notes=[
            "Token-gated. Writes only under the app restore-drills area; never overwrites active runtime data; secret values redacted.",
        ],
        examples=[
            "ship backup rehearse apply ./demo.production.export.tar --manifest .ophelia.yml --confirm <token> --json",
        ],
    )
)
