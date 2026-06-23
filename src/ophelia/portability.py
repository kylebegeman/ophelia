from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import shutil
import socket
import subprocess
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, unquote, urlparse

from .caddy_manager import reload_caddy, validate_caddy
from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .conflicts import scan_conflicts
from .findings import attach_remediation
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
from .path_safety import assert_no_external_symlinks, external_symlinks
from .policy import policy_check_entry
from .provider_config import validate_provider_config, validate_ttl
from .redaction import redact_url
from .remediation import remediation_for
from .redaction import (
    deep_redact,
    redact_command_string,
    redact_mapping,
    redacted_cloudflare_record,
    redacted_compose_text,
)
from .runtime import active_release, active_release_id, image_references, latest_release_id
from .templates import RUNTIME_INJECTED_ENV_KEYS, compose_network_summary, render_env_example
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

# Readiness score weights (factor name -> weight). Summing to 100, the
# portability score starts at 100 and subtracts the weight of each failed
# factor. Kept as a module constant so the weights are inspectable and the
# per-category roll-up in ``app_readiness_report`` stays in sync with
# ``portability_score``.
READINESS_SCORE_WEIGHTS: Dict[str, int] = {
    "pack_metadata": 10,
    "explicit_data_contracts": 20,
    "env_ready": 15,
    "backup_fresh": 20,
    "restore_drill": 15,
    "image_digest": 10,
    "checks_pass": 10,
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
    return deep_redact(payload) if isinstance(payload, dict) else {}


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
        "networking": manifest.to_lock_dict().get("networking", {}),
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
    report["score_details"] = _pack_quality_score_details(manifest, ok=not errors)
    return report


def _pack_quality_score_details(manifest: Manifest, *, ok: bool) -> Dict[str, Dict[str, object]]:
    """Manifest-only category roll-up of the readiness score factors.

    Pack validation cannot see runtime env/backup/restore state, so it scores
    only the manifest-derived factors it can actually assess (pack metadata,
    explicit data contracts, image digests, and whether validation checks pass).
    Weights come from :data:`READINESS_SCORE_WEIGHTS` so the categories match the
    full readiness roll-up. The omitted runtime factors are not invented here.
    """
    images = image_references(manifest)
    factor_assessments = [
        ("pack_metadata", "runtime", bool(manifest.pack.portability)),
        ("explicit_data_contracts", "data", not _inferred_data_contracts(manifest)),
        ("image_digest", "runtime", bool(images) and all("@sha256:" in image for image in images.values())),
        ("checks_pass", "runtime", ok),
    ]
    details: Dict[str, Dict[str, object]] = {}
    for name, category, passed in factor_assessments:
        weight = READINESS_SCORE_WEIGHTS[name]
        bucket = details.setdefault(
            category,
            {"points": 0, "max_points": 0, "ok_factors": 0, "total_factors": 0},
        )
        bucket["points"] = int(bucket["points"]) + (weight if passed else 0)
        bucket["max_points"] = int(bucket["max_points"]) + weight
        bucket["total_factors"] = int(bucket["total_factors"]) + 1
        if passed:
            bucket["ok_factors"] = int(bucket["ok_factors"]) + 1
    for category, bucket in details.items():
        total = int(bucket["total_factors"])
        ok_count = int(bucket["ok_factors"])
        bucket["reason"] = (
            f"{category}: {ok_count}/{total} manifest factor(s) ok, "
            f"{bucket['points']}/{bucket['max_points']} point(s) earned."
        )
    return details


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
        "networking": manifest.to_lock_dict().get("networking", {}),
        "compose_networks": compose_network_summary(manifest),
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
            {"name": check.name or redact_url(check.url), "url": redact_url(check.url), "expect_status": check.expect_status}
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
        "score_details": validation.get("score_details", {}),
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
    include_postgres: bool = False,
) -> Dict[str, object]:
    resolution = resolve_app_manifest(app, environment, manifest_path=manifest_path)
    blockers = list(resolution.blockers)
    warnings = list(resolution.warnings)
    manifest = resolution.manifest
    resolved_environment = environment

    if manifest is None or resolution.manifest_path is None:
        plan = _base_export_plan(app, environment, runtime_root, blockers, warnings, include_postgres=include_postgres)
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
                f"--runtime-root {runtime_root} {'--include-postgres ' if include_postgres else ''}"
                f"--confirm {plan['confirmation_token']}"
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
    bundle_tar_name = EXPORT_BUNDLE_TAR_FILENAME_TEMPLATE.format(
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
    external_runtime_links = [
        item for item in runtime_files if item.get("external_symlink")
    ]
    for item in external_runtime_links:
        blockers.append(
            f"External symlink is not export-eligible: {item.get('path')} -> {item.get('symlink_target')}"
        )
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
        "networking": manifest.to_lock_dict().get("networking", {}),
        "compose_networks": compose_network_summary(manifest),
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
            {"name": check.name or redact_url(check.url), "url": redact_url(check.url), "expect_status": check.expect_status}
            for check in verification_checks(manifest)
        ],
        "runtime_files": runtime_files,
        "estimated_export_size_bytes": _path_size(app_root) if app_root.exists() else None,
        "commands": commands,
        "include_postgres": include_postgres,
        "artifact_paths": {
            "bundle": str(artifact_root / bundle_name),
            "bundle_tar": str(artifact_root / bundle_tar_name),
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
        "create_mode": "metadata-runtime-static-volume-files" + ("-postgres" if include_postgres else ""),
        "read_only": True,
        "secrets_redacted": True,
        "summary": f"Export plan for {manifest.app} {resolved_environment}: {len(blockers)} blocker(s), {len(warnings)} warning(s).",
    }
    plan["confirmation_token"] = export_plan_token(plan)
    # Additive policy evaluation under `checks` only (never top-level status).
    restore_drills = _restore_drill_receipts(runtime_root, manifest.app)
    image_digest_pinned = bool(plan.get("images")) and all(
        "@sha256:" in image for image in (plan.get("images") or {}).values()
    )
    plan["checks"] = [
        policy_check_entry(
            "app.export.create",
            manifest.app,
            resolved_environment,
            {
                "confirmation_required": True,
                "restore_drill_present": bool(restore_drills),
                "provider_config_validated": False,
                "image_digest_pinned": image_digest_pinned,
                "json_receipts": True,
            },
            runtime_root=runtime_root,
        )
    ]
    _merge_plan_envelope(
        plan,
        "app.export.plan",
        manifest.app,
        str(resolved_environment),
        risk="high",
        exact_command=(
            f"ship app export create {manifest.app} --environment {resolved_environment} "
            f"--manifest {resolution.manifest_path} --runtime-root {runtime_root} "
            f"{'--include-postgres ' if include_postgres else ''}"
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
    include_postgres: bool = False,
) -> Dict[str, object]:
    started_at = _utc_now()
    plan = export_plan(app, environment, runtime_root, manifest_path, ophelia_root, include_postgres=include_postgres)
    blockers = _as_issues(plan.get("blockers", []))
    warnings = _as_issues(plan.get("warnings", []))
    expected = plan.get("confirmation_token")
    if not isinstance(confirm, str) or not confirm:
        blockers.append(schema_issue("confirmation_token_missing", "Export create requires a confirmation token from app export plan."))
    elif confirm != expected:
        blockers.append(schema_issue("confirmation_token_mismatch", "Export create confirmation token does not match the current plan."))
    app_root = runtime_root / "apps" / str(plan.get("app") or app)
    for link in _external_runtime_symlinks(app_root):
        blockers.append(schema_issue("external_symlink_blocked", f"External symlink is not export-eligible: {link} -> {link.readlink()}", str(link)))
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

    bundle_directory = _resolved_export_bundle_directory(plan)
    bundle_directory.mkdir(parents=True, exist_ok=False)
    (bundle_directory / "runtime").mkdir(parents=True, exist_ok=True)
    (bundle_directory / "receipts").mkdir(parents=True, exist_ok=True)
    (bundle_directory / "data").mkdir(parents=True, exist_ok=True)

    metadata = _export_metadata_from_plan(plan)
    _write_json(bundle_directory / "manifest.json", metadata)
    _write_json(bundle_directory / "source-host.json", plan.get("source_host") if isinstance(plan.get("source_host"), dict) else {})

    runtime_copies = _copy_export_runtime_files(runtime_root / "apps" / str(plan["app"]), bundle_directory)
    manifest_for_export = load_manifest(Path(str(plan["manifest_path"]))) if isinstance(plan.get("manifest_path"), str) else None
    data_archives = _create_data_archives(
        manifest_for_export,
        Path(str(plan["manifest_path"])) if isinstance(plan.get("manifest_path"), str) else None,
        runtime_root / "apps" / str(plan["app"]),
        bundle_directory,
    )
    postgres_exports = _create_postgres_exports(
        manifest_for_export,
        runtime_root / "apps" / str(plan["app"]),
        bundle_directory,
        include=include_postgres,
    )
    plan_receipt = _redacted_export_plan_receipt(plan)
    _write_json(bundle_directory / "receipts" / "export-plan.json", plan_receipt)
    bundle_tar_path = _resolved_export_bundle_tar(plan, bundle_directory)
    bundle_archive_path = _resolved_export_bundle_archive(plan, bundle_directory)

    postgres_failed = any(item.get("required") is True and item.get("status") != "dumped" for item in postgres_exports)
    receipt_status = "failed" if postgres_failed else "succeeded"
    receipt = receipt_envelope(
        EXPORT_RECEIPT_TYPES["export_create"],
        str(plan.get("app") or app),
        str(plan.get("environment") or environment or "unknown"),
        receipt_status,
        started_at,
        _utc_now(),
        artifacts=[
            artifact(str(bundle_directory), "export-bundle-directory", "Metadata and runtime export bundle", present=True),
            artifact(str(bundle_tar_path), "export-bundle-tar", "Portable tar archive of the redacted bundle directory", present=True),
            artifact(str(bundle_archive_path), "export-bundle-tar-zstd", "Compressed portable archive when zstd is available", present=False),
            artifact(str(bundle_directory / "manifest.json"), "export-metadata", present=True),
            artifact(str(bundle_directory / "checksums.sha256"), "checksums", present=True),
        ],
        checks=[
            {"name": "confirmation_token", "ok": True, "message": "Matched current export plan."},
            {"name": "runtime_files", "ok": True, "message": f"{len(runtime_copies)} runtime item(s) copied."},
            {"name": "secrets_redacted", "ok": True, "message": "Runtime env values were not included."},
            {"name": "data_exports", "ok": True, "message": f"{sum(1 for item in data_archives if item['status'] == 'archived')} data archive(s) created."},
            {"name": "postgres_dump", "ok": not postgres_failed, "message": _postgres_check_message(postgres_exports, include_postgres)},
        ],
        rollback={"available": False, "note": "Export create only wrote local export artifacts and did not mutate source runtime."},
        plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
        warnings=warnings,
        bundle_format_version=EXPORT_BUNDLE_FORMAT_VERSION,
        bundle_path=str(bundle_directory),
        bundle_archive_path=str(bundle_archive_path),
        bundle_tar_path=str(bundle_tar_path),
        create_mode="metadata-runtime-static-volume-files",
        runtime_files_copied=runtime_copies,
        checksum_manifest_path=str(bundle_directory / "checksums.sha256"),
        data_export_status=_data_export_status(plan, data_archives, postgres_exports),
        data_archives=data_archives,
        postgres_exports=postgres_exports,
        inputs_redacted=True,
        secrets_redacted=True,
    )
    _write_json(bundle_directory / "receipts" / "export-create.json", receipt)
    checksums = _write_checksums(bundle_directory)
    _create_tar_archive(bundle_directory, bundle_tar_path)
    compressed_archive = _create_zstd_archive(bundle_tar_path, bundle_archive_path)
    receipt["checksums"] = checksums
    receipt["compressed_archive"] = compressed_archive
    receipt["checks"].append(
        {
            "name": "compressed_archive",
            "ok": compressed_archive["status"] in {"created", "skipped"},
            "message": str(compressed_archive.get("reason") or compressed_archive["status"]),
        }
    )
    for item in receipt["artifacts"]:
        if isinstance(item, dict) and item.get("kind") == "export-bundle-tar-zstd":
            item["present"] = compressed_archive["status"] == "created"
            item["path"] = str(bundle_archive_path)
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
    networking = _metadata_networking(metadata)
    routes = _metadata_routes(metadata)
    source_artifacts = _source_export_artifacts(source)
    target_host = _host_summary(runtime_root, ophelia_root)
    host_match = _host_capability_match(host_requirements, target_host)
    blockers.extend(str(item["message"]) for item in host_match["blockers"])
    warnings.extend(str(item["message"]) for item in host_match["warnings"])
    if mode == "rehearsal":
        warnings.append("Import apply creates an isolated preview directory only; it does not restore data or change active runtime.")
    else:
        warnings.append("Cutover import apply is intentionally out of scope; this command only plans target changes.")

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
        "required_docker_networks": _required_docker_networks(app, environment, networking),
        "required_volumes": _required_import_volumes(app, data_dependencies),
        "caddy_route_changes": {
            "domains": _metadata_domains(metadata),
            "routes": routes,
            "operation": "stage generated snippets before validation",
        },
        "data_restore_commands": _import_restore_commands(data_dependencies),
        "source_artifacts": source_artifacts,
        "env_secret_refs_needed": _metadata_env_shape(metadata),
        "post_import_checks": _metadata_verification_checks(metadata),
        "warnings": warnings,
        "blockers": blockers,
        "can_apply": mode == "rehearsal" and not blockers and bool(app),
        "apply_supported": mode == "rehearsal",
        "confirmation_required_for_future_apply": True,
        "read_only": True,
        "secrets_redacted": True,
        "summary": f"Import plan for {app or 'unknown app'} {environment}: {len(blockers)} blocker(s), {len(warnings)} warning(s).",
    }
    plan["confirmation_token"] = import_plan_token(plan)
    # Additive policy evaluation under `checks` only (never top-level status).
    plan["checks"] = [
        policy_check_entry(
            "app.import.apply",
            app,
            environment,
            {
                "confirmation_required": True,
                "rollback_available": mode == "rehearsal",
                "readiness_clean": not blockers,
                "json_receipts": True,
            },
            runtime_root=runtime_root,
        )
    ]
    _merge_plan_envelope(
        plan,
        "app.import.plan",
        app,
        str(environment),
        risk="high",
        exact_command=f"ship app import apply {source} --mode {mode} --runtime-root {runtime_root} --confirm {plan['confirmation_token']}",
    )
    return plan


def import_apply(
    source: Path,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    mode: str = "rehearsal",
    confirm: Optional[str] = None,
    ophelia_root: Path = REPO_ROOT,
) -> Dict[str, object]:
    started_at = _utc_now()
    plan = import_plan(source, runtime_root=runtime_root, mode=mode, ophelia_root=ophelia_root)
    blockers = _as_issues(plan.get("blockers", []))
    warnings = _as_issues(plan.get("warnings", []))
    expected = plan.get("confirmation_token")
    app = str(plan.get("app") or "unknown")
    environment = str(plan.get("environment") or "unknown")
    if mode != "rehearsal":
        blockers.append(schema_issue("import_apply_mode_unsupported", "Only rehearsal import apply is implemented."))
    if not isinstance(confirm, str) or not confirm:
        blockers.append(schema_issue("confirmation_token_missing", "Import apply requires a confirmation token from app import plan."))
    elif confirm != expected:
        blockers.append(schema_issue("confirmation_token_mismatch", "Import apply confirmation token does not match the current plan."))
    if app == "unknown":
        blockers.append(schema_issue("app_unknown", "Import source did not identify an app."))
    if blockers:
        return receipt_envelope(
            EXPORT_RECEIPT_TYPES["import_apply"],
            app,
            environment,
            "blocked",
            started_at,
            _utc_now(),
            artifacts=_artifacts_from_plan(plan),
            checks=plan.get("checks", []) if isinstance(plan.get("checks"), list) else [],
            rollback={"available": False, "note": "Import apply was blocked before writing a preview."},
            plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
            blockers=blockers,
            warnings=warnings,
            inputs_redacted=True,
            secrets_redacted=True,
        )

    preview_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    preview_root = runtime_root / "apps" / app / "import-previews" / preview_id
    preview_root.mkdir(parents=True, exist_ok=False)
    _write_json(preview_root / "import-plan.json", plan)
    _write_json(preview_root / "source-artifacts.json", plan.get("source_artifacts", {}))
    _write_json(preview_root / "env-required.json", plan.get("env_secret_refs_needed", []))
    _write_json(preview_root / "data-restore-commands.json", plan.get("data_restore_commands", []))
    receipt = receipt_envelope(
        EXPORT_RECEIPT_TYPES["import_apply"],
        app,
        environment,
        "succeeded",
        started_at,
        _utc_now(),
        artifacts=[
            artifact(str(preview_root), "import-preview", "Isolated rehearsal import preview", present=True),
            artifact(str(preview_root / "import-plan.json"), "import-plan", present=True),
            artifact(str(preview_root / "source-artifacts.json"), "source-artifacts", present=True),
        ],
        checks=[
            {"name": "confirmation_token", "ok": True, "message": "Matched current import plan."},
            {"name": "active_runtime_unchanged", "ok": True, "message": "Only import preview artifacts were written."},
            {"name": "data_restore_not_executed", "ok": True, "message": "Data restore commands were recorded but not executed."},
        ],
        rollback={"available": True, "note": "Delete the import preview directory if this rehearsal preview is no longer needed."},
        plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
        warnings=warnings,
        preview_path=str(preview_root),
        mode=mode,
        inputs_redacted=True,
        secrets_redacted=True,
    )
    _write_json(preview_root / "receipts" / "import-apply.json", receipt)
    _write_json(runtime_root / "apps" / app / "receipts" / f"{receipt['operation_id']}.json", receipt)
    return receipt


def export_plan_token(plan: Dict[str, object]) -> str:
    return _token(
        "app.export.create",
        {
            "app": plan.get("app"),
            "environment": plan.get("environment"),
            "app_release_id": plan.get("app_release_id"),
            "data_dependencies": plan.get("data_dependencies"),
            "artifact_paths": plan.get("artifact_paths"),
            "include_postgres": plan.get("include_postgres"),
        },
    )


def import_plan_token(plan: Dict[str, object]) -> str:
    return _token(
        "app.import.apply",
        {
            "app": plan.get("app"),
            "environment": plan.get("environment"),
            "source": plan.get("source"),
            "mode": plan.get("mode"),
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
            required = bool(desired_entry.get("required", True))
            if not runtime_present:
                status = "missing" if required else "optional_missing"
            elif required and _is_placeholder(runtime[key]):
                status = "placeholder"
            else:
                status = "present"
        entries.append(
            {
                "key": key,
                "status": status,
                "runtime_present": runtime_present,
                "required": bool(desired_entry.get("required", True)) if desired_entry is not None else False,
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
    # A successful backup-verification receipt (Phase 12) is a stronger restore
    # drill: it satisfies the restore_drill factor below. This is purely
    # additive; apps without any verification receipt keep their exact prior
    # restore_drills list and score.
    latest_verification = _latest_successful_backup_verification(runtime_root, manifest.app, resolved_environment)
    restore_drill_satisfied = bool(restore_drills) or latest_verification is not None
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
    warnings.extend(_route_warning_issues(route_report, manifest))
    if not release:
        blockers.append(schema_issue("release_missing", "No active or latest release metadata is present.", "release.json"))
    if manifest.data.backups and manifest.data.backups.restore_drill_required and not restore_drill_satisfied:
        blockers.append(schema_issue("restore_drill_missing", "No restore drill receipt is recorded.", "restore-drills"))

    checks = [
        {"name": "pack_validation", "ok": validation["ok"], "message": validation["summary"]},
        {"name": "env_shape", "ok": not any(item["status"] in {"missing", "placeholder"} for item in env_report["entries"] if item["required"]), "message": "Env values redacted."},
        {"name": "backup_status", "ok": not backup_report["blockers"], "message": backup_report["freshness"]["status"]},
        {"name": "route_conflicts", "ok": not route_conflict_issues, "message": route_report["summary"]},
        {"name": "release_metadata", "ok": bool(release), "message": str(release.get("release_id") if release else "missing")},
        {
            "name": "restore_drill",
            "ok": restore_drill_satisfied,
            "message": f"{len(restore_drills)} drill receipt(s)"
            + (f"; backup verification {latest_verification.get('verify_id') or latest_verification.get('receipt_id')}" if latest_verification else ""),
        },
    ]
    score = portability_score(
        manifest,
        blockers,
        warnings,
        checks,
        env_report,
        backup_report,
        restore_drills,
        restore_drill_satisfied=restore_drill_satisfied,
    )
    # Score must never hide blockers: readiness stays blocked whenever any
    # blocker exists, regardless of how high the score is.
    level = "blocked" if blockers else "warning" if warnings else "ready"

    # Additive enrichment: attach a typed remediation to every blocker/warning
    # whose code is known, preserving the existing code/message/path keys.
    blockers = _enrich_findings(blockers, manifest.app, resolved_environment)
    warnings = _enrich_findings(warnings, manifest.app, resolved_environment)
    next_actions = _readiness_next_actions(blockers, warnings)
    score_details = _readiness_score_details(score)
    source_reports = _readiness_source_reports(
        env_report,
        backup_report,
        route_report,
        restore_drills,
        manifest.app,
        resolved_environment,
        runtime_root,
        resolution.manifest_path,
        latest_verification=latest_verification,
    )

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
        networking=manifest.to_lock_dict().get("networking", {}),
        compose_networks=compose_network_summary(manifest),
        release={"present": bool(release), "release_id": release.get("release_id") if release else None},
        restore_drill_receipts=restore_drills,
        next_actions=next_actions,
        score_details=score_details,
        source_reports=source_reports,
    )


def _enrich_findings(
    findings: List[Dict[str, str]],
    app: str,
    environment: str,
) -> List[Dict[str, str]]:
    """Return enriched copies of findings with a remediation when one is known.

    Existing keys (``code``/``message``/``path``) are preserved; only an extra
    ``remediation`` key is added when :func:`remediation_for` returns one.
    """
    enriched: List[Dict[str, str]] = []
    for finding in findings:
        code = str(finding.get("code", "")) if isinstance(finding, dict) else ""
        remediation = remediation_for(code, app=app, environment=environment)
        if remediation is None:
            enriched.append(dict(finding) if isinstance(finding, dict) else finding)
        else:
            enriched.append(attach_remediation(finding, remediation))
    return enriched


def _readiness_next_actions(
    blockers: List[Dict[str, str]],
    warnings: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """Priority-sorted, deterministic next actions from enriched findings.

    Blockers come before warnings; within each group findings keep their
    aggregation order. Only findings that carry a remediation with at least one
    command contribute an action, derived from that remediation's first command.
    """
    actions: List[Dict[str, str]] = []
    for area, group in (("blocker", blockers), ("warning", warnings)):
        for finding in group:
            if not isinstance(finding, dict):
                continue
            remediation = finding.get("remediation")
            if not isinstance(remediation, dict):
                continue
            commands = remediation.get("commands")
            if not isinstance(commands, list) or not commands:
                continue
            actions.append(
                {
                    "code": str(finding.get("code", "")),
                    "area": area,
                    "command": str(commands[0]),
                    "summary": str(remediation.get("summary", "")),
                }
            )
    return actions


def _readiness_score_details(score: Dict[str, object]) -> Dict[str, Dict[str, object]]:
    """Roll the score factors up by category.

    For each category present in ``factors`` the sum of earned points and the
    sum of weights are reported. By construction the sum of all ``points`` equals
    the top-level score and the sum of all ``max_points`` equals 100.
    """
    factors = score.get("factors") if isinstance(score.get("factors"), list) else []
    details: Dict[str, Dict[str, object]] = {}
    for factor in factors:
        if not isinstance(factor, dict):
            continue
        category = str(factor.get("category", "uncategorized"))
        weight = int(factor.get("points", 0) or 0)
        earned = weight if bool(factor.get("ok")) else 0
        bucket = details.setdefault(
            category,
            {"points": 0, "max_points": 0, "ok_factors": 0, "total_factors": 0},
        )
        bucket["points"] = int(bucket["points"]) + earned
        bucket["max_points"] = int(bucket["max_points"]) + weight
        bucket["total_factors"] = int(bucket["total_factors"]) + 1
        if bool(factor.get("ok")):
            bucket["ok_factors"] = int(bucket["ok_factors"]) + 1
    for category, bucket in details.items():
        total = int(bucket["total_factors"])
        ok = int(bucket["ok_factors"])
        bucket["reason"] = (
            f"{category}: {ok}/{total} factor(s) ok, "
            f"{bucket['points']}/{bucket['max_points']} point(s) earned."
        )
    return details


def _readiness_source_reports(
    env_report: Dict[str, object],
    backup_report: Dict[str, object],
    route_report: Dict[str, object],
    restore_drills: List[Dict[str, object]],
    app: str,
    environment: str,
    runtime_root: Path,
    manifest_path: Optional[Path],
    latest_verification: Optional[Dict[str, object]] = None,
) -> Dict[str, Dict[str, object]]:
    """Compact, redaction-safe pointers to the sub-reports readiness consumed.

    The secrets audit is computed by calling :func:`ophelia.secrets_audit.secrets_audit`
    (imported lazily to avoid an import cycle) so there is no duplicate env-scan
    logic. Only status/counts and names are surfaced; values stay redacted.
    """
    from .secrets_audit import secrets_audit  # local import avoids a cycle

    env_entries = env_report.get("entries") if isinstance(env_report.get("entries"), list) else []
    required_missing = [
        entry
        for entry in env_entries
        if isinstance(entry, dict) and entry.get("required") and entry.get("status") in {"missing", "placeholder"}
    ]
    freshness = backup_report.get("freshness") if isinstance(backup_report.get("freshness"), dict) else {}
    conflicts = route_report.get("conflicts") if isinstance(route_report.get("conflicts"), list) else []

    try:
        audit = secrets_audit(app, environment=environment, runtime_root=runtime_root)
        audit_keys = audit.get("keys") if isinstance(audit.get("keys"), list) else []
        secrets_pointer: Dict[str, object] = {
            "status": audit.get("status"),
            "key_count": len(audit_keys),
            "missing_required": sum(
                1 for key in audit_keys if isinstance(key, dict) and key.get("required") and not key.get("present")
            ),
            "values_redacted": True,
        }
    except Exception:  # secrets audit is best-effort; never block readiness on it
        secrets_pointer = {"status": "unavailable", "values_redacted": True}

    return {
        "env_shape": {
            "status": env_report.get("status"),
            "key_count": len(env_entries),
            "required_missing": len(required_missing),
            "values_redacted": True,
        },
        "backup_status": {
            "status": backup_report.get("status"),
            "freshness": freshness.get("status"),
            "backup_count": backup_report.get("backup_count"),
        },
        "route_conflicts": {
            "status": route_report.get("status"),
            "conflict_count": len(conflicts),
        },
        "secrets_audit": secrets_pointer,
        "restore_drill": {
            "receipt_count": len(restore_drills),
            "status": "ok" if (restore_drills or latest_verification) else "missing",
        },
        "backup_verification": {
            "status": (latest_verification.get("status") if latest_verification else "missing"),
            "verify_id": (
                latest_verification.get("verify_id") or latest_verification.get("receipt_id")
                if latest_verification
                else None
            ),
            "backup_id": (latest_verification.get("backup_id") if latest_verification else None),
        },
    }


def portability_score(
    manifest: Manifest,
    blockers: List[Dict[str, str]],
    warnings: List[Dict[str, str]],
    checks: List[Dict[str, object]],
    env_report: Dict[str, object],
    backup_report: Dict[str, object],
    restore_drills: List[Dict[str, object]],
    restore_drill_satisfied: Optional[bool] = None,
) -> Dict[str, object]:
    # ``restore_drill_satisfied`` lets a successful backup-verification receipt
    # (Phase 12) satisfy the restore_drill factor. When omitted, behavior is
    # exactly the legacy ``bool(restore_drills)`` so existing callers/scores are
    # unchanged.
    if restore_drill_satisfied is None:
        restore_drill_satisfied = bool(restore_drills)
    score = 100
    factors: List[Dict[str, object]] = []

    def factor(name: str, category: str, ok: bool, message: str) -> None:
        nonlocal score
        points = READINESS_SCORE_WEIGHTS[name]
        if not ok:
            score -= points
        factors.append({"name": name, "category": category, "points": points, "ok": ok, "message": message})

    factor("pack_metadata", "runtime", bool(manifest.pack.portability), "Pack portability is declared.")
    factor("explicit_data_contracts", "data", not _inferred_data_contracts(manifest), "Data contracts are explicit.")
    factor("env_ready", "secrets", not any(item["status"] in {"missing", "placeholder"} for item in env_report["entries"] if item["required"]), "Required env keys are present.")
    factor("backup_fresh", "backup", not backup_report["blockers"], "Backup status has no blockers.")
    factor("restore_drill", "restore", restore_drill_satisfied or not (manifest.data.backups and manifest.data.backups.restore_drill_required), "Restore drill or backup-verification receipt is recorded when required.")
    images = image_references(manifest)
    factor("image_digest", "runtime", bool(images) and all("@sha256:" in image for image in images.values()), "Images are digest-pinned.")
    factor("checks_pass", "runtime", all(bool(check.get("ok")) for check in checks if check.get("name") != "restore_drill"), "Readiness checks passed.")
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
        "- Validate pack: `./cli/ship pack validate <manifest> --json`",
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
    include_manifest: bool = False,
    manifest_kind: str = "service",
    domain: Optional[str] = None,
    image: Optional[str] = None,
    port: int = 8080,
    static_root: str = "public",
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
    blockers = _pack_init_manifest_blockers(
        include_manifest=include_manifest,
        manifest_kind=manifest_kind,
        domain=domain,
        image=image,
        port=port,
        static_root=static_root,
        root=root,
    )
    if include_manifest and not blockers:
        files[root / ".ophelia.yml"] = _pack_init_manifest_file(
            app=app,
            environment=environment,
            critical=critical,
            postgres=postgres,
            redis=redis,
            uploads=uploads,
            manifest_kind=manifest_kind,
            domain=str(domain),
            image=image,
            port=port,
            static_root=static_root,
        )
        if manifest_kind == "static":
            files[root / static_root / "index.html"] = _pack_init_static_index(app)
    planned = []
    written = []
    for path, content in files.items():
        exists = path.exists()
        planned.append(
            {
                "path": str(path),
                "exists": exists,
                "action": "overwrite" if exists and force else "create" if not exists else "skip",
                "executable": _pack_init_executable(path),
            }
        )
        if write and exists and not force:
            blockers.append(schema_issue("file_exists", f"Refusing to overwrite existing file: {path}", str(path)))
    if write and not blockers:
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            if _pack_init_executable(path):
                path.chmod(0o755)
            written.append(str(path))

    snippet = _pack_init_manifest_snippet(
        app,
        environment,
        critical,
        postgres,
        redis,
        uploads,
        manifest_kind=manifest_kind,
        static_root=static_root,
    )
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
        include_manifest=include_manifest,
        manifest_kind=manifest_kind,
        manifest_path=str(root / ".ophelia.yml") if include_manifest else None,
    )


def _pack_init_executable(path: Path) -> bool:
    return path.suffix == ".sh" and any(part in {"checks", "hooks"} for part in path.parts)


def _pack_init_manifest_blockers(
    *,
    include_manifest: bool,
    manifest_kind: str,
    domain: Optional[str],
    image: Optional[str],
    port: int,
    static_root: str,
    root: Path,
) -> List[Dict[str, str]]:
    if not include_manifest:
        return []
    blockers: List[Dict[str, str]] = []
    if manifest_kind not in {"service", "static"}:
        blockers.append(schema_issue("manifest_kind_invalid", "`--kind` must be `service` or `static`."))
    if not domain:
        blockers.append(schema_issue("manifest_domain_required", "`--domain` is required with `--include-manifest`."))
    if manifest_kind == "service" and not image:
        blockers.append(schema_issue("manifest_image_required", "`--image` is required for service manifests."))
    if isinstance(port, bool) or not isinstance(port, int) or port <= 0:
        blockers.append(schema_issue("manifest_port_invalid", "`--port` must be a positive integer."))
    if manifest_kind == "static":
        static_root_path = Path(static_root)
        if static_root_path.is_absolute() or ".." in static_root_path.parts or not str(static_root).strip():
            blockers.append(schema_issue("manifest_static_root_invalid", "`--static-root` must be a relative path inside --directory."))
        elif (root / static_root_path).resolve().is_relative_to(root.resolve()) is False:
            blockers.append(schema_issue("manifest_static_root_invalid", "`--static-root` must stay inside --directory."))
    return blockers


def _pack_init_manifest_file(
    *,
    app: str,
    environment: Optional[str],
    critical: bool,
    postgres: bool,
    redis: bool,
    uploads: bool,
    manifest_kind: str,
    domain: str,
    image: Optional[str],
    port: int,
    static_root: str,
) -> str:
    lines = ["version: 1", f"app: {app}"]
    if environment:
        lines.append(f"environment: {environment}")
    lines.append(f"kind: {manifest_kind}")
    if manifest_kind == "service":
        lines.extend(
            [
                "",
                "services:",
                "  web:",
                f"    image: {image}",
                f"    port: {port}",
                "    healthcheck:",
                "      path: /health",
                "",
                "routes:",
                f"  - domain: {domain}",
                "    service: web",
                "",
                "verify:",
                "  - name: health",
                f"    url: https://{domain}/health",
            ]
        )
    else:
        lines.extend(
            [
                f"static_root: {static_root}",
                "",
                "routes:",
                f"  - domain: {domain}",
                "",
                "verify:",
                "  - name: health",
                f"    url: https://{domain}/",
            ]
        )
    lines.extend(["", _pack_init_manifest_snippet(app, environment, critical, postgres, redis, uploads, manifest_kind=manifest_kind, static_root=static_root).rstrip()])
    return "\n".join(lines) + "\n"


def _pack_init_static_index(app: str) -> str:
    return (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "  <head>\n"
        "    <meta charset=\"utf-8\">\n"
        f"    <title>{app}</title>\n"
        "  </head>\n"
        "  <body>\n"
        f"    <main><h1>{app}</h1></main>\n"
        "  </body>\n"
        "</html>\n"
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
    receipt_path = Path(str(receipt_id)).expanduser()
    if receipt_path.exists() and receipt_path.is_file():
        payload = _read_json(receipt_path)
        if payload:
            resolved_id = _receipt_id(receipt_path, payload)
            return report_envelope(
                "receipts.show",
                payload.get("app") if isinstance(payload.get("app"), str) else None,
                payload.get("environment") if isinstance(payload.get("environment"), str) else None,
                f"Receipt {resolved_id}.",
                artifacts=[artifact(str(receipt_path), "receipt", present=True)],
                receipt=payload,
                receipt_id=resolved_id,
                requested_ref=receipt_id,
            )
        return report_envelope(
            "receipts.show",
            None,
            None,
            f"Receipt path is unreadable: {receipt_id}.",
            blockers=[schema_issue("receipt_unreadable", f"Could not read receipt JSON: {receipt_path}")],
            receipt_id=receipt_id,
        )
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
    source: Optional[Path] = None,
) -> Dict[str, object]:
    readiness = app_readiness_report(app, environment, runtime_root, manifest_path)
    source_plan = import_plan(source, runtime_root=runtime_root, mode="rehearsal") if source is not None else None
    blockers = _restore_drill_plan_blockers(readiness.get("blockers", []))
    warnings = list(readiness.get("warnings", []))
    checks = list(readiness.get("checks", []))
    if source is None:
        blockers.append(schema_issue("restore_source_missing", "Restore drill apply requires an export bundle source."))
    elif source_plan is not None:
        blockers.extend(_as_issues(source_plan.get("blockers", [])))
        warnings.extend(_as_issues(source_plan.get("warnings", [])))
        checks.append({"name": "source_import_plan", "ok": not source_plan.get("blockers"), "message": source_plan.get("summary")})
    token = _token(
        "app.restore-drill.apply",
        {"app": app, "environment": readiness.get("environment"), "source": str(source) if source is not None else None},
    )
    return plan_envelope(
        "app.restore-drill.plan",
        app,
        str(readiness.get("environment") or environment or "unknown"),
        f"Restore drill plan for {app}.",
        blockers=blockers,
        warnings=warnings,
        checks=checks,
        artifacts=[],
        confirmation_required=True,
        confirmation_token=token,
        exact_apply_input={
            "command": (
                f"ship app restore-drill apply {app} --environment {readiness.get('environment') or environment or 'unknown'} "
                f"{f'--source {source} ' if source is not None else ''}--confirm {token}"
            )
        },
        risk="high",
        readiness=readiness,
        source=str(source) if source is not None else None,
        source_plan=source_plan,
        apply_supported=source is not None,
    )


def restore_drill_apply(
    app: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest_path: Optional[Path] = None,
    source: Optional[Path] = None,
    confirm: Optional[str] = None,
) -> Dict[str, object]:
    started_at = _utc_now()
    plan = restore_drill_plan(app, environment, runtime_root, manifest_path, source)
    blockers = _as_issues(plan.get("blockers", []))
    warnings = _as_issues(plan.get("warnings", []))
    expected = plan.get("confirmation_token")
    resolved_environment = str(plan.get("environment") or environment or "unknown")
    if source is None:
        blockers.append(schema_issue("restore_source_missing", "Restore drill apply requires --source."))
    if not isinstance(confirm, str) or not confirm:
        blockers.append(schema_issue("confirmation_token_missing", "Restore drill apply requires a confirmation token from app restore-drill plan."))
    elif confirm != expected:
        blockers.append(schema_issue("confirmation_token_mismatch", "Restore drill confirmation token does not match the current plan."))
    if blockers:
        return receipt_envelope(
            "app.restore-drill.apply",
            app,
            resolved_environment,
            "blocked",
            started_at,
            _utc_now(),
            artifacts=_artifacts_from_plan(plan),
            checks=plan.get("checks", []) if isinstance(plan.get("checks"), list) else [],
            rollback={"available": False, "note": "Restore drill was blocked before writing artifacts."},
            plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
            blockers=blockers,
            warnings=warnings,
            inputs_redacted=True,
            secrets_redacted=True,
        )

    drill_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    drill_root = runtime_root / "apps" / app / "restore-drills" / drill_id
    drill_root.mkdir(parents=True, exist_ok=False)
    artifact_checks = _restore_drill_artifact_checks(source) if source is not None else []
    status = "succeeded" if all(bool(check.get("ok")) for check in artifact_checks) else "failed"
    _write_json(drill_root / "restore-drill-plan.json", plan)
    _write_json(drill_root / "artifact-checks.json", artifact_checks)
    receipt = receipt_envelope(
        "app.restore-drill.apply",
        app,
        resolved_environment,
        status,
        started_at,
        _utc_now(),
        artifacts=[
            artifact(str(drill_root), "restore-drill", "Isolated restore drill artifacts", present=True),
            artifact(str(drill_root / "restore-drill-plan.json"), "restore-drill-plan", present=True),
            artifact(str(drill_root / "artifact-checks.json"), "restore-drill-checks", present=True),
        ],
        checks=[
            {"name": "confirmation_token", "ok": True, "message": "Matched current restore drill plan."},
            {"name": "active_runtime_unchanged", "ok": True, "message": "No active runtime files were modified."},
            *artifact_checks,
        ],
        rollback={"available": True, "note": "Delete the restore drill directory if this rehearsal artifact is no longer needed."},
        plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
        warnings=warnings,
        drill_path=str(drill_root),
        source=str(source) if source is not None else None,
        inputs_redacted=True,
        secrets_redacted=True,
    )
    _write_json(drill_root / "receipts" / "restore-drill-apply.json", receipt)
    _write_json(runtime_root / "apps" / app / "receipts" / f"{receipt['operation_id']}.json", receipt)
    return receipt


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
    warnings = list(readiness.get("warnings", []))
    warnings.append(schema_issue("route_mutation_not_automated", "Cutover apply records a checkpoint receipt but does not mutate Caddy or DNS."))
    token = _token("app.cutover.apply", {"app": app, "environment": readiness.get("environment"), "from": source_host, "to": target_host})
    return plan_envelope(
        "app.cutover.plan",
        app,
        str(readiness.get("environment") or environment or "unknown"),
        f"Cutover plan for {app} from {source_host} to {target_host}.",
        blockers=blockers,
        warnings=warnings,
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
        apply_supported=True,
        route_mutation_supported=False,
    )


def cutover_apply(
    app: str,
    source_host: str,
    target_host: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest_path: Optional[Path] = None,
    confirm: Optional[str] = None,
) -> Dict[str, object]:
    started_at = _utc_now()
    plan = cutover_plan(app, source_host, target_host, environment, runtime_root, manifest_path)
    blockers = _as_issues(plan.get("blockers", []))
    warnings = _as_issues(plan.get("warnings", []))
    expected = plan.get("confirmation_token")
    resolved_environment = str(plan.get("environment") or environment or "unknown")
    if not isinstance(confirm, str) or not confirm:
        blockers.append(schema_issue("confirmation_token_missing", "Cutover apply requires a confirmation token from app cutover plan."))
    elif confirm != expected:
        blockers.append(schema_issue("confirmation_token_mismatch", "Cutover confirmation token does not match the current plan."))
    if blockers:
        return receipt_envelope(
            "app.cutover.apply",
            app,
            resolved_environment,
            "blocked",
            started_at,
            _utc_now(),
            artifacts=_artifacts_from_plan(plan),
            checks=plan.get("checks", []) if isinstance(plan.get("checks"), list) else [],
            rollback={"available": True, "note": "Cutover was blocked before any route or DNS change."},
            plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
            blockers=blockers,
            warnings=warnings,
            source_host=source_host,
            target_host=target_host,
            inputs_redacted=True,
            secrets_redacted=True,
        )
    cutover_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    cutover_root = runtime_root / "apps" / app / "cutovers" / cutover_id
    cutover_root.mkdir(parents=True, exist_ok=False)
    _write_json(cutover_root / "cutover-plan.json", plan)
    receipt = receipt_envelope(
        "app.cutover.apply",
        app,
        resolved_environment,
        "succeeded",
        started_at,
        _utc_now(),
        artifacts=[
            artifact(str(cutover_root), "cutover-checkpoint", "Cutover checkpoint artifacts", present=True),
            artifact(str(cutover_root / "cutover-plan.json"), "cutover-plan", present=True),
        ],
        checks=[
            {"name": "confirmation_token", "ok": True, "message": "Matched current cutover plan."},
            {"name": "active_runtime_unchanged", "ok": True, "message": "No active runtime files were modified."},
            {"name": "route_mutation", "ok": True, "message": "Caddy and DNS were not mutated by this checkpoint apply."},
        ],
        rollback={"available": True, "note": "No route mutation was performed. Rollback is canceling this checkpoint before manual traffic movement."},
        plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
        warnings=warnings,
        source_host=source_host,
        target_host=target_host,
        cutover_path=str(cutover_root),
        route_mutation_performed=False,
        inputs_redacted=True,
        secrets_redacted=True,
    )
    _write_json(cutover_root / "receipts" / "cutover-apply.json", receipt)
    _write_json(runtime_root / "apps" / app / "receipts" / f"{receipt['operation_id']}.json", receipt)
    return receipt


def traffic_plan(
    app: str,
    source_host: str,
    target_host: str,
    target_origin: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest_path: Optional[Path] = None,
    dns_provider: str = "manual",
    caddy_provider: str = "manual",
    ttl: int = 300,
    provider_config: Optional[Path] = None,
    execute_provider_mutation: bool = False,
    target_health_url: Optional[str] = None,
    run_target_health: bool = False,
    target_health_timeout: float = 10.0,
    target_health_expect_status: int = 200,
) -> Dict[str, object]:
    readiness = app_readiness_report(app, environment, runtime_root, manifest_path)
    resolution = resolve_app_manifest(app, environment, manifest_path=manifest_path)
    manifest = resolution.manifest
    resolved_environment = str(readiness.get("environment") or environment or "unknown")
    blockers = _as_issues(readiness.get("blockers", []))
    warnings = _as_issues(readiness.get("warnings", []))
    blockers.extend(schema_issue("manifest_unresolved", message) for message in resolution.blockers)
    warnings.extend(schema_issue("manifest_warning", message) for message in resolution.warnings)

    origin_issue = _target_origin_issue(target_origin)
    if origin_issue is not None:
        blockers.append(origin_issue)
    ttl, ttl_valid = _strict_positive_int(ttl, default=300)
    if not ttl_valid:
        blockers.append(schema_issue("ttl_invalid", "`ttl` must be greater than zero.", "ttl"))
    if dns_provider == "cloudflare" and not validate_ttl(ttl)[1]:
        blockers.append(
            schema_issue(
                "cloudflare_ttl_invalid",
                "Cloudflare TTL must be 1 for automatic or between 30 and 86400 seconds.",
                "ttl",
            )
        )

    traffic_changes = _traffic_changes(manifest, source_host, target_host, target_origin, dns_provider, caddy_provider, ttl)
    provider_execution = _traffic_provider_execution_plan(
        app,
        resolved_environment,
        traffic_changes,
        dns_provider,
        caddy_provider,
        provider_config,
        execute_provider_mutation,
    )
    blockers.extend(_as_issues(provider_execution.get("blockers", [])))
    warnings.extend(_as_issues(provider_execution.get("warnings", [])))
    _apply_traffic_provider_capabilities(traffic_changes, provider_execution)
    # Phase 13: run the supplied provider config through the single canonical
    # validator before an apply token can be produced. A blocked provider config
    # surfaces its blockers here and (later) withholds the confirmation token.
    provider_config_validation = _traffic_provider_config_validation(
        provider_config,
        execute_provider_mutation,
        blockers,
        warnings,
    )
    provider_config_digest = _traffic_provider_config_digest(provider_config)
    target_health = _traffic_target_health_plan(
        target_health_url,
        run_target_health,
        target_health_timeout,
        target_health_expect_status,
    )
    blockers.extend(_as_issues(target_health.get("blockers", [])))
    warnings.extend(_as_issues(target_health.get("warnings", [])))
    if manifest is not None and not traffic_changes:
        blockers.append(schema_issue("traffic_routes_missing", "Manifest has no routes to move.", "routes"))

    if execute_provider_mutation:
        warnings.append(
            schema_issue(
                "traffic_provider_execution_requested",
                "Traffic apply will execute only the configured providers after confirmation; Cloudflare DNS and Caddy reload each require their explicit provider gates.",
            )
        )
    else:
        warnings.append(
            schema_issue(
                "traffic_apply_checkpoint_by_default",
                "Traffic apply writes a checkpoint receipt unless provider execution is explicitly planned with --execute-provider-mutation.",
            )
        )
    # Phase 13: the canonical token input includes EVERY behavior-changing field
    # so a stale or mismatched plan can never confirm a different apply. The
    # provider config contributes only a digest (a hash of the validated config),
    # never the raw secrets. `traffic_apply` recomputes this identical input by
    # re-running `traffic_plan`, so a plan->apply round trip with the returned
    # token still validates.
    token = _token(
        "app.traffic.apply",
        {
            "app": app,
            "environment": resolved_environment,
            "from": source_host,
            "to": target_host,
            "target_origin": target_origin,
            "dns_provider": dns_provider,
            "caddy_provider": caddy_provider,
            "ttl": ttl,
            "provider_config": str(provider_config.expanduser()) if provider_config is not None else None,
            "provider_config_digest": provider_config_digest,
            "execute_provider_mutation": execute_provider_mutation,
            "target_health_url": target_health_url,
            "run_target_health": run_target_health,
            "target_health_timeout": target_health_timeout,
            "target_health_expect_status": target_health_expect_status,
        },
    )
    exact_parts: List[object] = [
        "ship",
        "app",
        "traffic",
        "apply",
        app,
        "--from",
        source_host,
        "--to",
        target_host,
        "--target-origin",
        target_origin,
    ]
    if resolved_environment in {"dev", "staging", "production"}:
        exact_parts.extend(["--environment", resolved_environment])
    exact_parts.extend(["--dns-provider", dns_provider, "--caddy-provider", caddy_provider, "--ttl", ttl])
    if provider_config is not None:
        exact_parts.extend(["--provider-config", provider_config.expanduser()])
    if execute_provider_mutation:
        exact_parts.append("--execute-provider-mutation")
    if target_health_url:
        exact_parts.extend(["--target-health-url", target_health_url])
    if run_target_health:
        exact_parts.append("--run-target-health")
    exact_parts.extend(
        [
            "--target-health-timeout",
            target_health_timeout,
            "--target-health-expect-status",
            target_health_expect_status,
            "--confirm",
            token,
        ]
    )
    exact_command = _shell_command(exact_parts)
    provider_changes = _traffic_provider_changes(traffic_changes, provider_execution, source_host, target_host)
    # Phase 13: the policy context reflects whether a target health check and a
    # rollback path are actually present. A configured-and-passing health check
    # (or a configured URL when not gated) counts as a target health check; a
    # configured URL that was executed and FAILED does not.
    target_health_present = _traffic_target_health_present(target_health)
    rollback_available = True
    policy_context = {
        "target_health_check": target_health_present,
        "rollback_available": rollback_available,
        "confirmation_required": True,
        "plan_exists": True,
        "json_receipts": True,
        "readiness_clean": not blockers,
    }
    policy_check = policy_check_entry(
        "app.traffic.apply",
        app,
        resolved_environment,
        policy_context,
        runtime_root=runtime_root,
    )
    # Phase 13: for a PRODUCTION apply, the policy result is a real gate. A policy
    # blocker (e.g. missing target health check) is surfaced as a plan blocker and
    # withholds the apply token. For non-production environments the policy result
    # stays additive (under `checks`) and never blocks, preserving prior behavior.
    policy_result = policy_check.get("result") if isinstance(policy_check, dict) else None
    if (
        resolved_environment == "production"
        and isinstance(policy_result, dict)
        and policy_result.get("status") == "blocked"
    ):
        for finding in _as_issues(policy_result.get("blockers", [])):
            blockers.append(
                schema_issue(
                    "traffic_policy_blocked",
                    str(finding.get("message") or "Production traffic policy blocked this apply."),
                    "policy",
                )
            )
    # Phase 13: a blocked provider config must withhold the apply token. The
    # config validation already appended its blockers above; recompute `blockers`
    # membership here for clarity.
    provider_config_blocked = bool(
        isinstance(provider_config_validation, dict)
        and provider_config_validation.get("status") == "blocked"
    )
    confirmation_token = None if blockers else token
    return plan_envelope(
        "app.traffic.plan",
        app,
        resolved_environment,
        f"Production traffic automation plan for {app} from {source_host} to {target_host}.",
        blockers=blockers,
        warnings=warnings,
        checks=[
            *_traffic_checks(readiness, traffic_changes, provider_execution),
            *(target_health.get("checks", []) if isinstance(target_health.get("checks"), list) else []),
            policy_check,
        ],
        artifacts=provider_execution.get("artifacts", []) if isinstance(provider_execution.get("artifacts"), list) else [],
        changes=provider_changes,
        confirmation_required=True,
        confirmation_token=confirmation_token,
        exact_apply_input={"command": exact_command},
        risk="critical",
        source_host=source_host,
        target_host=target_host,
        target_origin=target_origin,
        dns_provider=dns_provider,
        caddy_provider=caddy_provider,
        ttl=ttl,
        provider_config=str(provider_config.expanduser()) if provider_config is not None else None,
        provider_config_digest=provider_config_digest,
        provider_config_validation=provider_config_validation,
        provider_config_blocked=provider_config_blocked,
        provider_execution=provider_execution,
        target_health=target_health,
        target_health_present=target_health_present,
        readiness=readiness,
        traffic_changes=traffic_changes,
        preflight_gates=[
            "readiness has no blockers",
            "fresh backup or explicit backup waiver is recorded",
            "restore drill receipt exists when required",
            "route/domain conflict scan is clean",
            "target app health verification has passed",
            "source freeze/read-only step is reviewed",
            "DNS TTL and rollback window are accepted",
        ],
        rollback={
            "available": True,
            "note": "Before provider mutation, rollback is canceling this traffic plan. After file provider mutation, rollback must restore the prior records and staged Caddy files captured by the receipt.",
        },
        apply_supported=True,
        can_apply=not blockers,
        traffic_mutation_supported=bool(provider_execution.get("mutation_supported")),
        provider_mutation_supported=bool(provider_execution.get("mutation_supported")),
        provider_mutation_requested=execute_provider_mutation,
    )


def traffic_apply(
    app: str,
    source_host: str,
    target_host: str,
    target_origin: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest_path: Optional[Path] = None,
    dns_provider: str = "manual",
    caddy_provider: str = "manual",
    ttl: int = 300,
    confirm: Optional[str] = None,
    provider_config: Optional[Path] = None,
    execute_provider_mutation: bool = False,
    target_health_url: Optional[str] = None,
    run_target_health: bool = False,
    target_health_timeout: float = 10.0,
    target_health_expect_status: int = 200,
) -> Dict[str, object]:
    started_at = _utc_now()
    plan = traffic_plan(
        app,
        source_host,
        target_host,
        target_origin,
        environment=environment,
        runtime_root=runtime_root,
        manifest_path=manifest_path,
        dns_provider=dns_provider,
        caddy_provider=caddy_provider,
        ttl=ttl,
        provider_config=provider_config,
        execute_provider_mutation=execute_provider_mutation,
        target_health_url=target_health_url,
        run_target_health=run_target_health,
        target_health_timeout=target_health_timeout,
        target_health_expect_status=target_health_expect_status,
    )
    blockers = _as_issues(plan.get("blockers", []))
    warnings = _as_issues(plan.get("warnings", []))
    expected = plan.get("confirmation_token")
    resolved_environment = str(plan.get("environment") or environment or "unknown")
    if not isinstance(confirm, str) or not confirm:
        blockers.append(schema_issue("confirmation_token_missing", "Traffic apply requires a confirmation token from app traffic plan."))
    elif confirm != expected:
        blockers.append(schema_issue("confirmation_token_mismatch", "Traffic confirmation token does not match the current plan."))
    if blockers:
        return receipt_envelope(
            "app.traffic.apply",
            app,
            resolved_environment,
            "blocked",
            started_at,
            _utc_now(),
            artifacts=_artifacts_from_plan(plan),
            checks=plan.get("checks", []) if isinstance(plan.get("checks"), list) else [],
            rollback={"available": True, "note": "Traffic apply was blocked before any provider mutation."},
            plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
            blockers=blockers,
            warnings=warnings,
            source_host=source_host,
            target_host=target_host,
            target_origin=target_origin,
            inputs_redacted=True,
            secrets_redacted=True,
        )

    traffic_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    traffic_root = runtime_root / "apps" / app / "traffic" / traffic_id
    traffic_root.mkdir(parents=True, exist_ok=False)
    _write_json(traffic_root / "traffic-plan.json", plan)
    provider_mutations: List[Dict[str, object]] = []
    if execute_provider_mutation:
        provider_mutations = _apply_traffic_provider_mutations(plan, traffic_root)
        _write_json(traffic_root / "traffic-provider-mutations.json", provider_mutations)
    provider_mutation_succeeded = bool(provider_mutations) and all(item.get("status") == "succeeded" for item in provider_mutations)
    provider_mutation_performed = any(
        item.get("status") == "succeeded" or item.get("file_written") or item.get("changes")
        for item in provider_mutations
    )
    provider_mutation_failed = bool(provider_mutations) and not provider_mutation_succeeded
    status = "failed" if provider_mutation_failed else "succeeded"
    provider_artifacts = []
    for item in provider_mutations:
        path = item.get("path")
        if isinstance(path, str):
            provider_artifacts.append(artifact(path, str(item.get("provider") or "traffic-provider"), present=Path(path).exists()))
    receipt = receipt_envelope(
        "app.traffic.apply",
        app,
        resolved_environment,
        status,
        started_at,
        _utc_now(),
        artifacts=[
            artifact(str(traffic_root), "traffic-checkpoint", "Production traffic automation checkpoint", present=True),
            artifact(str(traffic_root / "traffic-plan.json"), "traffic-plan", present=True),
            *(
                [artifact(str(traffic_root / "traffic-provider-mutations.json"), "traffic-provider-mutations", present=True)]
                if provider_mutations
                else []
            ),
            *provider_artifacts,
        ],
        checks=[
            {"name": "confirmation_token", "ok": True, "message": "Matched current traffic plan."},
            {"name": "app_release_unchanged", "ok": True, "message": "No app release files were modified."},
            {
                "name": "provider_mutation",
                "ok": not provider_mutation_failed,
                "message": _traffic_provider_mutation_message(execute_provider_mutation, provider_mutations),
            },
        ],
        rollback={
            "available": True,
            "note": (
                "Provider mutation receipt includes previous record snapshots and prior staged Caddy files for manual rollback."
                if provider_mutations
                else "No traffic provider mutation was performed. Rollback is canceling this traffic checkpoint before manual traffic movement."
            ),
        },
        plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
        warnings=warnings,
        source_host=source_host,
        target_host=target_host,
        target_origin=target_origin,
        dns_provider=dns_provider,
        caddy_provider=caddy_provider,
        ttl=ttl,
        traffic_path=str(traffic_root),
        traffic_changes=plan.get("traffic_changes", []),
        provider_config=str(provider_config.expanduser()) if provider_config is not None else None,
        target_health=plan.get("target_health"),
        provider_mutations=provider_mutations,
        traffic_mutation_performed=provider_mutation_performed,
        provider_mutation_performed=provider_mutation_performed,
        provider_mutation_succeeded=provider_mutation_succeeded,
        inputs_redacted=True,
        secrets_redacted=True,
    )
    _write_json(traffic_root / "receipts" / "traffic-apply.json", receipt)
    _write_json(runtime_root / "apps" / app / "receipts" / f"{receipt['operation_id']}.json", receipt)
    return receipt


def traffic_rollback_plan(
    app: str,
    receipt_id: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    approve_unsafe_delete: bool = False,
) -> Dict[str, object]:
    receipt_payload, receipt_path, receipt_blockers = _load_traffic_receipt(receipt_id, runtime_root, app)
    resolved_environment = str(environment or receipt_payload.get("environment") or "unknown")
    blockers = [schema_issue("receipt_unresolved", message, "receipt_id") for message in receipt_blockers]
    warnings: List[Dict[str, str]] = []
    if receipt_payload:
        if receipt_payload.get("operation") != "app.traffic.apply":
            blockers.append(schema_issue("traffic_receipt_invalid", "Receipt is not an app.traffic.apply receipt.", "receipt_id"))
        if receipt_payload.get("app") not in {None, app}:
            blockers.append(schema_issue("traffic_receipt_app_mismatch", "Receipt app does not match rollback app.", "app"))
        if not receipt_payload.get("provider_mutation_performed"):
            blockers.append(schema_issue("traffic_receipt_no_provider_mutation", "Receipt did not perform provider mutation.", "receipt_id"))

    # Phase 13: rollback reads the prior provider state captured by the forward
    # apply receipt. A rollback that would DELETE a record/file the forward apply
    # CREATED (no prior state) is unsafe; it is refused with `rollback_unsafe_delete`
    # unless `approve_unsafe_delete` is explicitly set.
    rollback_changes = _traffic_rollback_changes(receipt_payload, blockers, approve_unsafe_delete)
    token = _token(
        "app.traffic.rollback.apply",
        {
            "app": app,
            "environment": resolved_environment,
            "receipt_id": receipt_id,
            "approve_unsafe_delete": approve_unsafe_delete,
        },
    )
    exact_parts = ["ship", "app", "traffic", "rollback", "apply", app, "--receipt", receipt_id]
    if resolved_environment in {"dev", "staging", "production"}:
        exact_parts.extend(["--environment", resolved_environment])
    if approve_unsafe_delete:
        exact_parts.append("--approve-unsafe-delete")
    exact_parts.extend(["--confirm", token])
    exact_command = _shell_command(exact_parts)
    return plan_envelope(
        "app.traffic.rollback.plan",
        app,
        resolved_environment,
        f"Traffic rollback plan for {app} from receipt {receipt_id}.",
        blockers=blockers,
        warnings=warnings,
        checks=[
            {"name": "receipt_found", "ok": bool(receipt_payload), "message": str(receipt_path) if receipt_path else "not found"},
            {"name": "provider_rollback_changes", "ok": bool(rollback_changes), "message": f"{len(rollback_changes)} provider rollback group(s)"},
        ],
        artifacts=[artifact(str(receipt_path), "traffic-apply-receipt", present=True)] if receipt_path is not None else [],
        confirmation_required=True,
        confirmation_token=None if blockers else token,
        exact_apply_input={"command": exact_command},
        risk="critical",
        receipt_id=receipt_id,
        receipt_path=str(receipt_path) if receipt_path is not None else None,
        rollback_changes=rollback_changes,
        approve_unsafe_delete=approve_unsafe_delete,
        apply_supported=True,
        can_apply=not blockers,
    )


def traffic_rollback_apply(
    app: str,
    receipt_id: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    confirm: Optional[str] = None,
    approve_unsafe_delete: bool = False,
) -> Dict[str, object]:
    started_at = _utc_now()
    plan = traffic_rollback_plan(app, receipt_id, environment, runtime_root, approve_unsafe_delete)
    blockers = _as_issues(plan.get("blockers", []))
    warnings = _as_issues(plan.get("warnings", []))
    expected = plan.get("confirmation_token")
    resolved_environment = str(plan.get("environment") or environment or "unknown")
    if not isinstance(confirm, str) or not confirm:
        blockers.append(schema_issue("confirmation_token_missing", "Traffic rollback apply requires a confirmation token from app traffic rollback plan."))
    elif confirm != expected:
        blockers.append(schema_issue("confirmation_token_mismatch", "Traffic rollback confirmation token does not match the current plan."))
    if blockers:
        return receipt_envelope(
            "app.traffic.rollback.apply",
            app,
            resolved_environment,
            "blocked",
            started_at,
            _utc_now(),
            artifacts=_artifacts_from_plan(plan),
            checks=plan.get("checks", []) if isinstance(plan.get("checks"), list) else [],
            rollback={"available": False, "note": "Traffic rollback was blocked before provider files were changed."},
            plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
            blockers=blockers,
            warnings=warnings,
            receipt_id=receipt_id,
            inputs_redacted=True,
            secrets_redacted=True,
        )

    rollback_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    rollback_root = runtime_root / "apps" / app / "traffic-rollbacks" / rollback_id
    rollback_root.mkdir(parents=True, exist_ok=False)
    _write_json(rollback_root / "traffic-rollback-plan.json", plan)
    rollback_results = _apply_traffic_provider_rollbacks(
        plan.get("rollback_changes") if isinstance(plan.get("rollback_changes"), list) else []
    )
    _write_json(rollback_root / "traffic-rollback-results.json", rollback_results)
    failed = [item for item in rollback_results if item.get("status") != "succeeded"]
    status = "failed" if failed else "succeeded"
    receipt = receipt_envelope(
        "app.traffic.rollback.apply",
        app,
        resolved_environment,
        status,
        started_at,
        _utc_now(),
        artifacts=[
            artifact(str(rollback_root), "traffic-rollback", present=True),
            artifact(str(rollback_root / "traffic-rollback-plan.json"), "traffic-rollback-plan", present=True),
            artifact(str(rollback_root / "traffic-rollback-results.json"), "traffic-rollback-results", present=True),
        ],
        checks=[
            {"name": "confirmation_token", "ok": True, "message": "Matched current traffic rollback plan."},
            {"name": "provider_rollback", "ok": not failed, "message": f"{len(rollback_results) - len(failed)} succeeded, {len(failed)} failed"},
        ],
        rollback={"available": False, "note": "This receipt is itself a rollback of provider files captured by an earlier traffic apply receipt."},
        plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
        warnings=warnings,
        receipt_id=receipt_id,
        traffic_rollback_path=str(rollback_root),
        rollback_results=rollback_results,
        provider_rollback_performed=not failed,
        inputs_redacted=True,
        secrets_redacted=True,
    )
    _write_json(rollback_root / "receipts" / "traffic-rollback-apply.json", receipt)
    _write_json(runtime_root / "apps" / app / "receipts" / f"{receipt['operation_id']}.json", receipt)
    return receipt


def traffic_status(
    app: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest_path: Optional[Path] = None,
) -> Dict[str, object]:
    """Read-only summary of an app's traffic state from receipts and files.

    Phase 13: combines the latest traffic apply receipt, the latest traffic
    rollback receipt, the route ownership inferred from the manifest/conflict
    scan, the target health status captured on the latest apply receipt (no live
    probe), and any active blockers carried by those receipts. Works WITHOUT any
    provider credentials: it only reads receipts and local files, never the
    network. All values pass through redaction so a credential can never leak.
    """
    runtime_root = Path(runtime_root)
    records = _receipt_records(runtime_root, app=app, environment=environment)
    apply_record = _latest_record_for(records, "app.traffic.apply")
    rollback_record = _latest_record_for(records, "app.traffic.rollback.apply")
    apply_receipt = _read_json(Path(str(apply_record["path"]))) if apply_record else {}
    rollback_receipt = _read_json(Path(str(rollback_record["path"]))) if rollback_record else {}

    resolved_environment = str(
        environment
        or apply_receipt.get("environment")
        or rollback_receipt.get("environment")
        or "unknown"
    )

    # Route ownership from the manifest / conflict scan (read-only, no network).
    route_ownership: List[Dict[str, object]] = []
    manifest_dir = manifest_path.expanduser().parent if manifest_path is not None else REPO_ROOT / "manifests"
    try:
        resolution = resolve_app_manifest(app, environment, manifest_path=manifest_path)
        manifest = resolution.manifest
        if manifest is not None:
            for route in manifest.routes:
                route_ownership.append(
                    {
                        "domain": route.domain,
                        "path": route.path,
                        "path_prefix": route.path_prefix,
                        "app": manifest.app,
                    }
                )
    except Exception:  # best-effort: a missing/invalid manifest must not break status
        manifest = None
    conflicts: Dict[str, object] = {}
    try:
        conflicts = scan_conflicts(manifest_dir, runtime_root=runtime_root)
    except Exception:
        conflicts = {}

    # Target health status as captured on the latest apply receipt (no live probe).
    target_health = apply_receipt.get("target_health") if isinstance(apply_receipt.get("target_health"), dict) else None

    # Current provider plan, if the latest apply receipt embedded one.
    provider_mutations = apply_receipt.get("provider_mutations") if isinstance(apply_receipt.get("provider_mutations"), list) else []
    provider_summary: List[Dict[str, object]] = []
    for mutation in provider_mutations:
        if isinstance(mutation, dict):
            provider_summary.append(
                {
                    "provider": mutation.get("provider"),
                    "status": mutation.get("status"),
                    "file_written": bool(mutation.get("file_written")),
                    "reload_performed": bool(mutation.get("reload_performed")),
                }
            )

    blockers: List[Dict[str, str]] = []
    blockers.extend(_as_issues(apply_receipt.get("blockers", [])))
    blockers.extend(_as_issues(rollback_receipt.get("blockers", [])))

    latest_apply = (
        {
            "receipt_id": apply_record["receipt_id"],
            "status": apply_receipt.get("status") or apply_record.get("status"),
            "completed_at": apply_receipt.get("completed_at") or apply_record.get("completed_at"),
            "source_host": apply_receipt.get("source_host"),
            "target_host": apply_receipt.get("target_host"),
            "target_origin": apply_receipt.get("target_origin"),
            "dns_provider": apply_receipt.get("dns_provider"),
            "caddy_provider": apply_receipt.get("caddy_provider"),
            "provider_mutation_performed": bool(apply_receipt.get("provider_mutation_performed")),
            "traffic_path": apply_receipt.get("traffic_path"),
        }
        if apply_record
        else None
    )
    latest_rollback = (
        {
            "receipt_id": rollback_record["receipt_id"],
            "status": rollback_receipt.get("status") or rollback_record.get("status"),
            "completed_at": rollback_receipt.get("completed_at") or rollback_record.get("completed_at"),
            "provider_rollback_performed": bool(rollback_receipt.get("provider_rollback_performed")),
            "traffic_rollback_path": rollback_receipt.get("traffic_rollback_path"),
        }
        if rollback_record
        else None
    )

    status = "blocked" if blockers else "ok"
    summary = (
        f"Traffic status for {app}"
        + (f" ({resolved_environment})" if resolved_environment != "unknown" else "")
        + ": "
        + (
            f"latest apply {latest_apply['status']}"
            if latest_apply
            else "no traffic apply receipts"
        )
        + (
            f", latest rollback {latest_rollback['status']}"
            if latest_rollback
            else ""
        )
        + f", {len(blockers)} active blocker(s)."
    )

    payload: Dict[str, object] = {
        "schema_version": 1,
        "kind": "ophelia.traffic_status",
        "status": status,
        "app": app,
        "environment": resolved_environment,
        "summary": summary,
        "latest_apply": latest_apply,
        "latest_rollback": latest_rollback,
        "provider_plan": provider_summary,
        "route_ownership": route_ownership,
        "route_conflicts": conflicts.get("conflicts") if isinstance(conflicts.get("conflicts"), list) else [],
        "target_health": target_health,
        "blockers": blockers,
        "credentials_required": False,
        "live_probe_performed": False,
        "inputs_redacted": True,
        "secrets_redacted": True,
    }
    # deep_redact recursively masks any nested credential-shaped scalar; the two
    # status-flag booleans are safe-keyed so they stay True rather than being
    # masked by their sensitive-looking key names.
    return deep_redact(
        payload,
        safe_keys={"secrets_redacted", "inputs_redacted", "credentials_required"},
        propagate=True,
    )


def _latest_record_for(records: List[Dict[str, object]], operation: str) -> Optional[Dict[str, object]]:
    """Most recent receipt record for ``operation`` (records are time-sorted)."""
    matching = [record for record in records if record.get("operation") == operation]
    return matching[-1] if matching else None


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
    if manifest is None:
        current_networks = ["ophelia-edge", "ophelia-internal"]
        target_networks = ["ophelia-edge", f"{app}-{resolved_environment}-internal"]
        mode = "unknown"
        warnings = [schema_issue("manifest_unresolved", "Manifest could not be resolved; assuming shared compatibility mode.")]
    else:
        summary = compose_network_summary(manifest)
        current_networks = ["ophelia-edge", "ophelia-internal"]
        target_networks = ["ophelia-edge", str(summary["internal"])]
        mode = str(summary["internal_mode"])
        warnings = []
        if manifest.networking.internal == "shared":
            warnings.append(
                schema_issue(
                    "compatibility_mode",
                    "Manifest still uses shared `ophelia-internal`; set `networking.internal: per-app` to opt into private internals.",
                    "networking.internal",
                )
            )
    return plan_envelope(
        "app.isolation.plan",
        app,
        resolved_environment,
        f"Per-app isolation compatibility plan for {app}.",
        blockers=blockers,
        warnings=warnings,
        checks=[{"name": "current_runtime_unchanged", "ok": True, "message": "Plan only; no Compose changes are written."}],
        artifacts=[],
        confirmation_required=False,
        risk="medium",
        current_networks=current_networks,
        target_networks=target_networks,
        mode=mode,
        manifest_networking=manifest.to_lock_dict().get("networking", {}) if manifest else {},
        apply_supported=False,
    )


def _traffic_provider_config_validation(
    provider_config: Optional[Path],
    execute_provider_mutation: bool,
    blockers: List[Dict[str, str]],
    warnings: List[Dict[str, str]],
) -> Optional[Dict[str, object]]:
    """Run a supplied provider config through the single canonical validator.

    Phase 13 gate: when a provider config is supplied for mutation, it must pass
    :func:`provider_config.validate_provider_config` before an apply token can be
    produced. Validation blockers are appended to ``blockers`` (which withholds
    the token in the caller). Manual / no-mutation plans that omit a provider
    config are unaffected. The returned validation payload never carries a secret
    value (it is already redacted by the validator).
    """
    if provider_config is None:
        return None
    config_path = provider_config.expanduser()
    if not config_path.exists():
        # Missing-config handling already happens in the provider execution plan;
        # do not double-block here. Validation is simply unavailable.
        return None
    validation = validate_provider_config(config_path)
    status = validation.get("status") if isinstance(validation, dict) else None
    if status == "blocked":
        target_list = blockers if execute_provider_mutation else warnings
        for finding in _as_issues(validation.get("blockers", []) if isinstance(validation, dict) else []):
            target_list.append(
                schema_issue(
                    "provider_config_validation_blocked",
                    str(finding.get("message") or "Provider config validation failed."),
                    str(finding.get("field") or "provider_config"),
                )
            )
    return validation


def _traffic_provider_config_digest(provider_config: Optional[Path]) -> Optional[str]:
    """Digest the provider config contents for the token input, never the secret.

    Returns a sha256 hex digest (truncated) of the config file bytes so the
    confirmation token is bound to the exact provider config that was planned. A
    secret leaks only if it is a literal in the config, which the validator
    blocks; the digest itself is one-way and never reversible to a token value.
    Returns ``None`` when no config is supplied or the file is unreadable so the
    token input stays stable across plan and apply.
    """
    if provider_config is None:
        return None
    config_path = provider_config.expanduser()
    try:
        raw = config_path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(raw).hexdigest()[:32]


def _traffic_target_health_present(target_health: Dict[str, object]) -> bool:
    """Whether the plan carries a usable target health check for policy context.

    Production traffic gates require evidence, not intent: the target health URL
    must be configured, executed, and successful. A configured-but-never-run URL
    is still useful advisory metadata, but it must not satisfy the production
    apply policy gate.
    """
    if not isinstance(target_health, dict):
        return False
    if not target_health.get("configured"):
        return False
    return bool(target_health.get("executed")) and bool(target_health.get("ok"))


def _target_origin_issue(target_origin: str) -> Optional[Dict[str, str]]:
    if not isinstance(target_origin, str) or not target_origin.strip():
        return schema_issue("target_origin_missing", "`--target-origin` is required for traffic planning.", "target_origin")
    if any(character.isspace() for character in target_origin):
        return schema_issue("target_origin_invalid", "`--target-origin` may not contain whitespace.", "target_origin")
    if any(fragment in target_origin for fragment in ("://", "/", "?", "#", "@")):
        return schema_issue(
            "target_origin_invalid",
            "`--target-origin` must be a host, IP address, or DNS target, not a URL or credential-bearing value.",
            "target_origin",
        )
    return None


def _traffic_changes(
    manifest: Optional[Manifest],
    source_host: str,
    target_host: str,
    target_origin: str,
    dns_provider: str,
    caddy_provider: str,
    ttl: int,
) -> List[Dict[str, object]]:
    if manifest is None:
        return []
    changes: List[Dict[str, object]] = []
    for route in manifest.routes:
        changes.append(
            {
                "domain": route.domain,
                "path": route.path,
                "path_prefix": route.path_prefix,
                "source_owner": source_host,
                "target_owner": target_host,
                "target_origin": target_origin,
                "dns": {
                    "provider": dns_provider,
                    "record_name": route.domain,
                    "record_type": _dns_record_type(target_origin),
                    "target": target_origin,
                    "ttl": ttl,
                    "mutation_supported": False,
                    "planned_action": "point domain at target origin after final verification",
                },
                "caddy": {
                    "provider": caddy_provider,
                    "site_file": f"{manifest.app}.caddy",
                    "mutation_supported": False,
                    "planned_action": "stage target route through the selected provider; reload only when explicitly configured",
                },
                "rollback": {
                    "source_owner": source_host,
                    "planned_action": "restore previous DNS/Caddy owner from pre-mutation receipt",
                },
            }
        )
    return changes


def _traffic_provider_changes(
    traffic_changes: List[Dict[str, object]],
    provider_execution: Dict[str, object],
    source_host: str,
    target_host: str,
) -> List[Dict[str, object]]:
    """Structured, redaction-safe before/after provider + DNS + Caddy changes.

    Every value here is a name, host, or target (never a credential). Cloudflare
    credentials are reduced to the env-ref *name* via :func:`redact_mapping`, so a
    token value can never appear. This is the contract artifact the traffic plan
    attaches under ``changes`` even when no provider file is written.
    """
    changes: List[Dict[str, object]] = []
    providers = provider_execution.get("providers")
    providers = providers if isinstance(providers, dict) else {}
    dns_provider = providers.get("dns") if isinstance(providers.get("dns"), dict) else {}
    caddy_provider = providers.get("caddy") if isinstance(providers.get("caddy"), dict) else {}

    for change in traffic_changes:
        dns = change.get("dns") if isinstance(change.get("dns"), dict) else {}
        caddy = change.get("caddy") if isinstance(change.get("caddy"), dict) else {}
        domain = change.get("domain")
        changes.append(
            {
                "kind": "dns_record",
                "target": "dns",
                "domain": domain,
                "provider": dns.get("provider"),
                "record_type": dns.get("record_type"),
                "before": {"owner": source_host},
                "after": {"owner": target_host, "value": dns.get("target"), "ttl": dns.get("ttl")},
                "mutation_supported": bool(dns.get("mutation_supported")),
                "mutation_requested": bool(dns.get("mutation_requested")),
            }
        )
        changes.append(
            {
                "kind": "caddy_site",
                "target": "caddy",
                "domain": domain,
                "provider": caddy.get("provider"),
                "site_file": caddy.get("site_file"),
                "before": {"owner": source_host},
                "after": {"owner": target_host},
                "mutation_supported": bool(caddy.get("mutation_supported")),
                "mutation_requested": bool(caddy.get("mutation_requested")),
            }
        )

    provider_summary = {
        "kind": "provider_execution",
        "target": "provider",
        "dns": redact_mapping(dns_provider) if dns_provider else {},
        "caddy": redact_mapping(caddy_provider) if caddy_provider else {},
        "mutation_supported": bool(provider_execution.get("mutation_supported")),
        "mutation_requested": bool(provider_execution.get("mutation_requested")),
    }
    changes.append(provider_summary)
    return changes


def _traffic_checks(
    readiness: Dict[str, object],
    traffic_changes: List[Dict[str, object]],
    provider_execution: Dict[str, object],
) -> List[Dict[str, object]]:
    readiness_blockers = readiness.get("blockers") if isinstance(readiness.get("blockers"), list) else []
    readiness_checks = readiness.get("checks") if isinstance(readiness.get("checks"), list) else []
    restore_ok = any(
        isinstance(check, dict)
        and check.get("name") == "restore_drill"
        and bool(check.get("ok"))
        for check in readiness_checks
    )
    provider_blockers = provider_execution.get("blockers") if isinstance(provider_execution.get("blockers"), list) else []
    provider_requested = bool(provider_execution.get("mutation_requested"))
    provider_message = (
        "provider mutation configured and ready"
        if provider_requested and not provider_blockers
        else "provider mutation requested but blocked"
        if provider_requested
        else "provider mutation not requested"
    )
    return [
        {"name": "readiness_blockers", "ok": not readiness_blockers, "message": f"{len(readiness_blockers)} blocker(s)"},
        {"name": "traffic_routes", "ok": bool(traffic_changes), "message": f"{len(traffic_changes)} route(s) planned"},
        {"name": "restore_drill_signal", "ok": restore_ok, "message": "restore drill receipt recorded" if restore_ok else "restore drill receipt missing or not required"},
        {"name": "provider_mutation", "ok": not provider_blockers, "message": provider_message},
    ]


def _traffic_target_health_plan(
    target_health_url: Optional[str],
    run_target_health: bool,
    timeout: float,
    expect_status: int,
) -> Dict[str, object]:
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    checks: List[Dict[str, object]] = []
    if not target_health_url:
        if run_target_health:
            blockers.append(schema_issue("target_health_url_missing", "`--target-health-url` is required with --run-target-health.", "target_health_url"))
        else:
            warnings.append(schema_issue("target_health_not_configured", "Target health verification was not configured for this traffic plan."))
        checks.append({"name": "target_health", "ok": not run_target_health, "message": "not configured"})
        return {
            "configured": False,
            "executed": False,
            "ok": not run_target_health,
            "blockers": blockers,
            "warnings": warnings,
            "checks": checks,
        }
    timeout, timeout_valid = _strict_positive_float(timeout, default=10.0)
    expect_status, expect_status_valid = _strict_http_status(expect_status, default=200)
    url_issue = _target_health_url_issue(target_health_url)
    if url_issue is not None:
        blockers.append(url_issue)
    if not timeout_valid:
        blockers.append(schema_issue("target_health_timeout_invalid", "`--target-health-timeout` must be greater than zero.", "target_health_timeout"))
    if not expect_status_valid:
        blockers.append(schema_issue("target_health_status_invalid", "`--target-health-expect-status` must be an HTTP status code.", "target_health_expect_status"))
    result: Dict[str, object] = {
        "url": target_health_url,
        "executed": False,
        "ok": False,
        "expected_status": expect_status,
    }
    if run_target_health and not blockers:
        result = _run_target_health_check(target_health_url, timeout, expect_status)
        if not result.get("ok"):
            blockers.append(schema_issue("target_health_failed", "Target health verification failed.", "target_health_url"))
    elif not run_target_health:
        warnings.append(schema_issue("target_health_not_executed", "Target health URL is configured but was not executed; pass --run-target-health to gate the plan."))
    checks.append(
        {
            "name": "target_health",
            "ok": bool(result.get("ok")) if run_target_health else True,
            "message": "passed" if result.get("ok") else "not executed" if not run_target_health else "failed",
            "executed": bool(result.get("executed")),
            "url": target_health_url,
        }
    )
    return {
        "configured": True,
        "executed": bool(result.get("executed")),
        "ok": bool(result.get("ok")) if run_target_health else None,
        "result": result,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
    }


def _target_health_url_issue(target_health_url: str) -> Optional[Dict[str, str]]:
    parsed = urlparse(target_health_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return schema_issue("target_health_url_invalid", "`--target-health-url` must be an http(s) URL.", "target_health_url")
    if parsed.username or parsed.password:
        return schema_issue("target_health_url_contains_credentials", "`--target-health-url` may not contain credentials.", "target_health_url")
    if parsed.query or parsed.fragment:
        return schema_issue("target_health_url_contains_secret_risk", "`--target-health-url` may not contain query strings or fragments.", "target_health_url")
    return None


def _run_target_health_check(target_health_url: str, timeout: float, expect_status: int) -> Dict[str, object]:
    req = request.Request(target_health_url, headers={"User-Agent": "ophelia-traffic-health/1.0"})
    try:
        with request.urlopen(req, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            return {
                "url": target_health_url,
                "executed": True,
                "ok": status == expect_status,
                "status_code": status,
                "expected_status": expect_status,
            }
    except HTTPError as exc:
        return {
            "url": target_health_url,
            "executed": True,
            "ok": exc.code == expect_status,
            "status_code": exc.code,
            "expected_status": expect_status,
            "error_kind": "http_status" if exc.code != expect_status else None,
        }
    except Exception as exc:  # pragma: no cover - network failures vary by environment.
        return {
            "url": target_health_url,
            "executed": True,
            "ok": False,
            "status_code": None,
            "expected_status": expect_status,
            "error": str(exc),
            "error_kind": type(exc).__name__,
        }


def _dns_record_type(target_origin: str) -> str:
    if ":" in target_origin and all(character in "0123456789abcdefABCDEF:" for character in target_origin):
        return "AAAA"
    if all(part.isdigit() and 0 <= int(part) <= 255 for part in target_origin.split(".") if part) and target_origin.count(".") == 3:
        return "A"
    return "CNAME"


def _traffic_provider_execution_plan(
    app: str,
    environment: str,
    traffic_changes: List[Dict[str, object]],
    dns_provider: str,
    caddy_provider: str,
    provider_config: Optional[Path],
    execute_provider_mutation: bool,
) -> Dict[str, object]:
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    artifacts: List[Dict[str, object]] = []
    provider_config_path = provider_config.expanduser() if provider_config is not None else None
    providers: Dict[str, Dict[str, object]] = {
        "dns": {
            "provider": dns_provider,
            "mutation_supported": False,
            "mutation_requested": execute_provider_mutation and dns_provider != "manual",
        },
        "caddy": {
            "provider": caddy_provider,
            "mutation_supported": False,
            "mutation_requested": execute_provider_mutation and caddy_provider != "manual",
        },
    }

    dns_supported = {"manual", "file", "cloudflare"}
    caddy_supported = {"manual", "file"}
    if dns_provider not in dns_supported:
        blockers.append(schema_issue("dns_provider_unsupported", f"DNS provider `{dns_provider}` is not supported.", "dns_provider"))
    if caddy_provider not in caddy_supported:
        blockers.append(schema_issue("caddy_provider_unsupported", f"Caddy provider `{caddy_provider}` is not supported.", "caddy_provider"))

    config: Dict[str, object] = {}
    if provider_config_path is not None:
        artifacts.append(artifact(str(provider_config_path), "traffic-provider-config", present=provider_config_path.exists()))
        if not provider_config_path.exists():
            if execute_provider_mutation:
                blockers.append(schema_issue("provider_config_missing", f"Provider config does not exist: {provider_config_path}", "provider_config"))
            else:
                warnings.append(schema_issue("provider_config_missing", f"Provider config does not exist: {provider_config_path}", "provider_config"))
        else:
            config = _read_json(provider_config_path)
            if not config:
                blockers.append(schema_issue("provider_config_invalid", f"Provider config is not valid JSON object: {provider_config_path}", "provider_config"))

    if execute_provider_mutation:
        if dns_provider == "manual" and caddy_provider == "manual":
            blockers.append(
                schema_issue(
                    "provider_execution_no_backend",
                    "Provider execution requires at least one non-manual provider.",
                    "execute_provider_mutation",
                )
            )
        if provider_config_path is None:
            blockers.append(
                schema_issue(
                    "provider_config_required",
                    "Provider execution requires --provider-config so apply can verify explicit mutation permission.",
                    "provider_config",
                )
            )
    elif dns_provider != "manual" or caddy_provider != "manual":
        warnings.append(
            schema_issue(
                "provider_execution_not_requested",
                "Non-manual provider labels are planned as intent only because --execute-provider-mutation was not set.",
            )
        )

    if dns_provider == "file":
        _plan_file_dns_provider(providers["dns"], config, execute_provider_mutation, blockers, artifacts)
    if dns_provider == "cloudflare":
        _plan_cloudflare_dns_provider(providers["dns"], config, execute_provider_mutation, blockers)
    if caddy_provider == "file":
        _plan_file_caddy_provider(
            providers["caddy"],
            config,
            execute_provider_mutation,
            app,
            environment,
            blockers,
            artifacts,
        )

    mutation_supported = any(bool(provider.get("mutation_supported")) for provider in providers.values())
    return {
        "schema_version": 1,
        "kind": "ophelia.traffic.provider_execution",
        "provider_config": str(provider_config_path) if provider_config_path is not None else None,
        "mutation_requested": execute_provider_mutation,
        "mutation_supported": mutation_supported,
        "can_execute": execute_provider_mutation and mutation_supported and not blockers and bool(traffic_changes),
        "providers": providers,
        "blockers": blockers,
        "warnings": warnings,
        "artifacts": artifacts,
    }


def _plan_file_dns_provider(
    provider: Dict[str, object],
    config: Dict[str, object],
    execute_provider_mutation: bool,
    blockers: List[Dict[str, str]],
    artifacts: List[Dict[str, object]],
) -> None:
    section = _traffic_provider_config_section(config, "dns", "file")
    record_file = section.get("record_file")
    allow_mutation = bool(section.get("allow_mutation"))
    if isinstance(record_file, str) and record_file.strip():
        record_path = Path(record_file).expanduser()
        provider.update(
            {
                "mutation_supported": execute_provider_mutation and allow_mutation,
                "record_file": str(record_path),
                "allow_mutation": allow_mutation,
            }
        )
        artifacts.append(artifact(str(record_path), "dns-file-records", present=record_path.exists()))
    elif execute_provider_mutation:
        blockers.append(schema_issue("dns_file_record_file_missing", "`dns.file.record_file` is required for file DNS execution.", "dns.record_file"))

    if execute_provider_mutation and not allow_mutation:
        blockers.append(schema_issue("dns_file_mutation_not_allowed", "`dns.file.allow_mutation` must be true.", "dns.allow_mutation"))


def _plan_file_caddy_provider(
    provider: Dict[str, object],
    config: Dict[str, object],
    execute_provider_mutation: bool,
    app: str,
    environment: str,
    blockers: List[Dict[str, str]],
    artifacts: List[Dict[str, object]],
) -> None:
    section = _traffic_provider_config_section(config, "caddy", "file")
    sites_dir = section.get("sites_dir")
    allow_mutation = bool(section.get("allow_mutation"))
    reload_requested = bool(section.get("reload", False))
    allow_reload = bool(section.get("allow_reload", False))
    validate_requested = bool(section.get("validate", False) or reload_requested)
    timeout, timeout_valid = _strict_positive_int(section.get("timeout"), default=30)
    if isinstance(sites_dir, str) and sites_dir.strip():
        sites_path = Path(sites_dir).expanduser()
        site_path = sites_path / f"{_safe_artifact_slug(app)}.{_safe_artifact_slug(environment)}.traffic.caddy"
        runtime_root = _caddy_runtime_root(section, sites_path)
        ophelia_root = Path(str(section.get("ophelia_root") or REPO_ROOT)).expanduser()
        provider.update(
            {
                "mutation_supported": execute_provider_mutation and allow_mutation,
                "sites_dir": str(sites_path),
                "site_file": str(site_path),
                "allow_mutation": allow_mutation,
                "validate": validate_requested,
                "reload": reload_requested,
                "allow_reload": allow_reload,
                "runtime_root": str(runtime_root),
                "ophelia_root": str(ophelia_root),
                "timeout": timeout,
            }
        )
        artifacts.append(artifact(str(site_path), "caddy-file-site", present=site_path.exists()))
        if execute_provider_mutation and validate_requested:
            expected_sites_path = runtime_root / "caddy" / "sites.d"
            if _normalized_path(sites_path) != _normalized_path(expected_sites_path):
                blockers.append(
                    schema_issue(
                        "caddy_file_sites_dir_mismatch",
                        "`caddy.file.sites_dir` must match `<runtime_root>/caddy/sites.d` when validation or reload is requested.",
                        "caddy.sites_dir",
                    )
                )
    elif execute_provider_mutation:
        blockers.append(schema_issue("caddy_file_sites_dir_missing", "`caddy.file.sites_dir` is required for file Caddy execution.", "caddy.sites_dir"))

    if execute_provider_mutation and not allow_mutation:
        blockers.append(schema_issue("caddy_file_mutation_not_allowed", "`caddy.file.allow_mutation` must be true.", "caddy.allow_mutation"))
    if execute_provider_mutation and reload_requested and not allow_reload:
        blockers.append(schema_issue("caddy_file_reload_not_allowed", "`caddy.file.allow_reload` must be true when `reload` is true.", "caddy.allow_reload"))
    if not timeout_valid:
        blockers.append(schema_issue("caddy_file_timeout_invalid", "`caddy.file.timeout` must be greater than zero.", "caddy.timeout"))


def _plan_cloudflare_dns_provider(
    provider: Dict[str, object],
    config: Dict[str, object],
    execute_provider_mutation: bool,
    blockers: List[Dict[str, str]],
) -> None:
    section = _traffic_provider_config_section(config, "dns", "cloudflare")
    zone_id = section.get("zone_id")
    api_token_env = section.get("api_token_env")
    allow_mutation = bool(section.get("allow_mutation"))
    allow_create = bool(section.get("allow_create", False))
    proxied = bool(section.get("proxied", False))
    base_url = str(section.get("base_url") or "https://api.cloudflare.com/client/v4").rstrip("/")
    token_available = isinstance(api_token_env, str) and bool(os.environ.get(api_token_env))
    if isinstance(zone_id, str) and zone_id.strip() and isinstance(api_token_env, str) and api_token_env.strip():
        provider.update(
            {
                "mutation_supported": execute_provider_mutation and allow_mutation and token_available,
                "zone_id": zone_id,
                "api_token_env": api_token_env,
                "token_available": token_available,
                "allow_mutation": allow_mutation,
                "allow_create": allow_create,
                "proxied": proxied,
                "base_url": base_url,
                "comment": section.get("comment") if isinstance(section.get("comment"), str) else None,
            }
        )
    if execute_provider_mutation:
        if not isinstance(zone_id, str) or not zone_id.strip():
            blockers.append(schema_issue("cloudflare_zone_id_missing", "`dns.cloudflare.zone_id` is required.", "dns.zone_id"))
        if not isinstance(api_token_env, str) or not api_token_env.strip():
            blockers.append(schema_issue("cloudflare_api_token_env_missing", "`dns.cloudflare.api_token_env` is required.", "dns.api_token_env"))
        elif not os.environ.get(api_token_env):
            blockers.append(schema_issue("cloudflare_api_token_missing", f"Environment variable `{api_token_env}` is not set.", "dns.api_token_env"))
        if not allow_mutation:
            blockers.append(schema_issue("cloudflare_mutation_not_allowed", "`dns.cloudflare.allow_mutation` must be true.", "dns.allow_mutation"))
        if not base_url.startswith("https://"):
            blockers.append(schema_issue("cloudflare_base_url_invalid", "`dns.cloudflare.base_url` must be https.", "dns.base_url"))


def _traffic_provider_config_section(config: Dict[str, object], group: str, provider_name: str) -> Dict[str, object]:
    providers = config.get("providers")
    if isinstance(providers, dict):
        grouped = providers.get(group)
        if isinstance(grouped, dict):
            nested = grouped.get(provider_name)
            if isinstance(nested, dict):
                return nested
    grouped = config.get(group)
    if isinstance(grouped, dict):
        nested = grouped.get(provider_name)
        if isinstance(nested, dict):
            return nested
        if grouped.get("provider") in {None, provider_name} or grouped.get("type") == provider_name:
            return grouped
    return {}


def _caddy_runtime_root(section: Dict[str, object], sites_path: Path) -> Path:
    runtime_root = section.get("runtime_root")
    if isinstance(runtime_root, str) and runtime_root.strip():
        return Path(runtime_root).expanduser()
    if sites_path.name == "sites.d" and sites_path.parent.name == "caddy":
        return sites_path.parent.parent
    return DEFAULT_RUNTIME_ROOT


def _strict_positive_int(value: object, default: int) -> Tuple[int, bool]:
    if value in {None, ""}:
        return default, True
    if isinstance(value, bool):
        return default, False
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default, False
    return parsed, parsed > 0


def _strict_positive_float(value: object, default: float) -> Tuple[float, bool]:
    if value in {None, ""}:
        return default, True
    if isinstance(value, bool):
        return default, False
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return default, False
    return parsed, parsed > 0


def _strict_http_status(value: object, default: int) -> Tuple[int, bool]:
    parsed, valid_positive = _strict_positive_int(value, default)
    return parsed, valid_positive and 100 <= parsed <= 599


def _normalized_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _apply_traffic_provider_capabilities(
    traffic_changes: List[Dict[str, object]],
    provider_execution: Dict[str, object],
) -> None:
    providers = provider_execution.get("providers")
    if not isinstance(providers, dict):
        return
    dns = providers.get("dns") if isinstance(providers.get("dns"), dict) else {}
    caddy = providers.get("caddy") if isinstance(providers.get("caddy"), dict) else {}
    for change in traffic_changes:
        dns_change = change.get("dns")
        if isinstance(dns_change, dict):
            dns_change["mutation_supported"] = bool(dns.get("mutation_supported"))
            dns_change["mutation_requested"] = bool(dns.get("mutation_requested"))
        caddy_change = change.get("caddy")
        if isinstance(caddy_change, dict):
            caddy_change["mutation_supported"] = bool(caddy.get("mutation_supported"))
            caddy_change["mutation_requested"] = bool(caddy.get("mutation_requested"))
            site_file = caddy.get("site_file")
            if isinstance(site_file, str):
                caddy_change["site_file"] = site_file


def _apply_traffic_provider_mutations(plan: Dict[str, object], traffic_root: Path) -> List[Dict[str, object]]:
    provider_execution = plan.get("provider_execution")
    if not isinstance(provider_execution, dict):
        return []
    providers = provider_execution.get("providers")
    if not isinstance(providers, dict):
        return []
    traffic_changes = plan.get("traffic_changes") if isinstance(plan.get("traffic_changes"), list) else []
    mutations: List[Dict[str, object]] = []
    dns = providers.get("dns") if isinstance(providers.get("dns"), dict) else {}
    if dns.get("provider") == "file" and dns.get("mutation_requested"):
        mutations.append(_apply_file_dns_provider(dns, traffic_changes))
    if dns.get("provider") == "cloudflare" and dns.get("mutation_requested"):
        mutations.append(_apply_cloudflare_dns_provider(dns, traffic_changes))
    caddy = providers.get("caddy") if isinstance(providers.get("caddy"), dict) else {}
    if caddy.get("provider") == "file" and caddy.get("mutation_requested"):
        mutations.append(_apply_file_caddy_provider(caddy, traffic_changes, plan, traffic_root))
    return mutations


def _apply_file_dns_provider(provider: Dict[str, object], traffic_changes: List[object]) -> Dict[str, object]:
    path_value = provider.get("record_file")
    if not isinstance(path_value, str):
        return {"provider": "dns.file", "status": "failed", "reason": "record_file_missing"}
    record_path = Path(path_value).expanduser()
    try:
        payload = _read_json(record_path)
        records = payload.get("records") if isinstance(payload.get("records"), dict) else {}
        next_records = dict(records)
        changes: List[Dict[str, object]] = []
        updated_at = _utc_now()
        for raw_change in traffic_changes:
            if not isinstance(raw_change, dict):
                continue
            domain = raw_change.get("domain")
            dns_change = raw_change.get("dns")
            if not isinstance(domain, str) or not isinstance(dns_change, dict):
                continue
            previous = next_records.get(domain)
            next_value = {
                "type": dns_change.get("record_type"),
                "target": dns_change.get("target"),
                "ttl": dns_change.get("ttl"),
                "source_owner": raw_change.get("source_owner"),
                "target_owner": raw_change.get("target_owner"),
                "updated_at": updated_at,
            }
            next_records[domain] = next_value
            changes.append({"domain": domain, "previous": previous, "next": next_value})
        _atomic_write_json(
            record_path,
            {
                "schema_version": 1,
                "kind": "ophelia.traffic.dns_records",
                "updated_at": updated_at,
                "records": next_records,
            },
        )
    except OSError as exc:
        return {"provider": "dns.file", "status": "failed", "path": str(record_path), "reason": str(exc)}
    return {
        "provider": "dns.file",
        "status": "succeeded",
        "path": str(record_path),
        "changes": changes,
        "file_written": True,
        "rollback": {"available": True, "restore_previous_records": changes},
    }


def _apply_cloudflare_dns_provider(provider: Dict[str, object], traffic_changes: List[object]) -> Dict[str, object]:
    zone_id = provider.get("zone_id")
    api_token_env = provider.get("api_token_env")
    base_url = str(provider.get("base_url") or "https://api.cloudflare.com/client/v4").rstrip("/")
    if not isinstance(zone_id, str) or not isinstance(api_token_env, str):
        return {"provider": "dns.cloudflare", "status": "failed", "reason": "provider_config_missing"}
    api_token = os.environ.get(api_token_env)
    if not api_token:
        return {"provider": "dns.cloudflare", "status": "failed", "reason": "api_token_env_missing", "api_token_env": api_token_env}
    changes: List[Dict[str, object]] = []
    for raw_change in traffic_changes:
        if not isinstance(raw_change, dict):
            continue
        domain = raw_change.get("domain")
        dns_change = raw_change.get("dns")
        if not isinstance(domain, str) or not isinstance(dns_change, dict):
            continue
        record_type = str(dns_change.get("record_type") or "")
        target = str(dns_change.get("target") or "")
        ttl = _positive_int(dns_change.get("ttl"), 300)
        try:
            existing = _cloudflare_find_dns_record(base_url, zone_id, api_token, domain, record_type)
            if len(existing) > 1:
                return {
                    "provider": "dns.cloudflare",
                    "status": "failed",
                    "reason": "multiple_matching_records",
                    "domain": domain,
                    "record_type": record_type,
                    "changes": changes,
                }
            body = {
                "type": record_type,
                "name": domain,
                "content": target,
                "ttl": ttl,
                "proxied": bool(provider.get("proxied", False)),
            }
            comment = provider.get("comment")
            if isinstance(comment, str) and comment:
                body["comment"] = comment
            if existing:
                previous = _redacted_cloudflare_record(existing[0])
                record_id = str(existing[0].get("id") or "")
                response = _cloudflare_request(
                    "PATCH",
                    f"{base_url}/zones/{zone_id}/dns_records/{record_id}",
                    api_token,
                    body,
                )
                action = "updated"
            else:
                previous = None
                if not provider.get("allow_create"):
                    return {
                        "provider": "dns.cloudflare",
                        "status": "failed",
                        "reason": "create_not_allowed",
                        "domain": domain,
                        "record_type": record_type,
                        "changes": changes,
                    }
                response = _cloudflare_request(
                    "POST",
                    f"{base_url}/zones/{zone_id}/dns_records",
                    api_token,
                    body,
                )
                action = "created"
            if not response.get("success"):
                return {
                    "provider": "dns.cloudflare",
                    "status": "failed",
                    "reason": "cloudflare_api_error",
                    "domain": domain,
                    "record_type": record_type,
                    "errors": _cloudflare_errors(response),
                    "changes": changes,
                }
            result = response.get("result") if isinstance(response.get("result"), dict) else {}
            changes.append(
                {
                    "domain": domain,
                    "record_type": record_type,
                    "action": action,
                    "previous": previous,
                    "next": _redacted_cloudflare_record(result),
                }
            )
        except (OSError, URLError) as exc:
            return {"provider": "dns.cloudflare", "status": "failed", "reason": str(exc), "changes": changes}
    return {
        "provider": "dns.cloudflare",
        "status": "succeeded",
        "zone_id": zone_id,
        "api_token_env": api_token_env,
        "base_url": base_url,
        "changes": changes,
        "rollback": {"available": True, "restore_previous_records": changes},
    }


def _cloudflare_find_dns_record(
    base_url: str,
    zone_id: str,
    api_token: str,
    domain: str,
    record_type: str,
) -> List[Dict[str, object]]:
    query = urlencode({"type": record_type, "name": domain, "per_page": "100"})
    response = _cloudflare_request("GET", f"{base_url}/zones/{zone_id}/dns_records?{query}", api_token)
    if not response.get("success"):
        raise OSError(f"Cloudflare DNS list failed: {_cloudflare_errors(response)}")
    result = response.get("result")
    records = result if isinstance(result, list) else []
    return [
        record
        for record in records
        if isinstance(record, dict)
        and record.get("type") == record_type
        and str(record.get("name") or "").rstrip(".") == domain.rstrip(".")
    ]


def _cloudflare_request(
    method: str,
    url: str,
    api_token: str,
    body: Optional[Dict[str, object]] = None,
    timeout: int = 30,
) -> Dict[str, object]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            try:
                payload = json.loads(response.read().decode("utf-8"))
            except json.JSONDecodeError:
                payload = {"success": False, "errors": [{"message": "Cloudflare API returned invalid JSON."}]}
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {"success": False, "errors": [{"message": str(exc)}]}
    if isinstance(payload, dict):
        return payload
    return {"success": False, "errors": [{"message": "Cloudflare API returned a non-object response."}]}


def _cloudflare_errors(response: Dict[str, object]) -> List[Dict[str, object]]:
    errors = response.get("errors")
    if not isinstance(errors, list):
        return []
    redacted = []
    for error in errors:
        if isinstance(error, dict):
            redacted.append({key: value for key, value in error.items() if key in {"code", "message", "documentation_url", "source"}})
    return redacted


def _redacted_cloudflare_record(record: Dict[str, object]) -> Dict[str, object]:
    return redacted_cloudflare_record(record)


def _apply_file_caddy_provider(
    provider: Dict[str, object],
    traffic_changes: List[object],
    plan: Dict[str, object],
    traffic_root: Path,
) -> Dict[str, object]:
    path_value = provider.get("site_file")
    if not isinstance(path_value, str):
        return {"provider": "caddy.file", "status": "failed", "reason": "site_file_missing"}
    site_path = Path(path_value).expanduser()
    try:
        previous_present = site_path.exists()
        previous_sha256 = None
        previous_snapshot_path = None
        if previous_present:
            previous_text = site_path.read_text()
            previous_sha256 = hashlib.sha256(previous_text.encode("utf-8")).hexdigest()
            previous_snapshot_path = traffic_root / "rollback" / site_path.name
            previous_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            previous_snapshot_path.write_text(previous_text)
        next_text = _render_file_caddy_traffic_site(plan, traffic_changes)
        next_sha256 = hashlib.sha256(next_text.encode("utf-8")).hexdigest()
        _atomic_write_text(site_path, next_text)
        validation = None
        reload_result = None
        runtime_root = Path(str(provider.get("runtime_root") or DEFAULT_RUNTIME_ROOT)).expanduser()
        ophelia_root = Path(str(provider.get("ophelia_root") or REPO_ROOT)).expanduser()
        timeout = _positive_int(provider.get("timeout"), 30)
        if provider.get("validate") and not provider.get("reload"):
            validation = validate_caddy(runtime_root=runtime_root, ophelia_root=ophelia_root, timeout=timeout)
            if validation.get("returncode") != 0:
                return {
                    "provider": "caddy.file",
                    "status": "failed",
                    "path": str(site_path),
                    "previous_present": previous_present,
                    "previous_sha256": previous_sha256,
                    "previous_snapshot_path": str(previous_snapshot_path) if previous_snapshot_path is not None else None,
                    "next_sha256": next_sha256,
                    "file_written": True,
                    "validation": validation,
                    "reason": "caddy_validation_failed",
                }
        if provider.get("reload"):
            reload_result = reload_caddy(
                runtime_root=runtime_root,
                ophelia_root=ophelia_root,
                timeout=timeout,
                validate_first=True,
            )
            validation = reload_result.get("validation") if isinstance(reload_result.get("validation"), dict) else validation
            if reload_result.get("returncode") != 0:
                return {
                    "provider": "caddy.file",
                    "status": "failed",
                    "path": str(site_path),
                    "previous_present": previous_present,
                    "previous_sha256": previous_sha256,
                    "previous_snapshot_path": str(previous_snapshot_path) if previous_snapshot_path is not None else None,
                    "next_sha256": next_sha256,
                    "file_written": True,
                    "validation": validation,
                    "reload": reload_result,
                    "reason": "caddy_reload_failed",
                }
    except OSError as exc:
        return {"provider": "caddy.file", "status": "failed", "path": str(site_path), "reason": str(exc)}
    return {
        "provider": "caddy.file",
        "status": "succeeded",
        "path": str(site_path),
        "previous_present": previous_present,
        "previous_sha256": previous_sha256,
        "previous_snapshot_path": str(previous_snapshot_path) if previous_snapshot_path is not None else None,
        "next_sha256": next_sha256,
        "file_written": True,
        "validation": validation,
        "reload": reload_result,
        "reload_performed": bool(reload_result and reload_result.get("returncode") == 0),
        "rollback": {
            "available": True,
            "note": "Restore previous_snapshot_path when present. Deletion-only rollback is blocked by traffic rollback plan.",
        },
    }


def _render_file_caddy_traffic_site(plan: Dict[str, object], traffic_changes: List[object]) -> str:
    lines = [
        "# Managed by Ophelia traffic apply.",
        f"# app: {plan.get('app')}",
        f"# environment: {plan.get('environment')}",
        f"# source_host: {plan.get('source_host')}",
        f"# target_host: {plan.get('target_host')}",
        "",
    ]
    for raw_change in traffic_changes:
        if not isinstance(raw_change, dict):
            continue
        domain = raw_change.get("domain")
        target_origin = raw_change.get("target_origin")
        if not isinstance(domain, str) or not isinstance(target_origin, str):
            continue
        lines.extend(
            [
                f"{domain} {{",
                "    encode zstd gzip",
                f"    reverse_proxy {target_origin}",
                "}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _traffic_provider_mutation_message(execute_provider_mutation: bool, provider_mutations: List[Dict[str, object]]) -> str:
    if not execute_provider_mutation:
        return "Provider mutation was not requested; checkpoint receipt only."
    if not provider_mutations:
        return "Provider mutation was requested but no provider mutations were produced."
    failed = [item for item in provider_mutations if item.get("status") != "succeeded"]
    if failed:
        return f"{len(failed)} provider mutation(s) failed."
    return f"{len(provider_mutations)} provider mutation(s) succeeded."


def _load_traffic_receipt(receipt_id: str, runtime_root: Path, app: str) -> Tuple[Dict[str, object], Optional[Path], List[str]]:
    if not isinstance(receipt_id, str) or not receipt_id.strip():
        return {}, None, ["Traffic receipt id is required."]
    receipt_path = Path(receipt_id).expanduser()
    if receipt_path.exists() and receipt_path.is_file():
        payload = _read_json(receipt_path)
        if payload:
            return payload, receipt_path, []
        return {}, receipt_path, [f"Could not read receipt JSON: {receipt_path}"]
    for record in _receipt_records(runtime_root, app=app):
        if record.get("receipt_id") == receipt_id:
            path = Path(str(record["path"]))
            payload = _read_json(path)
            if payload:
                return payload, path, []
            return {}, path, [f"Could not read receipt JSON: {path}"]
    return {}, None, [f"Traffic receipt not found: {receipt_id}"]


def _traffic_rollback_changes(
    receipt_payload: Dict[str, object],
    blockers: List[Dict[str, str]],
    approve_unsafe_delete: bool = False,
) -> List[Dict[str, object]]:
    mutations = receipt_payload.get("provider_mutations")
    if not isinstance(mutations, list):
        return []
    rollback_changes: List[Dict[str, object]] = []
    for mutation in mutations:
        if not isinstance(mutation, dict):
            continue
        provider = mutation.get("provider")
        if provider == "dns.file":
            rollback_changes.append(_dns_file_rollback_change(mutation, blockers, approve_unsafe_delete))
        elif provider == "dns.cloudflare":
            rollback_changes.append(_cloudflare_dns_rollback_change(mutation, blockers, approve_unsafe_delete))
        elif provider == "caddy.file":
            rollback_changes.append(_caddy_file_rollback_change(mutation, blockers, approve_unsafe_delete))
        elif isinstance(provider, str):
            blockers.append(schema_issue("traffic_rollback_provider_unsupported", f"Unsupported traffic rollback provider: {provider}"))
    return [item for item in rollback_changes if item]


def _rollback_unsafe_delete_issue(target: str) -> Dict[str, str]:
    """Refuse a rollback that would delete a record/file the forward apply created.

    Phase 13: a forward apply that created a previously-absent DNS record or Caddy
    site file leaves no prior state to restore; rolling it back means deleting it.
    That delete is refused with `rollback_unsafe_delete` unless the operator passes
    an explicit approval flag.
    """
    return schema_issue(
        "rollback_unsafe_delete",
        (
            f"Traffic rollback for {target} would DELETE a record/file that did not exist before the "
            "forward apply (it was created by the apply). Automatic deletion is refused; re-run with "
            "`--approve-unsafe-delete` to explicitly authorize the delete."
        ),
        "provider_mutations",
    )


def _dns_file_rollback_change(
    mutation: Dict[str, object],
    blockers: List[Dict[str, str]],
    approve_unsafe_delete: bool = False,
) -> Dict[str, object]:
    path = mutation.get("path")
    actions: List[Dict[str, object]] = []
    if not isinstance(path, str):
        blockers.append(schema_issue("dns_file_rollback_path_missing", "DNS file rollback is missing record file path."))
        return {}
    for raw_change in mutation.get("changes") if isinstance(mutation.get("changes"), list) else []:
        if not isinstance(raw_change, dict):
            continue
        domain = raw_change.get("domain")
        previous = raw_change.get("previous")
        if not isinstance(domain, str):
            continue
        if previous is None:
            if approve_unsafe_delete:
                actions.append({"domain": domain, "delete_record": True})
                continue
            blockers.append(_rollback_unsafe_delete_issue(f"DNS record `{domain}`"))
            continue
        if not isinstance(previous, dict):
            blockers.append(schema_issue("dns_file_rollback_previous_invalid", f"Previous DNS record for `{domain}` is not an object."))
            continue
        actions.append({"domain": domain, "restore_record": previous})
    return {"provider": "dns.file", "path": path, "actions": actions}


def _caddy_file_rollback_change(
    mutation: Dict[str, object],
    blockers: List[Dict[str, str]],
    approve_unsafe_delete: bool = False,
) -> Dict[str, object]:
    path = mutation.get("path")
    snapshot = mutation.get("previous_snapshot_path")
    if not isinstance(path, str):
        blockers.append(schema_issue("caddy_file_rollback_path_missing", "Caddy file rollback is missing site file path."))
        return {}
    if not mutation.get("previous_present"):
        if approve_unsafe_delete:
            return {"provider": "caddy.file", "path": path, "previous_snapshot_path": snapshot, "actions": [{"delete_file": True}]}
        blockers.append(_rollback_unsafe_delete_issue(f"Caddy site file `{Path(path).name}`"))
        return {"provider": "caddy.file", "path": path, "previous_snapshot_path": snapshot, "actions": []}
    if not isinstance(snapshot, str) or not Path(snapshot).exists():
        blockers.append(schema_issue("caddy_file_rollback_snapshot_missing", "Previous Caddy file snapshot is missing.", "previous_snapshot_path"))
        return {"provider": "caddy.file", "path": path, "previous_snapshot_path": snapshot, "actions": []}
    return {
        "provider": "caddy.file",
        "path": path,
        "previous_snapshot_path": snapshot,
        "actions": [{"restore_from": snapshot}],
    }


def _cloudflare_dns_rollback_change(
    mutation: Dict[str, object],
    blockers: List[Dict[str, str]],
    approve_unsafe_delete: bool = False,
) -> Dict[str, object]:
    zone_id = mutation.get("zone_id")
    api_token_env = mutation.get("api_token_env")
    base_url = mutation.get("base_url")
    actions: List[Dict[str, object]] = []
    if not isinstance(zone_id, str) or not isinstance(api_token_env, str) or not isinstance(base_url, str):
        blockers.append(schema_issue("cloudflare_rollback_config_missing", "Cloudflare rollback is missing zone_id, api_token_env, or base_url."))
        return {}
    if not os.environ.get(api_token_env):
        blockers.append(schema_issue("cloudflare_rollback_token_missing", f"Environment variable `{api_token_env}` is not set.", "api_token_env"))
    for raw_change in mutation.get("changes") if isinstance(mutation.get("changes"), list) else []:
        if not isinstance(raw_change, dict):
            continue
        domain = raw_change.get("domain")
        previous = raw_change.get("previous")
        if previous is None:
            if approve_unsafe_delete:
                next_record = raw_change.get("next") if isinstance(raw_change.get("next"), dict) else {}
                record_id = raw_change.get("record_id") or next_record.get("id")
                actions.append({"domain": domain, "delete_record": True, "record_id": record_id})
                continue
            blockers.append(_rollback_unsafe_delete_issue(f"Cloudflare DNS record `{domain}`"))
            continue
        if not isinstance(previous, dict) or not previous.get("id"):
            blockers.append(schema_issue("cloudflare_rollback_previous_invalid", f"Previous Cloudflare record for `{domain}` is missing an id."))
            continue
        actions.append({"domain": domain, "restore_record": previous})
    return {
        "provider": "dns.cloudflare",
        "zone_id": zone_id,
        "api_token_env": api_token_env,
        "base_url": base_url,
        "actions": actions,
    }


def _apply_traffic_provider_rollbacks(rollback_changes: List[object]) -> List[Dict[str, object]]:
    results: List[Dict[str, object]] = []
    for raw_change in rollback_changes:
        if not isinstance(raw_change, dict):
            continue
        provider = raw_change.get("provider")
        if provider == "dns.file":
            results.append(_rollback_file_dns_provider(raw_change))
        elif provider == "dns.cloudflare":
            results.append(_rollback_cloudflare_dns_provider(raw_change))
        elif provider == "caddy.file":
            results.append(_rollback_file_caddy_provider(raw_change))
    return results


def _rollback_file_dns_provider(change: Dict[str, object]) -> Dict[str, object]:
    path = change.get("path")
    if not isinstance(path, str):
        return {"provider": "dns.file", "status": "failed", "reason": "path_missing"}
    record_path = Path(path).expanduser()
    try:
        payload = _read_json(record_path)
        records = payload.get("records") if isinstance(payload.get("records"), dict) else {}
        next_records = dict(records)
        actions: List[Dict[str, object]] = []
        for action in change.get("actions") if isinstance(change.get("actions"), list) else []:
            if not isinstance(action, dict):
                continue
            domain = action.get("domain")
            restore_record = action.get("restore_record")
            if isinstance(domain, str) and isinstance(restore_record, dict):
                next_records[domain] = restore_record
                actions.append({"domain": domain, "restored": True})
            elif isinstance(domain, str) and action.get("delete_record"):
                # Approved unsafe delete: remove the record the forward apply created.
                next_records.pop(domain, None)
                actions.append({"domain": domain, "deleted": True})
        _atomic_write_json(
            record_path,
            {
                "schema_version": 1,
                "kind": "ophelia.traffic.dns_records",
                "updated_at": _utc_now(),
                "records": next_records,
            },
        )
    except OSError as exc:
        return {"provider": "dns.file", "status": "failed", "path": str(record_path), "reason": str(exc)}
    return {"provider": "dns.file", "status": "succeeded", "path": str(record_path), "actions": actions}


def _rollback_file_caddy_provider(change: Dict[str, object]) -> Dict[str, object]:
    path = change.get("path")
    snapshot = change.get("previous_snapshot_path")
    if not isinstance(path, str):
        return {"provider": "caddy.file", "status": "failed", "reason": "path_missing"}
    actions = change.get("actions") if isinstance(change.get("actions"), list) else []
    delete_requested = any(isinstance(item, dict) and item.get("delete_file") for item in actions)
    if delete_requested:
        # Approved unsafe delete: remove the site file the forward apply created.
        site_path = Path(path).expanduser()
        try:
            if site_path.exists():
                site_path.unlink()
        except OSError as exc:
            return {"provider": "caddy.file", "status": "failed", "path": str(site_path), "reason": str(exc)}
        return {"provider": "caddy.file", "status": "succeeded", "path": str(site_path), "deleted": True}
    if not isinstance(snapshot, str):
        return {"provider": "caddy.file", "status": "failed", "reason": "path_or_snapshot_missing"}
    site_path = Path(path).expanduser()
    snapshot_path = Path(snapshot).expanduser()
    try:
        previous_text = snapshot_path.read_text()
        _atomic_write_text(site_path, previous_text)
    except OSError as exc:
        return {"provider": "caddy.file", "status": "failed", "path": str(site_path), "reason": str(exc)}
    return {
        "provider": "caddy.file",
        "status": "succeeded",
        "path": str(site_path),
        "restored_from": str(snapshot_path),
        "sha256": hashlib.sha256(previous_text.encode("utf-8")).hexdigest(),
    }


def _rollback_cloudflare_dns_provider(change: Dict[str, object]) -> Dict[str, object]:
    zone_id = change.get("zone_id")
    api_token_env = change.get("api_token_env")
    base_url = str(change.get("base_url") or "https://api.cloudflare.com/client/v4").rstrip("/")
    if not isinstance(zone_id, str) or not isinstance(api_token_env, str):
        return {"provider": "dns.cloudflare", "status": "failed", "reason": "provider_config_missing"}
    api_token = os.environ.get(api_token_env)
    if not api_token:
        return {"provider": "dns.cloudflare", "status": "failed", "reason": "api_token_env_missing", "api_token_env": api_token_env}
    actions: List[Dict[str, object]] = []
    skipped: List[Dict[str, object]] = []
    try:
        for action in change.get("actions") if isinstance(change.get("actions"), list) else []:
            if not isinstance(action, dict):
                continue
            # A delete of a forward-created Cloudflare record is intentionally
            # NOT executed (the Cloudflare provider never issues a live DELETE).
            # Record it as skipped so the receipt does not imply a deletion that
            # did not happen; the operator must remove it via an approved path.
            # The producer emits ``delete_record: True`` (bool) with ``record_id``
            # on the action; tolerate a dict shape too for safety.
            delete_record = action.get("delete_record")
            if delete_record:
                delete_id = action.get("record_id")
                if delete_id is None and isinstance(delete_record, dict):
                    delete_id = delete_record.get("id")
                skipped.append(
                    {
                        "record_id": delete_id if isinstance(delete_id, str) else None,
                        "skipped": True,
                        "reason": "cloudflare_delete_not_supported",
                    }
                )
                continue
            restore_record = action.get("restore_record")
            if not isinstance(restore_record, dict):
                continue
            record_id = restore_record.get("id")
            if not isinstance(record_id, str) or not record_id:
                continue
            body = {
                key: restore_record[key]
                for key in ("type", "name", "content", "ttl", "proxied", "comment")
                if key in restore_record
            }
            response = _cloudflare_request(
                "PATCH",
                f"{base_url}/zones/{zone_id}/dns_records/{record_id}",
                api_token,
                body,
            )
            if not response.get("success"):
                return {
                    "provider": "dns.cloudflare",
                    "status": "failed",
                    "reason": "cloudflare_api_error",
                    "errors": _cloudflare_errors(response),
                    "actions": actions,
                }
            result = response.get("result") if isinstance(response.get("result"), dict) else {}
            actions.append({"record_id": record_id, "restored": True, "result": _redacted_cloudflare_record(result)})
    except (OSError, URLError) as exc:
        return {"provider": "dns.cloudflare", "status": "failed", "reason": str(exc), "actions": actions}
    result = {
        "provider": "dns.cloudflare",
        "status": "succeeded",
        "zone_id": zone_id,
        "api_token_env": api_token_env,
        "actions": actions,
    }
    if skipped:
        result["skipped_actions"] = skipped
        result["warnings"] = [
            "Cloudflare-created records are not auto-deleted on rollback; "
            f"{len(skipped)} record(s) were left live and must be removed via an approved path."
        ]
    return result


def _safe_artifact_slug(value: str) -> str:
    slug = "".join(character if character.isalnum() or character in {"-", "_"} else "-" for character in value)
    return slug.strip("-") or "unknown"


def _desired_env_entries(manifest: Manifest) -> Dict[str, Dict[str, object]]:
    entries: Dict[str, Dict[str, object]] = {}

    def add(key: str, source: str, required_by: str, *, required: bool = True) -> None:
        if required_by == "runtime" and key in RUNTIME_INJECTED_ENV_KEYS:
            required = False
        current = entries.setdefault(key, {"key": key, "sources": [], "required_by": [], "required": False})
        if source not in current["sources"]:
            current["sources"].append(source)
        if required_by not in current["required_by"]:
            current["required_by"].append(required_by)
        current["required"] = bool(current["required"] or required)

    for raw_line in render_env_example(manifest).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _value = line.split("=", 1)
        add(key, "env.example", _env_required_by(manifest, key))
    for key in manifest.env:
        add(key, "manifest.env", "manifest.env")
    for key in manifest.required_env:
        add(key, "manifest.required_env", "manifest.required_env")
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
    if key in manifest.required_env:
        return "manifest.required_env"
    if key.startswith("OPHELIA_CONSOLE_"):
        return "console"
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
                "coverage": deep_redact(payload.get("coverage", {}), propagate=True),
                "database": deep_redact(payload.get("database", {}), propagate=True),
                "warnings": deep_redact(payload.get("warnings", []), propagate=True),
                "secrets_redacted_in_report": True,
            }
        )
    # created_at is already normalized to a timestamp (or "") at construction;
    # break ties / empty-timestamp sets deterministically by backup_id so a mixed
    # set is never ordered by inconsistent keys.
    return sorted(records, key=lambda item: (str(item.get("created_at") or ""), str(item["backup_id"])))


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
            code = str(item.get("code") or item.get("type") or item.get("name") or "issue")
            message = str(item.get("message") or _issue_message_from_mapping(item, code))
            path = item.get("path") or item.get("field") or item.get("domain") or item.get("app")
            result.append(schema_issue(code, message, str(path) if path else None))
        else:
            result.append(schema_issue("issue", str(item)))
    return result


def _issue_message_from_mapping(item: Dict[str, object], code: str) -> str:
    app = item.get("app")
    domain = item.get("domain")
    path = item.get("path")
    if app and path:
        return f"{code} for `{app}` at {path}."
    if domain:
        return f"{code} for `{domain}`."
    if app:
        return f"{code} for `{app}`."
    return code


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


def _latest_successful_backup_verification(
    runtime_root: Path, app: str, environment: Optional[str]
) -> Optional[Dict[str, object]]:
    """Return the latest *succeeded* ``backup.verify.apply`` receipt for an app.

    Scans the same roots restore-drill receipts live in and reuses
    :func:`_read_json` so a malformed receipt is skipped rather than crashing.
    Only names/ids and the status are surfaced (never secret values). Returns
    ``None`` when no successful verification receipt exists, which keeps readiness
    additive: apps without verification keep their prior behavior and score.
    """
    roots = [
        runtime_root / "apps" / app / "restore-drills",
        runtime_root / "apps" / app / "receipts",
    ]
    candidates: List[Dict[str, object]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.json")):
            payload = _read_json(path)
            if str(payload.get("operation") or "") != "backup.verify.apply":
                continue
            if str(payload.get("status") or "") != "succeeded":
                continue
            if environment and payload.get("environment") not in (None, environment):
                continue
            candidates.append(
                {
                    "verify_id": payload.get("verify_id"),
                    "receipt_id": payload.get("operation_id") or path.stem,
                    "operation": payload.get("operation"),
                    "status": payload.get("status"),
                    "backup_id": payload.get("backup_id"),
                    "completed_at": payload.get("completed_at") or payload.get("started_at"),
                    "path": str(path),
                }
            )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (str(item.get("completed_at") or ""), str(item.get("receipt_id") or "")))
    return candidates[-1]


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


def _route_warning_issues(report: Dict[str, object], manifest: Manifest) -> List[Dict[str, str]]:
    issues: List[Dict[str, str]] = []
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    for warning in warnings:
        if not isinstance(warning, dict):
            continue
        if warning.get("app") != manifest.app:
            continue
        if warning.get("environment") not in {None, manifest.environment}:
            continue
        issues.extend(_as_issues([warning]))
    return issues


def _restore_drill_plan_blockers(items: object) -> List[Dict[str, str]]:
    blockers = _as_issues(items)
    return [item for item in blockers if item.get("code") != "restore_drill_missing"]


def _restore_drill_artifact_checks(source: Path) -> List[Dict[str, object]]:
    checks: List[Dict[str, object]] = []
    if not source.exists():
        return [{"name": "source_exists", "ok": False, "message": f"Source not found: {source}"}]
    checks.append({"name": "source_exists", "ok": True, "message": str(source)})
    artifacts = _source_export_artifacts(source)
    data_archives = artifacts.get("data_archives") if isinstance(artifacts.get("data_archives"), list) else []
    postgres_dumps = artifacts.get("postgres_dumps") if isinstance(artifacts.get("postgres_dumps"), list) else []
    checks.append({"name": "data_archives_present", "ok": bool(data_archives) or bool(postgres_dumps), "message": f"{len(data_archives)} archive(s), {len(postgres_dumps)} Postgres dump(s)."})
    if source.is_dir():
        checks.extend(_directory_nested_archive_checks(source, data_archives))
    elif source.suffix == ".tar":
        checks.append({"name": "bundle_tar_readable", "ok": _tar_readable(source), "message": str(source)})
    elif source.name.endswith(".tar.zst"):
        checks.append({"name": "bundle_tar_zst_readable", "ok": _tar_zst_readable(source), "message": str(source)})
    else:
        checks.append({"name": "source_artifact_scan", "ok": True, "message": "No deep archive validation available for this source type."})
    return checks


def _directory_nested_archive_checks(source: Path, records: List[object]) -> List[Dict[str, object]]:
    checks: List[Dict[str, object]] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            continue
        archive_path = source / str(record["path"])
        checks.append(
            {
                "name": f"archive_readable:{record['path']}",
                "ok": _tar_readable(archive_path),
                "message": str(record["path"]),
            }
        )
    return checks


def _tar_readable(path: Path) -> bool:
    try:
        with tarfile.open(path) as archive:
            archive.getmembers()
    except (tarfile.TarError, OSError):
        return False
    return True


def _tar_zst_readable(path: Path) -> bool:
    result = _tar_zst_member_names(path)
    return result is not None


def _pack_init_manifest_snippet(
    app: str,
    environment: Optional[str],
    critical: bool,
    postgres: bool,
    redis: bool,
    uploads: bool,
    manifest_kind: str = "service",
    static_root: str = "public",
) -> str:
    service_based = manifest_kind != "static"
    data_lines: List[str] = ["data:"]
    if manifest_kind == "static":
        data_lines.extend(
            [
                "  static_assets:",
                "    - name: static-root",
                f"      source: {static_root}",
            ]
        )
    if postgres:
        data_lines.extend(
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
        data_lines.extend(["  redis:", "    mode: redis-logical-db"])
    if uploads:
        data_lines.extend(
            [
                "  volumes:",
                "    - name: uploads",
                "      mount: /app/uploads",
                "      class: critical",
                "      export: tar-zstd",
                "      import: tar-zstd",
            ]
        )
    backup_required = critical or postgres or redis or uploads
    data_lines.extend(
        [
            "  backups:",
            f"    required: {_yaml_bool(backup_required)}",
            f"    restore_drill_required: {_yaml_bool(backup_required)}",
            f"    offsite_required: {_yaml_bool(backup_required)}",
        ]
    )
    lines = [
        "pack:",
        f"  portability: {'critical' if critical else 'static' if manifest_kind == 'static' else 'standard'}",
        "  owner: personal",
        f"  description: {app} {environment or 'environment'} app",
        "  deploy_binding_file: ophelia/deploy.json",
        "",
        "host_requirements:",
        "  min_memory: 1g",
        "  min_disk_free: 20g",
        "  requires_edge: true",
        f"  requires_docker: {_yaml_bool(service_based)}",
        "",
    ]
    if service_based:
        lines.extend(
            [
                "networking:",
                "  internal: per-app",
                "",
            ]
        )
    lines.extend(data_lines)
    lines.extend(
        [
            "",
            "hooks:",
            "  pre_export: ophelia/hooks/pre-export.sh",
            "  freeze: ophelia/hooks/freeze.sh",
            "  unfreeze: ophelia/hooks/unfreeze.sh",
            "  post_import: ophelia/hooks/post-import.sh",
        ]
    )
    return "\n".join(lines) + "\n"


def _yaml_bool(value: bool) -> str:
    return "true" if value else "false"


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
    include_postgres: bool = False,
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
        "include_postgres": include_postgres,
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
        links = external_symlinks(path, app_root)
        result.append(
            {
                "path": str(relative_path),
                "present": path.exists() or path.is_symlink(),
                "kind": "directory" if path.is_dir() else "file",
                "size_bytes": _path_size(path) if path.exists() else 0,
                "symlink": path.is_symlink(),
                "external_symlink": bool(links),
                "symlink_target": str(links[0].readlink()) if links else None,
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


def _resolved_export_bundle_tar(plan: Dict[str, object], bundle_directory: Path) -> Path:
    artifact_paths = plan.get("artifact_paths") if isinstance(plan.get("artifact_paths"), dict) else {}
    planned = artifact_paths.get("bundle_tar") if isinstance(artifact_paths.get("bundle_tar"), str) else None
    timestamp = bundle_directory.name.rsplit(".", 1)[-1]
    if planned:
        return Path(planned.replace("<timestamp>", timestamp))
    return bundle_directory.with_suffix(EXPORT_BUNDLE_TAR_EXTENSION)


def _resolved_export_bundle_archive(plan: Dict[str, object], bundle_directory: Path) -> Path:
    artifact_paths = plan.get("artifact_paths") if isinstance(plan.get("artifact_paths"), dict) else {}
    planned = artifact_paths.get("bundle") if isinstance(artifact_paths.get("bundle"), str) else None
    timestamp = bundle_directory.name.rsplit(".", 1)[-1]
    if planned:
        return Path(planned.replace("<timestamp>", timestamp))
    return bundle_directory.with_suffix(EXPORT_BUNDLE_EXTENSION)


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
        "networking": plan.get("networking", {}),
        "compose_networks": plan.get("compose_networks", {}),
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
        assert_no_external_symlinks(source, app_root)
        if source.is_symlink():
            shutil.copy2(source, target, follow_symlinks=False)
        elif relative == Path("manifest.lock.json"):
            _write_json(target, _redact_manifest_lock(_read_json(source)))
        elif relative == Path("compose.yml"):
            target.write_text(_redacted_compose_text(source.read_text()))
        elif source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
        else:
            shutil.copy2(source, target, follow_symlinks=False)
        copied.append(
            {
                "path": str(Path("runtime") / relative),
                "kind": "directory" if source.is_dir() else "file",
                "size_bytes": _path_size(target),
                "secret_values_redacted": relative in {Path("manifest.lock.json"), Path("compose.yml")},
            }
        )
    return copied


def _external_runtime_symlinks(app_root: Path) -> List[Path]:
    links: List[Path] = []
    for item in _runtime_export_files(app_root):
        if not item.get("present"):
            continue
        links.extend(external_symlinks(app_root / Path(str(item["path"])), app_root))
    return links


def _create_data_archives(
    manifest: Optional[Manifest],
    manifest_path: Optional[Path],
    app_root: Path,
    bundle_directory: Path,
) -> List[Dict[str, object]]:
    if manifest is None:
        return []
    records: List[Dict[str, object]] = []
    for source in _static_asset_sources(manifest, manifest_path):
        target = bundle_directory / "data" / "static-assets" / f"{_safe_artifact_name(str(source['name']))}.tar"
        records.append(_archive_data_source(source, target))
    for volume in manifest.data.volumes:
        source = _volume_source(volume, manifest_path, app_root)
        target = bundle_directory / "data" / "volumes" / f"{_safe_artifact_name(volume.name)}.tar"
        metadata_path = target.with_suffix(".metadata.json")
        if source is None:
            record = {
                "name": volume.name,
                "data": "volume",
                "status": "source_unresolved",
                "archive_path": None,
                "metadata_path": str(metadata_path.relative_to(bundle_directory)),
                "reason": "Volume contract does not declare a local source path and no compatible runtime data path exists.",
                "mount": volume.mount,
                "source": volume.source,
            }
            _write_json(metadata_path, record)
            records.append(record)
            continue
        records.append(
            _archive_data_source(
                {
                    "name": volume.name,
                    "data": "volume",
                    "source": source,
                    "contract_source": volume.source,
                    "mount": volume.mount,
                },
                target,
            )
        )
    return records


def _static_asset_sources(manifest: Manifest, manifest_path: Optional[Path]) -> List[Dict[str, object]]:
    sources: List[Dict[str, object]] = []
    if manifest.kind == "static" and manifest.static_root:
        sources.append(
            {
                "name": manifest.app,
                "data": "static_assets",
                "source": _resolve_pack_path(manifest.static_root, manifest_path),
                "contract_source": manifest.static_root,
            }
        )
    for index, item in enumerate(manifest.data.static_assets):
        name = item.get("name") or item.get("id") or f"static-{index + 1}"
        raw_source = item.get("source") or item.get("path") or item.get("root")
        if isinstance(raw_source, str) and raw_source:
            sources.append(
                {
                    "name": str(name),
                    "data": "static_assets",
                    "source": _resolve_pack_path(raw_source, manifest_path),
                    "contract_source": raw_source,
                }
            )
    return sources


def _volume_source(volume: DataVolumeConfig, manifest_path: Optional[Path], app_root: Path) -> Optional[Path]:
    candidates: List[Path] = []
    for raw in (volume.source, volume.extra.get("path"), volume.extra.get("source_path"), volume.extra.get("host_path")):
        if isinstance(raw, str) and raw:
            candidates.append(_resolve_pack_path(raw, manifest_path))
    candidates.extend(
        [
            app_root / "data" / "volumes" / volume.name,
            app_root / "volumes" / volume.name,
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _archive_data_source(source: Dict[str, object], target: Path) -> Dict[str, object]:
    source_path = Path(str(source["source"]))
    metadata_path = target.with_suffix(".metadata.json")
    if not source_path.exists():
        record = {
            "name": source.get("name"),
            "data": source.get("data"),
            "status": "source_missing",
            "archive_path": None,
            "metadata_path": str(metadata_path),
            "source_present": False,
            "contract_source": source.get("contract_source"),
        }
        _write_json(metadata_path, record)
        return record
    target.parent.mkdir(parents=True, exist_ok=True)
    _create_tar_from_path(source_path, target)
    record = {
        "name": source.get("name"),
        "data": source.get("data"),
        "status": "archived",
        "archive_path": str(target),
        "metadata_path": str(metadata_path),
        "source_present": True,
        "kind": "directory" if source_path.is_dir() else "file",
        "size_bytes": _path_size(source_path),
        "archive_size_bytes": target.stat().st_size,
        "contract_source": source.get("contract_source"),
        "secret_values_redacted": False,
    }
    if source.get("mount"):
        record["mount"] = source.get("mount")
    _write_json(metadata_path, record)
    return record


def _create_postgres_exports(
    manifest: Optional[Manifest],
    app_root: Path,
    bundle_directory: Path,
    include: bool,
) -> List[Dict[str, object]]:
    if manifest is None or manifest.data.postgres is None:
        return []
    postgres = manifest.data.postgres
    database = postgres.database or manifest.app.replace("-", "_")
    target = bundle_directory / "data" / "postgres" / f"{_safe_artifact_name(database)}.dump"
    metadata_path = target.with_suffix(".metadata.json")
    if not include:
        record = {
            "name": "postgres.export",
            "data": "postgres",
            "status": "not_requested",
            "required": False,
            "archive_path": None,
            "metadata_path": str(metadata_path.relative_to(bundle_directory)),
            "mode": postgres.mode,
            "database": database,
            "reason": "Pass --include-postgres on both export plan and export create to run pg_dump.",
        }
        _write_json(metadata_path, record)
        return [record]

    env_values = _env_values(app_root / "env")
    database_url = env_values.get("DATABASE_URL")
    if not database_url:
        record = _postgres_export_record("database_url_missing", postgres, database, target, metadata_path, "DATABASE_URL is not present in runtime env.")
        _write_json(metadata_path, record)
        return [record]
    pg_dump = str(postgres.export.get("command") or "pg_dump")
    if Path(pg_dump).name != "pg_dump" or any(character.isspace() for character in pg_dump):
        record = _postgres_export_record("command_not_allowlisted", postgres, database, target, metadata_path, "Only the pg_dump command is allowlisted for Postgres export.")
        _write_json(metadata_path, record)
        return [record]
    executable = shutil.which(pg_dump)
    if executable is None:
        record = _postgres_export_record("tool_missing", postgres, database, target, metadata_path, "pg_dump is not available on PATH.")
        _write_json(metadata_path, record)
        return [record]

    target.parent.mkdir(parents=True, exist_ok=True)
    format_name = str(postgres.export.get("format") or "custom")
    timeout = _positive_int(postgres.export.get("timeout_seconds"), default=600)
    try:
        pg_env = _pg_env_from_database_url(database_url)
    except ValueError as exc:
        record = _postgres_export_record("database_url_invalid", postgres, database, target, metadata_path, str(exc))
        _write_json(metadata_path, record)
        return [record]
    env = {**os.environ, **pg_env}
    args = [executable, "--format", format_name, "--file", str(target)]
    try:
        result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        record = _postgres_export_record("timeout", postgres, database, target, metadata_path, f"pg_dump exceeded {timeout} second timeout.")
        _write_json(metadata_path, record)
        return [record]
    except OSError as exc:
        record = _postgres_export_record("execution_failed", postgres, database, target, metadata_path, f"pg_dump could not start: {exc.__class__.__name__}.")
        _write_json(metadata_path, record)
        return [record]
    if result.returncode != 0:
        record = _postgres_export_record("pg_dump_failed", postgres, database, target, metadata_path, f"pg_dump exited with code {result.returncode}.")
        _write_json(metadata_path, record)
        return [record]
    record = {
        "name": "postgres.export",
        "data": "postgres",
        "status": "dumped",
        "required": True,
        "archive_path": str(target),
        "metadata_path": str(metadata_path.relative_to(bundle_directory)),
        "mode": postgres.mode,
        "database": database,
        "format": format_name,
        "size_bytes": target.stat().st_size if target.exists() else 0,
        "secret_values_redacted": True,
        "command": "pg_dump",
    }
    _write_json(metadata_path, record)
    return [record]


def _postgres_export_record(
    status: str,
    postgres: DataServiceConfig,
    database: str,
    target: Path,
    metadata_path: Path,
    reason: str,
) -> Dict[str, object]:
    return {
        "name": "postgres.export",
        "data": "postgres",
        "status": status,
        "required": True,
        "archive_path": str(target),
        "metadata_path": str(metadata_path),
        "mode": postgres.mode,
        "database": database,
        "reason": reason,
        "secret_values_redacted": True,
        "command": "pg_dump",
    }


def _pg_env_from_database_url(database_url: str) -> Dict[str, str]:
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must use postgres or postgresql scheme.")
    if not parsed.hostname:
        raise ValueError("DATABASE_URL is missing a host.")
    database = parsed.path.lstrip("/")
    if not database:
        raise ValueError("DATABASE_URL is missing a database name.")
    env = {
        "PGHOST": parsed.hostname,
        "PGDATABASE": unquote(database),
    }
    if parsed.port is not None:
        env["PGPORT"] = str(parsed.port)
    if parsed.username:
        env["PGUSER"] = unquote(parsed.username)
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    query = parse_qs(parsed.query)
    sslmode = query.get("sslmode", [None])[0]
    if sslmode:
        env["PGSSLMODE"] = sslmode
    return env


def _postgres_check_message(records: List[Dict[str, object]], include: bool) -> str:
    if not records:
        return "No Postgres contract declared."
    status = records[0].get("status")
    if status == "dumped":
        return "Postgres dump artifact created."
    if not include:
        return "Postgres dump not requested."
    return f"Postgres dump did not complete: {status}."


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return default
        return parsed if parsed > 0 else default
    return default


def _resolve_pack_path(value: str, manifest_path: Optional[Path]) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute() or manifest_path is None:
        return path
    return manifest_path.parent / path


def _safe_artifact_name(value: str) -> str:
    safe = "".join(character if character.isalnum() or character in {"-", "_", "."} else "-" for character in value)
    return safe.strip(".-") or "artifact"


def _redact_manifest_lock(payload: Dict[str, object]) -> Dict[str, object]:
    redacted = json.loads(json.dumps(payload))
    if isinstance(redacted.get("env"), dict):
        redacted["env"] = {key: "<redacted>" for key in redacted["env"]}
    services = redacted.get("services")
    if isinstance(services, dict):
        for service in services.values():
            if isinstance(service, dict) and isinstance(service.get("env"), dict):
                service["env"] = {key: "<redacted>" for key in service["env"]}
    # Defense-in-depth: mask any secret-shaped value nested elsewhere in the
    # lock, not just the two known env locations above.
    redacted = deep_redact(redacted, propagate=True)
    redacted["secret_values_redacted"] = True
    return redacted


def _redacted_compose_text(content: str) -> str:
    return redacted_compose_text(content)


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
    return deep_redact(redacted, safe_keys={"inputs_redacted", "secrets_redacted"}, propagate=True)


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


def _create_tar_archive(bundle_directory: Path, archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "w") as archive:
        archive.add(bundle_directory, arcname=".")


def _create_zstd_archive(tar_path: Path, archive_path: Path) -> Dict[str, object]:
    zstd = shutil.which("zstd")
    if not zstd or Path(zstd).name != "zstd":
        return {
            "status": "skipped",
            "path": str(archive_path),
            "reason": "zstd executable was not found; uncompressed .tar archive was created.",
        }
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [zstd, "-q", "-f", "-o", str(archive_path), str(tar_path)],
            text=True,
            capture_output=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"status": "failed", "path": str(archive_path), "reason": "zstd timed out."}
    except OSError as exc:
        return {
            "status": "failed",
            "path": str(archive_path),
            "reason": f"zstd could not start: {exc.__class__.__name__}.",
        }
    if result.returncode != 0 or not archive_path.exists():
        return {
            "status": "failed",
            "path": str(archive_path),
            "reason": f"zstd exited with code {result.returncode}.",
        }
    return {
        "status": "created",
        "path": str(archive_path),
        "size_bytes": archive_path.stat().st_size,
        "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
    }


def _create_tar_from_path(source_path: Path, archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "w") as archive:
        if source_path.is_dir():
            for child in sorted(source_path.rglob("*")):
                archive.add(child, arcname=str(child.relative_to(source_path)), recursive=False)
        else:
            archive.add(source_path, arcname=source_path.name)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _atomic_write_json(path: Path, payload: object) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(payload)
    temp_path.replace(path)


def _shell_command(parts: Iterable[object]) -> str:
    return " ".join(shlex.quote(str(part)) for part in parts)


def _data_export_status(
    plan: Dict[str, object],
    data_archives: Optional[List[Dict[str, object]]] = None,
    postgres_exports: Optional[List[Dict[str, object]]] = None,
) -> List[Dict[str, object]]:
    commands = plan.get("commands") if isinstance(plan.get("commands"), list) else []
    statuses = [
        {
            "name": item.get("name"),
            "data": item.get("data"),
            "status": "planned_not_executed",
            "reason": "metadata-and-runtime-files export create does not run live data commands",
        }
        for item in commands
        if isinstance(item, dict)
    ]
    for item in data_archives or []:
        statuses.append(
            {
                "name": item.get("name"),
                "data": item.get("data"),
                "status": item.get("status"),
                "archive_path": item.get("archive_path"),
                "metadata_path": item.get("metadata_path"),
                "reason": item.get("reason"),
            }
        )
    for item in postgres_exports or []:
        statuses.append(
            {
                "name": item.get("name"),
                "data": item.get("data"),
                "status": item.get("status"),
                "archive_path": item.get("archive_path"),
                "metadata_path": item.get("metadata_path"),
                "reason": item.get("reason"),
                "required": item.get("required"),
            }
        )
    return statuses


def _export_commands(manifest: Manifest) -> List[Dict[str, object]]:
    commands: List[Dict[str, object]] = []
    data = manifest.data
    if data.postgres:
        export = data.postgres.export
        command = redact_command_string(str(export.get("command") or "pg_dump"))
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
                "command": (
                    redact_command_string(str(export.get("command")))
                    if export.get("command")
                    else "redis export is not enabled by default for logical DB cache state"
                ),
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


def _source_export_artifacts(source: Path) -> Dict[str, object]:
    if source.is_dir():
        return _directory_export_artifacts(source)
    if source.suffix == ".tar":
        return _tar_export_artifacts(source)
    if source.name.endswith(".tar.zst"):
        return _tar_zst_export_artifacts(source)
    return {"data_archives": [], "postgres_dumps": [], "runtime_files": [], "receipts": []}


def _directory_export_artifacts(source: Path) -> Dict[str, object]:
    files = [path for path in sorted(source.rglob("*")) if path.is_file()]
    return {
        "data_archives": [
            _artifact_record(source, path)
            for path in files
            if _relative_parts(source, path)[:2] in {("data", "volumes"), ("data", "static-assets")} and path.suffix == ".tar"
        ],
        "postgres_dumps": [
            _artifact_record(source, path)
            for path in files
            if _relative_parts(source, path)[:2] == ("data", "postgres")
            and path.suffix in {".dump", ".sql", ".tar"}
        ],
        "runtime_files": [
            _artifact_record(source, path)
            for path in files
            if _relative_parts(source, path)[:1] == ("runtime",)
        ],
        "receipts": [
            _artifact_record(source, path)
            for path in files
            if "receipts" in _relative_parts(source, path) and path.suffix == ".json"
        ],
    }


def _tar_export_artifacts(source: Path) -> Dict[str, object]:
    try:
        with tarfile.open(source) as archive:
            members = [member for member in archive.getmembers() if member.isfile()]
    except tarfile.TarError:
        return {"data_archives": [], "postgres_dumps": [], "runtime_files": [], "receipts": [], "scan_error": "tar_read_failed"}
    return {
        "data_archives": [
            _tar_artifact_record(member)
            for member in members
            if _normalized_tar_member(member.name).startswith(("data/volumes/", "data/static-assets/"))
            and _normalized_tar_member(member.name).endswith(".tar")
        ],
        "postgres_dumps": [
            _tar_artifact_record(member)
            for member in members
            if _normalized_tar_member(member.name).startswith("data/postgres/")
            and Path(_normalized_tar_member(member.name)).suffix in {".dump", ".sql", ".tar"}
        ],
        "runtime_files": [
            _tar_artifact_record(member)
            for member in members
            if _normalized_tar_member(member.name).startswith("runtime/")
        ],
        "receipts": [
            _tar_artifact_record(member)
            for member in members
            if _normalized_tar_member(member.name).startswith("receipts/")
            and _normalized_tar_member(member.name).endswith(".json")
        ],
    }


def _tar_zst_export_artifacts(source: Path) -> Dict[str, object]:
    members = _tar_zst_member_names(source)
    if members is None:
        return {
            "data_archives": [],
            "postgres_dumps": [],
            "runtime_files": [],
            "receipts": [],
            "unsupported_scan": "tar.zst artifact scan requires local tar and zstd support",
        }
    return {
        "data_archives": [
            _tar_zst_artifact_record(name)
            for name in members
            if _normalized_tar_member(name).startswith(("data/volumes/", "data/static-assets/"))
            and _normalized_tar_member(name).endswith((".tar", ".tar.zst"))
        ],
        "postgres_dumps": [
            _tar_zst_artifact_record(name)
            for name in members
            if _normalized_tar_member(name).startswith("data/postgres/")
            and Path(_normalized_tar_member(name)).suffix in {".dump", ".sql", ".tar"}
        ],
        "runtime_files": [
            _tar_zst_artifact_record(name)
            for name in members
            if _normalized_tar_member(name).startswith("runtime/")
        ],
        "receipts": [
            _tar_zst_artifact_record(name)
            for name in members
            if _normalized_tar_member(name).startswith("receipts/")
            and _normalized_tar_member(name).endswith(".json")
        ],
    }


def _artifact_record(root: Path, path: Path) -> Dict[str, object]:
    return {
        "path": str(path.relative_to(root)),
        "size_bytes": path.stat().st_size,
        "kind": "file",
        "present": True,
    }


def _relative_parts(root: Path, path: Path) -> Tuple[str, ...]:
    return path.relative_to(root).parts


def _tar_artifact_record(member: tarfile.TarInfo) -> Dict[str, object]:
    return {
        "path": _normalized_tar_member(member.name),
        "size_bytes": member.size,
        "kind": "file",
        "present": True,
    }


def _tar_zst_artifact_record(name: str) -> Dict[str, object]:
    return {
        "path": _normalized_tar_member(name),
        "size_bytes": None,
        "kind": "file",
        "present": True,
    }


def _normalized_tar_member(name: str) -> str:
    return name[2:] if name.startswith("./") else name


def _read_tar_metadata(source: Path) -> Dict[str, object]:
    try:
        with tarfile.open(source) as archive:
            for member_name in _export_metadata_member_names():
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
    for member_name in _export_metadata_member_names():
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


def _tar_zst_member_names(source: Path) -> Optional[List[str]]:
    try:
        result = subprocess.run(
            ["tar", "--use-compress-program=zstd", "-tf", str(source)],
            text=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _export_metadata_member_names() -> Tuple[str, ...]:
    return (
        "manifest.json",
        "./manifest.json",
        "receipts/export-plan.json",
        "./receipts/export-plan.json",
        "export-plan.json",
        "./export-plan.json",
    )


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


def _metadata_networking(metadata: Dict[str, object]) -> Dict[str, object]:
    value = metadata.get("networking")
    if isinstance(value, dict):
        return value
    value = metadata.get("compose_networks")
    if isinstance(value, dict):
        internal_mode = value.get("internal_mode")
        edge_mode = value.get("edge_mode")
        return {
            "edge": edge_mode if isinstance(edge_mode, str) else "shared",
            "internal": internal_mode if isinstance(internal_mode, str) else "shared",
        }
    manifest = metadata.get("manifest")
    if isinstance(manifest, dict) and isinstance(manifest.get("networking"), dict):
        return manifest["networking"]  # type: ignore[return-value]
    return {}


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


def _required_docker_networks(
    app: Optional[str],
    environment: Optional[str],
    networking: Dict[str, object],
) -> Dict[str, object]:
    internal_mode = networking.get("internal") if isinstance(networking.get("internal"), str) else "shared"
    edge_mode = networking.get("edge") if isinstance(networking.get("edge"), str) else "shared"
    target_internal = (
        f"{app}-{environment or 'unknown'}-internal"
        if app and internal_mode == "per-app"
        else "ophelia-internal"
    )
    return {
        "edge": "ophelia-edge",
        "edge_mode": edge_mode,
        "internal": target_internal,
        "internal_mode": internal_mode,
        "current_compatibility": ["ophelia-edge", "ophelia-internal"],
        "target_private_network": target_internal if internal_mode == "per-app" else None,
    }


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
