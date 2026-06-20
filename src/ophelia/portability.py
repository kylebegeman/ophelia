from __future__ import annotations

import hashlib
import json
import platform
import shutil
import socket
import subprocess
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .conflicts import scan_conflicts
from .manifest import (
    DataServiceConfig,
    DataVolumeConfig,
    HooksConfig,
    Manifest,
    ManifestError,
    load_manifest,
)
from .operation_schema import artifact, issue as schema_issue, plan_envelope, receipt_envelope, report_envelope
from .operator_reports import host_inventory
from .runtime import active_release, active_release_id, image_references, latest_release_id
from .templates import render_env_example
from .verify import verification_checks


EXPORT_BUNDLE_FORMAT_VERSION = 1
EXPORT_BUNDLE_EXTENSION = ".tar.zst"
EXPORT_BUNDLE_TAR_EXTENSION = ".tar"
EXPORT_BUNDLE_FILENAME_TEMPLATE = "{app}.{environment}.export.{timestamp}.tar.zst"
EXPORT_BUNDLE_TAR_FILENAME_TEMPLATE = "{app}.{environment}.export.{timestamp}.tar"
EXPORT_BUNDLE_DIRECTORY_TEMPLATE = "{app}.{environment}.export.{timestamp}"
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

    report = {
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
    report.update(
        report_envelope(
            "pack.validate",
            manifest.app,
            manifest.environment,
            str(report["summary"]),
            blockers=_as_issues(errors),
            warnings=_as_issues(warnings),
            checks=checks,
            status="ok" if not errors else "blocked",
        )
    )
    report["ok"] = not errors
    report["errors"] = errors
    return report


def pack_explain_report(manifest: Manifest, manifest_path: Path) -> Dict[str, object]:
    checks = verification_checks(manifest)
    data_contracts = data_contract_dict(manifest)
    validation = pack_validation_report(manifest, manifest_path)
    report = {
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
    report.update(
        report_envelope(
            "pack.explain",
            manifest.app,
            manifest.environment,
            str(report["summary"]),
            blockers=_as_issues(validation["errors"]),
            warnings=_as_issues(validation["warnings"]),
            checks=[
                {"name": "pack_validation", "ok": validation["ok"], "message": validation["summary"]},
                {"name": "verification_checks", "ok": bool(checks), "message": f"{len(checks)} check(s)"},
            ],
            status="ok" if validation["ok"] else "blocked",
        )
    )
    report["movement_readiness"] = {
        **report["movement_readiness"],
        "errors": validation["errors"],
        "warnings": validation["warnings"],
    }
    return report


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
        _merge_plan_envelope(
            plan,
            "app.export.plan",
            app,
            environment or "unknown",
            risk="high",
            exact_command=(
                f"ship app export create {app} --environment {environment or 'unknown'} "
                f"--runtime-root {runtime_root} --confirm {plan['confirmation_token']}"
            ),
        )
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
    bundle_directory_name = EXPORT_BUNDLE_DIRECTORY_TEMPLATE.format(
        app=manifest.app,
        environment=resolved_environment,
        timestamp="<timestamp>",
    )
    artifact_root = app_root / "export-bundles"
    bundle_directory = artifact_root / bundle_directory_name
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
        "verification_checks": [
            {"name": check.name or check.url, "url": check.url, "expect_status": check.expect_status}
            for check in verification_checks(manifest)
        ],
        "runtime_files": runtime_files,
        "estimated_export_size_bytes": _path_size(app_root) if app_root.exists() else None,
        "commands": commands,
        "artifact_paths": {
            "bundle": str(artifact_root / bundle_name),
            "bundle_directory": str(bundle_directory),
            "metadata": str(bundle_directory / "manifest.json"),
            "source_host": str(bundle_directory / "source-host.json"),
            "checksums": str(bundle_directory / "checksums.sha256"),
            "export_plan_receipt": str(bundle_directory / "receipts" / "export-plan.json"),
            "export_create_receipt": str(bundle_directory / "receipts" / "export-create.json"),
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
        "create_supported": True,
        "create_mode": "metadata-and-runtime-files",
        "read_only": True,
        "secrets_redacted": True,
        "summary": f"Export plan for {manifest.app} {resolved_environment}: {len(blockers)} blocker(s), {len(warnings)} warning(s).",
    }
    plan["confirmation_token"] = export_plan_token(plan)
    _merge_plan_envelope(
        plan,
        "app.export.plan",
        manifest.app,
        str(resolved_environment),
        risk="high",
        exact_command=(
            f"ship app export create {manifest.app} --environment {resolved_environment} "
            f"--manifest {resolution.manifest_path} --runtime-root {runtime_root} "
            f"--confirm {plan['confirmation_token']}"
        ),
    )
    return plan


def export_create(
    app: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest_path: Optional[Path] = None,
    confirm: Optional[str] = None,
    ophelia_root: Path = REPO_ROOT,
) -> Dict[str, object]:
    started_at = _utc_now()
    plan = export_plan(app, environment, runtime_root, manifest_path, ophelia_root)
    blockers = _as_issues(plan.get("blockers", []))
    warnings = _as_issues(plan.get("warnings", []))
    expected = plan.get("confirmation_token")
    if not isinstance(confirm, str) or not confirm:
        blockers.append(schema_issue("confirmation_token_missing", "Export create requires a confirmation token from app export plan."))
    elif confirm != expected:
        blockers.append(schema_issue("confirmation_token_mismatch", "Export create confirmation token does not match the current plan."))
    if blockers:
        return receipt_envelope(
            EXPORT_RECEIPT_TYPES["export_create"],
            str(plan.get("app") or app),
            str(plan.get("environment") or environment or "unknown"),
            "blocked",
            started_at,
            _utc_now(),
            artifacts=_artifacts_from_plan(plan),
            checks=plan.get("checks", []) if isinstance(plan.get("checks"), list) else [],
            rollback={"available": False, "note": "Export create was blocked before writing artifacts."},
            plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
            blockers=blockers,
            warnings=warnings,
            bundle_format_version=EXPORT_BUNDLE_FORMAT_VERSION,
            inputs_redacted=True,
            secrets_redacted=True,
        )

    artifact_paths = plan.get("artifact_paths") if isinstance(plan.get("artifact_paths"), dict) else {}
    bundle_directory = _resolved_export_bundle_directory(plan)
    bundle_directory.mkdir(parents=True, exist_ok=False)
    (bundle_directory / "runtime").mkdir(parents=True, exist_ok=True)
    (bundle_directory / "receipts").mkdir(parents=True, exist_ok=True)
    (bundle_directory / "data").mkdir(parents=True, exist_ok=True)

    metadata = _export_metadata_from_plan(plan)
    _write_json(bundle_directory / "manifest.json", metadata)
    _write_json(bundle_directory / "source-host.json", plan.get("source_host") if isinstance(plan.get("source_host"), dict) else {})

    runtime_copies = _copy_export_runtime_files(runtime_root / "apps" / str(plan["app"]), bundle_directory)
    plan_receipt = _redacted_export_plan_receipt(plan)
    _write_json(bundle_directory / "receipts" / "export-plan.json", plan_receipt)
    checksums = _write_checksums(bundle_directory)

    receipt = receipt_envelope(
        EXPORT_RECEIPT_TYPES["export_create"],
        str(plan.get("app") or app),
        str(plan.get("environment") or environment or "unknown"),
        "succeeded",
        started_at,
        _utc_now(),
        artifacts=[
            artifact(str(bundle_directory), "export-bundle-directory", "Metadata and runtime export bundle", present=True),
            artifact(str(bundle_directory / "manifest.json"), "export-metadata", present=True),
            artifact(str(bundle_directory / "checksums.sha256"), "checksums", present=True),
        ],
        checks=[
            {"name": "confirmation_token", "ok": True, "message": "Matched current export plan."},
            {"name": "runtime_files", "ok": True, "message": f"{len(runtime_copies)} runtime item(s) copied."},
            {"name": "secrets_redacted", "ok": True, "message": "Runtime env values were not included."},
            {"name": "data_exports", "ok": True, "message": "Live data export is not executed by this create mode."},
        ],
        rollback={"available": False, "note": "Export create only wrote local export artifacts and did not mutate source runtime."},
        plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
        warnings=warnings,
        bundle_format_version=EXPORT_BUNDLE_FORMAT_VERSION,
        bundle_path=str(bundle_directory),
        bundle_archive_path=artifact_paths.get("bundle") if isinstance(artifact_paths.get("bundle"), str) else None,
        create_mode="metadata-and-runtime-files",
        runtime_files_copied=runtime_copies,
        checksums=checksums,
        data_export_status=_data_export_status(plan),
        inputs_redacted=True,
        secrets_redacted=True,
    )
    _write_json(bundle_directory / "receipts" / "export-create.json", receipt)
    _write_json(runtime_root / "apps" / str(plan["app"]) / "receipts" / f"{receipt['operation_id']}.json", receipt)
    return receipt


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
    _merge_plan_envelope(
        plan,
        "app.import.plan",
        app,
        str(environment),
        risk="high",
        exact_command=f"ship app import apply {source} --confirm {plan['confirmation_token']}",
    )
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


def env_shape_diff_report(
    app: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest_path: Optional[Path] = None,
) -> Dict[str, object]:
    resolution = resolve_app_manifest(app, environment, manifest_path=manifest_path)
    blockers = [schema_issue("manifest_unresolved", message) for message in resolution.blockers]
    warnings = [schema_issue("manifest_warning", message) for message in resolution.warnings]
    manifest = resolution.manifest
    if manifest is None:
        return report_envelope(
            "env.diff",
            app,
            environment or "unknown",
            f"Env shape diff for {app}: manifest unresolved.",
            blockers=blockers,
            warnings=warnings,
            entries=[],
            values_redacted=True,
        )

    app_root = runtime_root / "apps" / manifest.app
    desired = _desired_env_entries(manifest)
    runtime = _env_values(app_root / "env")
    entries: List[Dict[str, object]] = []
    for key in sorted(set(desired) | set(runtime)):
        desired_entry = desired.get(key)
        runtime_present = key in runtime
        if desired_entry is None:
            status = "extra"
            required_by = ["runtime.env"]
            sources = ["runtime.env"]
        else:
            required_by = desired_entry["required_by"]
            sources = desired_entry["sources"]
            if not runtime_present:
                status = "missing"
            elif _is_placeholder(runtime[key]):
                status = "placeholder"
            else:
                status = "present"
        entries.append(
            {
                "key": key,
                "status": status,
                "runtime_present": runtime_present,
                "required": desired_entry is not None,
                "required_by": required_by,
                "sources": sources,
                "value_redacted": True,
            }
        )

    env_blockers = [
        schema_issue(
            "env_key_missing" if item["status"] == "missing" else "env_key_placeholder",
            f"Required env key `{item['key']}` is {item['status']}.",
            str(item["key"]),
        )
        for item in entries
        if item["required"] and item["status"] in {"missing", "placeholder"}
    ]
    checks = [
        {
            "name": "runtime_env_file",
            "ok": (app_root / "env").exists(),
            "message": str(app_root / "env"),
        },
        {
            "name": "required_env_keys",
            "ok": not any(item["status"] in {"missing", "placeholder"} for item in entries if item["required"]),
            "message": f"{sum(1 for item in entries if item['required'])} required key(s) checked.",
        },
    ]
    return report_envelope(
        "env.diff",
        manifest.app,
        environment or manifest.environment or "unknown",
        f"Env shape diff for {manifest.app}: {len(entries)} key(s), values redacted.",
        blockers=blockers + env_blockers,
        warnings=warnings,
        checks=checks,
        entries=entries,
        runtime_env_path=str(app_root / "env"),
        values_redacted=True,
    )


def backup_status_report(
    app: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest_path: Optional[Path] = None,
) -> Dict[str, object]:
    resolution = resolve_app_manifest(app, environment, manifest_path=manifest_path)
    warnings = [schema_issue("manifest_warning", message) for message in resolution.warnings]
    blockers = [schema_issue("manifest_unresolved", message) for message in resolution.blockers]
    manifest = resolution.manifest
    backups_root = runtime_root / "backups" / "apps" / app
    backups = _backup_records(backups_root)
    latest = backups[-1] if backups else None
    threshold_hours = _backup_threshold_hours(manifest)
    freshness = _freshness_status(latest, threshold_hours)
    backup_required = bool(manifest and manifest.data.backups and manifest.data.backups.required)
    if backup_required and freshness["status"] in {"missing", "stale", "invalid"}:
        blockers.append(
            schema_issue(
                "backup_not_fresh",
                f"Backup status is {freshness['status']} for required backups.",
                "data.backups",
            )
        )
    if manifest and manifest.data.backups is None:
        warnings.append(schema_issue("backup_contract_missing", "No `data.backups` contract is declared.", "data.backups"))

    checks = [
        {"name": "backup_directory", "ok": backups_root.exists(), "message": str(backups_root)},
        {"name": "latest_backup", "ok": latest is not None, "message": latest["backup_id"] if latest else "none"},
        {"name": "freshness", "ok": freshness["status"] == "fresh", "message": freshness["status"]},
    ]
    artifacts = [
        artifact(str(item["path"]), "backup", f"Backup {item['backup_id']}", present=True)
        for item in backups
    ]
    return report_envelope(
        "backup.status",
        app,
        environment or (manifest.environment if manifest else None) or "unknown",
        f"Backup status for {app}: {freshness['status']}.",
        blockers=blockers,
        warnings=warnings,
        checks=checks,
        artifacts=artifacts,
        backup_root=str(backups_root),
        backup_count=len(backups),
        latest_backup=latest,
        freshness=freshness,
        coverage=(latest or {}).get("coverage", {}),
        validation={"status": "metadata-only", "destructive_restore_supported": False},
    )


def app_readiness_report(
    app: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest_path: Optional[Path] = None,
) -> Dict[str, object]:
    resolution = resolve_app_manifest(app, environment, manifest_path=manifest_path)
    if resolution.manifest is None or resolution.manifest_path is None:
        return report_envelope(
            "app.readiness",
            app,
            environment or "unknown",
            f"Readiness for {app}: manifest unresolved.",
            blockers=[schema_issue("manifest_unresolved", message) for message in resolution.blockers],
            warnings=[schema_issue("manifest_warning", message) for message in resolution.warnings],
            readiness_level="blocked",
            portability_score={"score": 0, "level": "blocked", "factors": []},
        )

    manifest = resolution.manifest
    resolved_environment = environment or manifest.environment or "unknown"
    validation = pack_validation_report(manifest, resolution.manifest_path)
    env_report = env_shape_diff_report(manifest.app, resolved_environment, runtime_root, resolution.manifest_path)
    backup_report = backup_status_report(manifest.app, resolved_environment, runtime_root, resolution.manifest_path)
    route_report = scan_conflicts(resolution.manifest_path.parent, runtime_root=runtime_root)
    release = active_release(runtime_root, manifest.app)
    restore_drills = _restore_drill_receipts(runtime_root, manifest.app)
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    blockers.extend(_as_issues(validation["errors"]))
    warnings.extend(_as_issues(validation["warnings"]))
    blockers.extend(_as_issues(env_report["blockers"]))
    warnings.extend(_as_issues(env_report["warnings"]))
    blockers.extend(_as_issues(backup_report["blockers"]))
    warnings.extend(_as_issues(backup_report["warnings"]))
    route_conflict_issues = _route_conflict_issues(route_report, manifest)
    blockers.extend(route_conflict_issues)
    warnings.extend(_as_issues(route_report.get("warnings", [])))
    if not release:
        blockers.append(schema_issue("release_missing", "No active or latest release metadata is present.", "release.json"))
    if manifest.data.backups and manifest.data.backups.restore_drill_required and not restore_drills:
        blockers.append(schema_issue("restore_drill_missing", "No restore drill receipt is recorded.", "restore-drills"))

    checks = [
        {"name": "pack_validation", "ok": validation["ok"], "message": validation["summary"]},
        {"name": "env_shape", "ok": not any(item["status"] in {"missing", "placeholder"} for item in env_report["entries"] if item["required"]), "message": "Env values redacted."},
        {"name": "backup_status", "ok": not backup_report["blockers"], "message": backup_report["freshness"]["status"]},
        {"name": "route_conflicts", "ok": not route_conflict_issues, "message": route_report["summary"]},
        {"name": "release_metadata", "ok": bool(release), "message": str(release.get("release_id") if release else "missing")},
        {"name": "restore_drill", "ok": bool(restore_drills), "message": f"{len(restore_drills)} receipt(s)"},
    ]
    score = portability_score(manifest, blockers, warnings, checks, env_report, backup_report, restore_drills)
    level = "blocked" if blockers else "warning" if warnings else "ready"
    return report_envelope(
        "app.readiness",
        manifest.app,
        resolved_environment,
        f"Readiness for {manifest.app} {resolved_environment}: {level}.",
        blockers=blockers,
        warnings=warnings,
        checks=checks,
        artifacts=[
            artifact(str(runtime_root / "apps" / manifest.app), "runtime", "App runtime root", present=(runtime_root / "apps" / manifest.app).exists())
        ],
        readiness_level=level,
        portability_score=score,
        manifest_path=str(resolution.manifest_path),
        env_shape=env_report,
        backup_status=backup_report,
        route_conflicts=route_report,
        release={"present": bool(release), "release_id": release.get("release_id") if release else None},
        restore_drill_receipts=restore_drills,
    )


def portability_score(
    manifest: Manifest,
    blockers: List[Dict[str, str]],
    warnings: List[Dict[str, str]],
    checks: List[Dict[str, object]],
    env_report: Dict[str, object],
    backup_report: Dict[str, object],
    restore_drills: List[Dict[str, object]],
) -> Dict[str, object]:
    score = 100
    factors: List[Dict[str, object]] = []

    def factor(name: str, category: str, points: int, ok: bool, message: str) -> None:
        nonlocal score
        if not ok:
            score -= points
        factors.append({"name": name, "category": category, "points": points, "ok": ok, "message": message})

    factor("pack_metadata", "runtime", 10, bool(manifest.pack.portability), "Pack portability is declared.")
    factor("explicit_data_contracts", "data", 20, not _inferred_data_contracts(manifest), "Data contracts are explicit.")
    factor("env_ready", "secrets", 15, not any(item["status"] in {"missing", "placeholder"} for item in env_report["entries"] if item["required"]), "Required env keys are present.")
    factor("backup_fresh", "backup", 20, not backup_report["blockers"], "Backup status has no blockers.")
    factor("restore_drill", "restore", 15, bool(restore_drills) or not (manifest.data.backups and manifest.data.backups.restore_drill_required), "Restore drill receipt is recorded when required.")
    factor("image_digest", "runtime", 10, all("@sha256:" in image for image in image_references(manifest).values()), "Images are digest-pinned.")
    factor("checks_pass", "runtime", 10, all(bool(check.get("ok")) for check in checks if check.get("name") != "restore_drill"), "Readiness checks passed.")
    score = max(0, min(100, score))
    if blockers:
        level = "blocked"
    elif score >= 85:
        level = "portable"
    elif score >= 60:
        level = "needs-work"
    else:
        level = "poor"
    return {"score": score, "level": level, "factors": factors, "blocker_count": len(blockers), "warning_count": len(warnings)}


def app_runbook_report(
    app: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest_path: Optional[Path] = None,
) -> Dict[str, object]:
    readiness = app_readiness_report(app, environment, runtime_root, manifest_path)
    markdown = render_app_runbook(readiness)
    return report_envelope(
        "app.runbook",
        readiness.get("app") if isinstance(readiness.get("app"), str) else app,
        readiness.get("environment") if isinstance(readiness.get("environment"), str) else environment,
        f"Generated runbook for {app}.",
        blockers=readiness.get("blockers", []),
        warnings=readiness.get("warnings", []),
        checks=readiness.get("checks", []),
        markdown=markdown,
        readiness=readiness,
    )


def render_app_runbook(readiness: Dict[str, object]) -> str:
    app = str(readiness.get("app") or "unknown")
    environment = str(readiness.get("environment") or "unknown")
    lines = [
        f"# {app} {environment} Runbook",
        "",
        f"Generated: {_utc_now()}",
        "",
        f"Readiness: {readiness.get('readiness_level', readiness.get('status', 'unknown'))}",
        "",
        "## Commands",
        "",
        f"- Validate pack: `./cli/ship pack validate <manifest> --json`",
        f"- Check readiness: `./cli/ship app readiness {app} --environment {environment} --json`",
        f"- Plan export: `./cli/ship app export plan {app} --environment {environment} --json`",
        f"- Check backups: `./cli/ship backup status {app} --environment {environment} --json`",
        "",
        "## Blockers",
        "",
    ]
    blockers = readiness.get("blockers") if isinstance(readiness.get("blockers"), list) else []
    if blockers:
        lines.extend(f"- {item.get('message') if isinstance(item, dict) else item}" for item in blockers)
    else:
        lines.append("- none")
    lines.extend(["", "## Warnings", ""])
    warnings = readiness.get("warnings") if isinstance(readiness.get("warnings"), list) else []
    if warnings:
        lines.extend(f"- {item.get('message') if isinstance(item, dict) else item}" for item in warnings)
    else:
        lines.append("- none")
    lines.extend(["", "## Backup Status", ""])
    backup = readiness.get("backup_status") if isinstance(readiness.get("backup_status"), dict) else {}
    freshness = backup.get("freshness") if isinstance(backup.get("freshness"), dict) else {}
    lines.append(f"- Status: {freshness.get('status', 'unknown')}")
    lines.append(f"- Latest backup: {(backup.get('latest_backup') or {}).get('backup_id') if isinstance(backup.get('latest_backup'), dict) else 'none'}")
    lines.extend(["", "## Rollback Notes", "", "- Export and readiness checks do not mutate runtime state."])
    return "\n".join(lines) + "\n"


def pack_init_report(
    app: str,
    environment: Optional[str],
    critical: bool,
    postgres: bool,
    redis: bool,
    uploads: bool,
    root: Path,
    write: bool = False,
    force: bool = False,
) -> Dict[str, object]:
    base = root / "ophelia"
    files = {
        base / "runbook.md": f"# {app} Runbook\n\nGenerated pack scaffold. Fill in app-specific operations.\n",
        base / "agent.md": f"# {app} Agent Notes\n\nUse `ship pack validate`, `ship pack explain`, and `ship app readiness` before proposing mutations.\n",
        base / "checks" / "data-verify.sh": "#!/usr/bin/env sh\nset -eu\n# Add read-only data verification here.\n",
        base / "hooks" / "pre-export.sh": "#!/usr/bin/env sh\nset -eu\n# Add bounded pre-export checks here.\n",
        base / "hooks" / "freeze.sh": "#!/usr/bin/env sh\nset -eu\n# Add app read-only/freeze behavior here.\n",
        base / "hooks" / "unfreeze.sh": "#!/usr/bin/env sh\nset -eu\n# Add app unfreeze behavior here.\n",
        base / "hooks" / "post-import.sh": "#!/usr/bin/env sh\nset -eu\n# Add target-side post-import checks here.\n",
    }
    planned = []
    blockers = []
    written = []
    for path, content in files.items():
        exists = path.exists()
        planned.append({"path": str(path), "exists": exists, "action": "overwrite" if exists and force else "create" if not exists else "skip"})
        if write and exists and not force:
            blockers.append(schema_issue("file_exists", f"Refusing to overwrite existing file: {path}", str(path)))
    if write and not blockers:
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            written.append(str(path))

    snippet = _pack_init_manifest_snippet(app, environment, critical, postgres, redis, uploads)
    return report_envelope(
        "pack.init",
        app,
        environment or "unknown",
        f"Pack init {'wrote' if write and not blockers else 'previewed'} {len(files)} file(s).",
        blockers=blockers,
        warnings=[],
        checks=[{"name": "write_requested", "ok": write, "message": "preview" if not write else "write"}],
        artifacts=[artifact(str(path), "scaffold", present=path.exists()) for path in files],
        dry_run=not write,
        root=str(root),
        planned_files=planned,
        written_files=written,
        manifest_snippet=snippet,
    )


def receipt_list_report(
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    app: Optional[str] = None,
    environment: Optional[str] = None,
) -> Dict[str, object]:
    receipts = _receipt_records(runtime_root, app=app, environment=environment)
    return report_envelope(
        "receipts.list",
        app,
        environment,
        f"Found {len(receipts)} receipt(s).",
        checks=[{"name": "receipt_scan", "ok": True, "message": str(runtime_root)}],
        receipts=receipts,
    )


def receipt_show_report(receipt_id: str, runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> Dict[str, object]:
    receipts = _receipt_records(runtime_root)
    for record in receipts:
        if record["receipt_id"] == receipt_id:
            payload = _read_json(Path(str(record["path"])))
            return report_envelope(
                "receipts.show",
                str(payload.get("app") or record.get("app") or "unknown"),
                payload.get("environment") if isinstance(payload.get("environment"), str) else None,
                f"Receipt {receipt_id}.",
                artifacts=[artifact(str(record["path"]), "receipt", present=True)],
                receipt=payload,
                receipt_id=receipt_id,
            )
    return report_envelope(
        "receipts.show",
        None,
        None,
        f"Receipt not found: {receipt_id}.",
        blockers=[schema_issue("receipt_not_found", f"Receipt not found: {receipt_id}")],
        receipt_id=receipt_id,
    )


def restore_drill_plan(
    app: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest_path: Optional[Path] = None,
) -> Dict[str, object]:
    readiness = app_readiness_report(app, environment, runtime_root, manifest_path)
    token = _token("app.restore-drill.apply", {"app": app, "environment": readiness.get("environment")})
    return plan_envelope(
        "app.restore-drill.plan",
        app,
        str(readiness.get("environment") or environment or "unknown"),
        f"Restore drill plan for {app}.",
        blockers=readiness.get("blockers", []),
        warnings=readiness.get("warnings", []),
        checks=readiness.get("checks", []),
        artifacts=[],
        confirmation_required=True,
        confirmation_token=token,
        exact_apply_input={
            "command": f"ship app restore-drill apply {app} --environment {readiness.get('environment') or environment or 'unknown'} --confirm {token}"
        },
        risk="high",
        readiness=readiness,
        apply_supported=False,
    )


def cutover_plan(
    app: str,
    source_host: str,
    target_host: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest_path: Optional[Path] = None,
) -> Dict[str, object]:
    readiness = app_readiness_report(app, environment, runtime_root, manifest_path)
    blockers = list(readiness.get("blockers", []))
    blockers.append(schema_issue("cutover_apply_not_enabled", "Cutover apply is not implemented in this planning pass."))
    token = _token("app.cutover.apply", {"app": app, "environment": readiness.get("environment"), "from": source_host, "to": target_host})
    return plan_envelope(
        "app.cutover.plan",
        app,
        str(readiness.get("environment") or environment or "unknown"),
        f"Cutover plan for {app} from {source_host} to {target_host}.",
        blockers=blockers,
        warnings=readiness.get("warnings", []),
        checks=readiness.get("checks", []),
        artifacts=[],
        confirmation_required=True,
        confirmation_token=token,
        exact_apply_input={
            "command": f"ship app cutover apply {app} --from {source_host} --to {target_host} --confirm {token}"
        },
        risk="critical",
        source_host=source_host,
        target_host=target_host,
        readiness=readiness,
        rollback={"available": True, "note": "Before DNS/Caddy changes, rollback is canceling cutover and leaving source active."},
        apply_supported=False,
    )


def isolation_plan(
    app: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest_path: Optional[Path] = None,
) -> Dict[str, object]:
    resolution = resolve_app_manifest(app, environment, manifest_path=manifest_path)
    manifest = resolution.manifest
    resolved_environment = environment or (manifest.environment if manifest else None) or "unknown"
    blockers = [schema_issue("manifest_unresolved", message) for message in resolution.blockers]
    target_network = f"{app}-{resolved_environment}-internal"
    return plan_envelope(
        "app.isolation.plan",
        app,
        resolved_environment,
        f"Per-app isolation compatibility plan for {app}.",
        blockers=blockers,
        warnings=[schema_issue("compatibility_mode", "Current Compose rendering still uses ophelia-internal until isolation is explicitly enabled.")],
        checks=[{"name": "current_runtime_unchanged", "ok": True, "message": "Plan only; no Compose changes are written."}],
        artifacts=[],
        confirmation_required=False,
        risk="medium",
        current_networks=["ophelia-edge", "ophelia-internal"],
        target_networks=["ophelia-edge", target_network],
        apply_supported=False,
    )


def _desired_env_entries(manifest: Manifest) -> Dict[str, Dict[str, object]]:
    entries: Dict[str, Dict[str, object]] = {}

    def add(key: str, source: str, required_by: str) -> None:
        current = entries.setdefault(key, {"key": key, "sources": [], "required_by": []})
        if source not in current["sources"]:
            current["sources"].append(source)
        if required_by not in current["required_by"]:
            current["required_by"].append(required_by)

    for raw_line in render_env_example(manifest).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _value = line.split("=", 1)
        add(key, "env.example", _env_required_by(manifest, key))
    for key in manifest.env:
        add(key, "manifest.env", "manifest.env")
    for service in manifest.services.values():
        for key in service.env:
            add(key, f"services.{service.name}.env", f"service:{service.name}")
    if manifest.data.postgres:
        add("DATABASE_URL", "data.postgres", "data.postgres")
    if manifest.data.redis:
        add("REDIS_URL", "data.redis", "data.redis")
    return entries


def _merge_plan_envelope(
    plan: Dict[str, object],
    operation: str,
    app: Optional[str],
    environment: Optional[str],
    risk: str,
    exact_command: str,
) -> None:
    envelope = plan_envelope(
        operation,
        app,
        environment,
        str(plan.get("summary") or operation),
        blockers=_as_issues(plan.get("blockers", [])),
        warnings=_as_issues(plan.get("warnings", [])),
        checks=plan.get("checks", []) if isinstance(plan.get("checks"), list) else [],
        artifacts=_artifacts_from_plan(plan),
        confirmation_required=bool(plan.get("confirmation_required") or plan.get("confirmation_required_for_future_apply")),
        confirmation_token=plan.get("confirmation_token") if isinstance(plan.get("confirmation_token"), str) else None,
        exact_apply_input={"command": exact_command},
        risk=risk,
    )
    plan.update(envelope)


def _artifacts_from_plan(plan: Dict[str, object]) -> List[Dict[str, object]]:
    artifacts: List[Dict[str, object]] = []
    artifact_paths = plan.get("artifact_paths")
    if isinstance(artifact_paths, dict):
        for name, path in sorted(artifact_paths.items()):
            if isinstance(path, str):
                artifacts.append(artifact(path, name))
    source = plan.get("source")
    if isinstance(source, str):
        artifacts.append(artifact(source, "source", present=Path(source).exists()))
    return artifacts


def _env_required_by(manifest: Manifest, key: str) -> str:
    if key == "DATABASE_URL":
        return "data.postgres" if manifest.data.postgres else "addons.postgres"
    if key == "REDIS_URL":
        return "data.redis" if manifest.data.redis else "addons.redis"
    if key in manifest.env:
        return "manifest.env"
    if key.startswith("PRISM_"):
        return "prism"
    return "runtime"


def _env_values(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    values: Dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _backup_records(backups_root: Path) -> List[Dict[str, object]]:
    records: List[Dict[str, object]] = []
    if not backups_root.exists():
        return records
    for manifest_path in sorted(backups_root.glob("*/backup-manifest.json")):
        payload = _read_json(manifest_path)
        backup_id = str(payload.get("backup_id") or manifest_path.parent.name)
        created_at = str(payload.get("created_at") or _backup_id_to_timestamp(backup_id) or "")
        records.append(
            {
                "backup_id": backup_id,
                "created_at": created_at or None,
                "path": str(manifest_path.parent),
                "manifest_path": str(manifest_path),
                "coverage": payload.get("coverage", {}),
                "database": payload.get("database", {}),
                "warnings": payload.get("warnings", []),
                "secrets_redacted_in_report": True,
            }
        )
    return sorted(records, key=lambda item: str(item.get("created_at") or item["backup_id"]))


def _backup_threshold_hours(manifest: Optional[Manifest]) -> Optional[float]:
    if manifest is None or manifest.data.backups is None:
        return None
    raw = manifest.data.backups.extra.get("max_age_hours")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return None
    return 24.0 if manifest.data.backups.required else None


def _freshness_status(latest: Optional[Dict[str, object]], threshold_hours: Optional[float]) -> Dict[str, object]:
    if latest is None:
        return {"status": "missing", "age_hours": None, "threshold_hours": threshold_hours}
    created_at = latest.get("created_at")
    parsed = _parse_timestamp(str(created_at)) if created_at else None
    if parsed is None:
        return {"status": "unknown", "age_hours": None, "threshold_hours": threshold_hours}
    age_seconds = (datetime.now(timezone.utc) - parsed).total_seconds()
    age_hours = round(age_seconds / 3600, 2)
    if threshold_hours is None:
        status = "unknown"
    else:
        status = "fresh" if age_hours <= threshold_hours else "stale"
    return {"status": status, "age_hours": age_hours, "threshold_hours": threshold_hours}


def _parse_timestamp(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _backup_id_to_timestamp(backup_id: str) -> Optional[str]:
    stamp = backup_id.split("-", 1)[0]
    try:
        parsed = datetime.strptime(stamp[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return parsed.isoformat().replace("+00:00", "Z")


def _as_issues(items: object) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    if not isinstance(items, list):
        return result
    for item in items:
        if isinstance(item, dict):
            code = str(item.get("code") or item.get("name") or "issue")
            message = str(item.get("message") or item)
            path = item.get("path") or item.get("field")
            result.append(schema_issue(code, message, str(path) if path else None))
        else:
            result.append(schema_issue("issue", str(item)))
    return result


def _restore_drill_receipts(runtime_root: Path, app: str) -> List[Dict[str, object]]:
    roots = [
        runtime_root / "apps" / app / "restore-drills",
        runtime_root / "apps" / app / "receipts",
    ]
    receipts: List[Dict[str, object]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.json")):
            payload = _read_json(path)
            operation = str(payload.get("operation") or "")
            if "restore-drill" in operation or "restore_drill" in operation:
                receipts.append(
                    {
                        "receipt_id": str(payload.get("operation_id") or path.stem),
                        "operation": operation,
                        "status": payload.get("status"),
                        "path": str(path),
                    }
                )
    return receipts


def _route_conflict_issues(report: Dict[str, object], manifest: Manifest) -> List[Dict[str, str]]:
    issues: List[Dict[str, str]] = []
    conflicts = report.get("conflicts") if isinstance(report.get("conflicts"), list) else []
    for conflict in conflicts:
        if not isinstance(conflict, dict) or conflict.get("type") not in {"duplicate_domain", "duplicate_route"}:
            continue
        owners = conflict.get("owners") if isinstance(conflict.get("owners"), list) else []
        if not any(isinstance(owner, dict) and owner.get("app") == manifest.app for owner in owners):
            continue
        competing = [
            owner
            for owner in owners
            if isinstance(owner, dict)
            and (owner.get("app"), owner.get("environment")) != (manifest.app, manifest.environment)
        ]
        if not competing:
            continue
        issues.append(
            schema_issue(
                "route_conflict",
                f"Route/domain `{conflict.get('domain')}` is also owned by another app or environment.",
                "routes",
            )
        )
    return issues


def _pack_init_manifest_snippet(
    app: str,
    environment: Optional[str],
    critical: bool,
    postgres: bool,
    redis: bool,
    uploads: bool,
) -> str:
    lines = [
        "pack:",
        f"  portability: {'critical' if critical else 'standard'}",
        "  owner: personal",
        f"  description: {app} {environment or 'environment'} app",
        "  deploy_binding_file: ophelia/deploy.json",
        "",
        "host_requirements:",
        "  min_memory: 1g",
        "  min_disk_free: 20g",
        "  requires_edge: true",
        "  requires_docker: true",
        "",
        "data:",
    ]
    if postgres:
        lines.extend(
            [
                "  postgres:",
                "    mode: shared-postgres-database",
                f"    database: {app.replace('-', '_')}",
                "    export:",
                "      format: custom",
                "      command: pg_dump",
                "    import:",
                "      command: pg_restore",
                "    verify:",
                "      command: ophelia/checks/data-verify.sh",
            ]
        )
    if redis:
        lines.extend(["  redis:", "    mode: redis-logical-db"])
    if uploads:
        lines.extend(
            [
                "  volumes:",
                "    - name: uploads",
                "      mount: /app/uploads",
                "      class: critical",
                "      export: tar-zstd",
                "      import: tar-zstd",
            ]
        )
    lines.extend(
        [
            "  backups:",
            "    required: true",
            "    restore_drill_required: true",
            "    offsite_required: true",
            "",
            "hooks:",
            "  pre_export: ophelia/hooks/pre-export.sh",
            "  freeze: ophelia/hooks/freeze.sh",
            "  unfreeze: ophelia/hooks/unfreeze.sh",
            "  post_import: ophelia/hooks/post-import.sh",
        ]
    )
    return "\n".join(lines) + "\n"


def _receipt_records(
    runtime_root: Path,
    app: Optional[str] = None,
    environment: Optional[str] = None,
) -> List[Dict[str, object]]:
    roots: List[Tuple[Optional[str], Optional[str], Path]] = []
    apps_root = runtime_root / "apps"
    if apps_root.exists():
        for app_root in sorted(path for path in apps_root.iterdir() if path.is_dir()):
            if app and app_root.name != app:
                continue
            roots.append((app_root.name, None, app_root / "receipts"))
            for env_root in sorted(path for path in app_root.iterdir() if path.is_dir()):
                if environment and env_root.name != environment:
                    continue
                roots.append((app_root.name, env_root.name, env_root / "receipts"))
            roots.append((app_root.name, None, app_root / "rollback-reports"))
    if app is None or (runtime_root / "backups" / "apps" / app).exists():
        backup_apps = [runtime_root / "backups" / "apps" / app] if app else sorted((runtime_root / "backups" / "apps").glob("*")) if (runtime_root / "backups" / "apps").exists() else []
        for backup_app_root in backup_apps:
            roots.append((backup_app_root.name, environment, backup_app_root))

    records: List[Dict[str, object]] = []
    for owner_app, owner_env, root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.json")):
            payload = _read_json(path)
            receipt_id = _receipt_id(path, payload)
            records.append(
                {
                    "receipt_id": receipt_id,
                    "operation": payload.get("operation") or _operation_from_receipt_path(path),
                    "status": payload.get("status") or "unknown",
                    "app": payload.get("app") or owner_app,
                    "environment": payload.get("environment") or owner_env,
                    "started_at": payload.get("started_at") or payload.get("created_at") or payload.get("applied_at"),
                    "completed_at": payload.get("completed_at") or payload.get("created_at") or payload.get("applied_at"),
                    "path": str(path),
                    "inputs_redacted": payload.get("inputs_redacted", True),
                }
            )
    return sorted(records, key=lambda item: (str(item.get("started_at") or ""), str(item["receipt_id"])))


def _receipt_id(path: Path, payload: Dict[str, object]) -> str:
    for key in ("receipt_id", "operation_id", "backup_id", "rollback_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:10]
    return f"{path.stem}-{digest}"


def _operation_from_receipt_path(path: Path) -> str:
    if path.name == "backup-manifest.json":
        return "backup.create"
    if path.name == "restore-report.json":
        return "restore.apply"
    if "rollback" in path.parts:
        return "rollback.apply"
    return path.stem.replace("-", ".")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
        owners = conflict.get("owners", [])
        if not isinstance(owners, list):
            continue
        if any(
            isinstance(owner, dict)
            and owner.get("manifest_path") == normalized_path
            and owner.get("app") == manifest.app
            for owner in owners
        ):
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


def _resolved_export_bundle_directory(plan: Dict[str, object]) -> Path:
    artifact_paths = plan.get("artifact_paths") if isinstance(plan.get("artifact_paths"), dict) else {}
    planned = artifact_paths.get("bundle_directory") if isinstance(artifact_paths.get("bundle_directory"), str) else None
    if planned:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return Path(planned.replace("<timestamp>", timestamp))
    app_root = Path(str(plan.get("source_host", {}).get("runtime_root", DEFAULT_RUNTIME_ROOT))) / "apps" / str(plan["app"])
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return app_root / "export-bundles" / EXPORT_BUNDLE_DIRECTORY_TEMPLATE.format(
        app=plan.get("app"),
        environment=plan.get("environment"),
        timestamp=timestamp,
    )


def _export_metadata_from_plan(plan: Dict[str, object]) -> Dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "ophelia.export.metadata",
        "bundle_format_version": EXPORT_BUNDLE_FORMAT_VERSION,
        "created_from_operation_id": plan.get("operation_id"),
        "app": plan.get("app"),
        "environment": plan.get("environment"),
        "source_host": plan.get("source_host"),
        "app_release_id": plan.get("app_release_id"),
        "active_release_id": plan.get("active_release_id"),
        "data_dependencies": plan.get("data_dependencies", {}),
        "data_export_status": _data_export_status(plan),
        "host_requirements": plan.get("host_requirements", {}),
        "routes": plan.get("routes", []),
        "domains": plan.get("domains", []),
        "images": plan.get("images", {}),
        "env_shape": plan.get("env_shape", []),
        "verification_checks": plan.get("verification_checks", []),
        "runtime_files": plan.get("runtime_files", []),
        "bundle_shape": plan.get("bundle_shape", {}),
        "secrets_redacted": True,
    }


def _copy_export_runtime_files(app_root: Path, bundle_directory: Path) -> List[Dict[str, object]]:
    copied: List[Dict[str, object]] = []
    for item in _runtime_export_files(app_root):
        if not item.get("present"):
            continue
        relative = Path(str(item["path"]))
        source = app_root / relative
        target = bundle_directory / "runtime" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative == Path("manifest.lock.json"):
            _write_json(target, _redact_manifest_lock(_read_json(source)))
        elif relative == Path("compose.yml"):
            target.write_text(_redacted_compose_text(source.read_text()))
        elif source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
        copied.append(
            {
                "path": str(Path("runtime") / relative),
                "kind": "directory" if source.is_dir() else "file",
                "size_bytes": _path_size(target),
                "secret_values_redacted": relative in {Path("manifest.lock.json"), Path("compose.yml")},
            }
        )
    return copied


def _redact_manifest_lock(payload: Dict[str, object]) -> Dict[str, object]:
    redacted = json.loads(json.dumps(payload))
    if isinstance(redacted.get("env"), dict):
        redacted["env"] = {key: "<redacted>" for key in redacted["env"]}
    services = redacted.get("services")
    if isinstance(services, dict):
        for service in services.values():
            if isinstance(service, dict) and isinstance(service.get("env"), dict):
                service["env"] = {key: "<redacted>" for key in service["env"]}
    redacted["secret_values_redacted"] = True
    return redacted


def _redacted_compose_text(content: str) -> str:
    safe_keys = {"OPHELIA_APP", "OPHELIA_SERVICE", "PORT"}
    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if ":" in stripped:
            key, _value = stripped.split(":", 1)
            if key and key.replace("_", "").isalnum() and key not in safe_keys and line.startswith("      "):
                indent = line[: len(line) - len(line.lstrip())]
                lines.append(f"{indent}{key}: \"<redacted>\"")
                continue
        lines.append(line)
    return "\n".join(lines) + "\n"


def _redacted_export_plan_receipt(plan: Dict[str, object]) -> Dict[str, object]:
    redacted = json.loads(json.dumps(plan, sort_keys=True, default=str))
    token_value = redacted.get("confirmation_token")
    if isinstance(token_value, str) and token_value:
        redacted["confirmation_token"] = "<redacted-after-create>"
        exact = redacted.get("exact_apply_input")
        if isinstance(exact, dict):
            command = exact.get("command")
            if isinstance(command, str):
                exact["command"] = command.replace(token_value, "<redacted-after-create>")
    redacted["inputs_redacted"] = True
    redacted["secrets_redacted"] = True
    return redacted


def _write_checksums(bundle_directory: Path) -> List[Dict[str, str]]:
    records: List[Dict[str, str]] = []
    checksum_path = bundle_directory / "checksums.sha256"
    for path in sorted(item for item in bundle_directory.rglob("*") if item.is_file()):
        if path == checksum_path:
            continue
        relative = path.relative_to(bundle_directory)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        records.append({"path": str(relative), "sha256": digest})
    checksum_path.write_text("".join(f"{item['sha256']}  {item['path']}\n" for item in records))
    return records


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _data_export_status(plan: Dict[str, object]) -> List[Dict[str, object]]:
    commands = plan.get("commands") if isinstance(plan.get("commands"), list) else []
    return [
        {
            "name": item.get("name"),
            "data": item.get("data"),
            "status": "planned_not_executed",
            "reason": "metadata-and-runtime-files export create does not run live data commands",
        }
        for item in commands
        if isinstance(item, dict)
    ]


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
