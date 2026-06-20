from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..manifest import ManifestError, load_manifest
from ..portability import pack_explain_report, pack_validation_report


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("pack", help="Validate and explain portable app packs")
    pack_subparsers = parser.add_subparsers(dest="pack_command")

    validate = pack_subparsers.add_parser("validate", help="Validate a portable app pack")
    validate.add_argument("manifest", type=Path, help="Path to the .ophelia manifest")
    validate.add_argument(
        "--manifest-dir",
        type=Path,
        help="Directory to scan for route ownership conflicts, default is the manifest directory",
    )
    validate.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    validate.set_defaults(handler=run_validate)

    explain = pack_subparsers.add_parser("explain", help="Explain a portable app pack")
    explain.add_argument("manifest", type=Path, help="Path to the .ophelia manifest")
    explain.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    explain.set_defaults(handler=run_explain)


def run_validate(args: Namespace) -> int:
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        return _print_error(str(exc), args.json)

    manifest_dir = args.manifest_dir or args.manifest.parent
    report = pack_validation_report(manifest, args.manifest, manifest_dir=manifest_dir)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        _print_issues("Errors", report["errors"])
        _print_issues("Warnings", report["warnings"])
    return 0 if report["ok"] else 1


def run_explain(args: Namespace) -> int:
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        return _print_error(str(exc), args.json)

    report = pack_explain_report(manifest, args.manifest)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        print(f"App: {report['app']}")
        print(f"Environment: {report['environment'] or 'unknown'}")
        print(f"Portability: {report['portability']}")
        print(f"Owner: {report['owner'] or 'unspecified'}")
        print("Inferred data contracts: " + (", ".join(report["inferred_data_contracts"]) or "none"))
        readiness = report["movement_readiness"]
        print(f"Pack validation: {'ok' if readiness['pack_validation_ok'] else 'blocked'}")
        _print_issues("Errors", readiness["errors"])
        _print_issues("Warnings", readiness["warnings"])
    return 0


def _print_error(message: str, emit_json: bool) -> int:
    if emit_json:
        print(json.dumps({"ok": False, "error": message}, indent=2, sort_keys=True))
    else:
        print(f"Manifest invalid: {message}")
    return 1


def _print_issues(label: str, issues: object) -> None:
    items = issues if isinstance(issues, list) else []
    if not items:
        print(f"{label}: none")
        return
    print(f"{label}:")
    for item in items:
        if isinstance(item, dict):
            print(f"  - {item.get('code')}: {item.get('message')}")
        else:
            print(f"  - {item}")
