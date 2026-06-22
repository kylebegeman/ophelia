from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .drift import manifest_drift
from .github_providers import github_provider_status
from .host_inventory import app_placement_plan, collect_host_inventory, host_readiness
from .lumen_adapter import dashboard_data
from .manifest import ManifestError, load_manifest
from .observability import compact_observability_summary, observability_status
from .operation_schema import SCHEMA_VERSION, issue, operation_id
from .operator_reports import manifest_registry
from .portability import app_readiness_report
from .redaction import deep_redact
from .secret_providers import secret_provider_report, secret_provider_status
from .state_db import state_status

LIVE_READINESS_KIND = "ophelia.live_readiness_report"


def live_readiness_report(
    *,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifests_dir: Path = REPO_ROOT / "manifests",
    ophelia_root: Path = REPO_ROOT,
    app: Optional[str] = None,
    environment: Optional[str] = None,
    manifest_path: Optional[Path] = None,
    host_config: Optional[Path] = None,
    provider_config: Optional[Path] = None,
    source_host: Optional[str] = None,
    target_host: Optional[str] = None,
    probe_http: bool = False,
    check_docker: bool = False,
    http_timeout: float = 5.0,
) -> Dict[str, Any]:
    """Aggregate read-only live-readiness checks for staging/prod data.

    This lane intentionally composes existing non-mutating report surfaces. It
    never calls apply/create commands, never writes state indexes or observability
    run artifacts, and only performs network/Docker probes when explicitly
    requested by flags.
    """
    runtime_root = Path(runtime_root)
    manifests_dir = Path(manifests_dir)
    ophelia_root = Path(ophelia_root)
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []

    host_inventory = _safe_report(
        "host_inventory",
        warnings,
        lambda: collect_host_inventory(runtime_root, ophelia_root, manifests_dir, host_config),
    )
    host_report = _safe_report(
        "host_readiness",
        warnings,
        lambda: host_readiness(target_host, runtime_root, ophelia_root, manifests_dir, host_config),
    )
    github_status = _safe_report(
        "github_provider_status",
        warnings,
        lambda: github_provider_status(runtime_root=runtime_root, ophelia_root=ophelia_root, config_path=provider_config),
    )
    secret_status = _safe_report(
        "secret_provider_status",
        warnings,
        lambda: secret_provider_status(runtime_root=runtime_root, ophelia_root=ophelia_root, config_path=provider_config),
    )
    state = _safe_report("state_status", warnings, lambda: state_status(runtime_root))
    dashboard = _safe_report(
        "lumen_dashboard",
        warnings,
        lambda: dashboard_data(runtime_root=runtime_root, manifests_dir=manifests_dir),
    )
    targets = _resolve_targets(
        manifests_dir=manifests_dir,
        runtime_root=runtime_root,
        app=app,
        environment=environment,
        manifest_path=manifest_path,
        blockers=blockers,
        warnings=warnings,
    )

    apps: List[Dict[str, Any]] = []
    for target in targets:
        apps.append(
            _app_live_readiness(
                target,
                runtime_root=runtime_root,
                manifests_dir=manifests_dir,
                ophelia_root=ophelia_root,
                host_config=host_config,
                provider_config=provider_config,
                source_host=source_host,
                target_host=target_host,
                probe_http=probe_http,
                check_docker=check_docker,
                http_timeout=http_timeout,
                aggregate_warnings=warnings,
            )
        )

    _append_issues(blockers, _issues_from_report(host_inventory, "host_inventory", "blockers"))
    _append_issues(blockers, _issues_from_report(host_report, "host_readiness", "blockers"))
    _append_issues(blockers, _issues_from_report(github_status, "github_provider_status", "blockers"))
    _append_issues(blockers, _issues_from_report(secret_status, "secret_provider_status", "blockers"))
    _append_issues(blockers, _issues_from_report(dashboard, "lumen_dashboard", "blockers"))
    _append_issues(warnings, _issues_from_report(host_inventory, "host_inventory", "warnings"))
    _append_issues(warnings, _issues_from_report(host_report, "host_readiness", "warnings"))
    _append_issues(warnings, _issues_from_report(github_status, "github_provider_status", "warnings"))
    _append_issues(warnings, _issues_from_report(secret_status, "secret_provider_status", "warnings"))
    _append_issues(warnings, _issues_from_report(dashboard, "lumen_dashboard", "warnings"))

    for entry in apps:
        _append_issues(blockers, entry.get("blockers", []))
        _append_issues(warnings, entry.get("warnings", []))

    totals = {
        "app_count": len(apps),
        "ok": sum(1 for item in apps if item.get("status") == "ok"),
        "warning": sum(1 for item in apps if item.get("status") == "warning"),
        "blocked": sum(1 for item in apps if item.get("status") == "blocked"),
        "drift": sum(1 for item in apps if bool(item.get("drift_detected"))),
    }
    status = "blocked" if blockers else "warning" if warnings else "ok"
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": LIVE_READINESS_KIND,
        "operation": "live.readiness.run",
        "operation_id": operation_id("live.readiness.run", app, environment),
        "status": status,
        "runtime_root": str(runtime_root),
        "manifests_dir": str(manifests_dir),
        "ophelia_root": str(ophelia_root),
        "app": app,
        "environment": environment,
        "read_only": True,
        "dry_run": True,
        "mutates_state": False,
        "confirmation_required": False,
        "probe_policy": {
            "http": {"enabled": bool(probe_http), "timeout_seconds": http_timeout},
            "docker": {"enabled": bool(check_docker)},
        },
        "mutation_guard": {
            "mutating_operations_called": [],
            "disallowed_operation_suffixes": [".apply", ".create", ".rebuild", ".refresh"],
            "state_index_rebuild": "not_run",
            "observability_schedule_artifacts": "not_written",
        },
        "observation_paths": _observation_paths(runtime_root, host_config, provider_config),
        "host_inventory": _compact_report(host_inventory),
        "host_readiness": _compact_report(host_report),
        "github_provider_status": _compact_report(github_status),
        "secret_provider_status": _compact_report(secret_status),
        "state_status": _compact_report(state),
        "lumen_dashboard": _compact_dashboard(dashboard),
        "apps": apps,
        "totals": totals,
        "blockers": _dedupe_issues(blockers),
        "warnings": _dedupe_issues(warnings),
        "values_redacted": True,
        "summary": (
            f"Live readiness checked {len(apps)} app(s): "
            f"{totals['blocked']} blocked, {totals['warning']} warning, {totals['drift']} drift."
        ),
    }
    return deep_redact(payload, safe_keys={"values_redacted"}, propagate=True)


def _app_live_readiness(
    target: Dict[str, Any],
    *,
    runtime_root: Path,
    manifests_dir: Path,
    ophelia_root: Path,
    host_config: Optional[Path],
    provider_config: Optional[Path],
    source_host: Optional[str],
    target_host: Optional[str],
    probe_http: bool,
    check_docker: bool,
    http_timeout: float,
    aggregate_warnings: List[Dict[str, str]],
) -> Dict[str, Any]:
    app = str(target["app"])
    environment = target.get("environment") if isinstance(target.get("environment"), str) else None
    manifest_path = Path(str(target["manifest_path"]))
    readiness = _safe_report(
        f"{app}.readiness",
        aggregate_warnings,
        lambda: app_readiness_report(app, environment, runtime_root, manifest_path),
    )
    placement = _safe_report(
        f"{app}.placement",
        aggregate_warnings,
        lambda: app_placement_plan(
            app,
            environment=environment,
            runtime_root=runtime_root,
            manifests_dir=manifests_dir,
            manifest_path=manifest_path,
            ophelia_root=ophelia_root,
            config_path=host_config,
            source_host=source_host,
            target_host=target_host,
        ),
    )
    observability = _safe_report(
        f"{app}.observability",
        aggregate_warnings,
        lambda: observability_status(
            app,
            environment=environment,
            runtime_root=runtime_root,
            manifest_path=manifest_path,
            probe_http=probe_http,
            check_docker=check_docker,
            http_timeout=http_timeout,
        ),
    )
    secrets = _safe_report(
        f"{app}.secrets",
        aggregate_warnings,
        lambda: secret_provider_report(
            manifest_path,
            environment=environment,
            runtime_root=runtime_root,
            ophelia_root=ophelia_root,
            config_path=provider_config,
        ),
    )
    drift = _safe_report(
        f"{app}.drift",
        aggregate_warnings,
        lambda: manifest_drift(load_manifest(manifest_path), manifest_path, runtime_root),
    )

    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    for name, report in (
        ("readiness", readiness),
        ("placement", placement),
        ("observability", observability),
        ("secrets", secrets),
    ):
        _append_issues(blockers, _issues_from_report(report, name, "blockers"))
        _append_issues(warnings, _issues_from_report(report, name, "warnings"))

    drift_detected = bool(isinstance(drift, dict) and drift.get("drift"))
    if drift_detected:
        warnings.append(issue("drift_detected", f"{app}: drift findings are present.", "drift"))
    drift_findings = drift.get("findings") if isinstance(drift, dict) and isinstance(drift.get("findings"), list) else []
    for finding in drift_findings:
        if isinstance(finding, dict) and str(finding.get("severity")) in {"high", "critical"}:
            warnings.append(
                issue(
                    "high_severity_drift",
                    f"{app}: {finding.get('message') or 'High-severity drift finding.'}",
                    str(finding.get("path") or "drift"),
                )
            )

    status = "blocked" if blockers else "warning" if warnings else "ok"
    return {
        "app": app,
        "environment": environment or "unknown",
        "manifest_path": str(manifest_path),
        "status": status,
        "drift_detected": drift_detected,
        "checks": {
            "readiness": _compact_report(readiness),
            "placement": _compact_placement(placement),
            "observability": _compact_observability(observability),
            "secrets": _compact_secrets(secrets),
            "drift": _compact_drift(drift),
        },
        "reports": {
            "readiness": readiness,
            "placement": placement,
            "observability": observability,
            "secrets": secrets,
            "drift": drift,
        },
        "blockers": _dedupe_issues(blockers),
        "warnings": _dedupe_issues(warnings),
    }


def _resolve_targets(
    *,
    manifests_dir: Path,
    runtime_root: Path,
    app: Optional[str],
    environment: Optional[str],
    manifest_path: Optional[Path],
    blockers: List[Dict[str, str]],
    warnings: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    if manifest_path is not None:
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError as exc:
            blockers.append(issue("manifest_invalid", str(exc), str(manifest_path)))
            return []
        if app and manifest.app != app:
            blockers.append(issue("manifest_app_mismatch", f"Manifest app `{manifest.app}` does not match requested `{app}`.", "app"))
            return []
        if environment and manifest.environment and manifest.environment != environment:
            blockers.append(
                issue(
                    "manifest_environment_mismatch",
                    f"Manifest environment `{manifest.environment}` does not match requested `{environment}`.",
                    "environment",
                )
            )
            return []
        return [
            {
                "app": manifest.app,
                "environment": environment or manifest.environment,
                "manifest_path": str(manifest_path),
            }
        ]

    registry = manifest_registry(manifests_dir, runtime_root)
    targets: List[Dict[str, Any]] = []
    for error in registry.get("errors", []) if isinstance(registry.get("errors"), list) else []:
        if isinstance(error, dict):
            warnings.append(issue("manifest_registry_error", str(error.get("error") or "Manifest could not be loaded."), str(error.get("path"))))
    for entry in registry.get("manifests", []) if isinstance(registry.get("manifests"), list) else []:
        if not isinstance(entry, dict):
            continue
        entry_app = entry.get("app")
        entry_environment = entry.get("environment")
        path = entry.get("manifest_path")
        if not isinstance(entry_app, str) or not isinstance(path, str):
            continue
        if app and entry_app != app:
            continue
        if environment and entry_environment not in {environment, None}:
            continue
        targets.append({"app": entry_app, "environment": entry_environment or environment, "manifest_path": path})
    if app and not targets:
        blockers.append(issue("manifest_not_found", f"No manifest found for app `{app}`.", "app"))
    return targets


def _safe_report(name: str, warnings: List[Dict[str, str]], factory) -> Dict[str, Any]:
    try:
        value = factory()
    except Exception as exc:  # noqa: BLE001 - live readiness should degrade, not crash
        warnings.append(issue("live_readiness_subreport_failed", f"{name}: {type(exc).__name__}", name))
        return {
            "kind": "ophelia.subreport_error",
            "status": "blocked",
            "summary": f"{name} failed: {type(exc).__name__}.",
            "blockers": [issue("subreport_failed", f"{name}: {type(exc).__name__}", name)],
            "warnings": [],
        }
    return value if isinstance(value, dict) else {"status": "blocked", "summary": f"{name} returned a non-object report."}


def _compact_report(report: Dict[str, Any]) -> Dict[str, Any]:
    blockers = report.get("blockers") if isinstance(report.get("blockers"), list) else []
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    return {
        "kind": report.get("kind"),
        "status": report.get("status"),
        "summary": report.get("summary"),
        "blocker_count": len(blockers),
        "warning_count": len(warnings),
    }


def _compact_dashboard(report: Dict[str, Any]) -> Dict[str, Any]:
    compact = _compact_report(report)
    compact["totals"] = report.get("totals") if isinstance(report.get("totals"), dict) else {}
    return compact


def _compact_placement(report: Dict[str, Any]) -> Dict[str, Any]:
    compact = _compact_report(report)
    compact["recommended_host"] = report.get("recommended_host")
    compact["recommended_hosts"] = report.get("recommended_hosts") if isinstance(report.get("recommended_hosts"), list) else []
    return compact


def _compact_observability(report: Dict[str, Any]) -> Dict[str, Any]:
    compact = _compact_report(report)
    compact["snapshot"] = compact_observability_summary(report) if isinstance(report, dict) else {}
    return compact


def _compact_secrets(report: Dict[str, Any]) -> Dict[str, Any]:
    compact = _compact_report(report)
    keys = report.get("keys") if isinstance(report.get("keys"), list) else []
    compact["key_count"] = len(keys)
    compact["missing_required"] = sum(1 for key in keys if isinstance(key, dict) and key.get("status") == "missing")
    return compact


def _compact_drift(report: Dict[str, Any]) -> Dict[str, Any]:
    compact = _compact_report(report)
    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    compact["drift"] = bool(report.get("drift"))
    compact["severity"] = report.get("severity")
    compact["finding_count"] = len(findings)
    return compact


def _issues_from_report(report: Dict[str, Any], source: str, key: str) -> List[Dict[str, str]]:
    items = report.get(key) if isinstance(report.get(key), list) else []
    issues: List[Dict[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            code = str(item.get("code") or f"{source}_{key}")
            message = str(item.get("message") or item)
            path = item.get("path")
            issues.append(issue(code, f"{source}: {message}", str(path) if path else source))
        else:
            issues.append(issue(f"{source}_{key}", f"{source}: {item}", source))
    return issues


def _append_issues(target: List[Dict[str, str]], issues: Any) -> None:
    for item in issues if isinstance(issues, list) else []:
        if isinstance(item, dict):
            target.append(
                issue(
                    str(item.get("code") or "issue"),
                    str(item.get("message") or item),
                    str(item.get("path")) if item.get("path") else None,
                )
            )


def _dedupe_issues(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen: set[tuple[str, str, str]] = set()
    result: List[Dict[str, str]] = []
    for item in items:
        key = (item.get("code", ""), item.get("message", ""), item.get("path", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _observation_paths(
    runtime_root: Path,
    host_config: Optional[Path],
    provider_config: Optional[Path],
) -> Dict[str, Any]:
    return {
        "host_config": str(host_config) if host_config else str(REPO_ROOT / "config" / "ophelia-hosts.yml"),
        "provider_config": str(provider_config) if provider_config else str(REPO_ROOT / "config" / "ophelia-integrations.yml"),
        "github_repo_observations": str(runtime_root / "github" / "observations"),
        "github_secret_observations": str(runtime_root / "github" / "secret-observations"),
        "sops_refs": [str(runtime_root / "secrets"), str(REPO_ROOT / "secrets")],
        "observability_latest": str(runtime_root / "observability" / "latest.json"),
    }

