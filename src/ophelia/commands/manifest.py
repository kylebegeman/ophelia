from __future__ import annotations

import json
import uuid
from argparse import Namespace, _SubParsersAction
from pathlib import Path

import yaml

from ..config import DEFAULT_RUNTIME_ROOT
from ..daemon.client import DaemonClient, DaemonClientError
from ..daemon.config import DEFAULT_SOCKET_PATH
from ..manifest import ManifestError, load_manifest
from ..manifest_v2 import ManifestV2Error, load_manifest_v2, migrate_v1_document
from ..manifest_v2_execution import (
    apply_manifest_v2_plan,
    local_approval_key,
    plan_manifest_v2,
)
from ..runtime_secrets import RuntimeSecretResolver
from ._output import print_error, print_json


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "manifest", help="Check, migrate, plan, and apply versioned manifests"
    )
    commands = parser.add_subparsers(dest="manifest_command")

    check = commands.add_parser("check", help="Strictly validate a v1 or v2 manifest")
    check.add_argument("manifest", type=Path)
    check.add_argument("--json", action="store_true")
    check.set_defaults(handler=run_check)

    migrate = commands.add_parser("migrate", help="Generate an explicit v2 candidate from v1")
    migrate.add_argument("manifest", type=Path)
    migrate.add_argument("--to", type=int, choices=[2], required=True)
    migrate.add_argument("--output", type=Path)
    migrate.add_argument("--json", action="store_true")
    migrate.set_defaults(handler=run_migrate)

    plan = commands.add_parser("plan", help="Calculate a read-only manifest v2 activation plan")
    plan.add_argument("manifest", type=Path)
    plan.add_argument("--runtime-root", type=Path)
    plan.add_argument("--host-id")
    plan.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
    plan.add_argument("--direct", action="store_true")
    plan.add_argument("--idempotency-key")
    plan.add_argument("--allow-missing-edge", action="store_true")
    plan.add_argument("--json", action="store_true")
    plan.set_defaults(handler=run_plan)

    apply = commands.add_parser("apply", help="Submit an exact reviewed manifest v2 plan")
    apply.add_argument("plan_id")
    apply.add_argument("--confirm", required=True)
    apply.add_argument("--runtime-root", type=Path)
    apply.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
    apply.add_argument("--direct", action="store_true")
    apply.add_argument("--idempotency-key")
    apply.add_argument("--allow-missing-edge", action="store_true")
    apply.add_argument("--json", action="store_true")
    apply.set_defaults(handler=run_apply)


def run_check(args: Namespace) -> int:
    try:
        version = _document(args.manifest).get("version")
        if version == 2:
            manifest = load_manifest_v2(args.manifest)
            report = {
                "ok": True,
                "schema_version": 1,
                "kind": "ophelia.manifest-check",
                "manifest_version": 2,
                "app": manifest.app,
                "environment": manifest.environment,
                "workloads": [
                    {"name": item.name, "kind": item.kind.value}
                    for item in manifest.workloads
                ],
                "migrations": [item.name for item in manifest.migrations],
                "routes": [item.name for item in manifest.routes],
                "manifest_digest": manifest.canonical_digest(),
            }
        elif version == 1:
            manifest = load_manifest(args.manifest)
            report = {
                "ok": True,
                "schema_version": 1,
                "kind": "ophelia.manifest-check",
                "manifest_version": 1,
                "app": manifest.app,
                "environment": manifest.environment,
                "compatibility_diagnostics": [item.to_dict() for item in manifest.diagnostics],
            }
        else:
            raise ManifestV2Error("Manifest version must be 1 or 2.")
    except (ManifestError, ManifestV2Error, OSError, ValueError) as exc:
        print_error(str(exc), "manifest_invalid", json_output=args.json)
        return 1
    if args.json:
        print_json(report)
    else:
        print("Manifest v%d valid: %s" % (report["manifest_version"], report["app"]))
    return 0


def run_migrate(args: Namespace) -> int:
    try:
        candidate = migrate_v1_document(_document(args.manifest))
        text = yaml.safe_dump(candidate, default_flow_style=False, sort_keys=False)
        # Validate the exact generated document before returning or writing it.
        if args.output is not None:
            output = Path(args.output)
            if output.exists():
                raise ValueError("Refusing to overwrite an existing migration output.")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="utf-8")
            load_manifest_v2(output)
        else:
            temporary = args.manifest.parent / ("." + args.manifest.name + ".v2-check")
            try:
                temporary.write_text(text, encoding="utf-8")
                load_manifest_v2(temporary)
            finally:
                temporary.unlink(missing_ok=True)
    except (ManifestV2Error, OSError, ValueError) as exc:
        print_error(str(exc), "manifest_migration_failed", json_output=args.json)
        return 1
    if args.json:
        print_json(
            {
                "ok": True,
                "schema_version": 1,
                "kind": "ophelia.manifest-migration",
                "source": str(args.manifest),
                "output": None if args.output is None else str(args.output),
                "candidate": candidate if args.output is None else None,
            }
        )
    elif args.output is None:
        print(text, end="")
    else:
        print("Wrote manifest v2 candidate: %s" % args.output)
    return 0


def run_plan(args: Namespace) -> int:
    try:
        if args.direct:
            runtime_root = args.runtime_root or DEFAULT_RUNTIME_ROOT
            key = local_approval_key(runtime_root, create=True)
            report = plan_manifest_v2(
                args.manifest,
                runtime_root=runtime_root,
                approval_key=key,
                host_id=args.host_id,
                idempotency_key=args.idempotency_key,
                require_edge_runtime=not args.allow_missing_edge,
                secret_resolver=RuntimeSecretResolver(runtime_root),
            )
        else:
            if args.runtime_root or args.host_id or args.allow_missing_edge:
                raise ValueError(
                    "Runtime root, host identity, and edge policy are daemon-owned; use --direct only for local recovery."
                )
            report = DaemonClient(args.socket).post(
                "/v1/plans",
                {"manifest_path": str(args.manifest.expanduser().resolve(strict=True))},
                idempotency_key=args.idempotency_key or "plan:" + uuid.uuid4().hex,
            )
    except (DaemonClientError, ManifestV2Error, OSError, ValueError) as exc:
        print_error(str(exc), "manifest_plan_failed", json_output=args.json)
        return 1
    if args.json:
        print_json(report)
    else:
        print(report["summary"])
        print("Plan: %s" % report["plan_id"])
        if report.get("confirmation_token"):
            print("Confirmation token: %s" % report["confirmation_token"])
    return 0 if report["can_apply"] else 2


def run_apply(args: Namespace) -> int:
    try:
        if args.direct:
            runtime_root = args.runtime_root or DEFAULT_RUNTIME_ROOT
            key = local_approval_key(runtime_root, create=False)
            report = apply_manifest_v2_plan(
                args.plan_id,
                runtime_root=runtime_root,
                approval_key=key,
                confirmation=args.confirm,
                require_edge_runtime=not args.allow_missing_edge,
                secret_resolver=RuntimeSecretResolver(runtime_root),
            )
        else:
            if args.runtime_root or args.allow_missing_edge:
                raise ValueError(
                    "Runtime root and edge policy are daemon-owned; use --direct only for local recovery."
                )
            report = DaemonClient(args.socket).post(
                "/v1/operations",
                {"plan_id": args.plan_id, "confirmation": args.confirm},
                idempotency_key=args.idempotency_key or "apply:" + args.plan_id,
            )
    except (DaemonClientError, OSError, RuntimeError, ValueError) as exc:
        print_error(str(exc), "manifest_apply_failed", json_output=args.json)
        return 1
    receipt = report.get("receipt") or {}
    if args.json:
        print_json(report)
    else:
        print(
            "Operation %s finished: %s"
            % (report["operation"]["operation_id"], receipt.get("outcome", "accepted"))
        )
        failure = report.get("failure")
        if isinstance(failure, dict):
            print(
                "Failure: phase=%s code=%s action=%s"
                % (
                    failure.get("phase", "unknown"),
                    failure.get("code", "unknown"),
                    failure.get("action", "n/a"),
                )
            )
    if args.direct:
        return 0 if receipt.get("outcome") == "succeeded" else 1
    return (
        0
        if receipt.get("outcome") == "succeeded"
        or report.get("operation", {}).get("state")
        in {"accepted", "planning", "awaiting_approval", "queued", "executing", "succeeded"}
        else 1
    )


def _document(path: Path) -> dict:
    try:
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError("Manifest is not valid YAML: %s" % path) from exc
    if not isinstance(value, dict):
        raise ValueError("Manifest root must be a mapping.")
    return value
