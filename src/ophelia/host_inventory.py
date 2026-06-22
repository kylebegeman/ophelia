from __future__ import annotations

import json
import platform
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .inspection import EDGE_NETWORK, INTERNAL_NETWORK, status_report
from .manifest import Manifest
from .operation_schema import SCHEMA_VERSION, issue, operation_id
from .redaction import deep_redact

HOST_INVENTORY_KIND = "ophelia.host_inventory"
HOST_READINESS_KIND = "ophelia.host_readiness"
APP_PLACEMENT_PLAN_KIND = "ophelia.app_placement_plan"

_DEFAULT_HOST_CONFIG_CANDIDATES = (
    Path("hosts/hosts.json"),
    Path("hosts/hosts.yml"),
    Path("hosts/hosts.yaml"),
    Path("inventory/hosts.json"),
    Path("inventory/hosts.yml"),
    Path("inventory/hosts.yaml"),
)

_CAPABILITY_KEYS = (
    "docker",
    "docker_compose",
    "caddy",
    "edge",
    "network",
    "backups",
    "postgres",
    "redis",
    "static_assets",
    "service_runtime",
)

_MIN_STATIC_DISK_MB = 1024
_MIN_SERVICE_DISK_MB = 5120
_MIN_DATABASE_DISK_MB = 5120
_MIN_REDIS_DISK_MB = 2048
_MIN_STATIC_MEMORY_MB = 512
_MIN_SERVICE_MEMORY_MB = 1024
_MIN_POSTGRES_MEMORY_MB = 512
_MIN_REDIS_MEMORY_MB = 256


def collect_host_inventory(
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    ophelia_root: Path = REPO_ROOT,
    manifests_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Collect read-only host inventory records.

    The local host is always represented from live, read-only observations.
    Optional host config can add remote hosts or annotate the local host, but
    this function never creates state files or mutates hosts.
    """
    runtime_root = Path(runtime_root)
    ophelia_root = Path(ophelia_root)
    manifests_dir = Path(manifests_dir or ophelia_root / "manifests")

    warnings: List[Dict[str, str]] = []
    blockers: List[Dict[str, str]] = []
    config, resolved_config_path = _load_host_config(runtime_root, ophelia_root, config_path, warnings, blockers)

    status = status_report(runtime_root, ophelia_root, manifests_dir)
    configured_hosts = _configured_hosts(config)
    local_host_id = _local_host_id(runtime_root, configured_hosts)
    local_record = _local_host_record(local_host_id, runtime_root, ophelia_root, status)

    hosts_by_id: Dict[str, Dict[str, Any]] = {local_record["id"]: local_record}
    for record in configured_hosts:
        normalized = _normalize_config_host(record, runtime_root, resolved_config_path)
        host_id = normalized["id"]
        if host_id == local_record["id"]:
            hosts_by_id[host_id] = _merge_local_with_config(local_record, normalized)
        else:
            hosts_by_id[host_id] = normalized

    hosts = sorted(hosts_by_id.values(), key=lambda host: str(host.get("id")))
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": HOST_INVENTORY_KIND,
        "operation": "host.inventory",
        "operation_id": operation_id("host.inventory"),
        "status": "blocked" if blockers else ("warning" if warnings else "ok"),
        "runtime_root": str(runtime_root),
        "ophelia_root": str(ophelia_root),
        "manifests_dir": str(manifests_dir),
        "config_path": str(resolved_config_path) if resolved_config_path else None,
        "read_only": True,
        "hosts": hosts,
        "summary": f"{len(hosts)} host(s) discovered; {len(blockers)} blocker(s), {len(warnings)} warning(s).",
        "blockers": blockers,
        "warnings": warnings,
        "values_redacted": True,
    }
    return deep_redact(payload, safe_keys={"values_redacted"}, propagate=True)


def host_readiness(
    host_id: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    ophelia_root: Path = REPO_ROOT,
    manifests_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    inventory = collect_host_inventory(runtime_root, ophelia_root, manifests_dir, config_path)
    inventory_hosts = [host for host in inventory.get("hosts", []) if isinstance(host, dict)]
    selected_hosts = [
        host for host in inventory_hosts if host_id is None or str(host.get("id")) == host_id
    ]

    blockers: List[Dict[str, str]] = []
    warnings = list(inventory.get("warnings", [])) if isinstance(inventory.get("warnings"), list) else []
    if host_id and not selected_hosts:
        blockers.append(issue("host_not_found", f"Host `{host_id}` was not found in inventory.", "host_id"))

    host_reports = [_host_readiness_record(host) for host in selected_hosts]
    for report in host_reports:
        blockers.extend(report.get("blockers", []))
        warnings.extend(report.get("warnings", []))

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": HOST_READINESS_KIND,
        "operation": "host.readiness",
        "operation_id": operation_id("host.readiness"),
        "status": "blocked" if blockers else ("warning" if warnings else "ok"),
        "runtime_root": str(runtime_root),
        "config_path": inventory.get("config_path"),
        "host_id": host_id,
        "read_only": True,
        "hosts": host_reports,
        "blockers": blockers,
        "warnings": warnings,
        "summary": f"{len(host_reports)} host(s) checked; {len(blockers)} blocker(s), {len(warnings)} warning(s).",
        "values_redacted": True,
    }
    return deep_redact(payload, safe_keys={"values_redacted"}, propagate=True)


def app_placement_plan(
    app: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifests_dir: Optional[Path] = None,
    manifest_path: Optional[Path] = None,
    ophelia_root: Path = REPO_ROOT,
    config_path: Optional[Path] = None,
    source_host: Optional[str] = None,
    target_host: Optional[str] = None,
) -> Dict[str, Any]:
    """Plan app placement against read-only host inventory."""
    runtime_root = Path(runtime_root)
    ophelia_root = Path(ophelia_root)
    manifests_dir = Path(manifests_dir or ophelia_root / "manifests")
    inventory = collect_host_inventory(runtime_root, ophelia_root, manifests_dir, config_path)

    try:
        from .portability import resolve_app_manifest
    except ImportError as exc:  # pragma: no cover - module is part of Ophelia
        return _blocked_placement_plan(
            app,
            environment,
            runtime_root,
            manifests_dir,
            inventory,
            [issue("manifest_resolver_unavailable", str(exc))],
            [],
            source_host,
            target_host,
        )

    resolution = resolve_app_manifest(
        app,
        environment,
        manifest_path=manifest_path,
        search_dirs=[manifests_dir],
    )
    warnings = [issue("manifest_resolution_warning", warning) for warning in resolution.warnings]
    blockers = [issue("manifest_resolution_blocker", blocker) for blocker in resolution.blockers]
    if resolution.manifest is None:
        return _blocked_placement_plan(
            app,
            environment,
            runtime_root,
            manifests_dir,
            inventory,
            blockers,
            warnings,
            source_host,
            target_host,
            candidates=resolution.candidates,
        )

    manifest = resolution.manifest
    resolved_environment = environment or manifest.environment or "unknown"
    requirements = _placement_requirements(manifest, resolution.manifest_path)
    hosts = [host for host in inventory.get("hosts", []) if isinstance(host, dict)]
    if not hosts:
        blockers.append(issue("host_inventory_empty", "No hosts are available for placement scoring."))

    placements = [
        _score_host_for_app(host, requirements, source_host=source_host, target_host=target_host)
        for host in hosts
    ]
    eligible = [placement for placement in placements if not placement.get("blockers")]
    recommended = sorted(
        eligible,
        key=lambda placement: (
            placement.get("matches_requested_target") is True,
            int(placement.get("score") or 0),
            str(placement.get("host_id")),
        ),
        reverse=True,
    )
    if target_host and not any(placement.get("host_id") == target_host for placement in placements):
        blockers.append(issue("target_host_not_found", f"Target host `{target_host}` was not found in inventory.", "target_host"))
    if not recommended:
        blockers.append(issue("no_eligible_host", f"No eligible host found for app `{manifest.app}`."))

    summary_status = "blocked" if blockers else ("warning" if warnings or any(p.get("warnings") for p in placements) else "ready")
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": APP_PLACEMENT_PLAN_KIND,
        "operation": "app.placement.plan",
        "operation_id": operation_id("app.placement.plan", manifest.app, resolved_environment),
        "status": summary_status,
        "app": manifest.app,
        "environment": resolved_environment,
        "manifest_path": str(resolution.manifest_path) if resolution.manifest_path else None,
        "runtime_root": str(runtime_root),
        "manifests_dir": str(manifests_dir),
        "config_path": inventory.get("config_path"),
        "read_only": True,
        "dry_run": True,
        "source_host": source_host,
        "target_host": target_host,
        "requirements": requirements,
        "placements": placements,
        "recommended_hosts": [placement["host_id"] for placement in recommended[:3]],
        "recommended_host": recommended[0]["host_id"] if recommended else None,
        "inventory_summary": {
            "host_count": len(hosts),
            "inventory_status": inventory.get("status"),
            "warnings": len(inventory.get("warnings", [])) if isinstance(inventory.get("warnings"), list) else 0,
        },
        "blockers": blockers,
        "warnings": warnings,
        "summary": (
            f"Placement plan for {manifest.app}: "
            f"{len(recommended)} eligible host(s), {len(blockers)} blocker(s)."
        ),
        "values_redacted": True,
    }
    return deep_redact(payload, safe_keys={"values_redacted"}, propagate=True)


def legacy_host_inventory(
    runtime_root: Path,
    ophelia_root: Path = REPO_ROOT,
    manifests_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Compatibility wrapper for the historical operator report shape."""
    inventory = collect_host_inventory(runtime_root, ophelia_root, manifests_dir, config_path)
    hosts = [host for host in inventory.get("hosts", []) if isinstance(host, dict)]
    local = next((host for host in hosts if host.get("observed") is True), hosts[0] if hosts else {})
    disk = local.get("disk") if isinstance(local.get("disk"), dict) else {}
    docker = local.get("docker") if isinstance(local.get("docker"), dict) else {}
    caddy = local.get("caddy") if isinstance(local.get("caddy"), dict) else {}
    shared_services = local.get("foundation_services") if isinstance(local.get("foundation_services"), dict) else {}
    networks = local.get("networks") if isinstance(local.get("networks"), dict) else {}
    payload = {
        **inventory,
        "ophelia_commit": _git_sha(ophelia_root),
        "docker": docker,
        "disk_usage": disk,
        "running_containers": _docker_lines(["docker", "ps", "--format", "{{.Names}}"]),
        "caddy_status": caddy,
        "networks": networks.get("docker", []) if isinstance(networks, dict) else [],
        "shared_service_status": shared_services,
    }
    return payload


def _blocked_placement_plan(
    app: str,
    environment: Optional[str],
    runtime_root: Path,
    manifests_dir: Path,
    inventory: Dict[str, Any],
    blockers: List[Dict[str, str]],
    warnings: List[Dict[str, str]],
    source_host: Optional[str],
    target_host: Optional[str],
    candidates: Optional[List[Dict[str, object]]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": APP_PLACEMENT_PLAN_KIND,
        "operation": "app.placement.plan",
        "operation_id": operation_id("app.placement.plan", app, environment),
        "status": "blocked",
        "app": app,
        "environment": environment,
        "runtime_root": str(runtime_root),
        "manifests_dir": str(manifests_dir),
        "config_path": inventory.get("config_path"),
        "read_only": True,
        "dry_run": True,
        "source_host": source_host,
        "target_host": target_host,
        "requirements": None,
        "placements": [],
        "recommended_hosts": [],
        "recommended_host": None,
        "inventory_summary": {
            "host_count": len(inventory.get("hosts", [])) if isinstance(inventory.get("hosts"), list) else 0,
            "inventory_status": inventory.get("status"),
        },
        "manifest_candidates": candidates or [],
        "blockers": blockers,
        "warnings": warnings,
        "summary": f"Placement plan for {app} is blocked before scoring.",
        "values_redacted": True,
    }
    return deep_redact(payload, safe_keys={"values_redacted"}, propagate=True)


def _placement_requirements(manifest: Manifest, manifest_path: Optional[Path]) -> Dict[str, Any]:
    host_req = manifest.host_requirements
    needs_postgres = bool(manifest.addons.postgres or manifest.data.postgres)
    needs_redis = bool(manifest.addons.redis or manifest.data.redis)
    has_data = bool(
        needs_postgres
        or needs_redis
        or manifest.data.volumes
        or manifest.data.static_assets
        or (manifest.data.backups and manifest.data.backups.required)
    )
    needs_backups = bool(has_data or (manifest.data.backups and manifest.data.backups.restore_drill_required))
    needs_docker = (
        host_req.requires_docker
        if host_req.requires_docker is not None
        else manifest.kind not in {"static", "redirect", "tunnel"} or bool(manifest.services) or needs_postgres or needs_redis
    )
    needs_caddy = bool(manifest.routes or manifest.kind in {"static", "redirect", "tunnel"})
    needs_edge = host_req.requires_edge if host_req.requires_edge is not None else needs_caddy
    min_memory_mb = _required_memory_mb(manifest)
    min_disk_free_mb = _required_disk_free_mb(manifest)
    return {
        "app": manifest.app,
        "environment": manifest.environment,
        "manifest_path": str(manifest_path) if manifest_path else None,
        "kind": manifest.kind,
        "profile": manifest.profile,
        "service_count": len(manifest.services),
        "route_count": len(manifest.routes),
        "data_dependencies": {
            "postgres": needs_postgres,
            "redis": needs_redis,
            "volumes": len(manifest.data.volumes),
            "static_assets": len(manifest.data.static_assets),
            "backups_required": bool(manifest.data.backups and manifest.data.backups.required),
            "restore_drill_required": bool(manifest.data.backups and manifest.data.backups.restore_drill_required),
        },
        "required_capabilities": {
            "docker": bool(needs_docker),
            "caddy": bool(needs_caddy),
            "edge": bool(needs_edge),
            "backups": bool(needs_backups),
            "postgres": needs_postgres,
            "redis": needs_redis,
            "static_assets": manifest.kind == "static",
        },
        "host_requirements": {
            "arch": host_req.arch,
            "min_memory": host_req.min_memory,
            "min_disk_free": host_req.min_disk_free,
            "requires_edge": host_req.requires_edge,
            "requires_docker": host_req.requires_docker,
        },
        "min_memory_mb": min_memory_mb,
        "min_disk_free_mb": min_disk_free_mb,
    }


def _score_host_for_app(
    host: Dict[str, Any],
    requirements: Dict[str, Any],
    *,
    source_host: Optional[str],
    target_host: Optional[str],
) -> Dict[str, Any]:
    host_id = str(host.get("id") or "unknown")
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    checks: List[Dict[str, Any]] = []

    required_capabilities = requirements.get("required_capabilities")
    required_capabilities = required_capabilities if isinstance(required_capabilities, dict) else {}
    capabilities = host.get("capabilities") if isinstance(host.get("capabilities"), dict) else {}

    services_score = 25
    for capability in ("docker", "caddy", "postgres", "redis"):
        required = bool(required_capabilities.get(capability))
        actual = _bool_or_none(capabilities.get(capability))
        if not required:
            checks.append({"name": f"capability.{capability}", "required": False, "actual": actual, "ok": True})
            continue
        ok = actual is True
        checks.append({"name": f"capability.{capability}", "required": True, "actual": actual, "ok": ok})
        if actual is False:
            blockers.append(issue(f"host_{capability}_unavailable", f"Host `{host_id}` does not provide required `{capability}` capability."))
            services_score -= 10
        elif actual is None:
            blockers.append(issue(f"host_{capability}_unknown", f"Host `{host_id}` has no declared `{capability}` capability."))
            services_score -= 7

    network_score = 15
    for capability in ("edge",):
        required = bool(required_capabilities.get(capability))
        actual = _bool_or_none(capabilities.get(capability))
        if not required:
            checks.append({"name": f"capability.{capability}", "required": False, "actual": actual, "ok": True})
            continue
        ok = actual is True
        checks.append({"name": f"capability.{capability}", "required": True, "actual": actual, "ok": ok})
        if actual is False:
            blockers.append(issue("host_edge_unavailable", f"Host `{host_id}` does not report edge readiness."))
            network_score -= 10
        elif actual is None:
            warnings.append(issue("host_edge_unknown", f"Host `{host_id}` has no declared edge capability."))
            network_score -= 5

    backup_score = 15
    backups_required = bool(required_capabilities.get("backups"))
    backup_capability = _bool_or_none(capabilities.get("backups"))
    checks.append(
        {"name": "capability.backups", "required": backups_required, "actual": backup_capability, "ok": (not backups_required) or backup_capability is True}
    )
    if backups_required and backup_capability is False:
        blockers.append(issue("host_backups_unavailable", f"Host `{host_id}` does not provide required backup staging."))
        backup_score = 0
    elif backups_required and backup_capability is None:
        warnings.append(issue("host_backups_unknown", f"Host `{host_id}` has no declared backup capability."))
        backup_score = 5

    capacity_score = _capacity_score(host, requirements, checks, warnings, blockers)
    provider_score = _provider_score(host, target_host, checks, warnings)
    locality_score = _locality_score(host, source_host, requirements, checks)
    score = max(0, min(100, capacity_score + max(0, services_score) + max(0, network_score) + backup_score + provider_score + locality_score))

    recommendation = "blocked" if blockers else "recommended"
    if target_host and host_id != target_host and not blockers:
        recommendation = "eligible"
    return {
        "host_id": host_id,
        "host": _host_public_summary(host),
        "score": score,
        "recommendation": recommendation,
        "matches_requested_target": bool(target_host and host_id == target_host),
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "score_breakdown": {
            "capacity_fit": capacity_score,
            "service_capabilities": max(0, services_score),
            "network_fit": max(0, network_score),
            "backup_restore_readiness": backup_score,
            "provider_constraints": provider_score,
            "data_locality": locality_score,
        },
    }


def _capacity_score(
    host: Dict[str, Any],
    requirements: Dict[str, Any],
    checks: List[Dict[str, Any]],
    warnings: List[Dict[str, str]],
    blockers: List[Dict[str, str]],
) -> int:
    score = 30
    host_id = str(host.get("id") or "unknown")
    capacity = host.get("capacity") if isinstance(host.get("capacity"), dict) else {}
    disk_free_mb = _number_or_none(capacity.get("disk_free_mb"))
    memory_total_mb = _number_or_none(capacity.get("memory_total_mb"))
    required_disk = _number_or_none(requirements.get("min_disk_free_mb"))
    required_memory = _number_or_none(requirements.get("min_memory_mb"))

    if required_disk is not None:
        ok = disk_free_mb is not None and disk_free_mb >= required_disk
        checks.append({"name": "capacity.disk_free_mb", "required": required_disk, "actual": disk_free_mb, "ok": ok})
        if disk_free_mb is None:
            warnings.append(issue("host_disk_unknown", f"Host `{host_id}` does not report free disk capacity."))
            score -= 8
        elif not ok:
            blockers.append(issue("host_disk_insufficient", f"Host `{host_id}` has {int(disk_free_mb)} MB free disk, below required {int(required_disk)} MB."))
            score -= 15

    if required_memory is not None:
        ok = memory_total_mb is not None and memory_total_mb >= required_memory
        checks.append({"name": "capacity.memory_total_mb", "required": required_memory, "actual": memory_total_mb, "ok": ok})
        if memory_total_mb is None:
            warnings.append(issue("host_memory_unknown", f"Host `{host_id}` does not report memory capacity."))
            score -= 8
        elif not ok:
            blockers.append(issue("host_memory_insufficient", f"Host `{host_id}` has {int(memory_total_mb)} MB memory, below required {int(required_memory)} MB."))
            score -= 15
    return max(0, score)


def _provider_score(
    host: Dict[str, Any],
    target_host: Optional[str],
    checks: List[Dict[str, Any]],
    warnings: List[Dict[str, str]],
) -> int:
    host_id = str(host.get("id") or "unknown")
    provider = host.get("provider")
    checks.append({"name": "provider.declared", "required": True, "actual": provider, "ok": isinstance(provider, str) and bool(provider)})
    if target_host and host_id != target_host:
        return 10
    if isinstance(provider, str) and provider:
        return 15
    warnings.append(issue("host_provider_unknown", f"Host `{host_id}` has no provider metadata."))
    return 8


def _locality_score(
    host: Dict[str, Any],
    source_host: Optional[str],
    requirements: Dict[str, Any],
    checks: List[Dict[str, Any]],
) -> int:
    host_id = str(host.get("id") or "unknown")
    app = str(requirements.get("app") or "")
    apps = host.get("apps") if isinstance(host.get("apps"), list) else []
    local_app_present = any(isinstance(item, dict) and item.get("app") == app for item in apps)
    source_match = bool(source_host and host_id == source_host)
    checks.append({"name": "data.locality", "source_host": source_host, "app_present": local_app_present, "ok": source_match or local_app_present})
    if source_match:
        return 15
    if local_app_present:
        return 12
    return 8


def _host_readiness_record(host: Dict[str, Any]) -> Dict[str, Any]:
    host_id = str(host.get("id") or "unknown")
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    checks: List[Dict[str, Any]] = []
    capabilities = host.get("capabilities") if isinstance(host.get("capabilities"), dict) else {}
    capacity = host.get("capacity") if isinstance(host.get("capacity"), dict) else {}

    for capability in ("docker", "docker_compose", "network", "edge", "backups"):
        value = _bool_or_none(capabilities.get(capability))
        checks.append({"name": f"capability.{capability}", "ok": value is True, "value": value})
        if value is False and capability in {"docker", "docker_compose"}:
            warnings.append(issue(f"host_{capability}_unavailable", f"Host `{host_id}` does not currently report `{capability}`."))
        elif value is None:
            warnings.append(issue(f"host_{capability}_unknown", f"Host `{host_id}` has no `{capability}` capability observation."))

    disk_free_mb = _number_or_none(capacity.get("disk_free_mb"))
    checks.append({"name": "capacity.disk_free_mb", "ok": disk_free_mb is None or disk_free_mb >= _MIN_SERVICE_DISK_MB, "value": disk_free_mb})
    if disk_free_mb is None:
        warnings.append(issue("host_disk_unknown", f"Host `{host_id}` does not report free disk."))
    elif disk_free_mb < _MIN_SERVICE_DISK_MB:
        blockers.append(issue("host_disk_low", f"Host `{host_id}` has less than {_MIN_SERVICE_DISK_MB} MB free disk."))

    memory_total_mb = _number_or_none(capacity.get("memory_total_mb"))
    checks.append({"name": "capacity.memory_total_mb", "ok": memory_total_mb is None or memory_total_mb >= _MIN_STATIC_MEMORY_MB, "value": memory_total_mb})
    if memory_total_mb is None:
        warnings.append(issue("host_memory_unknown", f"Host `{host_id}` does not report memory capacity."))

    return {
        "host_id": host_id,
        "host": _host_public_summary(host),
        "status": "blocked" if blockers else ("warning" if warnings else "ok"),
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
    }


def _local_host_record(
    host_id: str,
    runtime_root: Path,
    ophelia_root: Path,
    status: Dict[str, Any],
) -> Dict[str, Any]:
    docker = status.get("docker") if isinstance(status.get("docker"), dict) else {}
    shared_services = status.get("shared_services") if isinstance(status.get("shared_services"), dict) else {}
    docker_networks = status.get("docker_networks") if isinstance(status.get("docker_networks"), list) else []
    disk = status.get("disk_usage") if isinstance(status.get("disk_usage"), dict) else {}
    memory_bytes = _memory_total_bytes()
    backup_path = runtime_root / "backups"
    foundation = _foundation_services(shared_services)
    capabilities = {
        "docker": bool(docker.get("available") and docker.get("daemon_available")),
        "docker_compose": bool(docker.get("compose_available")),
        "caddy": bool(shared_services.get("available")),
        "edge": EDGE_NETWORK in docker_networks,
        "network": INTERNAL_NETWORK in docker_networks or EDGE_NETWORK in docker_networks,
        "backups": backup_path.exists(),
        "postgres": _service_available(foundation, "postgres"),
        "redis": _service_available(foundation, "redis"),
        "static_assets": True,
        "service_runtime": bool(docker.get("available") and docker.get("compose_available")),
    }
    return {
        "id": host_id,
        "name": host_id,
        "provider": "local",
        "region": "local",
        "hostname": platform.node(),
        "observed": True,
        "source": "local_status",
        "runtime_root": str(runtime_root),
        "ophelia_root": str(ophelia_root),
        "roles": ["runtime"],
        "labels": ["local"],
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
        },
        "architecture": _normalized_arch(platform.machine()),
        "capacity": {
            "memory_total_bytes": memory_bytes,
            "memory_total_mb": _bytes_to_mb(memory_bytes),
            "disk_total_bytes": disk.get("total_bytes"),
            "disk_used_bytes": disk.get("used_bytes"),
            "disk_free_bytes": disk.get("free_bytes"),
            "disk_free_mb": _bytes_to_mb(_int_or_none(disk.get("free_bytes"))),
            "disk_percent_used": disk.get("percent_used"),
        },
        "disk": disk,
        "docker": docker,
        "caddy": shared_services,
        "networks": {
            "docker": docker_networks,
            "edge": EDGE_NETWORK in docker_networks,
            "internal": INTERNAL_NETWORK in docker_networks,
        },
        "backup": {
            "available": backup_path.exists(),
            "path": str(backup_path),
            "restore_ready": (backup_path / "apps").exists() or (backup_path / "restore-drills").exists(),
        },
        "foundation_services": foundation,
        "apps": status.get("known_apps", []) if isinstance(status.get("known_apps"), list) else [],
        "capabilities": capabilities,
        "warnings": [issue("host_status_warning", warning) for warning in status.get("warnings", []) if isinstance(warning, str)],
    }


def _normalize_config_host(
    record: Dict[str, Any],
    runtime_root: Path,
    config_path: Optional[Path],
) -> Dict[str, Any]:
    host_id = str(record.get("id") or record.get("host_id") or record.get("name") or "").strip()
    if not host_id:
        host_id = "unnamed-host"
    capacity = record.get("capacity") if isinstance(record.get("capacity"), dict) else {}
    disk_free_mb = _capacity_mb(capacity, "disk_free")
    memory_total_mb = _capacity_mb(capacity, "memory")
    capabilities = _normalize_capabilities(record.get("capabilities"), observed=False)
    disk = record.get("disk") if isinstance(record.get("disk"), dict) else {}
    docker = record.get("docker") if isinstance(record.get("docker"), dict) else {}
    caddy = record.get("caddy") if isinstance(record.get("caddy"), dict) else {}
    network = record.get("network") if isinstance(record.get("network"), dict) else {}
    backup = record.get("backup") if isinstance(record.get("backup"), dict) else {}
    foundation = record.get("foundation_services") if isinstance(record.get("foundation_services"), dict) else {}
    return {
        "id": host_id,
        "name": str(record.get("name") or host_id),
        "provider": str(record.get("provider") or "unknown"),
        "region": str(record.get("region") or "unknown"),
        "hostname": record.get("hostname"),
        "observed": False,
        "source": str(config_path) if config_path else "configured",
        "runtime_root": str(record.get("runtime_root") or runtime_root),
        "roles": _string_list(record.get("roles")),
        "labels": _string_list(record.get("labels")),
        "os": record.get("os") if isinstance(record.get("os"), dict) else {},
        "architecture": str(record.get("architecture") or record.get("arch") or "unknown"),
        "capacity": {
            "memory_total_bytes": _mb_to_bytes(memory_total_mb),
            "memory_total_mb": memory_total_mb,
            "disk_total_bytes": _int_or_none(capacity.get("disk_total_bytes")),
            "disk_used_bytes": _int_or_none(capacity.get("disk_used_bytes")),
            "disk_free_bytes": _mb_to_bytes(disk_free_mb),
            "disk_free_mb": disk_free_mb,
            "disk_percent_used": _number_or_none(capacity.get("disk_percent_used")),
        },
        "disk": disk,
        "docker": docker,
        "caddy": caddy,
        "networks": network,
        "backup": backup,
        "foundation_services": foundation,
        "apps": record.get("apps") if isinstance(record.get("apps"), list) else [],
        "capabilities": capabilities,
        "warnings": [],
        "metadata": record.get("metadata") if isinstance(record.get("metadata"), dict) else {},
    }


def _merge_local_with_config(local: Dict[str, Any], configured: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(local)
    for key in ("name", "provider", "region", "roles", "labels", "metadata"):
        if configured.get(key):
            merged[key] = configured[key]
    if configured.get("hostname"):
        merged["hostname"] = configured["hostname"]
    if configured.get("source"):
        merged["source"] = "local_status+configured"
        merged["config_source"] = configured["source"]
    configured_capabilities = configured.get("capabilities") if isinstance(configured.get("capabilities"), dict) else {}
    local_capabilities = local.get("capabilities") if isinstance(local.get("capabilities"), dict) else {}
    merged_capabilities = dict(configured_capabilities)
    merged_capabilities.update(local_capabilities)
    merged["capabilities"] = merged_capabilities
    capacity = dict(configured.get("capacity") if isinstance(configured.get("capacity"), dict) else {})
    capacity.update(local.get("capacity") if isinstance(local.get("capacity"), dict) else {})
    merged["capacity"] = capacity
    return merged


def _configured_hosts(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    hosts = config.get("hosts") if isinstance(config.get("hosts"), list) else []
    return [host for host in hosts if isinstance(host, dict)]


def _load_host_config(
    runtime_root: Path,
    ophelia_root: Path,
    config_path: Optional[Path],
    warnings: List[Dict[str, str]],
    blockers: List[Dict[str, str]],
) -> Tuple[Dict[str, Any], Optional[Path]]:
    candidates: List[Path]
    explicit = config_path is not None
    if config_path is not None:
        candidates = [Path(config_path)]
    else:
        candidates = [runtime_root / relative for relative in _DEFAULT_HOST_CONFIG_CANDIDATES]
        candidates.append(ophelia_root / "config" / "ophelia-hosts.yml")

    selected = next((path for path in candidates if path.exists()), None)
    if selected is None:
        if explicit:
            blockers.append(issue("host_config_not_found", f"Host config not found: {config_path}", "config_path"))
        return {}, None

    try:
        payload = _read_config_file(selected)
    except (OSError, ValueError) as exc:
        blockers.append(issue("host_config_invalid", f"Could not read host config {selected}: {exc}", "config_path"))
        return {}, selected
    if not isinstance(payload, dict):
        blockers.append(issue("host_config_invalid", f"Host config {selected} must be a mapping.", "config_path"))
        return {}, selected
    version = payload.get("version")
    if version not in (None, 1):
        warnings.append(issue("host_config_version_unknown", f"Host config version `{version}` is not recognized.", "version"))
    return payload, selected


def _read_config_file(path: Path) -> Any:
    text = path.read_text()
    if path.suffix == ".json":
        return json.loads(text)
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover - PyYAML is a project dependency
        raise ValueError("PyYAML is required for YAML host inventory config") from exc
    return yaml.safe_load(text) or {}


def _normalize_capabilities(value: Any, *, observed: bool) -> Dict[str, Optional[bool]]:
    source = value if isinstance(value, dict) else {}
    capabilities: Dict[str, Optional[bool]] = {}
    for key in _CAPABILITY_KEYS:
        if key in source:
            capabilities[key] = _bool_or_none(source.get(key))
        elif observed:
            capabilities[key] = False
        else:
            capabilities[key] = None
    return capabilities


def _foundation_services(shared_services: Dict[str, Any]) -> Dict[str, Any]:
    if not shared_services:
        return {}
    if any(key in shared_services for key in ("postgres", "redis", "caddy")):
        return shared_services
    return {"shared": shared_services}


def _service_available(foundation: Dict[str, Any], name: str) -> bool:
    value = foundation.get(name)
    if isinstance(value, dict):
        if "available" in value:
            return bool(value.get("available"))
        if "running" in value:
            return bool(value.get("running"))
        if "status" in value:
            return str(value.get("status")).lower() in {"ok", "running", "healthy", "available"}
    return False


def _local_host_id(runtime_root: Path, configured_hosts: List[Dict[str, Any]]) -> str:
    for record in configured_hosts:
        if record.get("local") is True:
            value = record.get("id") or record.get("host_id") or record.get("name")
            if isinstance(value, str) and value:
                return value
    for path in (runtime_root / "host.json", runtime_root / "inventory" / "host.json"):
        try:
            payload = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            payload = {}
        value = payload.get("host_id") or payload.get("id")
        if isinstance(value, str) and value:
            return value
    return "local"


def _host_public_summary(host: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": host.get("id"),
        "name": host.get("name"),
        "provider": host.get("provider"),
        "region": host.get("region"),
        "hostname": host.get("hostname"),
        "architecture": host.get("architecture"),
        "observed": host.get("observed"),
        "roles": host.get("roles") if isinstance(host.get("roles"), list) else [],
        "labels": host.get("labels") if isinstance(host.get("labels"), list) else [],
    }


def _required_memory_mb(manifest: Manifest) -> int:
    explicit = _parse_size_to_mb(manifest.host_requirements.min_memory)
    if explicit is not None:
        return explicit
    service_count = max(1, len(manifest.services))
    service_memory = _parse_size_to_mb(manifest.resources.memory) or 256
    total = service_count * service_memory
    if manifest.kind in {"static", "redirect", "tunnel"} and not manifest.services:
        total = max(total, _MIN_STATIC_MEMORY_MB)
    else:
        total = max(total, _MIN_SERVICE_MEMORY_MB)
    if manifest.addons.postgres or manifest.data.postgres:
        total += _MIN_POSTGRES_MEMORY_MB
    if manifest.addons.redis or manifest.data.redis:
        total += _MIN_REDIS_MEMORY_MB
    return total


def _required_disk_free_mb(manifest: Manifest) -> int:
    explicit = _parse_size_to_mb(manifest.host_requirements.min_disk_free)
    if explicit is not None:
        return explicit
    total = _MIN_STATIC_DISK_MB if manifest.kind in {"static", "redirect", "tunnel"} else _MIN_SERVICE_DISK_MB
    if manifest.addons.postgres or manifest.data.postgres:
        total += _MIN_DATABASE_DISK_MB
    if manifest.addons.redis or manifest.data.redis:
        total += _MIN_REDIS_DISK_MB
    if manifest.data.volumes:
        total += _MIN_SERVICE_DISK_MB
    return total


def _parse_size_to_mb(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    multiplier = 1
    number = raw
    units = {
        "b": 1 / (1024 * 1024),
        "kb": 1 / 1024,
        "k": 1 / 1024,
        "mb": 1,
        "m": 1,
        "mib": 1,
        "gb": 1024,
        "g": 1024,
        "gib": 1024,
        "tb": 1024 * 1024,
        "t": 1024 * 1024,
        "tib": 1024 * 1024,
    }
    for suffix, value_multiplier in sorted(units.items(), key=lambda item: len(item[0]), reverse=True):
        if raw.endswith(suffix):
            multiplier = value_multiplier
            number = raw[: -len(suffix)]
            break
    try:
        return max(1, int(float(number.strip()) * multiplier))
    except ValueError:
        return None


def _capacity_mb(capacity: Dict[str, Any], prefix: str) -> Optional[int]:
    for key in (f"{prefix}_mb", f"{prefix}_mib"):
        value = _number_or_none(capacity.get(key))
        if value is not None:
            return int(value)
    for key in (f"{prefix}_bytes", f"{prefix}_b"):
        value = _int_or_none(capacity.get(key))
        if value is not None:
            return _bytes_to_mb(value)
    string_value = capacity.get(prefix) or capacity.get(f"{prefix}_size")
    return _parse_size_to_mb(string_value) if isinstance(string_value, str) else None


def _memory_total_bytes() -> Optional[int]:
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        try:
            for line in meminfo.read_text().splitlines():
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
        except (OSError, ValueError):
            return None
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(["sysctl", "-n", "hw.memsize"], text=True, capture_output=True, timeout=2)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode == 0:
            try:
                return int(result.stdout.strip())
            except ValueError:
                return None
    return None


def _normalized_arch(value: str) -> str:
    normalized = value.lower()
    if normalized in {"x86_64", "amd64"}:
        return "amd64"
    if normalized in {"aarch64", "arm64"}:
        return "arm64"
    return normalized or "unknown"


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float))]


def _bool_or_none(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "available", "ok", "enabled"}:
            return True
        if lowered in {"false", "no", "0", "unavailable", "disabled"}:
            return False
    return None


def _number_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _int_or_none(value: Any) -> Optional[int]:
    number = _number_or_none(value)
    return int(number) if number is not None else None


def _bytes_to_mb(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    return int(value / (1024 * 1024))


def _mb_to_bytes(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    return int(value * 1024 * 1024)


def _git_sha(root: Path) -> Optional[str]:
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _docker_lines(command: List[str]) -> List[str]:
    result: subprocess.CompletedProcess[str]
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]

