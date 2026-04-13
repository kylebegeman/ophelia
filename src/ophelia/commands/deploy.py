from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT
from ..manifest import ManifestError, load_manifest
from ..remote import RemoteError, stage_remote_bundle
from ..runtime import deploy_bundle


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "deploy",
        help="Write a runtime bundle into the local Ophelia runtime root",
    )
    parser.add_argument("manifest", type=Path, help="Path to the .ophelia manifest")
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT,
        help="Runtime root to deploy into",
    )
    parser.add_argument("--host", help="SSH target such as kyle@209.74.71.165")
    parser.add_argument("--ssh-port", type=int, default=22022, help="SSH port")
    parser.add_argument(
        "--remote-runtime-root",
        default="~/ophelia-runtime",
        help="Remote runtime root used when deploying over SSH",
    )
    parser.add_argument(
        "--remote-ophelia-root",
        default="~/ophelia",
        help="Remote Ophelia repo root",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Start or update the app and reload shared Caddy when available",
    )
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        print(f"Manifest invalid: {exc}")
        return 1

    if args.host:
        try:
            result = stage_remote_bundle(
                manifest=manifest,
                manifest_path=args.manifest,
                host=args.host,
                ssh_port=args.ssh_port,
                remote_runtime_root=args.remote_runtime_root,
                remote_ophelia_root=args.remote_ophelia_root,
                apply=args.apply,
            )
        except RemoteError as exc:
            print(f"Remote deploy failed: {exc}")
            return 1

        mode = "applied" if args.apply else "staged"
        print(f"{mode.capitalize()} bundle for {manifest.app} on {args.host}")
        if result:
            print(result)
        return 0

    app_root = deploy_bundle(manifest, args.manifest, args.runtime_root)
    print(f"Deployed bundle for {manifest.app} into {app_root}")
    print("Use --host to stage or apply this bundle on the VPS.")
    return 0
