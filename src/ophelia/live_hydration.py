from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .drift import manifest_drift
from .host_inventory import app_placement_plan
from .live_drills import DEFAULT_LOCAL_LIVE_DRILL_PROFILES, resolve_live_drill_profile
from .manifest import load_manifest
from .operation_schema import SCHEMA_VERSION, issue, operation_id
from .portability import app_readiness_report, env_shape_diff_report, resolve_app_manifest
from .redaction import deep_redact
from .runtime import active_release, latest_release_id, list_releases
from .secret_providers import secret_provider_report

LIVE_HYDRATION_KIND = "ophelia.live_hydration_report"
LIVE_HYDRATION_SCAFFOLD_KIND = "ophelia.live_hydration_scaffold"


def live_hydration_report(
    *,
    app: Optional[str] = None,
    environment: Optional[str] = None,
    profile: Optional[str] = None,
    profiles_path: Path = DEFAULT_LOCAL_LIVE_DRILL_PROFILES,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifests_dir: Path = REPO_ROOT / "manifests",
    ophelia_root: Path = REPO_ROOT,
    manifest_path: Optional[Path] = None,
    host_config: Optional[Path] = None,
    provider_config: Optional[Path] = None,
    target_host: Optional[str] = None,
) -> Dict[str, Any]:
    """Read-only report for hydrating one live app baseline.

    The report tells operators which non-secret runtime observations are missing
    before opt-in HTTP/Docker/provider probes should be attempted. It never
    creates runtime files or reads secret values directly.
    """
    resolved = _resolve_inputs(
        app=app,
        environment=environment,
        profile=profile,
        profiles_path=profiles_path,
        runtime_root=runtime_root,
        manifests_dir=manifests_dir,
        ophelia_root=ophelia_root,
        manifest_path=manifest_path,
        host_config=host_config,
        provider_config=provider_config,
        target_host=target_host,
    )
    blockers: List[Dict[str, str]] = list(resolved.get("blockers", []))
    warnings: List[Dict[str, str]] = list(resolved.get("warnings", []))
    if blockers:
        return _redact(_base_payload(resolved, blockers, warnings, sections={}, hydration_steps=[]))

    resolved_app = str(resolved["app"])
    resolved_environment = str(resolved.get("environment") or "unknown")
    runtime_root = Path(str(resolved["runtime_root"]))
    manifests_dir = Path(str(resolved["manifests_dir"]))
    ophelia_root = Path(str(resolved["ophelia_root"]))
    host_config = Path(str(resolved["host_config"])) if resolved.get("host_config") else None
    provider_config = Path(str(resolved["provider_config"])) if resolved.get("provider_config") else None
    manifest_path = Path(str(resolved["manifest_path"]))
    app_root = runtime_root / "apps" / resolved_app

    env_report = env_shape_diff_report(resolved_app, resolved_environment, runtime_root, manifest_path)
    secret_report = secret_provider_report(
        manifest_path,
        environment=resolved_environment,
        runtime_root=runtime_root,
        ophelia_root=ophelia_root,
        config_path=provider_config,
    )
    placement = app_placement_plan(
        resolved_app,
        environment=resolved_environment,
        runtime_root=runtime_root,
        manifests_dir=manifests_dir,
        manifest_path=manifest_path,
        ophelia_root=ophelia_root,
        config_path=host_config,
        target_host=target_host,
    )
    readiness = app_readiness_report(resolved_app, resolved_environment, runtime_root, manifest_path)
    manifest = load_manifest(manifest_path)
    drift = manifest_drift(manifest, manifest_path, runtime_root)

    sections = {
        "runtime": _runtime_section(runtime_root, app_root),
        "env": _env_section(env_report, app_root),
        "secrets": _secret_section(secret_report),
        "release": _release_section(runtime_root, resolved_app),
        "host": _host_section(placement),
        "drift": _drift_section(drift),
        "readiness": _compact_report(readiness),
    }
    hydration_steps = _hydration_steps(
        sections,
        resolved_app,
        resolved_environment,
        runtime_root,
        manifest_path,
        host_config,
        provider_config,
        _optional_str(resolved.get("profile")),
        _optional_str(resolved.get("profiles_path")),
    )
    blockers.extend(_section_blockers(sections))
    warnings.extend(_section_warnings(sections))
    status = "blocked" if blockers else "warning" if warnings else "ok"
    payload = _base_payload(resolved, blockers, warnings, sections=sections, hydration_steps=hydration_steps)
    payload["status"] = status
    payload["summary"] = f"Live hydration for {resolved_app}/{resolved_environment}: {status}; {len(hydration_steps)} step(s)."
    return _redact(payload)


def live_hydration_scaffold(
    *,
    app: Optional[str] = None,
    environment: Optional[str] = None,
    profile: Optional[str] = None,
    profiles_path: Path = DEFAULT_LOCAL_LIVE_DRILL_PROFILES,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifests_dir: Path = REPO_ROOT / "manifests",
    ophelia_root: Path = REPO_ROOT,
    manifest_path: Optional[Path] = None,
    host_config: Optional[Path] = None,
    provider_config: Optional[Path] = None,
    target_host: Optional[str] = None,
    output_dir: Optional[Path] = None,
    write: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    """Build or write a non-secret evidence scaffold for one hydration report."""
    report = live_hydration_report(
        app=app,
        environment=environment,
        profile=profile,
        profiles_path=profiles_path,
        runtime_root=runtime_root,
        manifests_dir=manifests_dir,
        ophelia_root=ophelia_root,
        manifest_path=manifest_path,
        host_config=host_config,
        provider_config=provider_config,
        target_host=target_host,
    )
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    resolved_app = _optional_str(report.get("app"))
    resolved_environment = _optional_str(report.get("environment"))
    resolved_runtime_root = Path(str(report.get("runtime_root") or runtime_root))
    if report.get("status") in {"blocked", "warning"}:
        warnings.append(
            issue(
                "hydration_report_not_clean",
                f"Hydration report is {report.get('status')}; scaffold templates are for collecting evidence, not satisfying readiness.",
                "hydration",
            )
        )
    if resolved_app is None:
        blockers.append(issue("live_hydration_scaffold_app_missing", "Scaffold requires a resolved app.", "app"))
    if resolved_environment is None:
        blockers.append(issue("live_hydration_scaffold_environment_missing", "Scaffold requires a resolved environment.", "environment"))
    if report.get("manifest_path") is None:
        blockers.append(issue("live_hydration_scaffold_manifest_missing", "Scaffold requires a resolved manifest path.", "manifest"))

    scaffold_root = Path(output_dir) if output_dir is not None else resolved_runtime_root / "hydration" / (resolved_app or "unknown") / (resolved_environment or "unknown")
    files: List[Dict[str, Any]] = []
    if not blockers and resolved_app is not None and resolved_environment is not None:
        files = _scaffold_files(report, scaffold_root, resolved_app, resolved_environment, resolved_runtime_root)
        if write:
            blockers.extend(_write_scaffold_files(files, force=force))

    status = "blocked" if blockers else "warning" if warnings else "ok"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": LIVE_HYDRATION_SCAFFOLD_KIND,
        "operation": "live_hydration.scaffold",
        "operation_id": operation_id("live_hydration.scaffold", resolved_app, resolved_environment),
        "status": status,
        "app": resolved_app,
        "environment": resolved_environment,
        "profile": report.get("profile"),
        "profiles_path": report.get("profiles_path"),
        "runtime_root": str(resolved_runtime_root),
        "output_dir": str(scaffold_root),
        "manifest_path": report.get("manifest_path"),
        "host_config": report.get("host_config"),
        "provider_config": report.get("provider_config"),
        "read_only": not write,
        "dry_run": not write,
        "mutates_state": bool(write),
        "confirmation_required": False,
        "write_requested": bool(write),
        "force": bool(force),
        "values_redacted": True,
        "template_only": True,
        "target_paths": _target_paths(report, resolved_app, resolved_environment, resolved_runtime_root),
        "hydration_status": report.get("status"),
        "hydration_blocker_count": len(report.get("blockers", [])) if isinstance(report.get("blockers"), list) else 0,
        "hydration_warning_count": len(report.get("warnings", [])) if isinstance(report.get("warnings"), list) else 0,
        "files": files,
        "blockers": _dedupe_issues(blockers),
        "warnings": _dedupe_issues(warnings),
        "summary": f"Live hydration scaffold for {resolved_app or 'unknown'}/{resolved_environment or 'unknown'}: {status}; {len(files)} template file(s).",
    }
    return _redact(payload)


def _resolve_inputs(
    *,
    app: Optional[str],
    environment: Optional[str],
    profile: Optional[str],
    profiles_path: Path,
    runtime_root: Path,
    manifests_dir: Path,
    ophelia_root: Path,
    manifest_path: Optional[Path],
    host_config: Optional[Path],
    provider_config: Optional[Path],
    target_host: Optional[str],
) -> Dict[str, Any]:
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    raw_profile: Dict[str, Any] = {}
    if profile:
        resolution = resolve_live_drill_profile(profile, profiles_path)
        blockers.extend(_issues(resolution.get("blockers")))
        warnings.extend(_issues(resolution.get("warnings")))
        raw_profile = resolution.get("raw_profile") if isinstance(resolution.get("raw_profile"), dict) else {}
        paths = resolution.get("paths") if isinstance(resolution.get("paths"), dict) else {}
        app = app or _optional_str(raw_profile.get("app"))
        environment = environment or _optional_str(raw_profile.get("environment"))
        runtime_root = Path(str(paths.get("runtime_root") or runtime_root))
        manifests_dir = Path(str(paths.get("manifests_dir") or manifests_dir))
        ophelia_root = Path(str(paths.get("ophelia_root") or ophelia_root))
        manifest_path = Path(str(paths["manifest"])) if paths.get("manifest") else manifest_path
        host_config = Path(str(paths["host_config"])) if paths.get("host_config") else host_config
        provider_config = Path(str(paths["provider_config"])) if paths.get("provider_config") else provider_config
        target_host = target_host or _optional_str(raw_profile.get("target_host"))
        if app is None:
            blockers.append(issue("live_hydration_profile_app_missing", "Hydration requires a profile with an `app` or an explicit --app.", "profile"))
    if app is None:
        blockers.append(issue("live_hydration_app_missing", "Hydration requires --app or a profile with an app.", "app"))
    if blockers:
        return {
            "app": app,
            "environment": environment,
            "profile": profile,
            "profiles_path": str(profiles_path),
            "runtime_root": str(runtime_root),
            "manifests_dir": str(manifests_dir),
            "ophelia_root": str(ophelia_root),
            "manifest_path": str(manifest_path) if manifest_path else None,
            "host_config": str(host_config) if host_config else None,
            "provider_config": str(provider_config) if provider_config else None,
            "target_host": target_host,
            "blockers": blockers,
            "warnings": warnings,
        }

    resolution = resolve_app_manifest(str(app), environment, manifest_path=manifest_path, search_dirs=[manifests_dir])
    warnings.extend(issue("manifest_resolution_warning", item, "manifest") for item in resolution.warnings)
    blockers.extend(issue("manifest_resolution_blocker", item, "manifest") for item in resolution.blockers)
    if resolution.manifest is None or resolution.manifest_path is None:
        blockers.append(issue("manifest_unresolved", f"No manifest resolved for `{app}`.", "manifest"))
        resolved_environment = environment or "unknown"
        resolved_manifest_path = manifest_path
    else:
        resolved_environment = environment or resolution.manifest.environment or "unknown"
        resolved_manifest_path = resolution.manifest_path
    return {
        "app": app,
        "environment": resolved_environment,
        "profile": profile,
        "profiles_path": str(profiles_path),
        "runtime_root": str(runtime_root),
        "manifests_dir": str(manifests_dir),
        "ophelia_root": str(ophelia_root),
        "manifest_path": str(resolved_manifest_path) if resolved_manifest_path else None,
        "host_config": str(host_config) if host_config else None,
        "provider_config": str(provider_config) if provider_config else None,
        "target_host": target_host,
        "blockers": blockers,
        "warnings": warnings,
    }


def _base_payload(
    resolved: Dict[str, Any],
    blockers: List[Dict[str, str]],
    warnings: List[Dict[str, str]],
    *,
    sections: Dict[str, Any],
    hydration_steps: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": LIVE_HYDRATION_KIND,
        "operation": "live_hydration.report",
        "operation_id": operation_id("live_hydration.report", _optional_str(resolved.get("app")), _optional_str(resolved.get("environment"))),
        "status": "blocked" if blockers else "warning" if warnings else "ok",
        "app": resolved.get("app"),
        "environment": resolved.get("environment"),
        "profile": resolved.get("profile"),
        "profiles_path": resolved.get("profiles_path"),
        "runtime_root": resolved.get("runtime_root"),
        "manifests_dir": resolved.get("manifests_dir"),
        "ophelia_root": resolved.get("ophelia_root"),
        "manifest_path": resolved.get("manifest_path"),
        "host_config": resolved.get("host_config"),
        "provider_config": resolved.get("provider_config"),
        "target_host": resolved.get("target_host"),
        "read_only": True,
        "dry_run": True,
        "mutates_state": False,
        "confirmation_required": False,
        "probe_policy": {"http": {"enabled": False}, "docker": {"enabled": False}},
        "sections": sections,
        "hydration_steps": hydration_steps,
        "blockers": _dedupe_issues(blockers),
        "warnings": _dedupe_issues(warnings),
        "summary": "Live hydration report.",
    }


def _runtime_section(runtime_root: Path, app_root: Path) -> Dict[str, Any]:
    expected = {
        "app_root": app_root,
        "env": app_root / "env",
        "active_release": app_root / "active_release.json",
        "legacy_release": app_root / "release.json",
        "releases_dir": app_root / "releases",
    }
    return {
        "status": "ok" if app_root.exists() else "blocked",
        "app_root": str(app_root),
        "runtime_root": str(runtime_root),
        "expected_paths": {name: str(path) for name, path in expected.items()},
        "present": {name: path.exists() for name, path in expected.items()},
    }


def _env_section(env_report: Dict[str, Any], app_root: Path) -> Dict[str, Any]:
    entries = env_report.get("entries") if isinstance(env_report.get("entries"), list) else []
    missing = [entry for entry in entries if isinstance(entry, dict) and entry.get("required") and entry.get("status") == "missing"]
    placeholder = [entry for entry in entries if isinstance(entry, dict) and entry.get("required") and entry.get("status") == "placeholder"]
    present = [entry for entry in entries if isinstance(entry, dict) and entry.get("required") and entry.get("status") == "present"]
    return {
        "status": "blocked" if missing or placeholder else "ok",
        "env_path": str(app_root / "env"),
        "required_count": sum(1 for entry in entries if isinstance(entry, dict) and entry.get("required")),
        "present_required_count": len(present),
        "missing_required": _entry_keys(missing),
        "placeholder_required": _entry_keys(placeholder),
        "extra_keys": _entry_keys([entry for entry in entries if isinstance(entry, dict) and entry.get("status") == "extra"]),
        "blocker_count": len(env_report.get("blockers", [])) if isinstance(env_report.get("blockers"), list) else 0,
    }


def _secret_section(secret_report: Dict[str, Any]) -> Dict[str, Any]:
    keys = secret_report.get("keys") if isinstance(secret_report.get("keys"), list) else []
    missing = [key for key in keys if isinstance(key, dict) and key.get("required") and key.get("status") == "missing"]
    present = [key for key in keys if isinstance(key, dict) and key.get("required") and key.get("status") == "present"]
    provider_locations: Dict[str, List[str]] = {}
    for key in keys:
        if not isinstance(key, dict):
            continue
        for provider in key.get("providers", []) if isinstance(key.get("providers"), list) else []:
            if isinstance(provider, dict):
                name = str(provider.get("provider") or "unknown")
                location = provider.get("location")
                if isinstance(location, str) and location not in provider_locations.setdefault(name, []):
                    provider_locations[name].append(location)
    return {
        "status": "blocked" if missing else "ok",
        "required_count": sum(1 for key in keys if isinstance(key, dict) and key.get("required")),
        "present_required_count": len(present),
        "missing_required": [str(key.get("name")) for key in missing],
        "provider_locations": provider_locations,
        "blocker_count": len(secret_report.get("blockers", [])) if isinstance(secret_report.get("blockers"), list) else 0,
        "summary": secret_report.get("summary"),
    }


def _release_section(runtime_root: Path, app: str) -> Dict[str, Any]:
    active = active_release(runtime_root, app)
    latest = latest_release_id(runtime_root, app)
    releases = list_releases(runtime_root, app)
    return {
        "status": "ok" if active or latest else "blocked",
        "active_present": bool(active),
        "latest_release_id": latest,
        "release_count": len(releases),
        "expected_paths": {
            "active_release": str(runtime_root / "apps" / app / "active_release.json"),
            "legacy_release": str(runtime_root / "apps" / app / "release.json"),
            "releases_dir": str(runtime_root / "apps" / app / "releases"),
        },
    }


def _scaffold_files(report: Dict[str, Any], scaffold_root: Path, app: str, environment: str, runtime_root: Path) -> List[Dict[str, Any]]:
    env_keys, secret_keys, host_capabilities = _scaffold_inputs(report)
    target_paths = _target_paths(report, app, environment, runtime_root)
    file_specs = [
        ("readme", scaffold_root / "README.md", _scaffold_readme(app, environment, report, target_paths)),
        ("runtime_env_template", scaffold_root / "env.required.template", _env_template(env_keys, target_paths.get("runtime_env"))),
        (
            "github_secret_observation_template",
            scaffold_root / "github-secret-observation.template.json",
            _json_text(_github_secret_observation_template(app, environment, secret_keys, target_paths.get("github_secret_observation"))),
        ),
        (
            "release_metadata_template",
            scaffold_root / "release-metadata.template.json",
            _json_text(_release_metadata_template(app, environment, report, target_paths)),
        ),
        ("host_capabilities_template", scaffold_root / "host-capabilities.template.yml", _host_capabilities_template(host_capabilities, target_paths.get("host_config"))),
    ]
    files: List[Dict[str, Any]] = []
    for kind, path, content in file_specs:
        files.append(
            {
                "kind": kind,
                "path": str(path),
                "exists": path.exists(),
                "bytes": len(content.encode("utf-8")),
                "sha256": _sha256_text(content),
                "written": False,
                "content": content,
                "values_redacted": True,
                "template_only": True,
            }
        )
    return files


def _write_scaffold_files(files: List[Dict[str, Any]], *, force: bool) -> List[Dict[str, str]]:
    blockers: List[Dict[str, str]] = []
    for item in files:
        path = Path(str(item.get("path") or ""))
        content = item.get("content")
        if not isinstance(content, str):
            blockers.append(issue("live_hydration_scaffold_content_invalid", f"Scaffold content is invalid for {path}.", str(path)))
            item["written"] = False
            continue
        if path.exists() and not force:
            blockers.append(issue("live_hydration_scaffold_file_exists", f"Refusing to overwrite existing scaffold file: {path}", str(path)))
            item["written"] = False
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        except OSError as exc:
            blockers.append(issue("live_hydration_scaffold_write_failed", f"Failed to write scaffold file {path}: {exc}", str(path)))
            item["written"] = False
            continue
        item["exists"] = True
        item["written"] = True
    return blockers


def _scaffold_inputs(report: Dict[str, Any]) -> Tuple[List[str], List[str], Dict[str, Any]]:
    sections = report.get("sections") if isinstance(report.get("sections"), dict) else {}
    env = sections.get("env") if isinstance(sections.get("env"), dict) else {}
    secrets = sections.get("secrets") if isinstance(sections.get("secrets"), dict) else {}
    host = sections.get("host") if isinstance(sections.get("host"), dict) else {}
    env_keys = sorted(set(_str_list(env.get("missing_required")) + _str_list(env.get("placeholder_required"))))
    secret_keys = sorted(set(_str_list(secrets.get("missing_required"))))
    host_capabilities = host.get("required_capabilities") if isinstance(host.get("required_capabilities"), dict) else {}
    return env_keys, secret_keys, host_capabilities


def _target_paths(report: Dict[str, Any], app: Optional[str], environment: Optional[str], runtime_root: Path) -> Dict[str, str]:
    app_name = app or "unknown"
    env_name = environment or "unknown"
    return {
        "runtime_app_root": str(runtime_root / "apps" / app_name),
        "runtime_env": str(runtime_root / "apps" / app_name / "env"),
        "github_secret_observation": str(runtime_root / "github" / "secret-observations" / f"{app_name}.{env_name}.json"),
        "active_release": str(runtime_root / "apps" / app_name / "active_release.json"),
        "legacy_release": str(runtime_root / "apps" / app_name / "release.json"),
        "releases_dir": str(runtime_root / "apps" / app_name / "releases"),
        "host_config": str(report.get("host_config")) if report.get("host_config") else "",
    }


def _scaffold_readme(app: str, environment: str, report: Dict[str, Any], target_paths: Dict[str, str]) -> str:
    env_keys, secret_keys, host_capabilities = _scaffold_inputs(report)
    lines = [
        f"# Live Hydration Evidence Scaffold: {app}/{environment}",
        "",
        "This directory contains templates only. It is safe to generate and review,",
        "but it is not runtime evidence by itself.",
        "",
        "Do not store real secret values in this directory or in Git.",
        "",
        "Target paths:",
        f"- runtime env: {target_paths['runtime_env']}",
        f"- GitHub secret-name observation: {target_paths['github_secret_observation']}",
        f"- active release metadata: {target_paths['active_release']}",
        f"- host inventory: {target_paths['host_config'] or 'not configured'}",
        "",
        "Template contents:",
        f"- env.required.template: {len(env_keys)} required env key placeholder(s)",
        f"- github-secret-observation.template.json: {len(secret_keys)} secret name placeholder(s)",
        f"- release-metadata.template.json: release metadata fields to collect from the live runtime",
        f"- host-capabilities.template.yml: {sum(1 for value in host_capabilities.values() if value)} required host capability flag(s)",
        "",
        "Safe workflow:",
        "1. Fill runtime env values only in the real runtime env target, outside Git.",
        "2. Replace the secret observation template with names collected from the provider.",
        "3. Populate release metadata from an actual active/latest runtime release.",
        "4. Merge host capabilities into the real host inventory after review.",
        "5. Rerun `ship live-hydration report` before enabling probes.",
        "",
    ]
    return "\n".join(lines)


def _env_template(keys: List[str], target_path: Optional[str]) -> str:
    lines = [
        "# Required env-key placeholders for live hydration.",
        "# Fill real values only in the runtime env target, never in Git.",
        f"# Runtime target: {target_path or 'unknown'}",
        "",
    ]
    lines.extend(f"{key}=" for key in keys)
    return "\n".join(lines) + "\n"


def _github_secret_observation_template(app: str, environment: str, keys: List[str], target_path: Optional[str]) -> Dict[str, Any]:
    return {
        "template": True,
        "values_redacted": True,
        "app": app,
        "environment": environment,
        "target_path": target_path,
        "instructions": "Replace this template with names observed from the provider before copying it to the target path.",
        "environments": {
            environment: {
                "secrets": [{"name": key, "observed": False} for key in keys],
            }
        },
    }


def _release_metadata_template(app: str, environment: str, report: Dict[str, Any], target_paths: Dict[str, str]) -> Dict[str, Any]:
    return {
        "template": True,
        "values_redacted": True,
        "app": app,
        "environment": environment,
        "release_id": "replace-with-real-release-id",
        "source_manifest": report.get("manifest_path"),
        "manifest_path": report.get("manifest_path"),
        "git_sha": "replace-with-deployed-git-sha-if-known",
        "images": [],
        "generated_files": [],
        "deployed_at": "replace-with-real-deploy-timestamp",
        "target_paths": {
            "active_release": target_paths.get("active_release"),
            "legacy_release": target_paths.get("legacy_release"),
            "releases_dir": target_paths.get("releases_dir"),
        },
    }


def _host_capabilities_template(capabilities: Dict[str, Any], target_path: Optional[str]) -> str:
    required = {key: value for key, value in capabilities.items() if value}
    lines = [
        "# Host capability inventory template for live hydration.",
        "# Merge reviewed capability facts into the real host inventory.",
        f"# Host inventory target: {target_path or 'unknown'}",
        "version: 1",
        "hosts:",
        "  - id: replace-with-host-id",
        "    capabilities:",
    ]
    if required:
        lines.extend(f"      {key}: true" for key in sorted(required))
    else:
        lines.append("      docker: true")
    return "\n".join(lines) + "\n"


def _json_text(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _sha256_text(content: str) -> str:
    import hashlib

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _host_section(placement: Dict[str, Any]) -> Dict[str, Any]:
    requirements = placement.get("requirements") if isinstance(placement.get("requirements"), dict) else {}
    required_capabilities = requirements.get("required_capabilities") if isinstance(requirements.get("required_capabilities"), dict) else {}
    placements = placement.get("placements") if isinstance(placement.get("placements"), list) else []
    host_checks = []
    for host in placements:
        if not isinstance(host, dict):
            continue
        host_checks.append(
            {
                "host_id": host.get("host_id"),
                "eligible": not bool(host.get("blockers")),
                "blockers": host.get("blockers") if isinstance(host.get("blockers"), list) else [],
                "warnings": host.get("warnings") if isinstance(host.get("warnings"), list) else [],
            }
        )
    return {
        "status": "blocked" if placement.get("status") == "blocked" else placement.get("status"),
        "config_path": placement.get("config_path"),
        "required_capabilities": required_capabilities,
        "recommended_host": placement.get("recommended_host"),
        "recommended_hosts": placement.get("recommended_hosts") if isinstance(placement.get("recommended_hosts"), list) else [],
        "host_checks": host_checks,
        "summary": placement.get("summary"),
    }


def _drift_section(drift: Dict[str, Any]) -> Dict[str, Any]:
    findings = drift.get("findings") if isinstance(drift.get("findings"), list) else []
    return {
        "status": "warning" if drift.get("drift") else "ok",
        "drift": bool(drift.get("drift")),
        "severity": drift.get("severity"),
        "finding_count": len(findings),
        "high_or_critical_count": sum(1 for item in findings if isinstance(item, dict) and str(item.get("severity")) in {"high", "critical"}),
        "summary": drift.get("summary"),
    }


def _hydration_steps(
    sections: Dict[str, Any],
    app: str,
    environment: str,
    runtime_root: Path,
    manifest_path: Path,
    host_config: Optional[Path],
    provider_config: Optional[Path],
    profile: Optional[str],
    profiles_path: Optional[str],
) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    runtime = sections.get("runtime", {})
    env = sections.get("env", {})
    secrets = sections.get("secrets", {})
    release = sections.get("release", {})
    host = sections.get("host", {})
    if isinstance(runtime, dict) and not runtime.get("present", {}).get("app_root"):
        steps.append(_step("create_runtime_app_root", "Create or collect the app runtime directory.", str(runtime_root / "apps" / app), ["mkdir -p path only; do not commit runtime data"]))
    if isinstance(env, dict) and (env.get("missing_required") or env.get("placeholder_required")):
        keys = sorted(set(env.get("missing_required", []) + env.get("placeholder_required", [])))
        steps.append(_step("hydrate_runtime_env_shape", f"Provide runtime env presence for {len(keys)} required key(s), values stay outside Git.", str(runtime_root / "apps" / app / "env"), keys))
    if isinstance(secrets, dict) and secrets.get("missing_required"):
        steps.append(_step("record_secret_name_observations", f"Record observed secret names for {len(secrets['missing_required'])} required key(s), never values.", str(provider_config) if provider_config else "provider observations", secrets["missing_required"]))
    if isinstance(release, dict) and release.get("status") == "blocked":
        steps.append(_step("record_release_metadata", "Record active/latest release metadata from the live runtime.", str(runtime_root / "apps" / app / "active_release.json"), [str(runtime_root / "apps" / app / "release.json"), str(runtime_root / "apps" / app / "releases")]))
    if isinstance(host, dict) and host.get("status") == "blocked":
        steps.append(_step("complete_host_capability_inventory", "Declare or collect required host capabilities before placement can pass.", str(host_config) if host_config else "host inventory config", sorted(k for k, v in host.get("required_capabilities", {}).items() if v)))
    drift = sections.get("drift", {})
    if isinstance(drift, dict) and drift.get("drift"):
        steps.append(
            _step(
                "refresh_or_review_runtime_drift",
                "Review drift after runtime env/release evidence exists.",
                f"ship drift {manifest_path} --runtime-root {runtime_root} --json",
                [drift.get("summary")],
            )
        )
    steps.append(
        _step(
            "rerun_live_profile",
            "Rerun the focused file-based profile before enabling probes.",
            (
                f"ship live-drills run {profile} --profiles {profiles_path} --json"
                if profile and profiles_path
                else f"ship live-readiness run --app {app} --environment {environment} --json"
            ),
            ["Do not enable HTTP/Docker probes until file-based blockers are understood."],
        )
    )
    return steps


def _step(code: str, summary: str, target: str, details: List[Any]) -> Dict[str, Any]:
    return {"code": code, "summary": summary, "target": target, "details": [item for item in details if item]}


def _section_blockers(sections: Dict[str, Any]) -> List[Dict[str, str]]:
    blockers: List[Dict[str, str]] = []
    for name, section in sections.items():
        if isinstance(section, dict) and section.get("status") == "blocked":
            blockers.append(issue(f"{name}_hydration_blocked", f"{name} evidence is incomplete.", name))
    return blockers


def _section_warnings(sections: Dict[str, Any]) -> List[Dict[str, str]]:
    warnings: List[Dict[str, str]] = []
    for name, section in sections.items():
        if not isinstance(section, dict) or section.get("status") != "warning":
            continue
        if name == "drift":
            warnings.append(issue("drift_requires_review", "Drift is present and should be reviewed after baseline hydration.", "drift"))
        else:
            warnings.append(issue(f"{name}_hydration_warning", f"{name} evidence has warnings.", name))
    return warnings


def _compact_report(report: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": report.get("kind"),
        "status": report.get("status") or report.get("readiness_level"),
        "summary": report.get("summary"),
        "blocker_count": len(report.get("blockers", [])) if isinstance(report.get("blockers"), list) else 0,
        "warning_count": len(report.get("warnings", [])) if isinstance(report.get("warnings"), list) else 0,
    }


def _entry_keys(entries: List[Dict[str, Any]]) -> List[str]:
    return sorted(str(entry.get("key")) for entry in entries if isinstance(entry.get("key"), str))


def _str_list(value: Any) -> List[str]:
    return [str(item) for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _issues(value: Any) -> List[Dict[str, str]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _optional_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _dedupe_issues(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen: set[tuple[str, str, str]] = set()
    result: List[Dict[str, str]] = []
    for item in items:
        key = (str(item.get("code") or ""), str(item.get("message") or ""), str(item.get("path") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _redact(payload: Dict[str, Any]) -> Dict[str, Any]:
    return deep_redact(
        payload,
        safe_keys={
            "confirmation_required",
            "dry_run",
            "github_secret_observation",
            "github_secret_observations",
            "mutates_state",
            "probe_policy",
            "read_only",
            "secret_provider_status",
            "secrets",
            "values_redacted",
        },
        propagate=True,
    )
