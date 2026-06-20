from __future__ import annotations

import hashlib
import json
import platform
import socket
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .manifest import (
    DataServiceConfig,
    DataVolumeConfig,
    HooksConfig,
    Manifest,
    ManifestError,
    load_manifest,
)
from .operator_reports import host_inventory
from .runtime import active_release, active_release_id, image_references, latest_release_id
from .templates import render_env_example
from .verify import verification_checks


EXPORT_BUNDLE_FORMAT_VERSION = 1
EXPORT_BUNDLE_EXTENSION = ".tar.zst"
EXPORT_BUNDLE_FILENAME_TEMPLATE = "{app}.{environment}.export.{timestamp}.tar.zst"
EXPORT_BUNDLE_REQUIRED_FILES = (
    "manifest.json",
    "checksums.sha256",
    "source-host.json",
    "receipts/export-plan.json",
    "receipts/export-create.json",
)
EXPORT_BUNDLE_RUNTIME_FILES = (
    "runtime/manifest.lock.json",
    "runtime/release.json",
    "runtime/active_release.json",
    "runtime/compose.yml",
    "runtime/caddy/",
)
EXPORT_BUNDLE_DATA_PATHS = (
    "data/postgres/",
    "data/volumes/",
)
EXPORT_RECEIPT_TYPES = {
    "export_plan": "app.export.plan",
    "export_create": "app.export.create",
    "import_plan": "app.import.plan",
    "import_apply": "app.import.apply",
}


@dataclass
class ManifestResolution:
    manifest: Optional[Manifest]
    manifest_path: Optional[Path]
    warnings: List[str]
    blockers: List[str]
    candidates: List[Dict[str, object]]


def data_contract_dict(manifest: Manifest) -> Dict[str, object]:
    payload = manifest.to_lock_dict().get("data", {})
    return payload if isinstance(payload, dict) else {}


def pack_validation_report(
    manifest: Manifest,
    manifest_path: Path,
    manifest_dir: Optional[Path] = None,
) -> Dict[str, object]:
    errors: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    checks: List[Dict[str, object]] = []

    data = manifest.data
    critical_pack = manifest.pack.portability == "critical"

    if manifest.environment == "production" and not verification_checks(manifest):
        _issue(errors, "production_missing_verification", "verify", "Production packs require at least one health verification check.")

    for service_name, service in manifest.services.items():
        if not service.image and not manifest.image:
            _issue(
                errors,
                "service_missing_image",
                f"services.{service_name}.image",
                f"Service `{service_name}` has no image after manifest defaults are resolved.",
            )

    _validate_data_service(
        manifest,
        "postgres",
        data.postgres,
        critical_pack,
        errors,
        warnings,
    )
    _validate_data_service(
        manifest,
        "redis",
        data.redis,
        critical_pack,
        errors,
        warnings,
    )

    for index, volume in enumerate(data.volumes):
        critical_volume = volume.class_name == "critical" or critical_pack
        if critical_volume and not _has_data_action(volume.export):
            _issue(
                errors,
                "critical_volume_missing_export",
                f"data.volumes[{index}].export",
                f"Critical volume `{volume.name}` must declare export behavior.",
            )
        if critical_volume and not _has_data_action(volume.import_config):
            _issue(
                errors,
                "critical_volume_missing_import",
                f"data.volumes[{index}].import",
                f"Critical volume `{volume.name}` must declare import behavior.",
            )

    if data.postgres and data.postgres.mode == "app-postgres" and not _has_postgres_volume(data.volumes):
        _issue(
            errors,
            "app_postgres_missing_volume",
            "data.volumes",
            "`data.postgres.mode: app-postgres` requires an explicit Postgres volume declaration.",
        )

    if manifest.environment == "production":
        for service_name, service in manifest.services.items():
            for index, mount in enumerate(service.mounts):
                if mount.bind and not mount.read_only and not _has_backup_behavior(manifest):
                    _issue(
                        errors,
                        "production_writable_bind_without_backup",
                        f"services.{service_name}.mounts[{index}]",
                        "Production writable host bind mounts require `data.backups.required` or an explicit data volume contract.",
                    )

    for field_name, hook_path in _hook_paths(manifest.hooks):
        if not _hook_path_is_allowlisted(hook_path):
            _issue(
                errors,
                "hook_path_outside_allowlist",
                f"hooks.{field_name}",
                f"Hook `{hook_path}` must be a relative path under `ophelia/hooks/`.",
            )

    if manifest_dir is not None:
        _append_route_conflict_issues(manifest, manifest_path, manifest_dir, errors)

    if critical_pack and data.postgres and data.postgres.mode == "shared-postgres-database":
        _issue(
            warnings,
            "critical_app_shared_postgres",
            "data.postgres.mode",
            "Critical apps using shared Postgres need rehearsed dump, restore, and verify receipts before cutover.",
        )
    if data.redis and data.redis.mode == "redis-logical-db" and (
        data.redis.class_name == "critical" or data.redis.durable is True
    ):
        _issue(
            warnings,
            "durable_redis_shared_logical_db",
            "data.redis.mode",
            "Durable Redis state uses a shared logical DB; prefer app-owned Redis for critical queues or durable state.",
        )
    if not data.backups or not data.backups.restore_drill_required:
        _issue(
            warnings,
            "restore_drill_not_required",
            "data.backups.restore_drill_required",
            "No restore drill requirement is recorded for this pack.",
        )
    if not data.backups or not data.backups.offsite_required:
        _issue(
            warnings,
            "offsite_backup_not_required",
            "data.backups.offsite_required",
            "No offsite backup requirement is recorded for this pack.",
        )
    if manifest.environment == "production":
        for service, image in sorted(image_references(manifest).items()):
            if "@sha256:" not in image:
                _issue(
                    warnings,
                    "production_image_without_digest",
                    f"image:{service}",
                    f"Production image `{image}` is not pinned by digest.",
                )

    checks.append(
        {
            "name": "base_manifest_validation",
            "ok": True,
            "message": "Manifest parsed with Ophelia base validation.",
        }
    )
    checks.append(
        {
            "name": "data_contracts_present",
            "ok": any([data.postgres, data.redis, data.volumes, data.object_storage, data.static_assets]),
            "message": "Data contracts are explicit or inferred from addons.",
        }
    )

    return {
        "ok": not errors,
        "app": manifest.app,
        "environment": manifest.environment,
        "manifest_path": str(manifest_path),
        "pack": manifest.to_lock_dict().get("pack", {}),
        "host_requirements": manifest.to_lock_dict().get("host_requirements", {}),
        "data_contracts": data_contract_dict(manifest),
        "hooks": manifest.to_lock_dict().get("hooks", {}),
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "summary": f"Pack validation for {manifest.app}: {len(errors)} error(s), {len(warnings)} warning(s).",
    }


def pack_explain_report(manifest: Manifest, manifest_path: Path) -> Dict[str, object]:
    checks = verification_checks(manifest)
    data_contracts = data_contract_dict(manifest)
    validation = pack_validation_report(manifest, manifest_path)
    return {
        "app": manifest.app,
        "environment": manifest.environment,
        "manifest_path": str(manifest_path),
        "portability": manifest.pack.portability or "unspecified",
        "owner": manifest.pack.owner,
        "description": manifest.pack.description,
        "deploy_binding_file": manifest.pack.deploy_binding_file,
        "host_requirements": manifest.to_lock_dict().get("host_requirements", {}),
        "data_contracts": data_contracts,
        "inferred_data_contracts": _inferred_data_contracts(manifest),
        "hooks": manifest.to_lock_dict().get("hooks", {}),
        "routes": [
            {
                "domain": route.domain,
                "path": route.path,
                "path_prefix": route.path_prefix,
                "service": route.service,
                "upstream": route.upstream,
            }
            for route in manifest.routes
        ],
        "verification_checks": [
            {"name": check.name or check.url, "url": check.url, "expect_status": check.expect_status}
            for check in checks
        ],
        "movement_readiness": {
            "export_plan_command": f"./cli/ship app export plan {manifest.app}"
            + (f" --environment {manifest.environment}" if manifest.environment else ""),
            "import_plan_command": "./cli/ship app import plan <export-bundle-or-metadata>",
            "pack_validation_ok": validation["ok"],
            "errors": validation["errors"],
            "warnings": validation["warnings"],
        },
        "summary": (
            f"{manifest.app} portability={manifest.pack.portability or 'unspecified'} "
            f"with {len(data_contracts)} data section(s), {len(checks)} verification check(s), "
            f"and {len(_hook_paths(manifest.hooks))} hook(s)."
        ),
    }


def resolve_app_manifest(
    app: str,
    environment: Optional[str],
    manifest_path: Optional[Path] = None,
    search_dirs: Optional[Iterable[Path]] = None,
) -> ManifestResolution:
    warnings: List[str] = []
    blockers: List[str] = []
    candidates: List[Tuple[Path, Manifest]] = []

    if manifest_path is not None:
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError as exc:
            return ManifestResolution(None, None, [], [str(exc)], [])
        if manifest.app != app:
            blockers.append(f"Manifest app `{manifest.app}` does not match requested app `{app}`.")
        if environment and manifest.environment and manifest.environment != environment:
            blockers.append(
                f"Manifest environment `{manifest.environment}` does not match requested environment `{environment}`."
            )
        if environment and manifest.environment is None:
            warnings.append(
                f"Manifest has no environment; planning requested environment `{environment}` for backward compatibility."
            )
        return ManifestResolution(
            manifest if not blockers else None,
            manifest_path if not blockers else None,
            warnings,
            blockers,
            [_candidate_dict(manifest_path, manifest)] if not blockers else [],
        )

    for directory in _default_manifest_search_dirs(search_dirs):
        for path in _manifest_paths(directory):
            try:
                manifest = load_manifest(path)
            except ManifestError as exc:
                warnings.append(f"Skipped invalid manifest {path}: {exc}")
                continue
            if manifest.app == app:
                candidates.append((path, manifest))

    candidate_dicts = [_candidate_dict(path, manifest) for path, manifest in candidates]
    if not candidates:
        blockers.append(f"No manifest found for app `{app}`.")
        return ManifestResolution(None, None, warnings, blockers, candidate_dicts)

    exact = [
        (path, manifest)
        for path, manifest in candidates
        if environment is None or manifest.environment == environment
    ]
    if len(exact) == 1:
        path, manifest = exact[0]
        return ManifestResolution(manifest, path, warnings, blockers, candidate_dicts)
    if len(exact) > 1:
        blockers.append(
            f"Multiple manifests matched app `{app}`"
            + (f" and environment `{environment}`" if environment else "")
            + "; pass --manifest to disambiguate."
        )
        return ManifestResolution(None, None, warnings, blockers, candidate_dicts)

    unknown = [(path, manifest) for path, manifest in candidates if manifest.environment is None]
    if environment and len(unknown) == 1:
        path, manifest = unknown[0]
        warnings.append(
            f"Manifest {path} has no environment; planning requested environment `{environment}` for backward compatibility."
        )
        return ManifestResolution(manifest, path, warnings, blockers, candidate_dicts)

    blockers.append(
        f"No unambiguous manifest found for app `{app}`"
        + (f" and environment `{environment}`" if environment else "")
        + "; pass --manifest to disambiguate."
    )
    return ManifestResolution(None, None, warnings, blockers, candidate_dicts)


def export_plan(
    app: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest_path: Optional[Path] = None,
    ophelia_root: Path = REPO_ROOT,
) -> Dict[str, object]:
    resolution = resolve_app_manifest(app, environment, manifest_path=manifest_path)
    blockers = list(resolution.blockers)
    warnings = list(resolution.warnings)
    manifest = resolution.manifest
    resolved_environment = environment

    if manifest is None or resolution.manifest_path is None:
        plan = _base_export_plan(app, environment, runtime_root, blockers, warnings)
        plan["manifest_candidates"] = resolution.candidates
        plan["confirmation_token"] = export_plan_token(plan)
        return plan

    resolved_environment = environment or manifest.environment or "unknown"
    app_root = runtime_root / "apps" / manifest.app
    release = active_release(runtime_root, manifest.app)
    release_id = active_release_id(runtime_root, manifest.app) or latest_release_id(runtime_root, manifest.app)
    validation = pack_validation_report(manifest, resolution.manifest_path)
    critical_validation_errors = [
        item for item in validation["errors"] if _is_movement_blocking_validation(str(item.get("code")))
    ]
    blockers.extend(str(item["message"]) for item in critical_validation_errors)

    if not app_root.exists():
        blockers.append(f"App runtime path does not exist: {app_root}")
    if release_id is None:
        blockers.append(f"No active or latest release metadata found for {manifest.app}.")
    if manifest.pack.portability == "critical" and _inferred_data_contracts(manifest):
        blockers.append("Critical portability packs need explicit data export/import contracts, not inferred addon contracts.")

    data_contracts = data_contract_dict(manifest)
    if not any(value for value in data_contracts.values()):
        warnings.append("No data dependencies are declared or inferred.")
    if validation["warnings"]:
        warnings.extend(str(item["message"]) for item in validation["warnings"])

    bundle_name = EXPORT_BUNDLE_FILENAME_TEMPLATE.format(
        app=manifest.app,
        environment=resolved_environment,
        timestamp="<timestamp>",
    )
    artifact_root = app_root / "export-bundles"
    commands = _export_commands(manifest)
    runtime_files = _runtime_export_files(app_root)
    plan: Dict[str, object] = {
        "action": "app.export.plan",
        "receipt_type": EXPORT_RECEIPT_TYPES["export_plan"],
        "bundle_format_version": EXPORT_BUNDLE_FORMAT_VERSION,
        "app": manifest.app,
        "environment": resolved_environment,
        "manifest_path": str(resolution.manifest_path),
        "source_host": _host_summary(runtime_root, ophelia_root),
        "app_release_id": release_id,
        "active_release_id": release.get("release_id") if release else None,
        "data_dependencies": data_contracts,
        "host_requirements": manifest.to_lock_dict().get("host_requirements", {}),
        "routes": [
            {
                "domain": route.domain,
                "path": route.path,
                "path_prefix": route.path_prefix,
                "service": route.service,
                "upstream": route.upstream,
            }
            for route in manifest.routes
        ],
        "domains": sorted({route.domain for route in manifest.routes}),
        "images": image_references(manifest),
        "env_shape": _env_shape(manifest, app_root),
        "runtime_files": runtime_files,
        "estimated_export_size_bytes": _path_size(app_root) if app_root.exists() else None,
        "commands": commands,
        "artifact_paths": {
            "bundle": str(artifact_root / bundle_name),
            "metadata": str(artifact_root / "manifest.json"),
            "checksums": str(artifact_root / "checksums.sha256"),
            "export_plan_receipt": str(artifact_root / "receipts" / "export-plan.json"),
            "export_create_receipt": str(artifact_root / "receipts" / "export-create.json"),
        },
        "bundle_shape": {
            "required_files": list(EXPORT_BUNDLE_REQUIRED_FILES),
            "runtime_files": list(EXPORT_BUNDLE_RUNTIME_FILES),
            "data_paths": list(EXPORT_BUNDLE_DATA_PATHS),
        },
        "restore_drill_requirement_status": _restore_drill_requirement_status(manifest),
        "warnings": warnings,
        "blockers": blockers,
        "can_create": not blockers,
        "confirmation_required": True,
        "create_supported": False,
        "read_only": True,
        "secrets_redacted": True,
        "summary": f"Export plan for {manifest.app} {resolved_environment}: {len(blockers)} blocker(s), {len(warnings)} warning(s).",
    }
    plan["confirmation_token"] = export_plan_token(plan)
    return plan


def import_plan(
    source: Path,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    mode: str = "rehearsal",
    ophelia_root: Path = REPO_ROOT,
) -> Dict[str, object]:
    metadata, source_warnings, source_blockers = _load_export_metadata(source)
    blockers = list(source_blockers)
    warnings = list(source_warnings)
    app = _metadata_app(metadata) or _app_from_bundle_name(source)
    environment = _metadata_environment(metadata) or _environment_from_bundle_name(source) or "unknown"
    data_dependencies = _metadata_data_dependencies(metadata)
    host_requirements = _metadata_host_requirements(metadata)
    routes = _metadata_routes(metadata)
    target_host = _host_summary(runtime_root, ophelia_root)
    host_match = _host_capability_match(host_requirements, target_host)
    blockers.extend(str(item["message"]) for item in host_match["blockers"])
    warnings.extend(str(item["message"]) for item in host_match["warnings"])
    warnings.append("Import apply is intentionally out of scope; this command only plans target changes.")

    plan: Dict[str, object] = {
        "action": "app.import.plan",
        "receipt_type": EXPORT_RECEIPT_TYPES["import_plan"],
        "bundle_format_version": EXPORT_BUNDLE_FORMAT_VERSION,
        "source": str(source),
        "source_kind": _source_kind(source),
        "app": app,
        "environment": environment,
        "mode": mode,
        "target_host": target_host,
        "host_capability_match": host_match,
        "required_docker_networks": {
            "current_compatibility": ["ophelia-edge", "ophelia-internal"],
            "target_private_network": f"{app}-{environment}-internal" if app else None,
        },
        "required_volumes": _required_import_volumes(app, data_dependencies),
        "caddy_route_changes": {
            "domains": _metadata_domains(metadata),
            "routes": routes,
            "operation": "stage generated snippets before validation",
        },
        "data_restore_commands": _import_restore_commands(data_dependencies),
        "env_secret_refs_needed": _metadata_env_shape(metadata),
        "post_import_checks": _metadata_verification_checks(metadata),
        "warnings": warnings,
        "blockers": blockers,
        "can_apply": False,
        "apply_supported": False,
        "confirmation_required_for_future_apply": True,
        "read_only": True,
        "secrets_redacted": True,
        "summary": f"Import plan for {app or 'unknown app'} {environment}: {len(blockers)} blocker(s), {len(warnings)} warning(s).",
    }
    plan["confirmation_token"] = import_plan_token(plan)
    return plan


def export_plan_token(plan: Dict[str, object]) -> str:
    return _token(
        "app.export.create",
        {
            "app": plan.get("app"),
            "environment": plan.get("environment"),
            "app_release_id": plan.get("app_release_id"),
            "data_dependencies": plan.get("data_dependencies"),
            "artifact_paths": plan.get("artifact_paths"),
        },
    )


def import_plan_token(plan: Dict[str, object]) -> str:
    return _token(
        "app.import.apply",
        {
            "app": plan.get("app"),
            "environment": plan.get("environment"),
            "source": plan.get("source"),
            "required_volumes": plan.get("required_volumes"),
            "caddy_route_changes": plan.get("caddy_route_changes"),
            "data_restore_commands": plan.get("data_restore_commands"),
        },
    )


def _validate_data_service(
    manifest: Manifest,
    name: str,
    service: Optional[DataServiceConfig],
    critical_pack: bool,
    errors: List[Dict[str, str]],
    warnings: List[Dict[str, str]],
) -> None:
    if service is None:
        return

    field_name = f"data.{name}"
    critical = critical_pack or service.class_name == "critical" or service.durable is True
    if service.inferred_from_addon:
        target = errors if critical else warnings
        _issue(
            target,
            f"{name}_data_contract_inferred",
            field_name,
            f"`{field_name}` is inferred from addons; explicit export/import/verify behavior is required before critical movement.",
        )
        return
    if critical and not service.export:
        _issue(
            errors,
            f"critical_{name}_missing_export",
            f"{field_name}.export",
            f"Critical `{name}` data must declare export behavior.",
        )
    if critical and not service.import_config:
        _issue(
            errors,
            f"critical_{name}_missing_import",
            f"{field_name}.import",
            f"Critical `{name}` data must declare import behavior.",
        )
    if critical and not service.verify:
        _issue(
            warnings,
            f"critical_{name}_missing_verify",
            f"{field_name}.verify",
            f"Critical `{name}` data should declare verification behavior.",
        )


def _append_route_conflict_issues(
    manifest: Manifest,
    manifest_path: Path,
    manifest_dir: Path,
    errors: List[Dict[str, str]],
) -> None:
    from .conflicts import scan_conflicts

    report = scan_conflicts(manifest_dir)
    normalized_path = str(manifest_path)
    for conflict in report["conflicts"]:
        if conflict.get("type") != "duplicate_domain":
            continue
        owners = [str(owner) for owner in conflict.get("owners", [])]
        if any(owner.startswith(normalized_path) and f":{manifest.app}" in owner for owner in owners):
            _issue(
                errors,
                "route_domain_conflict",
                "routes",
                f"Domain `{conflict.get('domain')}` is already owned by another manifest in {manifest_dir}.",
            )


def _issue(target: List[Dict[str, str]], code: str, field: str, message: str) -> None:
    target.append({"code": code, "field": field, "message": message})


def _has_data_action(value: Any) -> bool:
    return bool(value)


def _has_postgres_volume(volumes: List[DataVolumeConfig]) -> bool:
    for volume in volumes:
        haystack = " ".join(
            item for item in [volume.name, volume.mount, volume.source] if isinstance(item, str)
        ).lower()
        if "postgres" in haystack or "pgdata" in haystack:
            return True
    return False


def _has_backup_behavior(manifest: Manifest) -> bool:
    data = manifest.data
    return bool((data.backups and data.backups.required) or data.volumes)


def _hook_paths(hooks: HooksConfig) -> List[Tuple[str, str]]:
    values = []
    for field_name in ("pre_export", "freeze", "unfreeze", "post_import", "post_cutover"):
        value = getattr(hooks, field_name)
        if value:
            values.append((field_name, value))
    return values


def _hook_path_is_allowlisted(value: str) -> bool:
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts and path.parts[:2] == ("ophelia", "hooks")


def _is_movement_blocking_validation(code: str) -> bool:
    return code in {
        "postgres_data_contract_inferred",
        "redis_data_contract_inferred",
        "critical_postgres_missing_export",
        "critical_postgres_missing_import",
        "critical_redis_missing_export",
        "critical_redis_missing_import",
        "critical_volume_missing_export",
        "critical_volume_missing_import",
        "app_postgres_missing_volume",
        "production_writable_bind_without_backup",
        "hook_path_outside_allowlist",
    }


def _inferred_data_contracts(manifest: Manifest) -> List[str]:
    inferred = []
    if manifest.data.postgres and manifest.data.postgres.inferred_from_addon:
        inferred.append("postgres")
    if manifest.data.redis and manifest.data.redis.inferred_from_addon:
        inferred.append("redis")
    return inferred


def _default_manifest_search_dirs(search_dirs: Optional[Iterable[Path]]) -> List[Path]:
    if search_dirs is not None:
        return list(search_dirs)
    candidates = [Path.cwd(), REPO_ROOT / "manifests", REPO_ROOT / "examples"]
    unique: List[Path] = []
    seen = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def _manifest_paths(directory: Path) -> List[Path]:
    if not directory.exists() or not directory.is_dir():
        return []
    paths = list(directory.glob("*.ophelia.yml"))
    dot_manifest = directory / ".ophelia.yml"
    if dot_manifest.exists():
        paths.append(dot_manifest)
    return sorted(set(paths))


def _candidate_dict(path: Path, manifest: Manifest) -> Dict[str, object]:
    return {
        "path": str(path),
        "app": manifest.app,
        "environment": manifest.environment,
        "kind": manifest.kind,
    }


def _base_export_plan(
    app: str,
    environment: Optional[str],
    runtime_root: Path,
    blockers: List[str],
    warnings: List[str],
) -> Dict[str, object]:
    return {
        "action": "app.export.plan",
        "receipt_type": EXPORT_RECEIPT_TYPES["export_plan"],
        "bundle_format_version": EXPORT_BUNDLE_FORMAT_VERSION,
        "app": app,
        "environment": environment or "unknown",
        "source_host": _host_summary(runtime_root, REPO_ROOT),
        "warnings": warnings,
        "blockers": blockers,
        "can_create": False,
        "confirmation_required": True,
        "create_supported": False,
        "read_only": True,
        "secrets_redacted": True,
        "summary": f"Export plan for {app}: {len(blockers)} blocker(s), {len(warnings)} warning(s).",
    }


def _host_summary(runtime_root: Path, ophelia_root: Path) -> Dict[str, object]:
    inventory = host_inventory(runtime_root, ophelia_root)
    return {
        "host_id": _host_id(runtime_root),
        "hostname": socket.gethostname(),
        "runtime_root": inventory["runtime_root"],
        "ophelia_commit": inventory.get("ophelia_commit"),
        "docker": inventory.get("docker", {}),
        "disk_usage": inventory.get("disk_usage", {}),
        "networks": inventory.get("networks", []),
        "caddy_status": inventory.get("caddy_status", {}),
        "warnings": inventory.get("warnings", []),
    }


def _host_id(runtime_root: Path) -> str:
    for path in (runtime_root / "host.json", runtime_root / "inventory" / "host.json"):
        payload = _read_json(path)
        host_id = payload.get("host_id") or payload.get("id")
        if isinstance(host_id, str) and host_id:
            return host_id
    return f"local:{socket.gethostname()}"


def _env_shape(manifest: Manifest, app_root: Path) -> List[Dict[str, object]]:
    keys = {}
    for raw_line in render_env_example(manifest).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        keys[key] = {
            "key": key,
            "source": "env.example",
            "placeholder": _is_placeholder(value),
            "secret_value_redacted": True,
        }

    env_path = app_root / "env"
    if env_path.exists():
        for raw_line in env_path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _value = line.split("=", 1)
            keys.setdefault(
                key,
                {
                    "key": key,
                    "source": "runtime.env",
                    "placeholder": False,
                    "secret_value_redacted": True,
                },
            )
    return [keys[key] for key in sorted(keys)]


def _runtime_export_files(app_root: Path) -> List[Dict[str, object]]:
    paths = [
        Path("manifest.lock.json"),
        Path("release.json"),
        Path("active_release.json"),
        Path("compose.yml"),
        Path("caddy"),
    ]
    result = []
    for relative_path in paths:
        path = app_root / relative_path
        result.append(
            {
                "path": str(relative_path),
                "present": path.exists(),
                "kind": "directory" if path.is_dir() else "file",
                "size_bytes": _path_size(path) if path.exists() else 0,
            }
        )
    return result


def _export_commands(manifest: Manifest) -> List[Dict[str, object]]:
    commands: List[Dict[str, object]] = []
    data = manifest.data
    if data.postgres:
        export = data.postgres.export
        command = export.get("command") or "pg_dump"
        database = data.postgres.database or "<database-from-env-or-addon>"
        commands.append(
            {
                "name": "postgres.export",
                "data": "postgres",
                "mode": data.postgres.mode,
                "command": f"{command} --format={export.get('format', 'custom')} --file data/postgres/{database}.dump <redacted-postgres-url>",
                "executes_in_plan": False,
            }
        )
    if data.redis:
        export = data.redis.export
        commands.append(
            {
                "name": "redis.export",
                "data": "redis",
                "mode": data.redis.mode,
                "command": export.get("command") or "redis export is not enabled by default for logical DB cache state",
                "executes_in_plan": False,
            }
        )
    for volume in data.volumes:
        commands.append(
            {
                "name": f"volume.{volume.name}.export",
                "data": "volume",
                "mode": volume.export or "tar-zstd",
                "command": f"tar archive {volume.mount or volume.source or volume.name} -> data/volumes/{volume.name}.tar.zst",
                "executes_in_plan": False,
            }
        )
    return commands


def _restore_drill_requirement_status(manifest: Manifest) -> Dict[str, object]:
    backups = manifest.data.backups
    return {
        "required": bool(backups and backups.restore_drill_required),
        "latest_success": None,
        "status": "not_recorded",
    }


def _path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def _load_export_metadata(source: Path) -> Tuple[Dict[str, object], List[str], List[str]]:
    warnings: List[str] = []
    blockers: List[str] = []
    if not source.exists():
        return {}, warnings, [f"Export source not found: {source}"]

    if source.is_dir():
        for relative in ("manifest.json", "receipts/export-plan.json", "export-plan.json"):
            payload = _read_json(source / relative)
            if payload:
                return payload, warnings, blockers
        blockers.append(f"No export metadata found in directory: {source}")
        return {}, warnings, blockers

    if source.suffix == ".json":
        payload = _read_json(source)
        if not payload:
            blockers.append(f"Export metadata JSON is empty or invalid: {source}")
        return payload, warnings, blockers

    if source.name.endswith(".tar.zst"):
        payload = _read_tar_zst_metadata(source)
        if payload:
            return payload, warnings, blockers
        blockers.append(
            "Could not read manifest.json or receipts/export-plan.json from compressed export bundle; pass exported metadata JSON if zstd tar support is unavailable."
        )
        return {}, warnings, blockers

    if source.suffix == ".tar":
        payload = _read_tar_metadata(source)
        if payload:
            return payload, warnings, blockers
        blockers.append(f"Could not read export metadata from tar bundle: {source}")
        return {}, warnings, blockers

    blockers.append(f"Unsupported import source; expected export bundle directory, JSON metadata, .tar, or .tar.zst: {source}")
    return {}, warnings, blockers


def _read_tar_metadata(source: Path) -> Dict[str, object]:
    try:
        with tarfile.open(source) as archive:
            for member_name in ("manifest.json", "receipts/export-plan.json", "export-plan.json"):
                try:
                    member = archive.extractfile(member_name)
                except KeyError:
                    continue
                if member is None:
                    continue
                payload = json.loads(member.read().decode("utf-8"))
                return payload if isinstance(payload, dict) else {}
    except (tarfile.TarError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return {}


def _read_tar_zst_metadata(source: Path) -> Dict[str, object]:
    for member_name in ("manifest.json", "receipts/export-plan.json", "export-plan.json"):
        try:
            result = subprocess.run(
                ["tar", "--use-compress-program=zstd", "-xOf", str(source), member_name],
                text=True,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        if result.returncode != 0 or not result.stdout.strip():
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _metadata_app(metadata: Dict[str, object]) -> Optional[str]:
    value = metadata.get("app")
    if isinstance(value, str):
        return value
    manifest = metadata.get("manifest")
    if isinstance(manifest, dict) and isinstance(manifest.get("app"), str):
        return str(manifest["app"])
    return None


def _metadata_environment(metadata: Dict[str, object]) -> Optional[str]:
    value = metadata.get("environment")
    if isinstance(value, str):
        return value
    manifest = metadata.get("manifest")
    if isinstance(manifest, dict) and isinstance(manifest.get("environment"), str):
        return str(manifest["environment"])
    return None


def _metadata_data_dependencies(metadata: Dict[str, object]) -> Dict[str, object]:
    value = metadata.get("data_dependencies") or metadata.get("data_contracts")
    if isinstance(value, dict):
        return value
    manifest = metadata.get("manifest")
    if isinstance(manifest, dict) and isinstance(manifest.get("data"), dict):
        return manifest["data"]  # type: ignore[return-value]
    return {}


def _metadata_host_requirements(metadata: Dict[str, object]) -> Dict[str, object]:
    value = metadata.get("host_requirements")
    return value if isinstance(value, dict) else {}


def _metadata_routes(metadata: Dict[str, object]) -> List[Dict[str, object]]:
    value = metadata.get("routes")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    manifest = metadata.get("manifest")
    if isinstance(manifest, dict) and isinstance(manifest.get("routes"), list):
        return [item for item in manifest["routes"] if isinstance(item, dict)]  # type: ignore[index]
    return []


def _metadata_domains(metadata: Dict[str, object]) -> List[str]:
    value = metadata.get("domains")
    if isinstance(value, list):
        return sorted(str(item) for item in value if isinstance(item, str))
    return sorted({str(route["domain"]) for route in _metadata_routes(metadata) if isinstance(route.get("domain"), str)})


def _metadata_env_shape(metadata: Dict[str, object]) -> List[Dict[str, object]]:
    value = metadata.get("env_shape") or metadata.get("env_secret_refs_needed")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _metadata_verification_checks(metadata: Dict[str, object]) -> List[Dict[str, object]]:
    value = metadata.get("verification_checks") or metadata.get("post_import_checks")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _required_import_volumes(app: Optional[str], data_dependencies: Dict[str, object]) -> List[Dict[str, object]]:
    volumes: List[Dict[str, object]] = []
    postgres = data_dependencies.get("postgres")
    if isinstance(postgres, dict) and postgres.get("mode") == "app-postgres":
        volumes.append({"name": f"{app or 'app'}-postgres-data", "source": "data.postgres", "required": True})
    raw_volumes = data_dependencies.get("volumes")
    if isinstance(raw_volumes, list):
        for item in raw_volumes:
            if isinstance(item, dict):
                volumes.append(
                    {
                        "name": item.get("name"),
                        "mount": item.get("mount"),
                        "source": item.get("source"),
                        "required": item.get("class") == "critical",
                    }
                )
    return volumes


def _import_restore_commands(data_dependencies: Dict[str, object]) -> List[Dict[str, object]]:
    commands: List[Dict[str, object]] = []
    postgres = data_dependencies.get("postgres")
    if isinstance(postgres, dict):
        import_config = postgres.get("import") if isinstance(postgres.get("import"), dict) else {}
        command = import_config.get("command") if isinstance(import_config, dict) else None
        commands.append(
            {
                "name": "postgres.import",
                "data": "postgres",
                "mode": postgres.get("mode"),
                "command": command or "pg_restore --clean --if-exists <redacted-target-postgres-url> data/postgres/<database>.dump",
                "executes_in_plan": False,
            }
        )
    raw_volumes = data_dependencies.get("volumes")
    if isinstance(raw_volumes, list):
        for item in raw_volumes:
            if isinstance(item, dict):
                name = item.get("name") or "volume"
                commands.append(
                    {
                        "name": f"volume.{name}.import",
                        "data": "volume",
                        "command": f"extract data/volumes/{name}.tar.zst into declared target volume or mount",
                        "executes_in_plan": False,
                    }
                )
    return commands


def _host_capability_match(
    requirements: Dict[str, object],
    target_host: Dict[str, object],
) -> Dict[str, object]:
    checks: List[Dict[str, object]] = []
    warnings: List[Dict[str, str]] = []
    blockers: List[Dict[str, str]] = []

    arch = requirements.get("arch")
    if isinstance(arch, str):
        current_arch = _normalized_arch(platform.machine())
        ok = current_arch == arch
        checks.append({"name": "arch", "required": arch, "actual": current_arch, "ok": ok})
        if not ok:
            _issue(blockers, "host_arch_mismatch", "host_requirements.arch", f"Target arch `{current_arch}` does not match required `{arch}`.")

    min_disk = requirements.get("min_disk_free")
    disk_usage = target_host.get("disk_usage") if isinstance(target_host.get("disk_usage"), dict) else {}
    free_bytes = disk_usage.get("free_bytes") if isinstance(disk_usage, dict) else None
    if isinstance(min_disk, str) and isinstance(free_bytes, int):
        required_bytes = _parse_size(min_disk)
        ok = required_bytes is None or free_bytes >= required_bytes
        checks.append({"name": "disk_free", "required": min_disk, "actual_bytes": free_bytes, "ok": ok})
        if required_bytes is None:
            _issue(warnings, "host_disk_requirement_unparsed", "host_requirements.min_disk_free", f"Could not parse disk requirement `{min_disk}`.")
        elif not ok:
            _issue(blockers, "host_disk_insufficient", "host_requirements.min_disk_free", f"Target free disk is below required `{min_disk}`.")

    min_memory = requirements.get("min_memory")
    if isinstance(min_memory, str):
        checks.append({"name": "memory", "required": min_memory, "actual_bytes": None, "ok": None})
        _issue(
            warnings,
            "host_memory_not_checked",
            "host_requirements.min_memory",
            "Target memory is not reported by current host inventory yet.",
        )

    docker_required = requirements.get("requires_docker")
    docker = target_host.get("docker") if isinstance(target_host.get("docker"), dict) else {}
    if docker_required is True:
        ok = bool(docker.get("available") and docker.get("compose_available"))
        checks.append({"name": "docker", "required": True, "ok": ok})
        if not ok:
            _issue(blockers, "host_docker_unavailable", "host_requirements.requires_docker", "Target host does not report Docker and Compose availability.")

    edge_required = requirements.get("requires_edge")
    caddy = target_host.get("caddy_status") if isinstance(target_host.get("caddy_status"), dict) else {}
    if edge_required is True:
        ok = bool(caddy.get("available"))
        checks.append({"name": "edge", "required": True, "ok": ok})
        if not ok:
            _issue(warnings, "host_edge_not_confirmed", "host_requirements.requires_edge", "Target Ophelia edge readiness is not confirmed by local inventory.")

    return {"ok": not blockers, "checks": checks, "warnings": warnings, "blockers": blockers}


def _parse_size(value: str) -> Optional[int]:
    normalized = value.strip().lower()
    units = {
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "t": 1024**4,
        "tb": 1024**4,
    }
    for suffix, multiplier in sorted(units.items(), key=lambda item: len(item[0]), reverse=True):
        if normalized.endswith(suffix):
            number = normalized[: -len(suffix)].strip()
            try:
                return int(float(number) * multiplier)
            except ValueError:
                return None
    try:
        return int(normalized)
    except ValueError:
        return None


def _normalized_arch(value: str) -> str:
    lowered = value.lower()
    if lowered in {"x86_64", "x64"}:
        return "amd64"
    if lowered in {"aarch64", "arm64"}:
        return "arm64"
    return lowered


def _read_json(path: Path) -> Dict[str, object]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _app_from_bundle_name(path: Path) -> Optional[str]:
    parts = path.name.split(".")
    if len(parts) >= 6 and parts[-4] == "export" and parts[-2:] == ["tar", "zst"]:
        return ".".join(parts[:-5]) or None
    return None


def _environment_from_bundle_name(path: Path) -> Optional[str]:
    parts = path.name.split(".")
    if len(parts) >= 6 and parts[-4] == "export" and parts[-2:] == ["tar", "zst"]:
        return parts[-5]
    return None


def _source_kind(path: Path) -> str:
    if path.is_dir():
        return "bundle-directory"
    if path.suffix == ".json":
        return "metadata-json"
    if path.name.endswith(".tar.zst"):
        return "compressed-export-bundle"
    if path.suffix == ".tar":
        return "tar-export-bundle"
    return "unknown"


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered in {"", "replace-me", "changeme", "todo"} or "replace-me" in lowered


def _token(action: str, payload: Dict[str, object]) -> str:
    encoded = json.dumps({"action": action, **payload}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]
