from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT


SHARED_CADDY_CONTAINER_CANDIDATES = (
    "shared-caddy-1",
    "ophelia-shared-caddy-1",
    "ophelia-caddy-1",
    "ophelia-caddy",
    "caddy",
    "edge-caddy-1",
    "quark-reverse-proxy-caddy-1",
)


def validate_caddy(
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    ophelia_root: Path = REPO_ROOT,
    timeout: int = 30,
    static_root: Optional[Path] = None,
) -> Dict[str, object]:
    if timeout <= 0:
        return {"returncode": 1, "stdout": "", "stderr": "timeout must be greater than 0", "command": []}
    runtime_root = runtime_root.expanduser()
    ophelia_root = ophelia_root.expanduser()
    static_root = (static_root or Path(os.environ.get("OPHELIA_STATIC_ROOT", str(runtime_root / "static")))).expanduser()
    caddyfile = ophelia_root / "platform" / "shared" / "caddy" / "Caddyfile"
    env_file = runtime_root / "caddy" / "env"
    global_dir = runtime_root / "caddy" / "global.d"
    sites_dir = runtime_root / "caddy" / "sites.d"
    setup_error = _ensure_caddy_runtime_paths(env_file, global_dir, sites_dir, static_root)
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
        f"{static_root}:{static_root}:ro",
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
    runtime_root = runtime_root.expanduser()
    ophelia_root = ophelia_root.expanduser()
    config_path = ophelia_root / "platform" / "shared" / "caddy" / "Caddyfile"
    report: Dict[str, object] = {
        "kind": "ophelia.edge.reload",
        "ok": False,
        "container": None,
        "validated": False,
        "reloaded": False,
        "runtime_root": str(runtime_root),
        "config_path": str(config_path),
        "warnings": [],
        "errors": [],
        "returncode": 1,
        "stdout": "",
        "stderr": "",
        "command": [],
    }
    if timeout <= 0:
        _add_error(report, "timeout_invalid", "timeout must be greater than 0")
        report["stderr"] = "timeout must be greater than 0"
        return report
    validation: Optional[Dict[str, object]] = None
    if validate_first:
        validation = validate_caddy(runtime_root=runtime_root, ophelia_root=ophelia_root, timeout=timeout)
        report["validation"] = _process_summary(validation)
        if validation["returncode"] != 0:
            _add_error(report, "caddy_validation_failed", "Caddy validation failed; reload skipped.")
            report["stderr"] = "Caddy validation failed; reload skipped."
            return report
        report["validated"] = True
    else:
        report["warnings"].append({"code": "validation_skipped", "message": "Caddy validation was skipped before reload."})

    discovery = discover_shared_caddy_container(runtime_root=runtime_root, timeout=timeout)
    report["container"] = discovery.get("container")
    warnings = report.get("warnings")
    if isinstance(warnings, list):
        warnings.extend(discovery.get("warnings", []))
    if not report["container"]:
        _add_error(report, "caddy_container_not_found", "No running shared Caddy container was found.")
        report["stderr"] = "No running shared Caddy container was found."
        return report

    command = [
        "docker",
        "exec",
        str(report["container"]),
        "caddy",
        "reload",
        "--config",
        "/etc/caddy/Caddyfile",
        "--adapter",
        "caddyfile",
    ]
    result = _run(command, timeout=timeout, runtime_root=runtime_root)
    report["command"] = command
    report["reload"] = _process_summary(result)
    report["stdout"] = result.get("stdout", "")
    report["stderr"] = result.get("stderr", "")
    report["returncode"] = result.get("returncode", 1)
    if result.get("returncode") == 0:
        report["ok"] = True
        report["reloaded"] = True
        return report

    _add_error(report, "caddy_reload_failed", "Caddy reload command failed.")
    return report


def discover_shared_caddy_container(
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    timeout: int = 30,
) -> Dict[str, object]:
    warnings: List[Dict[str, str]] = []
    for candidate in SHARED_CADDY_CONTAINER_CANDIDATES:
        result = _run(
            ["docker", "inspect", "--format", "{{.State.Running}}", candidate],
            timeout=timeout,
            runtime_root=runtime_root,
        )
        if result.get("returncode") == 0 and str(result.get("stdout")).strip().lower() == "true":
            if candidate != SHARED_CADDY_CONTAINER_CANDIDATES[0]:
                warnings.append(
                    {
                        "code": "legacy_caddy_container_name",
                        "message": f"Using fallback Caddy container `{candidate}`.",
                    }
                )
            return {"container": candidate, "warnings": warnings}
        if result.get("returncode") == 0:
            warnings.append({"code": "caddy_container_not_running", "message": f"Caddy container `{candidate}` is not running."})

    listed = _run(["docker", "ps", "--format", "{{.Names}}"], timeout=timeout, runtime_root=runtime_root)
    if listed.get("returncode") == 0:
        for name in str(listed.get("stdout") or "").splitlines():
            if "caddy" in name.lower():
                warnings.append(
                    {
                        "code": "discovered_caddy_container_name",
                        "message": f"Using discovered Caddy container `{name}`.",
                    }
                )
                return {"container": name, "warnings": warnings}
    return {"container": None, "warnings": warnings}


def _process_summary(report: Dict[str, object]) -> Dict[str, object]:
    return {
        "returncode": report.get("returncode", 1),
        "stdout_excerpt": _excerpt(str(report.get("stdout") or "")),
        "stderr_excerpt": _excerpt(str(report.get("stderr") or "")),
        "command": report.get("command", []),
    }


def _add_error(report: Dict[str, object], code: str, message: str) -> None:
    errors = report.setdefault("errors", [])
    if isinstance(errors, list):
        errors.append({"code": code, "message": message})


def _excerpt(value: str, limit: int = 1000) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 15] + "...<truncated>"


def _run(command: List[str], timeout: int = 30, runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> Dict[str, object]:
    env = os.environ.copy()
    env.setdefault("OPHELIA_RUNTIME_ROOT", str(runtime_root))
    env.setdefault("OPHELIA_STATIC_ROOT", str(runtime_root / "static"))
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


def _ensure_caddy_runtime_paths(env_file: Path, global_dir: Path, sites_dir: Path, static_root: Path) -> str | None:
    env_file.parent.mkdir(parents=True, exist_ok=True)
    global_dir.mkdir(parents=True, exist_ok=True)
    sites_dir.mkdir(parents=True, exist_ok=True)
    static_root.mkdir(parents=True, exist_ok=True)
    if env_file.exists() and not env_file.is_file():
        return f"Expected Caddy env path to be a file: {env_file}"
    if not env_file.exists():
        env_file.write_text("")
    return None
