import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..manifest import ManifestError, load_manifest
from ..manifest_v2 import ManifestV2Error, load_manifest_v2
from ..operation_schema import error_envelope
from ..verify import verification_checks


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("validate", help="Validate an app manifest")
    parser.add_argument("manifest", type=Path, help="Path to the .ophelia manifest")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    try:
        import yaml

        raw = yaml.safe_load(args.manifest.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("version") == 2:
            manifest_v2 = load_manifest_v2(args.manifest)
            report = {
                "ok": True,
                "app": manifest_v2.app,
                "kind": "workloads",
                "environment": manifest_v2.environment,
                "profile": None,
                "routes": len(manifest_v2.routes),
                "services": [item.name for item in manifest_v2.workloads],
                "workloads": [
                    {"name": item.name, "kind": item.kind.value}
                    for item in manifest_v2.workloads
                ],
                "migrations": [item.name for item in manifest_v2.migrations],
                "addons": {"postgres": False, "redis": False},
                "verification_checks": sum(
                    1
                    for item in manifest_v2.workloads
                    if item.readiness is not None
                ),
                "explicit_verification_checks": sum(
                    1
                    for item in manifest_v2.workloads
                    if item.readiness is not None
                ),
                "manifest_version": 2,
                "manifest_digest": manifest_v2.canonical_digest(),
            }
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(f"Manifest valid: {manifest_v2.app}")
                print("  version: 2")
                print("  workloads: " + ", ".join(item.name for item in manifest_v2.workloads))
            return 0
        manifest = load_manifest(args.manifest)
    except (ManifestError, ManifestV2Error, OSError, ValueError) as exc:
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {"ok": False, **error_envelope(str(exc), "manifest_invalid")},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 1
        print(f"Manifest invalid: {exc}")
        return 1

    checks = verification_checks(manifest)
    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "app": manifest.app,
                    "kind": manifest.kind,
                    "environment": getattr(manifest, "environment", None),
                    "profile": manifest.profile,
                    "routes": len(manifest.routes),
                    "services": sorted(manifest.services.keys()),
                    "addons": {
                        "postgres": manifest.addons.postgres,
                        "redis": manifest.addons.redis,
                    },
                    "verification_checks": len(checks),
                    "explicit_verification_checks": len(manifest.verify),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(f"Manifest valid: {manifest.app}")
    print(f"  kind: {manifest.kind}")
    print(f"  environment: {getattr(manifest, 'environment', None) or 'unknown'}")
    print(f"  profile: {manifest.profile or 'none'}")
    print(f"  routes: {len(manifest.routes)}")
    print(f"  services: {', '.join(manifest.services.keys()) or 'none'}")
    print(f"  postgres: {'yes' if manifest.addons.postgres else 'no'}")
    print(f"  redis: {'yes' if manifest.addons.redis else 'no'}")
    if checks:
        kind = "explicit" if manifest.verify else "inferred"
        print(f"  verify: {len(checks)} {kind} checks")
    return 0
