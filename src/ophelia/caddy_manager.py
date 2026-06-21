from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT


def validate_caddy(
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    ophelia_root: Path = REPO_ROOT,
    timeout: int = 30,
) -> Dict[str, object]:
    if timeout <= 0:
        return {"returncode": 1, "stdout": "", "stderr": "timeout must be greater than 0", "command": []}
    runtime_root = runtime_root.expanduser()
    ophelia_root = ophelia_root.expanduser()
    caddyfile = ophelia_root / "platform" / "shared" / "caddy" / "Caddyfile"
    env_file = runtime_root / "caddy" / "env"
    global_dir = runtime_root / "caddy" / "global.d"
    sites_dir = runtime_root / "caddy" / "sites.d"
    setup_error = _ensure_caddy_runtime_paths(env_file, global_dir, sites_dir)
    if setup_error is not None:
        return {"returncode": 1, "stdout": "", "stderr": setup_error, "command": []}

    command = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{caddyfile}:/etc/caddy/Caddyfile:ro",
        "-v",
        f"{env_file}:/etc/caddy/env:ro",
        "-v",
        f"{global_dir}:/etc/caddy/global.d:ro",
        "-v",
        f"{sites_dir}:/etc/caddy/sites.d:ro",
        "-v",
        f"{runtime_root}:{runtime_root}:ro",
        "-v",
        "/home/kyle/websites:/home/kyle/websites:ro",
        "caddy:2-alpine",
        "caddy",
        "validate",
        "--config",
        "/etc/caddy/Caddyfile",
        "--envfile",
        "/etc/caddy/env",
    ]
    return _run(command, timeout=timeout)


def reload_caddy(
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    ophelia_root: Path = REPO_ROOT,
    timeout: int = 30,
    validate_first: bool = True,
) -> Dict[str, object]:
    if timeout <= 0:
        return {"returncode": 1, "stdout": "", "stderr": "timeout must be greater than 0", "command": []}
    validation: Optional[Dict[str, object]] = None
    if validate_first:
        validation = validate_caddy(runtime_root=runtime_root, ophelia_root=ophelia_root, timeout=timeout)
        if validation["returncode"] != 0:
            return {
                "returncode": 1,
                "stdout": "",
                "stderr": "Caddy validation failed; reload skipped.",
                "validation": validation,
            }

    compose = ophelia_root / "platform" / "shared" / "compose.yml"
    env_file = ophelia_root / "platform" / "shared" / ".env"
    command = ["docker", "compose"]
    if env_file.exists():
        command.extend(["--env-file", str(env_file)])
    command.extend(
        [
            "-f",
            str(compose),
            "exec",
            "-T",
            "caddy",
            "caddy",
            "reload",
            "--config",
            "/etc/caddy/Caddyfile",
            "--adapter",
            "caddyfile",
        ]
    )
    result = _run(command, timeout=timeout, runtime_root=runtime_root)
    if validation is not None:
        result["validation"] = validation
    return result


def _run(command: List[str], timeout: int = 30, runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> Dict[str, object]:
    env = os.environ.copy()
    env.setdefault("OPHELIA_RUNTIME_ROOT", str(runtime_root))
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": 1, "stdout": "", "stderr": str(exc), "command": command}
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "command": command,
    }


def _ensure_caddy_runtime_paths(env_file: Path, global_dir: Path, sites_dir: Path) -> str | None:
    env_file.parent.mkdir(parents=True, exist_ok=True)
    global_dir.mkdir(parents=True, exist_ok=True)
    sites_dir.mkdir(parents=True, exist_ok=True)
    if env_file.exists() and not env_file.is_file():
        return f"Expected Caddy env path to be a file: {env_file}"
    if not env_file.exists():
        env_file.write_text("")
    return None
