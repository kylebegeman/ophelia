from __future__ import annotations

import json
import subprocess
from argparse import SUPPRESS, Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..execution.staging import (
    ConfirmedStaging,
    StagingError,
    consume_confirmed_staging,
    find_confirmed_staging,
)
from ..manifest import ManifestError, load_manifest
from ..operation_schema import error_envelope
from ..planning import deploy_plan
from ..remote import RemoteError, stage_remote_bundle
from ..runtime import (
    ApplyPhaseError,
    DeployMetadata,
    apply_local_bundle,
    current_release_id,
    deploy_bundle,
    update_current_release_verification,
)
from ..verify import run_verifications, verification_blocks_release


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
    parser.add_argument("--host", help="SSH target such as operator@example-host")
    parser.add_argument("--ssh-port", type=int, default=None, help="SSH port; omit to use SSH config/default port")
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
        "--artifacts-dir",
        type=Path,
        default=None,
        help="Compatibility option restricted to the operation staging tree",
    )
    parser.add_argument(
        "--plan-operation-id",
        default=None,
        help=SUPPRESS,
    )
    parser.add_argument(
        "--confirm",
        help="Confirmation token required for production apply",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Run post-deploy verification checks after --apply succeeds",
    )
    parser.add_argument("--verify-attempts", type=int, help="Override manifest verification attempts")
    parser.add_argument("--verify-interval", type=float, help="Override manifest verification interval in seconds")
    parser.add_argument("--verify-timeout", type=float, help="Override manifest verification request timeout in seconds")
    parser.add_argument("--verify-failure-mode", choices=["hard", "warn"], help="Override manifest verification failure mode")
    parser.add_argument(
        "--release-id",
        help="App release id to inject as OPHELIA_RELEASE_ID (env fallback: OPHELIA_DEPLOY_RELEASE_ID, OPHELIA_RELEASE_ID)",
    )
    parser.add_argument(
        "--commit-sha",
        help="App commit SHA to inject as OPHELIA_COMMIT_SHA (env fallback: OPHELIA_DEPLOY_COMMIT_SHA, OPHELIA_COMMIT_SHA, GITHUB_SHA)",
    )
    parser.add_argument(
        "--build-time",
        help="App build time to inject as OPHELIA_BUILD_TIME (env fallback: OPHELIA_DEPLOY_BUILD_TIME, OPHELIA_BUILD_TIME)",
    )
    parser.add_argument(
        "--ophelia-root",
        type=Path,
        default=REPO_ROOT,
        help="Local Ophelia repo root used for shared compose lookups",
    )
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    requested_metadata = DeployMetadata(
        release_id=args.release_id,
        commit_sha=args.commit_sha,
        build_time=args.build_time,
    )
    confirmed: ConfirmedStaging | None = None
    confirmed_app = _confirmed_app_reference(args.manifest)
    if confirmed_app is not None:
        if not args.apply or not args.confirm:
            print("Confirmed staging references require `--apply --confirm <token>`.")
            return 1
        try:
            confirmed = find_confirmed_staging(args.runtime_root, confirmed_app, args.confirm)
            requested_metadata = _locked_confirmed_metadata(confirmed, requested_metadata)
        except StagingError as exc:
            print(f"Production confirmation rejected: {exc}")
            return 1
        args.manifest = confirmed.manifest_path

    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        if getattr(args, "json", False):
            print(json.dumps(error_envelope(f"Manifest invalid: {exc}", "manifest_invalid"), indent=2, sort_keys=True))
        else:
            print(f"Manifest invalid: {exc}")
        return 1

    if args.verify and not args.apply:
        print("`--verify` requires `--apply`.")
        return 1
    if args.plan and args.apply:
        print("`--plan` cannot be combined with `--apply`.")
        return 1
    if args.json and not args.plan:
        message = "`--json` is currently supported for `deploy --plan`."
        print(json.dumps(error_envelope(message, "json_requires_plan"), indent=2, sort_keys=True))
        return 1
    if args.verify_attempts is not None and args.verify_attempts < 1:
        print("verification attempts must be at least 1.")
        return 1
    if args.verify_interval is not None and args.verify_interval < 0:
        print("verification interval must be 0 or greater.")
        return 1
    if args.verify_timeout is not None and args.verify_timeout <= 0:
        print("verification timeout must be greater than 0.")
        return 1
    deploy_metadata = requested_metadata

    if args.plan:
        if args.host:
            try:
                result = stage_remote_bundle(
                    manifest=manifest,
                    manifest_path=args.manifest,
                    host=args.host,
                    ssh_port=args.ssh_port,
                    remote_runtime_root=args.remote_runtime_root,
                    remote_ophelia_root=args.remote_ophelia_root,
                    apply=False,
                    plan=True,
                    json_output=args.json,
                    deploy_metadata=deploy_metadata,
                )
            except RemoteError as exc:
                print(f"Remote deploy plan failed: {exc}")
                return 1
            if result:
                print(result)
            return 0

        try:
            plan = deploy_plan(
                manifest,
                args.manifest,
                args.runtime_root,
                artifacts_dir=args.artifacts_dir,
                plan_operation_id=getattr(args, "plan_operation_id", None),
                deploy_metadata=deploy_metadata,
            )
        except StagingError as exc:
            if args.json:
                print(json.dumps(error_envelope(str(exc), "plan_staging_failed"), indent=2, sort_keys=True))
            else:
                print(f"Deploy plan failed: {exc}")
            return 1
        if args.json:
            print(json.dumps(plan, indent=2, sort_keys=True))
        else:
            print(plan["summary"])
            print(f"App: {plan['app']}")
            print(f"Environment: {plan['environment'] or 'unknown'}")
            print("Services: " + (", ".join(plan["services_affected"]) or "none"))
            print("Domains: " + (", ".join(plan["domains"]) or "none"))
            policy = plan["verification_policy"]
            print(
                "Verification policy: "
                f"attempts={policy['attempts']} interval={policy['interval']}s "
                f"timeout={policy['timeout']}s failure_mode={policy['failure_mode']}"
            )
            print("Generated files:")
            for path in plan["generated_files"]:
                print(f"  - {path}")
            static_assets = plan.get("static_assets") or {}
            if static_assets.get("mode") and static_assets.get("mode") != "none":
                print("Static assets:")
                print(f"  mode: {static_assets.get('mode')}")
                print(f"  source: {static_assets.get('source')}")
                print(f"  serving root: {static_assets.get('serving_root')}")
                if static_assets.get("managed"):
                    print(f"  change: {static_assets.get('change')}")
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
        if args.apply and manifest.environment == "production" and not args.confirm:
            try:
                result = stage_remote_bundle(
                    manifest=manifest,
                    manifest_path=args.manifest,
                    host=args.host,
                    ssh_port=args.ssh_port,
                    remote_runtime_root=args.remote_runtime_root,
                    remote_ophelia_root=args.remote_ophelia_root,
                    apply=False,
                    plan=True,
                    json_output=False,
                    deploy_metadata=deploy_metadata,
                )
            except RemoteError as exc:
                print(f"Remote deploy plan failed: {exc}")
                return 1
            if result:
                print(result)
            print("Remote production apply requires the confirmation token from the remote plan above.")
            print("Re-run the same command with `--confirm <token>` after reviewing that plan.")
            return 1

        try:
            result = stage_remote_bundle(
                manifest=manifest,
                manifest_path=args.manifest,
                host=args.host,
                ssh_port=args.ssh_port,
                remote_runtime_root=args.remote_runtime_root,
                remote_ophelia_root=args.remote_ophelia_root,
                apply=args.apply,
                confirm=args.confirm,
                verify=args.verify,
                verify_attempts=args.verify_attempts,
                verify_interval=args.verify_interval,
                verify_timeout=args.verify_timeout,
                verify_failure_mode=args.verify_failure_mode,
                deploy_metadata=deploy_metadata,
            )
        except RemoteError as exc:
            print(f"Remote deploy failed: {exc}")
            return 1

        mode = "applied" if args.apply else "staged"
        print(f"{mode.capitalize()} bundle for {manifest.app} on {args.host}")
        if result:
            print(result)
        return 0

    plan = None
    if args.apply and manifest.environment == "production" and args.confirm:
        if confirmed is None:
            try:
                confirmed = find_confirmed_staging(args.runtime_root, manifest.app, args.confirm)
                deploy_metadata = _locked_confirmed_metadata(confirmed, deploy_metadata)
                args.manifest = confirmed.manifest_path
                manifest = load_manifest(args.manifest)
            except (StagingError, ManifestError) as exc:
                print(f"Production confirmation rejected: {exc}")
                return 1
    else:
        try:
            plan = deploy_plan(
                manifest,
                args.manifest,
                args.runtime_root,
                deploy_metadata=deploy_metadata,
            )
        except StagingError as exc:
            print(f"Deploy plan failed: {exc}")
            return 1
    if (
        args.apply
        and manifest.environment == "production"
        and plan is not None
        and plan.get("blockers")
    ):
        codes = sorted(
            str(item.get("code"))
            for item in plan["blockers"]
            if isinstance(item, dict) and item.get("code")
        )
        detail = ", ".join(codes) or "policy_blocked"
        print(f"Production apply blocked by policy: {detail}.")
        return 1
    if args.apply and manifest.environment == "production" and not args.confirm:
        expected = plan.get("confirmation_token") if plan is not None else "unknown"
        print(
            "Production apply requires confirmation token "
            f"{expected}. Run `ship deploy {args.manifest} --plan` first."
        )
        return 1

    if args.apply:
        try:
            app_root = apply_local_bundle(
                manifest=manifest,
                manifest_path=args.manifest,
                runtime_root=args.runtime_root,
                ophelia_root=args.ophelia_root,
                deploy_metadata=deploy_metadata,
                candidate_root=confirmed.staging.candidate if confirmed is not None else None,
                candidate_generated_files=(
                    list(confirmed.generated_files) if confirmed is not None else None
                ),
                expected_candidate_digest=(
                    confirmed.candidate_digest if confirmed is not None else None
                ),
                expected_bundle_hash=(
                    confirmed.rendered_bundle_hash if confirmed is not None else None
                ),
                expected_baseline_digest=(
                    confirmed.baseline_digest if confirmed is not None else None
                ),
            )
        except ApplyPhaseError as exc:
            print(f"Local apply failed during {exc.phase}: {exc}")
            return 1
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            print(f"Local apply failed: {exc}")
            return 1
        if confirmed is not None:
            try:
                consume_confirmed_staging(confirmed)
            except StagingError as exc:
                print(f"Apply succeeded, but confirmation consumption failed: {exc}")
                return 1
        print(f"Applied bundle for {manifest.app} into {app_root}")
        release_id = current_release_id(args.runtime_root, manifest.app) or "unknown"
        verified = "not_run"
        print(f"Apply result: app={manifest.app} release={release_id} applied=true verified={verified} runtime={app_root}")
        if args.verify:
            try:
                verification = _run_verification(manifest, args)
            except ValueError as exc:
                print(str(exc))
                return 1
            update_current_release_verification(args.runtime_root, manifest.app, verification)
            _print_verification(manifest.app, verification)
            if not verification["ok"]:
                print(
                    "Verify result: "
                    f"app={manifest.app} release={release_id} applied=true verified=false "
                    f"phase={verification.get('phase')} failure_mode={verification.get('failure_mode')} "
                    f"rerun=\"ship verify {manifest.app} --runtime-root {args.runtime_root}\""
                )
            else:
                print(f"Verify result: app={manifest.app} release={release_id} applied=true verified=true")
            return 1 if verification_blocks_release(verification) else 0
        return 0

    app_root = deploy_bundle(manifest, args.manifest, args.runtime_root, deploy_metadata=deploy_metadata)
    print(f"Deployed bundle for {manifest.app} into {app_root}")
    print("Use --apply to activate locally, or --host to stage/apply on the VPS.")
    return 0


def _confirmed_app_reference(path: Path) -> str | None:
    value = str(path)
    prefix = "@confirmed:"
    if not value.startswith(prefix):
        return None
    app = value[len(prefix) :]
    return app or None


def _locked_confirmed_metadata(
    confirmed: ConfirmedStaging,
    requested: DeployMetadata,
) -> DeployMetadata:
    payload = confirmed.binding.get("deploy_metadata")
    if not isinstance(payload, dict):
        raise StagingError("Confirmed plan deploy metadata is missing.")
    values: dict[str, str] = {}
    for field in ("release_id", "commit_sha", "build_time"):
        bound = payload.get(field)
        if not isinstance(bound, str):
            raise StagingError(f"Confirmed plan deploy metadata is invalid: {field}")
        requested_value = getattr(requested, field)
        if requested_value and requested_value != bound:
            raise StagingError(f"Confirmed plan deploy metadata does not match --{field.replace('_', '-')}.")
        values[field] = bound
    return DeployMetadata(**values, locked=True)


def _run_verification(manifest, args: Namespace):
    return run_verifications(
        manifest,
        timeout=args.verify_timeout,
        attempts=args.verify_attempts,
        interval=args.verify_interval,
        failure_mode=args.verify_failure_mode,
        runtime_root=args.runtime_root,
    )


def _print_verification(app: str, verification: dict) -> None:
    attempt_detail = ""
    if verification.get("attempts", 1) > 1:
        attempt_detail = f" after attempt {verification.get('attempt')}/{verification.get('attempts')}"
    print(
        f"Verification results for {app}{attempt_detail}: "
        f"phase={verification.get('phase')} status={verification.get('status')} "
        f"failure_mode={verification.get('failure_mode')}"
    )
    for item in verification["results"]:
        prefix = "ok" if item["ok"] else "failed"
        if item.get("type") == "command":
            detail = f"exit {item.get('returncode')}"
        else:
            detail = f"HTTP {item['status_code']}" if item.get("status_code") is not None else "request failed"
        phase = item.get("phase") or verification.get("phase")
        target = item.get("url") or item.get("command") or item.get("service") or ""
        print(f"  {prefix} {item['name']}: phase={phase} {detail} -> {target}")
        if item.get("error"):
            kind = f"{item.get('error_kind')}: " if item.get("error_kind") else ""
            print(f"    {kind}{item['error']}")
