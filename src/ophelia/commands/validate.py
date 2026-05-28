import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..manifest import ManifestError, load_manifest
from ..verify import verification_checks


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("validate", help="Validate an app manifest")
    parser.add_argument("manifest", type=Path, help="Path to the .ophelia manifest")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
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
