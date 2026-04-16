from argparse import ArgumentParser, Namespace, _SubParsersAction
from pathlib import Path

from ..manifest import ManifestError, load_manifest


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("validate", help="Validate an app manifest")
    parser.add_argument("manifest", type=Path, help="Path to the .ophelia manifest")
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        print(f"Manifest invalid: {exc}")
        return 1

    print(f"Manifest valid: {manifest.app}")
    print(f"  kind: {manifest.kind}")
    print(f"  profile: {manifest.profile or 'none'}")
    print(f"  routes: {len(manifest.routes)}")
    print(f"  services: {', '.join(manifest.services.keys()) or 'none'}")
    print(f"  postgres: {'yes' if manifest.addons.postgres else 'no'}")
    print(f"  redis: {'yes' if manifest.addons.redis else 'no'}")
    if manifest.verify:
        print(f"  verify: {len(manifest.verify)} explicit checks")
    return 0
