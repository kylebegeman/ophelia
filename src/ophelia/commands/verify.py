from __future__ import annotations

from argparse import Namespace, _SubParsersAction
import json
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT
from ..manifest import ManifestError, load_manifest
from ..runtime import update_current_release_verification
from ..verify import run_verifications, verification_blocks_release, verification_checks
from ._output import print_error


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("verify", help="Run post-deploy verification checks for a manifest or deployed app")
    parser.add_argument("target", help="Path to the .ophelia manifest, or a deployed app name")
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--timeout", type=float, help="Override manifest per-request timeout in seconds")
    parser.add_argument("--attempts", type=int, help="Override manifest verification attempts")
    parser.add_argument("--interval", type=float, help="Override manifest verification interval in seconds")
    parser.add_argument("--delay", type=float, dest="interval", help="Alias for --interval")
    parser.add_argument("--failure-mode", choices=["hard", "warn"], help="Override manifest verification failure mode")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    override_error = _validate_overrides(args)
    if override_error is not None:
        print_error(override_error, "verification_override_invalid", json_output=args.json)
        return 1

    try:
        manifest, manifest_path = _load_target(args.target, args.runtime_root)
    except ManifestError as exc:
        print_error(f"Manifest invalid: {exc}", "manifest_invalid", json_output=args.json)
        return 1

    checks = verification_checks(manifest)
    if not checks:
        if args.json:
            print(json.dumps({"ok": True, "count": 0, "results": []}, indent=2, sort_keys=True))
            return 0
        print(f"No verification checks configured or inferred for {manifest.app}.")
        return 0

    try:
        payload = run_verifications(
            manifest,
            timeout=args.timeout,
            attempts=args.attempts,
            interval=args.interval,
            failure_mode=args.failure_mode,
            runtime_root=args.runtime_root,
        )
    except ValueError as exc:
        print_error(str(exc), "verification_error", json_output=args.json)
        return 1
    update_current_release_verification(args.runtime_root, manifest.app, payload)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if verification_blocks_release(payload) else 0
    attempt_detail = ""
    if payload.get("attempts", 1) > 1:
        attempt_detail = f" after attempt {payload.get('attempt')}/{payload.get('attempts')}"
    print(
        f"Verification results for {manifest.app}{attempt_detail}: "
        f"phase={payload.get('phase')} status={payload.get('status')} "
        f"failure_mode={payload.get('failure_mode')}"
    )
    for result in payload["results"]:
        prefix = "ok" if result["ok"] else "failed"
        if result.get("type") == "command":
            detail = f"exit {result.get('returncode')}"
        else:
            detail = f"HTTP {result['status_code']}" if result.get("status_code") is not None else "request failed"
        phase = result.get("phase") or payload.get("phase")
        target = result.get("url") or result.get("command") or result.get("service") or ""
        print(f"  {prefix} {result['name']}: phase={phase} {detail} -> {target}")
        if result.get("error"):
            kind = f"{result.get('error_kind')}: " if result.get("error_kind") else ""
            print(f"    {kind}{result['error']}")
    print(
        "Verify result: "
        f"app={manifest.app} applied={_current_applied_status(args.runtime_root, manifest.app)} "
        f"verified={str(bool(payload['ok'])).lower()} "
        f"phase={payload.get('phase')} failure_mode={payload.get('failure_mode')} "
        f"manifest={manifest_path}"
    )

    return 1 if verification_blocks_release(payload) else 0


def _load_target(target: str, runtime_root: Path):
    path = Path(target).expanduser()
    if path.exists() or path.suffix in {".yml", ".yaml", ".json"} or "/" in target:
        return load_manifest(path), path

    manifest_path = runtime_root / "apps" / target / "manifest.lock.json"
    return load_manifest(manifest_path), manifest_path


def _validate_overrides(args: Namespace) -> str | None:
    if args.attempts is not None and args.attempts < 1:
        return "verification attempts must be at least 1."
    if args.interval is not None and args.interval < 0:
        return "verification interval must be 0 or greater."
    if args.timeout is not None and args.timeout <= 0:
        return "verification timeout must be greater than 0."
    return None


def _current_applied_status(runtime_root: Path, app: str) -> str:
    release_path = runtime_root / "apps" / app / "release.json"
    if not release_path.exists():
        return "unknown"
    try:
        payload = json.loads(release_path.read_text())
    except json.JSONDecodeError:
        return "unknown"
    applied = payload.get("applied")
    if applied is True:
        return "true"
    if applied is False:
        return "false"
    return "unknown"
