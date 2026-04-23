from __future__ import annotations

import json
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List

from .manifest import Manifest
from .runtime import materialize_bundle


class RemoteError(RuntimeError):
    """Raised when remote execution fails."""


def bootstrap_host(
    host: str,
    ssh_port: int,
    remote_ophelia_root: str,
    runtime_root: str,
) -> None:
    remote_ophelia_root = _shell_path(remote_ophelia_root)
    runtime_root = _shell_path(runtime_root)
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
    ssh_port: int,
    remote_runtime_root: str,
    remote_ophelia_root: str,
    apply: bool,
) -> str:
    remote_runtime_root = _rsync_path(remote_runtime_root)
    with tempfile.TemporaryDirectory(prefix=f"ophelia-{manifest.app}-") as temp_dir:
        bundle_root = Path(temp_dir) / manifest.app
        materialize_bundle(manifest, manifest_path, bundle_root)
        _sync_bundle(bundle_root, host, ssh_port, remote_runtime_root, manifest.app)

    script = _build_remote_stage_script(
        manifest=manifest,
        manifest_path=manifest_path,
        remote_runtime_root=remote_runtime_root,
        remote_ophelia_root=remote_ophelia_root,
        apply=apply,
    )
    completed = _run(_ssh_command(host, ssh_port, script), capture_output=True)
    return completed.stdout.strip()


def _sync_bundle(
    bundle_root: Path,
    host: str,
    ssh_port: int,
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
        "releases/",
        "-e",
        f"ssh -p {ssh_port}",
        f"{bundle_root}/",
        remote_app_root,
    ]
    _run(command)


def _build_remote_stage_script(
    manifest: Manifest,
    manifest_path: Path,
    remote_runtime_root: str,
    remote_ophelia_root: str,
    apply: bool,
) -> str:
    app_root = f"{_shell_ref(remote_runtime_root)}/apps/{manifest.app}"
    caddy_target = f"{_shell_ref(remote_runtime_root)}/caddy/sites.d/{manifest.app}.caddy"
    caddy_global_target = f"{_shell_ref(remote_runtime_root)}/caddy/global.d/{manifest.app}.caddy"
    release = {
        "app": manifest.app,
        "kind": manifest.kind,
        "source_manifest": str(manifest_path.resolve()),
        "runtime_path": f"{remote_runtime_root}/apps/{manifest.app}",
        "deployed_at": _utc_now(),
        "mode": "applied" if apply else "staged",
    }

    lines: List[str] = [
        "set -euo pipefail",
        f"APP_ROOT={app_root}",
        f"CADDY_TARGET={caddy_target}",
        f"CADDY_GLOBAL_TARGET={caddy_global_target}",
        f"REMOTE_RUNTIME_ROOT={_shell_ref(remote_runtime_root)}",
        f"REMOTE_OPHELIA_ROOT={_shell_ref(remote_ophelia_root)}",
    ]

    if apply:
        lines.extend(_build_apply_lines())
    else:
        lines.extend(
            [
                'mkdir -p "$APP_ROOT/caddy"',
                'mkdir -p "$(dirname "$CADDY_TARGET")"',
                'mkdir -p "$(dirname "$CADDY_GLOBAL_TARGET")"',
                'if [ ! -f "$APP_ROOT/env" ] && [ -f "$APP_ROOT/env.example" ]; then cp "$APP_ROOT/env.example" "$APP_ROOT/env"; fi',
                f'cat > "$APP_ROOT/release.json" <<\'EOF_RELEASE\'\n{json.dumps(release, indent=2, sort_keys=True)}\nEOF_RELEASE',
                f'cp "$APP_ROOT/caddy/{manifest.app}.caddy" "$CADDY_TARGET"',
                f'if [ -f "$APP_ROOT/caddy/global.d/{manifest.app}.caddy" ]; then cp "$APP_ROOT/caddy/global.d/{manifest.app}.caddy" "$CADDY_GLOBAL_TARGET"; else rm -f "$CADDY_GLOBAL_TARGET"; fi',
                'echo "Staged runtime bundle and Caddy snippet."',
            ]
        )

    return "\n".join(lines)


def _build_apply_lines() -> List[str]:
    return [
        'cd "$REMOTE_OPHELIA_ROOT"',
        './cli/ship deploy "$APP_ROOT/manifest.lock.json" --runtime-root "$REMOTE_RUNTIME_ROOT" --ophelia-root "$REMOTE_OPHELIA_ROOT" --apply',
    ]


def _ssh_command(host: str, ssh_port: int, script: str) -> List[str]:
    return [
        "ssh",
        "-p",
        str(ssh_port),
        host,
        f"bash -lc {shlex.quote(script)}",
    ]


def _run(command: List[str], capture_output: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=capture_output,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        stdout = exc.stdout.strip() if exc.stdout else ""
        detail = stderr or stdout or str(exc)
        raise RemoteError(detail) from exc


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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
    return path
