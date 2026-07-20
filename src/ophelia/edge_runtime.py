"""Installed-package bootstrap for the shared Ophelia edge runtime."""

from __future__ import annotations

import os
import subprocess
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Dict, Sequence

from .config import DEFAULT_RUNTIME_ROOT


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def bootstrap_edge_runtime(
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    *,
    http_port: int = 80,
    https_port: int = 443,
    static_root: Path | None = None,
    start: bool = False,
    runner: CommandRunner | None = None,
) -> Dict[str, Any]:
    """Materialize the packaged shared edge and optionally start Caddy."""

    root = _safe_runtime_root(runtime_root)
    _port(http_port, "http_port")
    _port(https_port, "https_port")
    if http_port == https_port:
        raise ValueError("HTTP and HTTPS ports must be distinct.")
    resolved_static_root = _safe_static_root(static_root or root / "static")

    shared = root / "platform" / "shared"
    caddy_runtime = root / "caddy"
    for directory in (
        shared / "caddy",
        caddy_runtime,
        caddy_runtime / "global.d",
        caddy_runtime / "sites.d",
    ):
        _safe_directory(directory)

    resource_root = resources.files("ophelia.resources").joinpath("edge")
    compose_text = resource_root.joinpath("compose.yml").read_text(encoding="utf-8")
    caddy_text = resource_root.joinpath("caddy", "Caddyfile").read_text(encoding="utf-8")
    env_text = (
        "# Managed by `ship caddy bootstrap`.\n"
        f"OPHELIA_RUNTIME_ROOT={root}\n"
        f"OPHELIA_STATIC_ROOT={resolved_static_root}\n"
        f"OPHELIA_HTTP_PORT={http_port}\n"
        f"OPHELIA_HTTPS_PORT={https_port}\n"
    )

    changed = []
    for path, content, mode in (
        (shared / "compose.yml", compose_text, 0o600),
        (shared / "caddy" / "Caddyfile", caddy_text, 0o600),
        (shared / ".env", env_text, 0o600),
    ):
        if _write_managed(path, content, mode=mode):
            changed.append(str(path.relative_to(root)))
    env_path = caddy_runtime / "env"
    if not env_path.exists():
        _atomic_text(env_path, "", mode=0o600)
        changed.append(str(env_path.relative_to(root)))
    elif env_path.is_symlink() or not env_path.is_file():
        raise ValueError("Shared Caddy env path must be a regular file.")
    else:
        os.chmod(env_path, 0o600)

    commands: list[list[str]] = []
    started = False
    if start:
        execute = runner or _run
        inspect = ["docker", "network", "inspect", "ophelia-edge"]
        inspected = execute(inspect)
        commands.append(inspect)
        if inspected.returncode != 0:
            create = ["docker", "network", "create", "ophelia-edge"]
            created = execute(create)
            commands.append(create)
            if created.returncode != 0:
                raise RuntimeError("Could not create the Ophelia edge network.")
        up = [
            "docker",
            "compose",
            "-f",
            str(shared / "compose.yml"),
            "up",
            "-d",
            "caddy",
        ]
        result = execute(up)
        commands.append(up)
        if result.returncode != 0:
            detail = _command_failure_detail(result)
            raise RuntimeError(
                "Could not start the shared Ophelia Caddy runtime"
                + (f": {detail}" if detail else ".")
            )
        started = True

    return {
        "ok": True,
        "schema_version": 1,
        "kind": "ophelia.edge-bootstrap",
        "runtime_root": str(root),
        "static_root": str(resolved_static_root),
        "shared_compose": str(shared / "compose.yml"),
        "changed": changed,
        "started": started,
        "commands": commands,
    }


def _safe_runtime_root(value: Path) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() or candidate == Path(candidate.anchor):
        raise ValueError("Runtime root must be a non-root absolute path.")
    if candidate.is_symlink():
        raise ValueError("Runtime root must be a real directory.")
    if candidate.exists() and not candidate.is_dir():
        raise ValueError("Runtime root must be a real directory.")
    return candidate


def _port(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError(f"{field} must be an integer TCP port.")


def _safe_static_root(value: Path) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Static root must be a non-root absolute path.")
    if candidate.is_symlink():
        raise ValueError("Static root must be a real directory.")
    root = candidate.resolve(strict=False)
    if root == Path(root.anchor):
        raise ValueError("Static root must be a non-root absolute path.")
    if root.exists():
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Static root must be a real directory.")
    else:
        root.mkdir(mode=0o755, parents=True)
    return root


def _command_failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    value = (result.stderr or result.stdout or "").strip().replace("\n", " ")
    return value if len(value) <= 500 else value[:485] + "...<truncated>"


def _safe_directory(path: Path) -> None:
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise ValueError(f"Managed edge directory is unsafe: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_managed(path: Path, content: str, *, mode: int) -> bool:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Managed edge file is unsafe: {path}")
        if path.read_text(encoding="utf-8") == content:
            os.chmod(path, mode)
            return False
    _atomic_text(path, content, mode=mode)
    return True


def _atomic_text(path: Path, content: str, *, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
