from argparse import Namespace, _SubParsersAction
import json
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..manifest import ManifestError, load_manifest
from ..planning import deploy_plan
from ..remote import RemoteError, stage_remote_bundle
from ..runtime import apply_local_bundle, deploy_bundle, update_current_release_verification
from ..verify import run_verifications


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
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Show the deploy plan without writing runtime state",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON for --plan")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Run post-deploy verification checks after --apply succeeds",
    )
    parser.add_argument(
        "--ophelia-root",
        type=Path,
        default=REPO_ROOT,
        help="Local Ophelia repo root used for shared compose lookups",
    )
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        print(f"Manifest invalid: {exc}")
        return 1

    if args.verify and not args.apply:
        print("`--verify` requires `--apply`.")
        return 1
    if args.plan and args.apply:
        print("`--plan` cannot be combined with `--apply`.")
        return 1
    if args.json and not args.plan:
        print("`--json` is currently supported for `deploy --plan`.")
        return 1

    if args.plan:
        plan = deploy_plan(manifest, args.manifest, args.runtime_root)
        if args.json:
            print(json.dumps(plan, indent=2, sort_keys=True))
        else:
            print(plan["summary"])
            print(f"App: {plan['app']}")
            print(f"Environment: {plan['environment'] or 'unknown'}")
            print("Services: " + (", ".join(plan["services_affected"]) or "none"))
            print("Domains: " + (", ".join(plan["domains"]) or "none"))
            print("Generated files:")
            for path in plan["generated_files"]:
                print(f"  - {path}")
            print("Changed files:")
            for item in plan["changed_files"]:
                print(f"  - {item['path']} ({item['change']})")
            if not plan["changed_files"]:
                print("  none")
            print("Risk notes:")
            for note in plan["risk_notes"]:
                print(f"  - {note}")
            if not plan["risk_notes"]:
                print("  none")
        return 0

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
        if args.verify:
            verification = run_verifications(manifest)
            for item in verification["results"]:
                prefix = "✓" if item["ok"] else "✗"
                detail = f"HTTP {item['status_code']}" if item["status_code"] is not None else "request failed"
                print(f"{prefix} {item['name']}: {detail} -> {item['url']}")
            if not verification["ok"]:
                return 1
        return 0

    if args.apply:
        app_root = apply_local_bundle(
            manifest=manifest,
            manifest_path=args.manifest,
            runtime_root=args.runtime_root,
            ophelia_root=args.ophelia_root,
        )
        print(f"Applied bundle for {manifest.app} into {app_root}")
        if args.verify:
            verification = run_verifications(manifest)
            update_current_release_verification(args.runtime_root, manifest.app, verification)
            for item in verification["results"]:
                prefix = "✓" if item["ok"] else "✗"
                detail = f"HTTP {item['status_code']}" if item["status_code"] is not None else "request failed"
                print(f"{prefix} {item['name']}: {detail} -> {item['url']}")
            return 0 if verification["ok"] else 1
        return 0

    app_root = deploy_bundle(manifest, args.manifest, args.runtime_root)
    print(f"Deployed bundle for {manifest.app} into {app_root}")
    print("Use --apply to activate locally, or --host to stage/apply on the VPS.")
    return 0
