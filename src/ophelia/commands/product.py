from __future__ import annotations

import json
import os
import re
import stat
from argparse import Namespace, _SubParsersAction
from pathlib import Path
from typing import Dict

from ..command_catalog import (
    CommandDescriptor,
    PLAN_SCHEMA_REF,
    RECEIPT_SCHEMA_REF,
    REPORT_SCHEMA_REF,
    register_cli_descriptor,
)
from ..config import DEFAULT_RUNTIME_ROOT
from ..execution import (
    JournaledExecutorError,
    OperationStoreError,
    RuntimeFenceError,
)
from ..execution.process_backend import ProductProcessBackendError
from ..product_bundle import (
    ProductBundleError,
    bundle_report,
    load_product_operations_bundle,
    verify_product_artifact,
)
from ..product_execution import (
    ProductExecutionError,
    apply_product_release,
    product_release_plan,
)
from ..product_recovery import (
    ProductRecoveryError,
    apply_product_restore_drill,
    create_product_backup,
    product_backup_plan,
    product_restore_drill_plan,
)
from ._output import print_error, print_json


_PRODUCT_COMMAND_ERRORS = (
    ProductBundleError,
    ProductExecutionError,
    ProductRecoveryError,
    JournaledExecutorError,
    OperationStoreError,
    RuntimeFenceError,
    ProductProcessBackendError,
    OSError,
    ValueError,
)


def register(subparsers: _SubParsersAction) -> None:
    product = subparsers.add_parser(
        "product", help="Consume and execute product operations bundles"
    )
    commands = product.add_subparsers(dest="product_command")

    validate = commands.add_parser("validate", help="Validate a product operations bundle")
    validate.add_argument("bundle", type=Path, help="Path to .product/operations")
    validate.add_argument("--artifact", type=Path, help="Optional artifact bytes to verify")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(handler=run_validate)

    _register_release_pair(commands, "release", "Execute a product release")
    _register_release_pair(commands, "rollback", "Roll back to a retained product release")

    backup = commands.add_parser("backup", help="Create contract-driven product backups")
    backup_commands = backup.add_subparsers(dest="product_backup_command")
    for action in ("plan", "apply"):
        parser = backup_commands.add_parser(action)
        _bundle_runtime_args(parser)
        parser.add_argument("--backup-id", required=True)
        parser.add_argument("--dataset", action="append", default=[], metavar="ID=PATH")
        parser.add_argument("--env-file", type=Path)
        if action == "apply":
            parser.add_argument("--confirm", required=True)
            parser.set_defaults(handler=run_backup_apply)
        else:
            parser.set_defaults(handler=run_backup_plan)

    restore = commands.add_parser(
        "restore-drill", help="Restore and boot a backup in an isolated target"
    )
    restore_commands = restore.add_subparsers(dest="product_restore_command")
    for action in ("plan", "apply"):
        parser = restore_commands.add_parser(action)
        _bundle_runtime_args(parser)
        parser.add_argument("--backup-id", required=True)
        parser.add_argument("--drill-id", required=True)
        parser.add_argument("--env-file", type=Path)
        if action == "apply":
            parser.add_argument("--confirm", required=True)
            parser.set_defaults(handler=run_restore_apply)
        else:
            parser.set_defaults(handler=run_restore_plan)


def _register_release_pair(commands, name: str, help_text: str) -> None:
    release = commands.add_parser(name, help=help_text)
    actions = release.add_subparsers(dest=f"product_{name}_command")
    for action in ("plan", "apply"):
        parser = actions.add_parser(action)
        _bundle_runtime_args(parser)
        parser.add_argument("--artifact", type=Path, required=True)
        parser.add_argument("--env-file", type=Path)
        parser.add_argument(
            "--evidence",
            action="append",
            default=[],
            metavar="PRECONDITION=PATH",
            help="Bind reviewed release precondition evidence; repeatable",
        )
        if action == "apply":
            parser.add_argument("--confirm", required=True)
            parser.set_defaults(
                handler=run_release_apply,
                product_operation="rollback.apply" if name == "rollback" else "deploy.apply",
            )
        else:
            parser.set_defaults(
                handler=run_release_plan,
                product_operation="rollback.apply" if name == "rollback" else "deploy.apply",
            )


def _bundle_runtime_args(parser) -> None:
    parser.add_argument("bundle", type=Path, help="Path to .product/operations")
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--host-id")
    parser.add_argument("--json", action="store_true")


def run_validate(args: Namespace) -> int:
    try:
        bundle = load_product_operations_bundle(args.bundle)
        report = bundle_report(bundle)
        if args.artifact is not None:
            artifact_id = str(bundle.runtime["processes"][0]["artifact_id"])
            artifact = verify_product_artifact(
                bundle, artifact_id, args.artifact, require_compatible=False
            )
            report["artifact"] = {
                "id": artifact.artifact_id,
                "digest": artifact.digest,
                "size_bytes": artifact.size_bytes,
                "compatible": artifact.compatible,
                "platform": {
                    "os": artifact.platform_os,
                    "arch": artifact.platform_arch,
                },
            }
    except (ProductBundleError, OSError, ValueError) as exc:
        print_error(str(exc), "product_bundle_invalid", json_output=args.json)
        return 1
    _emit(report, args.json)
    return 0


def run_release_plan(args: Namespace) -> int:
    try:
        report = product_release_plan(
            args.bundle,
            args.artifact,
            runtime_root=args.runtime_root,
            environment_values=_environment(args.env_file),
            host_id=args.host_id,
            operation=args.product_operation,
            precondition_evidence=_bindings(args.evidence, owner="Precondition evidence"),
        )
    except _PRODUCT_COMMAND_ERRORS as exc:
        return _failure(args, exc)
    _emit(report, args.json)
    return 0 if report["can_apply"] else 1


def run_release_apply(args: Namespace) -> int:
    try:
        report = apply_product_release(
            args.bundle,
            args.artifact,
            runtime_root=args.runtime_root,
            environment_values=_environment(args.env_file),
            confirm=args.confirm,
            host_id=args.host_id,
            operation=args.product_operation,
            precondition_evidence=_bindings(args.evidence, owner="Precondition evidence"),
        )
    except _PRODUCT_COMMAND_ERRORS as exc:
        return _failure(args, exc)
    _emit(report, args.json)
    return 0 if report["status"] == "succeeded" else 1


def run_backup_plan(args: Namespace) -> int:
    try:
        report = product_backup_plan(
            args.bundle,
            runtime_root=args.runtime_root,
            backup_id=args.backup_id,
            dataset_bindings=_bindings(args.dataset),
            environment_values=_environment(args.env_file),
            host_id=args.host_id,
        )
    except _PRODUCT_COMMAND_ERRORS as exc:
        return _failure(args, exc)
    _emit(report, args.json)
    return 0 if report["can_apply"] else 1


def run_backup_apply(args: Namespace) -> int:
    try:
        report = create_product_backup(
            args.bundle,
            runtime_root=args.runtime_root,
            backup_id=args.backup_id,
            dataset_bindings=_bindings(args.dataset),
            environment_values=_environment(args.env_file),
            confirm=args.confirm,
            host_id=args.host_id,
        )
    except _PRODUCT_COMMAND_ERRORS as exc:
        return _failure(args, exc)
    _emit(report, args.json)
    return 0 if report["status"] == "succeeded" else 1


def run_restore_plan(args: Namespace) -> int:
    try:
        report = product_restore_drill_plan(
            args.bundle,
            runtime_root=args.runtime_root,
            backup_id=args.backup_id,
            drill_id=args.drill_id,
            environment_values=_environment(args.env_file),
            host_id=args.host_id,
        )
    except _PRODUCT_COMMAND_ERRORS as exc:
        return _failure(args, exc)
    _emit(report, args.json)
    return 0 if report["can_apply"] else 1


def run_restore_apply(args: Namespace) -> int:
    try:
        report = apply_product_restore_drill(
            args.bundle,
            runtime_root=args.runtime_root,
            backup_id=args.backup_id,
            drill_id=args.drill_id,
            environment_values=_environment(args.env_file),
            confirm=args.confirm,
            host_id=args.host_id,
        )
    except _PRODUCT_COMMAND_ERRORS as exc:
        return _failure(args, exc)
    _emit(report, args.json)
    return 0 if report["status"] == "succeeded" else 1


def _environment(path: Path | None) -> Dict[str, str]:
    if path is None:
        return {}
    path = Path(path)
    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode) or path.is_symlink():
        raise ValueError("Environment file must be a real regular file.")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not os.path.samestat(initial, metadata)
        ):
            raise ValueError("Environment file changed while it was opened.")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError(
                "Environment file must not be accessible by group or other users."
            )
        if metadata.st_uid != os.geteuid():
            raise ValueError(
                "Environment file must be owned by the Ophelia process user."
            )
        if metadata.st_size > 1 << 20:
            raise ValueError("Environment file exceeds the 1 MiB safety limit.")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw_bytes = handle.read((1 << 20) + 1)
        final = os.fstat(descriptor)
        if (
            final.st_size != metadata.st_size
            or final.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise ValueError("Environment file changed while it was read.")
        if len(raw_bytes) > 1 << 20:
            raise ValueError("Environment file exceeds the 1 MiB safety limit.")
        raw_text = raw_bytes.decode("utf-8")
    finally:
        os.close(descriptor)
    values: Dict[str, str] = {}
    for number, raw in enumerate(raw_text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        line = raw
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            raise ValueError(f"Environment file line {number} is malformed.")
        name, value = line.split("=", 1)
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", name) is None:
            raise ValueError(f"Environment file line {number} has an invalid name.")
        if name in values or "\x00" in value:
            raise ValueError(f"Environment file line {number} is duplicated or invalid.")
        values[name] = value
    return values


def _bindings(values, *, owner: str = "Dataset bindings") -> Dict[str, Path]:
    result: Dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"{owner} must use ID=PATH.")
        identifier, path = value.split("=", 1)
        if not identifier or not path or identifier in result:
            raise ValueError(f"{owner} must have unique non-empty ids and paths.")
        result[identifier] = Path(path)
    return result


def _failure(args: Namespace, exc: Exception) -> int:
    print_error(str(exc), "product_operation_failed", json_output=args.json)
    return 1


def _emit(report, json_output: bool) -> None:
    if json_output:
        print_json(report)
    else:
        print(report.get("summary") or f"{report.get('operation') or report.get('kind')}: {report.get('status') or 'ok'}")
        if report.get("confirmation_token"):
            print(f"Confirmation token: {report['confirmation_token']}")


_DESCRIPTORS = (
    ("ship product validate", "product.bundle.validate", False, False, REPORT_SCHEMA_REF),
    ("ship product release plan", "product.release.plan", False, False, PLAN_SCHEMA_REF),
    ("ship product release apply", "deploy.apply", True, True, RECEIPT_SCHEMA_REF),
    ("ship product rollback plan", "product.rollback.plan", False, False, PLAN_SCHEMA_REF),
    ("ship product rollback apply", "rollback.apply", True, True, RECEIPT_SCHEMA_REF),
    ("ship product backup plan", "product.backup.plan", False, False, PLAN_SCHEMA_REF),
    ("ship product backup apply", "backup.apply", True, True, RECEIPT_SCHEMA_REF),
    ("ship product restore-drill plan", "product.restore-drill.plan", False, False, PLAN_SCHEMA_REF),
    ("ship product restore-drill apply", "restore-drill.apply", True, True, RECEIPT_SCHEMA_REF),
)


def _descriptor_args(command: str) -> Dict[str, object]:
    properties: Dict[str, object] = {
        "bundle": {"type": "string"},
        "json": {"type": "boolean"},
    }
    required = ["bundle"]
    if command != "ship product validate":
        properties.update(
            {
                "runtime_root": {"type": "string"},
                "host_id": {"type": "string"},
                "env_file": {"type": "string"},
            }
        )
    if " release " in command or " rollback " in command:
        properties["artifact"] = {"type": "string"}
        properties["evidence"] = {"type": "array", "items": {"type": "string"}}
        required.append("artifact")
    elif command == "ship product validate":
        properties["artifact"] = {"type": "string"}
    if " backup " in command:
        properties.update(
            {
                "backup_id": {"type": "string"},
                "dataset": {"type": "array", "items": {"type": "string"}},
            }
        )
        required.append("backup_id")
    if " restore-drill " in command:
        properties.update(
            {
                "backup_id": {"type": "string"},
                "drill_id": {"type": "string"},
            }
        )
        required.extend(("backup_id", "drill_id"))
    if command.endswith(" apply"):
        properties["confirm"] = {"type": "string"}
        required.append("confirm")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _descriptor_examples(command: str) -> list[str]:
    suffix = " .product/operations"
    if command == "ship product validate":
        return [command + suffix + " --artifact ./dist/product-server --json"]
    if " release " in command or " rollback " in command:
        suffix += " --artifact ./dist/product-server --env-file /etc/ophelia/product.env"
    elif " backup " in command:
        suffix += " --backup-id backup-2026-07-14 --env-file /etc/ophelia/product.env"
    else:
        suffix += " --backup-id backup-2026-07-14 --drill-id drill-2026-07-14 --env-file /etc/ophelia/product.env"
    if command.endswith(" apply"):
        suffix += " --confirm <token>"
    return [command + suffix + " --json"]

for command, operation, mutates, confirms, schema in _DESCRIPTORS:
    register_cli_descriptor(
        CommandDescriptor(
            command=command,
            operation=operation,
            summary="Validate or execute a content-addressed product operations contract.",
            risk=(
                "critical"
                if operation in {"deploy.apply", "rollback.apply"}
                else ("high" if mutates else "low")
            ),
            mutates_state=mutates,
            requires_confirmation=confirms,
            plan_command=(command.rsplit(" ", 1)[0] + " plan") if confirms else None,
            apply_command=(command.rsplit(" ", 1)[0] + " apply") if command.endswith(" plan") else (command if confirms else None),
            json_kind="ophelia.receipt" if mutates else ("ophelia.plan" if schema == PLAN_SCHEMA_REF else "ophelia.report"),
            args_schema=_descriptor_args(command),
            output_schema_ref=schema,
            artifacts=["content-addressed product receipts"] if mutates else [],
            safety_notes=["Secret values are loaded from a private host file and never persisted in plans or receipts."],
            examples=_descriptor_examples(command),
        )
    )
