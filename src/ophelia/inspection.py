from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

from .operation_schema import SCHEMA_VERSION
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
        generated_hash = _file_hash(generated_caddy)
        active_hash = _file_hash(active_caddy)
        apps.append(
            {
                "app": deployment.app,
                "kind": deployment.kind,
                "environment": deployment.environment,
                "release_id": deployment.release_id,
                "latest_release_id": deployment.release_id,
                "active_release_id": deployment.active_release_id,
                "applied": deployment.applied,
                "verified": deployment.verified,
                "deployed_at": deployment.deployed_at,
                "runtime_path": str(deployment.runtime_path),
                "caddy": {
                    "generated": generated_caddy.exists(),
                    "active": active_caddy.exists(),
                    "synced": generated_hash is not None and generated_hash == active_hash,
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

    _record(
        checks,
        "python.version",
        sys.version_info >= (3, 9),
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )
    yaml_available = _module_available("yaml")
    _record(
        checks,
        "python.imports.yaml",
        yaml_available,
        "PyYAML importable" if yaml_available else "PyYAML is not importable",
    )
    _record(
        checks,
        "ophelia_root.exists",
        ophelia_root.exists(),
        str(ophelia_root),
    )

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
    runtime_layout = _runtime_layout_status(runtime_root)
    _record(
        checks,
        "runtime_root.writable",
        bool(runtime_layout["writable"]),
        str(runtime_layout["writable_target"]),
        warning=not bool(runtime_layout["writable"]),
    )
    for directory, present in runtime_layout["directories"].items():
        _record(
            checks,
            f"runtime_root.layout.{directory}",
            bool(present),
            f"{directory}: {'present' if present else 'missing'}",
            warning=not bool(present),
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

    registry = _manifest_registry_status(manifest_dir, runtime_root)
    _record(
        checks,
        "manifest_registry.parse",
        bool(registry["ok"]),
        registry["message"],
        warning=not bool(registry["ok"]),
    )

    state = _state_index_status(runtime_root)
    _record(
        checks,
        "state.index.current",
        bool(state["ok"]),
        state["message"],
        warning=not bool(state["ok"]),
    )

    catalog_status = _command_catalog_status()
    _record(
        checks,
        "command_catalog.complete",
        bool(catalog_status["ok"]),
        catalog_status["message"],
        warning=not bool(catalog_status["ok"]),
    )

    docs_status = _docs_status(ophelia_root)
    _record(
        checks,
        "docs.changelog.index",
        bool(docs_status["ok"]),
        docs_status["message"],
        warning=not bool(docs_status["ok"]),
    )

    gh_status = _github_status()
    _record(
        checks,
        "github.gh.available",
        bool(gh_status["available"]),
        gh_status["message"],
        warning=not bool(gh_status["available"]),
    )
    if gh_status["available"]:
        _record(
            checks,
            "github.gh.auth",
            bool(gh_status["authenticated"]),
            gh_status["auth_message"],
            warning=not bool(gh_status["authenticated"]),
        )

    provider_status = _provider_config_status(ophelia_root, runtime_root)
    _record(
        checks,
        "provider_config.valid",
        bool(provider_status["ok"]),
        provider_status["message"],
        warning=not bool(provider_status["ok"]),
    )

    redaction_status = _redaction_smoke_status()
    _record(
        checks,
        "redaction.smoke",
        bool(redaction_status["ok"]),
        redaction_status["message"],
    )

    for check in checks:
        if not check["ok"] and not check.get("warning"):
            blockers.append(str(check["name"]))
        elif not check["ok"] or check.get("warning"):
            warnings.append(str(check["message"]))

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "ophelia.doctor",
        "status": "ok" if not blockers else "blocked",
        "ok": not blockers,
        "runtime_root": str(runtime_root),
        "checks": checks,
        "manifests": manifest_results,
        "manifest_registry": registry,
        "state_index": state,
        "command_catalog": catalog_status,
        "docs": docs_status,
        "github": gh_status,
        "provider_config": provider_status,
        "redaction": redaction_status,
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


def _module_available(name: str) -> bool:
    try:
        __import__(name)
    except ImportError:
        return False
    return True


def _runtime_layout_status(runtime_root: Path) -> Dict[str, object]:
    writable_target = runtime_root if runtime_root.exists() else runtime_root.parent
    directories = {
        "apps": (runtime_root / "apps").exists(),
        "state": (runtime_root / "state").exists(),
        "caddy": (runtime_root / "caddy").exists(),
        "backups": (runtime_root / "backups").exists(),
    }
    return {
        "writable_target": str(writable_target),
        "writable": writable_target.exists() and os.access(writable_target, os.W_OK),
        "directories": directories,
    }


def _manifest_registry_status(manifest_dir: Path, runtime_root: Path) -> Dict[str, object]:
    try:
        from .operator_reports import manifest_registry

        registry = manifest_registry(manifest_dir, runtime_root)
    except Exception as exc:  # noqa: BLE001 - diagnostics should not crash
        return {"ok": False, "message": f"manifest registry unavailable ({type(exc).__name__})", "errors": []}
    errors = registry.get("errors") if isinstance(registry.get("errors"), list) else []
    manifests = registry.get("manifests") if isinstance(registry.get("manifests"), list) else []
    return {
        "ok": not errors,
        "message": f"{len(manifests)} manifest(s), {len(errors)} error(s)",
        "manifest_count": len(manifests),
        "error_count": len(errors),
        "errors": errors,
    }


def _state_index_status(runtime_root: Path) -> Dict[str, object]:
    try:
        from .state_db import state_status

        status = state_status(runtime_root)
    except Exception as exc:  # noqa: BLE001 - diagnostics should not crash
        return {"ok": False, "message": f"state index unavailable ({type(exc).__name__})"}
    needs_rebuild = bool(status.get("needs_rebuild"))
    return {
        "ok": not needs_rebuild,
        "message": str(status.get("summary") or "state index status unknown"),
        "db_path": status.get("db_path"),
        "exists": bool(status.get("exists")),
        "needs_rebuild": needs_rebuild,
        "schema_version_db": status.get("schema_version_db"),
        "schema_version_code": status.get("schema_version_code"),
    }


def _command_catalog_status() -> Dict[str, object]:
    try:
        from .actions import action_catalog
        from .command_catalog import catalog

        commands = catalog()
        action_ids = {item["id"] for item in action_catalog()}
    except Exception as exc:  # noqa: BLE001 - diagnostics should not crash
        return {"ok": False, "message": f"command catalog unavailable ({type(exc).__name__})"}
    operations = {str(item.get("operation")) for item in commands if item.get("operation")}
    missing_actions = sorted(action_ids - operations)
    missing_examples = sorted(str(item.get("command")) for item in commands if not item.get("examples"))
    ok = not missing_actions and not missing_examples
    return {
        "ok": ok,
        "message": (
            f"{len(commands)} command(s), {len(missing_actions)} missing action descriptor(s), "
            f"{len(missing_examples)} missing example set(s)"
        ),
        "command_count": len(commands),
        "missing_actions": missing_actions,
        "missing_examples": missing_examples[:20],
    }


def _docs_status(ophelia_root: Path) -> Dict[str, object]:
    docs_root = ophelia_root / "docs"
    changelog_root = docs_root / "changelog"
    index = changelog_root / "INDEX.md"
    if not changelog_root.exists() or not index.exists():
        return {"ok": False, "message": "docs/changelog or INDEX.md is missing"}
    try:
        index_text = index.read_text()
    except OSError as exc:
        return {"ok": False, "message": f"could not read changelog index ({exc})"}
    records = [
        path
        for path in sorted(changelog_root.glob("*.md"))
        if path.name not in {"README.md", "TEMPLATE.md", "INDEX.md"}
    ]
    missing = [path.name for path in records if path.name not in index_text]
    fenced_failures = []
    for path in sorted(docs_root.glob("**/*.md")):
        if "_worktrees" in path.parts:
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        if text.count("```") % 2 != 0:
            fenced_failures.append(str(path.relative_to(ophelia_root)))
    ok = not missing and not fenced_failures
    return {
        "ok": ok,
        "message": f"{len(records)} changelog record(s), {len(missing)} unindexed, {len(fenced_failures)} markdown fence issue(s)",
        "records": len(records),
        "unindexed": missing,
        "fenced_failures": fenced_failures[:20],
    }


def _github_status() -> Dict[str, object]:
    if os.environ.get("OPHELIA_SKIP_GH_STATUS") == "1":
        return {
            "available": False,
            "authenticated": False,
            "message": "gh status skipped by OPHELIA_SKIP_GH_STATUS",
            "auth_message": "skipped",
        }
    gh_path = shutil.which("gh")
    if not gh_path:
        return {
            "available": False,
            "authenticated": False,
            "message": "gh not found",
            "auth_message": "gh not found",
        }
    auth = _run(["gh", "auth", "status", "--hostname", "github.com"], timeout=3)
    authenticated = int(auth["returncode"]) == 0
    auth_message = "authenticated" if authenticated else "gh auth status failed"
    return {
        "available": True,
        "authenticated": authenticated,
        "path": gh_path,
        "message": f"gh found at {gh_path}",
        "auth_message": auth_message,
    }


def _provider_config_status(ophelia_root: Path, runtime_root: Path) -> Dict[str, object]:
    candidates = _provider_config_candidates(ophelia_root, runtime_root)
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return {
            "ok": False,
            "message": "no provider config discovered",
            "checked_paths": [str(path) for path in candidates],
        }
    try:
        from .provider_config import validate_provider_config

        reports = [validate_provider_config(path) for path in existing]
    except Exception as exc:  # noqa: BLE001 - diagnostics should not crash
        return {"ok": False, "message": f"provider config validation unavailable ({type(exc).__name__})"}
    blocked = [report for report in reports if report.get("status") == "blocked"]
    return {
        "ok": not blocked,
        "message": f"{len(existing)} provider config(s) checked, {len(blocked)} blocked",
        "paths": [str(path) for path in existing],
        "blocked": len(blocked),
    }


def _provider_config_candidates(ophelia_root: Path, runtime_root: Path) -> List[Path]:
    candidates = [
        ophelia_root / "traffic-providers.json",
        ophelia_root / "providers.json",
        ophelia_root / "config" / "traffic-providers.json",
        runtime_root / "providers.json",
        runtime_root / "traffic-providers.json",
    ]
    seen: set[str] = set()
    unique: List[Path] = []
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _redaction_smoke_status() -> Dict[str, object]:
    try:
        from .redaction import deep_redact

        payload = {
            "token": "ophelia-doctor-secret-token",
            "url": "postgres://user:ophelia-doctor-secret@db/app",
            "command": "check --token ophelia-doctor-secret-token",
        }
        redacted = deep_redact(payload)
        encoded = json.dumps(redacted)
        leaked = "ophelia-doctor-secret" in encoded or "postgres://user:" in encoded
    except Exception as exc:  # noqa: BLE001 - diagnostics should not crash
        return {"ok": False, "message": f"redaction smoke failed ({type(exc).__name__})"}
    return {"ok": not leaked, "message": "secret-shaped smoke payload redacted"}


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
