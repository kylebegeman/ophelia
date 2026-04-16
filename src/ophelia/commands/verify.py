from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..manifest import ManifestError, load_manifest
from ..verify import run_verifications, verification_checks


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("verify", help="Run post-deploy verification checks for an app manifest")
    parser.add_argument("manifest", type=Path, help="Path to the .ophelia manifest")
    parser.add_argument("--timeout", type=int, default=10, help="Per-request timeout in seconds")
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        print(f"Manifest invalid: {exc}")
        return 1

    checks = verification_checks(manifest)
    if not checks:
        print(f"No verification checks configured or inferred for {manifest.app}.")
        return 0

    payload = run_verifications(manifest, timeout=args.timeout)
    print(f"Verification results for {manifest.app}")
    for result in payload["results"]:
        prefix = "✓" if result["ok"] else "✗"
        detail = f"HTTP {result['status_code']}" if result["status_code"] is not None else "request failed"
        print(f"  {prefix} {result['name']}: {detail} -> {result['url']}")
        if result.get("error"):
            print(f"    {result['error']}")

    return 0 if payload["ok"] else 1
