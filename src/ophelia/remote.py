from __future__ import annotations

import json
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import List

from .manifest import Manifest
from .operation_schema import operation_id
from .runtime import DeployMetadata, materialize_bundle


class RemoteError(RuntimeError):
    """Raised when remote execution fails."""


def bootstrap_host(
    host: str,
    ssh_port: int | None,
    remote_ophelia_root: str,
    runtime_root: str,
) -> None:
    remote_ophelia_root = _shell_path(
        _validate_remote_root(remote_ophelia_root, "remote Ophelia root")
    )
    runtime_root = _shell_path(_validate_remote_root(runtime_root, "remote runtime root"))
    script = "\n".join(
        [
            "set -euo pipefail",
            f"cd {remote_ophelia_root}",
            f"./platform/scripts/bootstrap-host.sh {runtime_root}",
        ]
    )
    _run(_ssh_command(host, ssh_port, script))


def stage_remote_bundle(
    manifest: Manifest,
    manifest_path: Path,
    host: str,
    ssh_port: int | None,
    remote_runtime_root: str,
    remote_ophelia_root: str,
    apply: bool,
    verify: bool = False,
    verify_attempts: int | None = None,
    verify_interval: float | None = None,
    verify_timeout: float | None = None,
    verify_failure_mode: str | None = None,
    *,
    plan: bool = False,
    json_output: bool = False,
    confirm: str | None = None,
    deploy_metadata: DeployMetadata | None = None,
) -> str:
    remote_runtime_root = _validate_remote_root(remote_runtime_root, "remote runtime root")
    remote_ophelia_root = _validate_remote_root(remote_ophelia_root, "remote Ophelia root")
    remote_runtime_root = _rsync_path(remote_runtime_root)
    if apply and manifest.environment == "production":
        if not confirm:
            raise RemoteError("Remote production apply requires a confirmed staged plan.")
        script = _build_remote_stage_script(
            manifest=manifest,
            manifest_path=manifest_path,
            remote_runtime_root=remote_runtime_root,
            remote_ophelia_root=remote_ophelia_root,
            apply=True,
            confirm=confirm,
            verify=verify,
            verify_attempts=verify_attempts,
            verify_interval=verify_interval,
            verify_timeout=verify_timeout,
            verify_failure_mode=verify_failure_mode,
            deploy_metadata=deploy_metadata,
            confirmed_apply=True,
        )
        completed = _run(_ssh_command(host, ssh_port, script), capture_output=True)
        return completed.stdout.strip()

    plan_operation_id = (
        operation_id("deploy.plan", manifest.app, getattr(manifest, "environment", None))
        if plan
        else None
    )
    with tempfile.TemporaryDirectory(prefix=f"ophelia-{manifest.app}-") as temp_dir:
        bundle_root = Path(temp_dir) / manifest.app
        materialize_bundle(manifest, manifest_path, bundle_root)
        if plan_operation_id is not None:
            _prepare_remote_plan_staging(
                host, ssh_port, remote_runtime_root, plan_operation_id
            )
            _sync_plan_candidate(
                bundle_root,
                host,
                ssh_port,
                remote_runtime_root,
                plan_operation_id,
            )
        else:
            # Legacy stage/apply transport intentionally remains isolated from plans.
            _sync_bundle(bundle_root, host, ssh_port, remote_runtime_root, manifest.app)

    script = _build_remote_stage_script(
        manifest=manifest,
        manifest_path=manifest_path,
        remote_runtime_root=remote_runtime_root,
        remote_ophelia_root=remote_ophelia_root,
        apply=apply,
        plan=plan,
        json_output=json_output,
        confirm=confirm,
        verify=verify,
        verify_attempts=verify_attempts,
        verify_interval=verify_interval,
        verify_timeout=verify_timeout,
        verify_failure_mode=verify_failure_mode,
        deploy_metadata=deploy_metadata,
        plan_operation_id=plan_operation_id,
    )
    completed = _run(_ssh_command(host, ssh_port, script), capture_output=True)
    return completed.stdout.strip()


def _prepare_remote_plan_staging(
    host: str,
    ssh_port: int | None,
    remote_runtime_root: str,
    plan_operation_id: str,
) -> None:
    staging_base = f"{_shell_ref(remote_runtime_root)}/staging"
    operation_root = f"{staging_base}/{shlex.quote(plan_operation_id)}"
    candidate = f"{operation_root}/candidate"
    script = "\n".join(
        [
            "set -euo pipefail",
            "umask 077",
            f"mkdir -p -m 700 {staging_base}",
            f"chmod 700 {staging_base}",
            f"mkdir -m 700 {operation_root}",
            f"mkdir -m 700 {candidate}",
        ]
    )
    _run(_ssh_command(host, ssh_port, script))


def _sync_plan_candidate(
    bundle_root: Path,
    host: str,
    ssh_port: int | None,
    remote_runtime_root: str,
    plan_operation_id: str,
) -> None:
    remote_candidate = (
        f"{host}:{remote_runtime_root}/staging/{plan_operation_id}/candidate/"
    )
    command = [
        "rsync",
        "-az",
        "--chmod=Du+rwx,Dgo-rwx,Fu+rw,Fgo-rwx",
    ]
    if ssh_port is not None:
        command.extend(["-e", f"ssh -p {ssh_port}"])
    command.extend([f"{bundle_root}/", remote_candidate])
    _run(command)


def _sync_bundle(
    bundle_root: Path,
    host: str,
    ssh_port: int | None,
    remote_runtime_root: str,
    app: str,
) -> None:
    remote_app_root = f"{host}:{remote_runtime_root}/apps/{app}/"
    command = [
        "rsync",
        "-az",
        "--delete",
        "--exclude",
        "env",
        "--exclude",
        "release.json",
        "--exclude",
        "active_release.json",
        "--exclude",
        "releases/",
        "--exclude",
        "release-bundles/",
        "--exclude",
        "addons.json",
        "--exclude",
        "restore-previews/",
    ]
    if ssh_port is not None:
        command.extend(["-e", f"ssh -p {ssh_port}"])
    command.extend([f"{bundle_root}/", remote_app_root])
    _run(command)


def _build_remote_stage_script(
    manifest: Manifest,
    manifest_path: Path,
    remote_runtime_root: str,
    remote_ophelia_root: str,
    apply: bool,
    verify: bool = False,
    verify_attempts: int | None = None,
    verify_interval: float | None = None,
    verify_timeout: float | None = None,
    verify_failure_mode: str | None = None,
    *,
    plan: bool = False,
    json_output: bool = False,
    confirm: str | None = None,
    deploy_metadata: DeployMetadata | None = None,
    plan_operation_id: str | None = None,
    confirmed_apply: bool = False,
) -> str:
    if apply and plan:
        raise ValueError("remote deploy script cannot both plan and apply")
    if plan:
        if plan_operation_id is None:
            plan_operation_id = operation_id(
                "deploy.plan", manifest.app, getattr(manifest, "environment", None)
            )
        app_root = (
            f"{_shell_ref(remote_runtime_root)}/staging/"
            f"{shlex.quote(plan_operation_id)}/candidate"
        )
    else:
        app_root = f"{_shell_ref(remote_runtime_root)}/apps/{manifest.app}"

    lines: List[str] = [
        "set -euo pipefail",
        f"APP_ROOT={app_root}",
        f"REMOTE_RUNTIME_ROOT={_shell_ref(remote_runtime_root)}",
        f"REMOTE_OPHELIA_ROOT={_shell_ref(remote_ophelia_root)}",
    ]

    if plan:
        lines.extend(
            _build_plan_lines(
                json_output=json_output,
                deploy_metadata=deploy_metadata,
                plan_operation_id=plan_operation_id,
            )
        )
    elif apply:
        lines.extend(
            _build_apply_lines(
                confirm=confirm,
                verify=verify,
                verify_attempts=verify_attempts,
                verify_interval=verify_interval,
                verify_timeout=verify_timeout,
                verify_failure_mode=verify_failure_mode,
                deploy_metadata=deploy_metadata,
                confirmed_app=manifest.app if confirmed_apply else None,
            )
        )
    else:
        lines.extend(_build_stage_lines(manifest, manifest_path, remote_runtime_root))

    return "\n".join(lines)


def _build_plan_lines(
    json_output: bool = False,
    deploy_metadata: DeployMetadata | None = None,
    plan_operation_id: str | None = None,
) -> List[str]:
    command = _remote_deploy_command()
    command.append("--plan")
    if json_output:
        command.append("--json")
    if plan_operation_id is not None:
        command.extend(["--plan-operation-id", shlex.quote(plan_operation_id)])
    command.extend(_deploy_metadata_args(deploy_metadata))
    return [
        'cd "$REMOTE_OPHELIA_ROOT"',
        " ".join(command),
    ]


def _build_apply_lines(
    confirm: str | None = None,
    verify: bool = False,
    verify_attempts: int | None = None,
    verify_interval: float | None = None,
    verify_timeout: float | None = None,
    verify_failure_mode: str | None = None,
    deploy_metadata: DeployMetadata | None = None,
    confirmed_app: str | None = None,
) -> List[str]:
    command = _remote_deploy_command(
        shlex.quote(f"@confirmed:{confirmed_app}") if confirmed_app else None
    )
    command.append("--apply")
    if confirm:
        command.extend(["--confirm", shlex.quote(confirm)])
    if verify:
        command.append("--verify")
        if verify_attempts is not None:
            command.extend(["--verify-attempts", str(verify_attempts)])
        if verify_interval is not None:
            command.extend(["--verify-interval", str(verify_interval)])
        if verify_timeout is not None:
            command.extend(["--verify-timeout", str(verify_timeout)])
        if verify_failure_mode is not None:
            command.extend(["--verify-failure-mode", shlex.quote(verify_failure_mode)])
    command.extend(_deploy_metadata_args(deploy_metadata))
    return [
        'cd "$REMOTE_OPHELIA_ROOT"',
        " ".join(command),
    ]


def _deploy_metadata_args(deploy_metadata: DeployMetadata | None) -> List[str]:
    if deploy_metadata is None:
        return []
    args: List[str] = []
    if deploy_metadata.release_id:
        args.extend(["--release-id", shlex.quote(deploy_metadata.release_id)])
    if deploy_metadata.commit_sha:
        args.extend(["--commit-sha", shlex.quote(deploy_metadata.commit_sha)])
    if deploy_metadata.build_time:
        args.extend(["--build-time", shlex.quote(deploy_metadata.build_time)])
    return args


def _build_stage_lines(manifest: Manifest, manifest_path: Path, remote_runtime_root: str) -> List[str]:
    release = {
        "app": manifest.app,
        "kind": manifest.kind,
        "source_manifest": str(manifest_path.resolve()),
        "runtime_path": f"{remote_runtime_root}/apps/{manifest.app}",
        "deployed_at": _utc_now(),
        "mode": "staged",
    }
    return [
        'mkdir -p "$APP_ROOT/caddy"',
        f'CADDY_TARGET="$REMOTE_RUNTIME_ROOT/caddy/sites.d/{manifest.app}.caddy"',
        f'CADDY_GLOBAL_TARGET="$REMOTE_RUNTIME_ROOT/caddy/global.d/{manifest.app}.caddy"',
        'mkdir -p "$(dirname "$CADDY_TARGET")"',
        'mkdir -p "$(dirname "$CADDY_GLOBAL_TARGET")"',
        'if [ ! -f "$APP_ROOT/env" ] && [ -f "$APP_ROOT/env.example" ]; then cp "$APP_ROOT/env.example" "$APP_ROOT/env"; fi',
        f'cat > "$APP_ROOT/release.json" <<\'EOF_RELEASE\'\n{json.dumps(release, indent=2, sort_keys=True)}\nEOF_RELEASE',
        f'cp "$APP_ROOT/caddy/{manifest.app}.caddy" "$CADDY_TARGET"',
        f'if [ -f "$APP_ROOT/caddy/global.d/{manifest.app}.caddy" ]; then cp "$APP_ROOT/caddy/global.d/{manifest.app}.caddy" "$CADDY_GLOBAL_TARGET"; else rm -f "$CADDY_GLOBAL_TARGET"; fi',
        'echo "Staged runtime bundle and Caddy snippet."',
    ]


def _remote_deploy_command(manifest_ref: str | None = None) -> List[str]:
    return [
        "./cli/ship",
        "deploy",
        manifest_ref or '"$APP_ROOT/manifest.lock.json"',
        "--runtime-root",
        '"$REMOTE_RUNTIME_ROOT"',
        "--ophelia-root",
        '"$REMOTE_OPHELIA_ROOT"',
    ]


def _ssh_command(host: str, ssh_port: int | None, script: str) -> List[str]:
    command = ["ssh"]
    if ssh_port is not None:
        command.extend(["-p", str(ssh_port)])
    command.extend([host, f"bash -lc {shlex.quote(script)}"])
    return command


def _run(command: List[str], capture_output: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=capture_output,
        )
    except OSError as exc:
        raise RemoteError(str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        stdout = exc.stdout.strip() if exc.stdout else ""
        detail = stderr or stdout or str(exc)
        raise RemoteError(detail) from exc


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _validate_remote_root(path: str, label: str) -> str:
    if path == "~":
        return path
    if path.startswith("~/"):
        suffix = path[2:]
    elif path.startswith("/"):
        suffix = path[1:]
    else:
        raise RemoteError(f"{label} must be an absolute path or start with ~/.")
    if not suffix or any(part in {"", ".", ".."} for part in suffix.split("/")):
        raise RemoteError(f"{label} contains an unsafe path segment.")
    if any(not (character.isalnum() or character in "._-/") for character in suffix):
        raise RemoteError(f"{label} contains unsafe characters.")
    return path


def _shell_path(path: str) -> str:
    if path == "~":
        return "$HOME"
    if path.startswith("~/"):
        return "$HOME/" + path[2:]
    return shlex.quote(path)


def _rsync_path(path: str) -> str:
    if path == "~":
        return "~"
    if path.startswith("~/"):
        return "~/" + path[2:]
    return path


def _shell_ref(path: str) -> str:
    if path == "~":
        return "$HOME"
    if path.startswith("~/"):
        return "$HOME/" + path[2:]
    return shlex.quote(path)
