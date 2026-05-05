from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List

from .manifest import ManifestError, load_manifest
from .runtime import list_deployments
from .templates import render_caddy


EDGE_NETWORK = "ophelia-edge"
INTERNAL_NETWORK = "ophelia-internal"


def status_report(runtime_root: Path, ophelia_root: Path, manifest_dir: Path) -> Dict[str, object]:
    warnings: List[str] = []
    deployments = list_deployments(runtime_root)
    docker = _docker_info()
    if not docker["available"]:
        warnings.append("Docker is not available; shared service and network status are limited.")
    elif not docker["daemon_available"]:
        warnings.append("Docker CLI is available, but the Docker daemon is not reachable.")

    apps = []
    for deployment in deployments:
        app_root = runtime_root / "apps" / deployment.app
        generated_caddy = app_root / "caddy" / f"{deployment.app}.caddy"
        active_caddy = runtime_root / "caddy" / "sites.d" / f"{deployment.app}.caddy"
        apps.append(
            {
                "app": deployment.app,
                "kind": deployment.kind,
                "environment": deployment.environment,
                "release_id": deployment.release_id,
                "deployed_at": deployment.deployed_at,
                "runtime_path": str(deployment.runtime_path),
                "caddy": {
                    "generated": generated_caddy.exists(),
                    "active": active_caddy.exists(),
                    "synced": _file_hash(generated_caddy) == _file_hash(active_caddy),
                },
            }
        )

    disk_path = runtime_root if runtime_root.exists() else runtime_root.parent
    disk_usage = _disk_usage(disk_path)
    networks = _docker_networks(docker["daemon_available"])
    shared = _shared_service_status(ophelia_root, docker)
    for network in (EDGE_NETWORK, INTERNAL_NETWORK):
        if docker["daemon_available"] and network not in networks:
            warnings.append(f"Docker network `{network}` was not found.")

    return {
        "runtime_root": str(runtime_root),
        "runtime_root_exists": runtime_root.exists(),
        "known_apps": apps,
        "docker": docker,
        "docker_networks": networks,
        "shared_services": shared,
        "disk_usage": disk_usage,
        "manifest_count": len(sorted(manifest_dir.glob("*.ophelia.yml"))) if manifest_dir.exists() else 0,
        "warnings": warnings,
    }


def doctor_report(runtime_root: Path, ophelia_root: Path, manifest_dir: Path) -> Dict[str, object]:
    checks: List[Dict[str, object]] = []
    warnings: List[str] = []
    blockers: List[str] = []

    docker = _docker_info()
    _record(
        checks,
        "docker.available",
        bool(docker["available"]),
        docker.get("version") or "docker not found",
        warning=not bool(docker["available"]),
    )
    _record(
        checks,
        "docker.compose.available",
        bool(docker["compose_available"]),
        docker.get("compose_version") or "docker compose not available",
        warning=not bool(docker["compose_available"]),
    )
    _record(
        checks,
        "runtime_root.exists",
        runtime_root.exists(),
        str(runtime_root),
        warning=not runtime_root.exists(),
    )

    networks = _docker_networks(docker["daemon_available"])
    if docker["daemon_available"]:
        _record(
            checks,
            "network.ophelia-edge",
            EDGE_NETWORK in networks,
            EDGE_NETWORK,
            warning=EDGE_NETWORK not in networks,
        )
        _record(
            checks,
            "network.ophelia-internal",
            INTERNAL_NETWORK in networks,
            INTERNAL_NETWORK,
            warning=INTERNAL_NETWORK not in networks,
        )
    else:
        warnings.append("Skipped Docker network checks because Docker is unavailable.")

    shared_compose = ophelia_root / "platform" / "shared" / "compose.yml"
    shared_env = ophelia_root / "platform" / "shared" / ".env"
    _record(checks, "shared.compose.exists", shared_compose.exists(), str(shared_compose))
    _record(
        checks,
        "shared.env.present_or_absent",
        True,
        f"{shared_env} exists" if shared_env.exists() else f"{shared_env} intentionally absent in source checkout",
        warning=not shared_env.exists(),
    )

    manifest_results = []
    for manifest_path in sorted(manifest_dir.glob("*.ophelia.yml")) if manifest_dir.exists() else []:
        try:
            manifest = load_manifest(manifest_path)
            render_caddy(manifest)
            manifest_results.append({"path": str(manifest_path), "app": manifest.app, "ok": True})
        except (ManifestError, OSError) as exc:
            manifest_results.append({"path": str(manifest_path), "ok": False, "error": str(exc)})

    manifests_ok = all(item["ok"] for item in manifest_results)
    _record(checks, "manifests.validate_and_render", manifests_ok, f"{len(manifest_results)} manifests checked")

    for check in checks:
        if not check["ok"] and not check.get("warning"):
            blockers.append(str(check["name"]))
        elif not check["ok"] or check.get("warning"):
            warnings.append(str(check["message"]))

    return {
        "ok": not blockers,
        "runtime_root": str(runtime_root),
        "checks": checks,
        "manifests": manifest_results,
        "docker": docker,
        "warnings": warnings,
        "blockers": blockers,
    }


def _docker_info() -> Dict[str, object]:
    if os.environ.get("OPHELIA_SKIP_DOCKER_STATUS") == "1":
        return {
            "available": False,
            "daemon_available": False,
            "compose_available": False,
            "version": None,
            "daemon_version": None,
            "compose_version": None,
        }

    docker_path = shutil.which("docker")
    if not docker_path:
        return {
            "available": False,
            "daemon_available": False,
            "compose_available": False,
            "version": None,
            "daemon_version": None,
            "compose_version": None,
        }

    version = _run(["docker", "--version"])
    daemon = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=3)
    compose = _run(["docker", "compose", "version"])
    return {
        "available": version["returncode"] == 0,
        "daemon_available": daemon["returncode"] == 0,
        "compose_available": compose["returncode"] == 0,
        "version": version["stdout"] or version["stderr"],
        "daemon_version": daemon["stdout"] or None,
        "compose_version": compose["stdout"] or compose["stderr"],
    }


def _docker_networks(docker_available: object) -> List[str]:
    if not docker_available:
        return []
    result = _run(["docker", "network", "ls", "--format", "{{.Name}}"])
    if result["returncode"] != 0:
        return []
    return sorted(line.strip() for line in str(result["stdout"]).splitlines() if line.strip())


def _shared_service_status(ophelia_root: Path, docker: Dict[str, object]) -> Dict[str, object]:
    shared_compose = ophelia_root / "platform" / "shared" / "compose.yml"
    if not shared_compose.exists():
        return {"available": False, "reason": f"shared compose not found: {shared_compose}"}
    if not docker.get("compose_available"):
        return {"available": False, "reason": "docker compose is unavailable"}
    if not docker.get("daemon_available"):
        return {"available": False, "reason": "Docker daemon is unavailable"}

    command = ["docker", "compose", "-f", str(shared_compose), "ps"]
    shared_env = ophelia_root / "platform" / "shared" / ".env"
    if shared_env.exists():
        command = ["docker", "compose", "--env-file", str(shared_env), "-f", str(shared_compose), "ps"]
    result = _run(command)
    return {
        "available": result["returncode"] == 0,
        "output": result["stdout"] if result["returncode"] == 0 else result["stderr"],
    }


def _disk_usage(path: Path) -> Dict[str, object]:
    target = path if path.exists() else Path.home()
    usage = shutil.disk_usage(target)
    return {
        "path": str(target),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "percent_used": round((usage.used / usage.total) * 100, 2) if usage.total else 0,
    }


def _record(
    checks: List[Dict[str, object]],
    name: str,
    ok: bool,
    message: str,
    warning: bool = False,
) -> None:
    checks.append({"name": name, "ok": ok, "message": message, "warning": warning})


def _file_hash(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(command: List[str], timeout: int = 10) -> Dict[str, object]:
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": 1, "stdout": "", "stderr": str(exc)}
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
