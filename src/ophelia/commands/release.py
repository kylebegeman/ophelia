from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, PLAN_SCHEMA_REF, RECEIPT_SCHEMA_REF, register_cli_descriptor
from ..config import DEFAULT_RUNTIME_ROOT
from ..image_lock import image_lock_apply, image_lock_plan
from ..runtime import list_releases, load_release


def register(subparsers: _SubParsersAction) -> None:
    releases_parser = subparsers.add_parser("releases", help="List release records for an app")
    releases_parser.add_argument("app", help="App id")
    releases_parser.add_argument(
        "--runtime-root",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT,
        help="Runtime root to inspect",
    )
    releases_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    releases_parser.set_defaults(handler=run_releases)

    release_parser = subparsers.add_parser("release", help="Inspect one release record")
    release_subparsers = release_parser.add_subparsers(dest="release_command")
    show_parser = release_subparsers.add_parser("show", help="Show a release record")
    show_parser.add_argument("app", help="App id")
    show_parser.add_argument("release_id", help="Release id, 'current' for latest, or 'active'")
    show_parser.add_argument(
        "--runtime-root",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT,
        help="Runtime root to inspect",
    )
    show_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    show_parser.set_defaults(handler=run_release_show)

    image_lock = release_subparsers.add_parser("image-lock", help="Resolve image tags to immutable digest locks")
    image_lock_subparsers = image_lock.add_subparsers(dest="release_image_lock_command")

    image_lock_plan_parser = image_lock_subparsers.add_parser("plan", help="Plan production image digest locking")
    image_lock_plan_parser.add_argument("manifest", type=Path, help="Path to the .ophelia manifest")
    image_lock_plan_parser.add_argument("--output", type=Path, help="Image lock JSON output path")
    image_lock_plan_parser.add_argument("--pinned-manifest", type=Path, help="Optional pinned manifest copy to write during apply")
    image_lock_plan_parser.add_argument("--timeout", type=float, default=30.0, help="Digest resolver timeout in seconds")
    image_lock_plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    image_lock_plan_parser.set_defaults(handler=run_image_lock_plan)

    image_lock_apply_parser = image_lock_subparsers.add_parser("apply", help="Write confirmed image digest lock artifacts")
    image_lock_apply_parser.add_argument("manifest", type=Path, help="Path to the .ophelia manifest")
    image_lock_apply_parser.add_argument("--output", type=Path, help="Image lock JSON output path")
    image_lock_apply_parser.add_argument("--pinned-manifest", type=Path, help="Optional pinned manifest copy to write")
    image_lock_apply_parser.add_argument("--timeout", type=float, default=30.0, help="Digest resolver timeout in seconds")
    image_lock_apply_parser.add_argument("--confirm", required=True, help="Confirmation token from release image-lock plan")
    image_lock_apply_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    image_lock_apply_parser.set_defaults(handler=run_image_lock_apply)


def run_releases(args: Namespace) -> int:
    records = list_releases(args.runtime_root, args.app)
    if args.json:
        print(json.dumps({"app": args.app, "releases": records}, indent=2, sort_keys=True))
        return 0
    if not records:
        print(f"No releases found for {args.app} in {args.runtime_root}")
        return 0

    print("RELEASE ID\tDEPLOYED AT\tENVIRONMENT\tACTIVE\tLATEST\tAPPLIED\tVERIFIED\tVERIFY\tMANIFEST HASH")
    for record in records:
        verification = record.get("verification")
        if isinstance(verification, dict):
            verify_status = str(verification.get("status") or verification.get("ok") or "unknown")
        else:
            verify_status = "unknown"
        applied = _bool_status(record.get("applied"), none="unknown")
        verified = _bool_status(record.get("verified"), none="not_run")
        print(
            "\t".join(
                [
                    str(record.get("release_id", "")),
                    str(record.get("deployed_at", "")),
                    str(record.get("environment") or "unknown"),
                    _bool_status(record.get("active"), none="false"),
                    _bool_status(record.get("latest"), none="false"),
                    applied,
                    verified,
                    verify_status,
                    str(record.get("manifest_hash") or "")[:12],
                ]
            )
        )
    return 0


def _bool_status(value, none: str) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return none


def run_release_show(args: Namespace) -> int:
    try:
        record = load_release(args.runtime_root, args.app, args.release_id)
    except FileNotFoundError as exc:
        print(str(exc))
        return 1

    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def run_image_lock_plan(args: Namespace) -> int:
    try:
        plan = image_lock_plan(
            args.manifest,
            output=args.output,
            pinned_manifest=args.pinned_manifest,
            timeout=args.timeout,
        )
    except Exception as exc:  # noqa: BLE001 - keep CLI errors clear and concise
        return _print_image_lock_error(exc, args.json)

    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print(plan["summary"])
        for blocker in plan.get("blockers", []):
            if isinstance(blocker, dict):
                print(f"  blocker: {blocker.get('code')}: {blocker.get('message')}")
        for warning in plan.get("warnings", []):
            if isinstance(warning, dict):
                print(f"  warning: {warning.get('code')}: {warning.get('message')}")
        if plan.get("confirmation_token"):
            print(f"Confirm: {plan['confirmation_token']}")
    return 0 if not plan.get("blockers") else 1


def run_image_lock_apply(args: Namespace) -> int:
    try:
        receipt = image_lock_apply(
            args.manifest,
            output=args.output,
            pinned_manifest=args.pinned_manifest,
            confirm=args.confirm,
            timeout=args.timeout,
        )
    except Exception as exc:  # noqa: BLE001 - keep CLI errors clear and concise
        return _print_image_lock_error(exc, args.json)

    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(receipt["summary"])
        if receipt.get("output"):
            print(f"Image lock: {receipt['output']}")
        if receipt.get("pinned_manifest"):
            print(f"Pinned manifest: {receipt['pinned_manifest']}")
    return 0 if receipt.get("status") == "succeeded" else 1


def _print_image_lock_error(exc: Exception, emit_json: bool) -> int:
    payload = {
        "schema_version": 1,
        "kind": "ophelia.error",
        "status": "failed",
        "error": str(exc),
        "blockers": [{"code": "image_lock_failed", "message": str(exc)}],
        "warnings": [],
    }
    if emit_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Image lock failed: {exc}")
    return 1


register_cli_descriptor(
    CommandDescriptor(
        command="ship release image-lock plan",
        operation="release.image-lock.plan",
        summary="Resolve manifest image tags to immutable digest lock artifacts without writing files.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command="ship release image-lock apply",
        json_kind="ophelia.plan",
        args_schema={
            "type": "object",
            "properties": {
                "manifest": {"type": "string"},
                "output": {"type": "string"},
                "pinned_manifest": {"type": "string"},
                "timeout": {"type": "number"},
                "json": {"type": "boolean"},
            },
            "required": ["manifest"],
            "additionalProperties": False,
        },
        output_schema_ref=PLAN_SCHEMA_REF,
        artifacts=["image lock JSON path", "optional pinned manifest copy"],
        safety_notes=["Read-only. Uses Docker registry credentials if Docker is configured, but never prints credentials."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship release image-lock apply",
        operation="release.image-lock.apply",
        summary="Write confirmed image digest lock artifacts for a manifest.",
        risk="medium",
        mutates_state=True,
        requires_confirmation=True,
        plan_command="ship release image-lock plan",
        apply_command="ship release image-lock apply",
        json_kind="ophelia.receipt",
        args_schema={
            "type": "object",
            "properties": {
                "manifest": {"type": "string"},
                "output": {"type": "string"},
                "pinned_manifest": {"type": "string"},
                "timeout": {"type": "number"},
                "confirm": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["manifest", "confirm"],
            "additionalProperties": False,
        },
        output_schema_ref=RECEIPT_SCHEMA_REF,
        artifacts=["image lock JSON path", "optional pinned manifest copy"],
        safety_notes=["Writes only explicit output files. Does not mutate runtime state or deploy apps."],
    )
)
