from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..explain import explain_manifest
from ..manifest import ManifestError, load_manifest


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("explain", help="Explain what a manifest declares")
    parser.add_argument("manifest", type=Path, help="Path to the .ophelia manifest")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        print(f"Manifest invalid: {exc}")
        return 1

    report = explain_manifest(manifest, args.manifest)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        print(f"App: {report['app']}")
        print(f"Kind: {report['kind']}")
        print(f"Profile: {report['profile'] or 'none'}")
        print(f"Environment: {report['environment'] or 'unknown'}")
        print("Domains: " + (", ".join(report["domains"]) or "none"))
        print("Services: " + (", ".join(service["name"] for service in report["services"]) or "none"))
        print("Required secrets: " + (", ".join(item["key"] for item in report["required_secrets"]) or "none"))
        print("Verification checks: " + str(len(report["verification_checks"])))
        policy = report["verification_policy"]
        print(
            "Verification policy: "
            f"attempts={policy['attempts']} interval={policy['interval']}s "
            f"timeout={policy['timeout']}s failure_mode={policy['failure_mode']}"
        )
        if report["risk_notes"]:
            print("Risk notes:")
            for note in report["risk_notes"]:
                print(f"  - {note}")
    return 0
