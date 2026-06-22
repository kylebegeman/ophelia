from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .config import REPO_ROOT
from .redaction import redact_url


DEFAULT_APP_REGISTRY = REPO_ROOT / "config" / "hostinger-app-registry.json"


@dataclass(frozen=True)
class HealthURL:
    name: str
    url: str
    expect_status: int = 200
    contains: Optional[str] = None


@dataclass(frozen=True)
class AppRegistryEntry:
    name: str
    environment: str
    compose_project: str
    root_path: str
    domains: List[str]
    caddy_site_file: str
    container_names: List[str]
    health_urls: List[HealthURL]
    public_docker_network: str
    notes: str = ""
    protected: bool = False


def default_registry_path() -> Path:
    override = os.environ.get("OPHELIA_APP_REGISTRY")
    return Path(override).expanduser() if override else DEFAULT_APP_REGISTRY


def load_app_registry(path: Optional[Path] = None) -> List[AppRegistryEntry]:
    registry_path = path or default_registry_path()
    try:
        payload = json.loads(registry_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"App registry JSON is invalid: {registry_path}") from exc
    entries = payload.get("apps", payload) if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError(f"App registry must contain a list of apps: {registry_path}")
    return [_entry(item) for item in entries]


def find_app(name: str, path: Optional[Path] = None) -> AppRegistryEntry:
    for entry in load_app_registry(path):
        if entry.name == name:
            return entry
    raise KeyError(f"Unknown app `{name}`.")


def registry_conflicts(entries: Iterable[AppRegistryEntry]) -> Dict[str, List[str]]:
    domains: Dict[str, List[str]] = {}
    containers: Dict[str, List[str]] = {}
    caddy_files: Dict[str, List[str]] = {}
    compose_projects: Dict[str, List[str]] = {}
    for entry in entries:
        for domain in entry.domains:
            domains.setdefault(domain, []).append(entry.name)
        for container in entry.container_names:
            containers.setdefault(container, []).append(entry.name)
        caddy_files.setdefault(entry.caddy_site_file, []).append(entry.name)
        compose_projects.setdefault(entry.compose_project, []).append(entry.name)
    return {
        "domains": _duplicates(domains),
        "containers": _duplicates(containers),
        "caddy_site_files": _duplicates(caddy_files),
        "compose_projects": _duplicates(compose_projects),
    }


def app_health(entry: AppRegistryEntry, timeout: int = 10, skip_docker: bool = False) -> Dict[str, object]:
    if timeout <= 0:
        raise ValueError("Health check timeout must be greater than 0.")
    containers = [] if skip_docker else _container_health(entry.container_names)
    urls = [_check_url(check, timeout) for check in entry.health_urls]
    ok = all(item.get("ok") for item in containers) and all(item.get("ok") for item in urls)
    return {
        "app": entry.name,
        "environment": entry.environment,
        "compose_project": entry.compose_project,
        "containers": containers,
        "health_urls": urls,
        "ok": ok,
    }


def app_logs(entry: AppRegistryEntry, tail: int = 100, container: Optional[str] = None) -> Dict[str, object]:
    if tail <= 0:
        raise ValueError("Log tail must be greater than 0.")
    selected = [container] if container else entry.container_names
    unknown = [name for name in selected if name not in entry.container_names]
    if unknown:
        raise ValueError(f"Container is not registered for {entry.name}: {', '.join(unknown)}")
    logs = []
    for name in selected:
        result = _run(["docker", "logs", "--tail", str(tail), name], timeout=15)
        logs.append(
            {
                "container": name,
                "ok": result["returncode"] == 0,
                "stdout": result["stdout"],
                "stderr": result["stderr"],
            }
        )
    return {"app": entry.name, "logs": logs}


def _entry(raw: Dict[str, object]) -> AppRegistryEntry:
    if not isinstance(raw, dict):
        raise ValueError("App registry entries must be objects.")
    name = _required_str(raw, "name")
    health_urls = []
    raw_health_urls = raw.get("health_urls", []) or []
    if not isinstance(raw_health_urls, list):
        raise ValueError(f"`health_urls` must be a list for app `{name}`.")
    for item in raw_health_urls:
        if isinstance(item, str):
            _validate_health_url(item)
            health_urls.append(HealthURL(name=item, url=item))
        elif isinstance(item, dict):
            if "url" not in item:
                raise ValueError("App registry health URL objects require `url`.")
            url = str(item["url"])
            _validate_health_url(url)
            try:
                expect_status = int(item.get("expect_status", 200))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Health URL expect_status must be an HTTP status code: {redact_url(url)}") from exc
            if expect_status < 100 or expect_status > 599:
                raise ValueError(f"Health URL expect_status must be an HTTP status code: {redact_url(url)}")
            health_urls.append(
                HealthURL(
                    name=str(item.get("name") or url),
                    url=url,
                    expect_status=expect_status,
                    contains=str(item["contains"]) if item.get("contains") is not None else None,
                )
            )
        else:
            raise ValueError("App registry health_urls entries must be strings or objects.")
    protected = raw.get("protected", False)
    if not isinstance(protected, bool):
        raise ValueError(f"`protected` must be a boolean for app `{name}`.")
    return AppRegistryEntry(
        name=name,
        environment=_optional_str(raw, "environment", default="unknown"),
        compose_project=_required_str(raw, "compose_project"),
        root_path=_required_str(raw, "root_path"),
        domains=_string_list(raw, "domains"),
        caddy_site_file=_required_str(raw, "caddy_site_file"),
        container_names=_string_list(raw, "container_names"),
        health_urls=health_urls,
        public_docker_network=_optional_str(raw, "public_docker_network", default="ophelia-edge"),
        notes=_optional_str(raw, "notes", default=""),
        protected=protected,
    )


def _required_str(raw: Dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        name = raw.get("name")
        suffix = f" for app `{name}`" if isinstance(name, str) and name else ""
        raise ValueError(f"`{key}` must be a non-empty string{suffix}.")
    return value


def _optional_str(raw: Dict[str, object], key: str, default: str) -> str:
    value = raw.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        name = raw.get("name")
        suffix = f" for app `{name}`" if isinstance(name, str) and name else ""
        raise ValueError(f"`{key}` must be a string{suffix}.")
    return value


def _string_list(raw: Dict[str, object], key: str) -> List[str]:
    value = raw.get(key, [])
    if value is None:
        return []
    name = raw.get("name")
    suffix = f" for app `{name}`" if isinstance(name, str) and name else ""
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"`{key}` must be a list of non-empty strings{suffix}.")
    return value


def _validate_health_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Health URL must be http(s): {redact_url(url)}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Health URL must not contain credentials, query strings, or fragments.")


def _duplicates(values: Dict[str, List[str]]) -> List[str]:
    return [key for key, owners in sorted(values.items()) if len(set(owners)) > 1]


def _container_health(names: List[str]) -> List[Dict[str, object]]:
    results = []
    for name in names:
        inspect = _run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.Name}}\t{{.State.Status}}\t{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                name,
            ],
            timeout=5,
        )
        if inspect["returncode"] != 0:
            results.append({"container": name, "ok": False, "status": "missing", "health": None})
            continue
        parts = str(inspect["stdout"]).strip().split("\t")
        status = parts[1] if len(parts) > 1 else "unknown"
        health = parts[2] if len(parts) > 2 else "none"
        healthy = status == "running" and health in {"healthy", "none"}
        results.append({"container": name, "ok": healthy, "status": status, "health": health})
    return results


def _check_url(check: HealthURL, timeout: int) -> Dict[str, object]:
    display_url = redact_url(check.url)
    display_name = redact_url(check.name)
    try:
        request = urllib.request.Request(check.url, headers={"User-Agent": "ophelia-health/1"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(65536).decode("utf-8", errors="replace")
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        body = exc.read(65536).decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError) as exc:
        return {
            "name": display_name,
            "url": display_url,
            "ok": False,
            "status_code": None,
            "error": str(exc),
        }
    contains_ok = check.contains is None or check.contains in body
    return {
        "name": display_name,
        "url": display_url,
        "ok": status == check.expect_status and contains_ok,
        "status_code": status,
        "expect_status": check.expect_status,
        "contains_required": check.contains is not None,
    }


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
