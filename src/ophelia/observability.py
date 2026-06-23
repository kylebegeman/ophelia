"""Built-in observability and telemetry layer (read-only, lightweight).

This module composes a *configured + locally-derived* observability picture for
an app/environment without touching the network or Docker by default. The
``observability_status`` view reads only local files (release metadata, backup
freshness, restore-drill receipts, and receipt failure counts) plus the
declared observability config from the manifest. Live probes are strictly
opt-in:

* ``probe_http=True`` performs a single, timeout-bounded HTTP GET against the
  declared health URL. Any failure or timeout becomes a *warning*, never a
  crash and never a hang.
* ``check_docker=True`` runs a timeout-bounded ``docker ps`` only when a docker
  binary is present; otherwise it degrades to a note. It never starts, stops,
  or otherwise mutates containers.

Secret safety: only summaries are emitted, never unbounded logs. Health and
metrics URLs are validated up front to reject credentials, query strings, and
fragments, so emitting the validated URL by value carries no secret. The auth
mode for metrics is referenced by *name* only (``none``/``bearer_env``/
``basic_env``); no secret value is ever read or printed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import request
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlparse

from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .operation_schema import SCHEMA_VERSION, artifact, issue as schema_issue, receipt_envelope, utc_now
from .portability import (
    _receipt_records,
    _restore_drill_receipts,
    backup_status_report,
    resolve_app_manifest,
)
from .redaction import deep_redact
from .runtime import active_release

STATUS_KIND = "ophelia.observability_status"
PLAN_KIND = "ophelia.observability_plan"
EXPORT_KIND = "ophelia.observability_export"
SCHEDULE_RUN_KIND = "ophelia.observability_schedule_run"

# Receipt statuses that count as a failure for the rolled-up failure count.
_FAILURE_STATUSES = {"failed", "error", "blocked"}

# Default and maximum bounds for the opt-in HTTP probe, in seconds. Kept small
# so an opt-in probe can never hang a status call for long.
_DEFAULT_HTTP_TIMEOUT = 5.0
_MAX_HTTP_TIMEOUT = 30.0
_DOCKER_TIMEOUT = 5.0


def validate_health_url(url: Optional[str]) -> Optional[str]:
    """Validate a health/metrics URL, returning an error message or ``None``.

    Mirrors the rule enforced for ``target_health_url`` in
    :mod:`ophelia.actions`: the URL must be an ``http(s)`` URL with a netloc and
    must not carry credentials, query strings, or fragments. Returning a message
    (rather than raising) lets both the manifest parser and the plan/status
    surfaces reuse the same rule and decide how to report it.
    """
    if url is None:
        return None
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return "must be an http(s) URL without credentials, query strings, or fragments."
    return None


def observability_status(
    app: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest_path: Optional[Path] = None,
    *,
    probe_http: bool = False,
    check_docker: bool = False,
    http_timeout: float = _DEFAULT_HTTP_TIMEOUT,
) -> Dict[str, Any]:
    """Read-only observability status for an app/environment.

    By default makes NO network or Docker call: every status is derived from the
    declared manifest config plus local runtime files. ``probe_http`` and
    ``check_docker`` are opt-in, timeout-bounded, and degrade to warnings.
    """
    runtime_root = Path(runtime_root)
    resolution = resolve_app_manifest(app, environment, manifest_path=manifest_path)
    warnings: List[Dict[str, str]] = [schema_issue("manifest_warning", message) for message in resolution.warnings]
    blockers: List[Dict[str, str]] = [schema_issue("manifest_unresolved", message) for message in resolution.blockers]
    manifest = resolution.manifest
    resolved_environment = environment or (manifest.environment if manifest else None) or "unknown"

    health = _health_summary(manifest)
    metrics = _metrics_summary(manifest, warnings)
    logs = _logs_summary(manifest)
    routes = _route_summary(manifest)

    release = _release_summary(runtime_root, app)
    backups = _backup_summary(app, resolved_environment, runtime_root, resolution.manifest_path)
    restore_drill = _restore_drill_summary(runtime_root, app)
    receipts_failures = _receipt_failure_summary(runtime_root, app, environment)

    # Opt-in live probes. Each degrades to a warning on any failure.
    if probe_http:
        _apply_http_probe(health, http_timeout, warnings)
    container_status = _container_summary(check_docker, app, warnings)

    status = "blocked" if blockers else "warning" if warnings else "ok"
    summary = (
        f"Observability for {app} {resolved_environment}: {status}; "
        f"{len(blockers)} blocker(s), {len(warnings)} warning(s), "
        f"{receipts_failures['count']} failed receipt(s)."
    )

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": STATUS_KIND,
        "app": app,
        "environment": resolved_environment,
        "status": status,
        "summary": summary,
        "manifest_path": str(resolution.manifest_path) if resolution.manifest_path else None,
        "probed_http": bool(probe_http),
        "checked_docker": bool(check_docker),
        "health": health,
        "metrics": metrics,
        "logs": logs,
        "release": release,
        "backups": backups,
        "restore_drill": restore_drill,
        "receipts_failures": receipts_failures,
        "routes": routes,
        "containers": container_status,
        "blockers": blockers,
        "warnings": warnings,
        "secrets_redacted": True,
    }
    return deep_redact(payload, safe_keys={"secrets_redacted"})


def observability_plan(
    app: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Read-only description of what would be monitored, plus config blockers.

    Performs no network/Docker call and no mutation. Surfaces any configuration
    problem (missing/invalid/credentialed health URL, missing metrics endpoint)
    as a blocker or warning so an operator can fix the config before relying on
    monitoring.
    """
    runtime_root = Path(runtime_root)
    resolution = resolve_app_manifest(app, environment, manifest_path=manifest_path)
    warnings: List[Dict[str, str]] = [schema_issue("manifest_warning", message) for message in resolution.warnings]
    blockers: List[Dict[str, str]] = [schema_issue("manifest_unresolved", message) for message in resolution.blockers]
    manifest = resolution.manifest
    resolved_environment = environment or (manifest.environment if manifest else None) or "unknown"

    health = _health_summary(manifest)
    metrics = _metrics_summary(manifest, warnings)
    logs = _logs_summary(manifest)
    routes = _route_summary(manifest)

    monitors: List[Dict[str, Any]] = []
    if manifest is not None:
        if health["configured"]:
            error = health.get("config_error")
            if error:
                blockers.append(schema_issue("observability_health_url_invalid", str(error), "observability.health.url"))
            else:
                monitors.append({"type": "http_health", "url": health["url"], "expect_status": health["expect_status"]})
        else:
            warnings.append(
                schema_issue(
                    "observability_health_not_configured",
                    "No `observability.health.url` is declared; HTTP health probing is unavailable.",
                    "observability.health",
                )
            )
        if metrics["configured"] and metrics.get("url") and metrics.get("format") != "none":
            monitors.append({"type": "metrics", "url": metrics["url"], "format": metrics["format"], "auth": metrics["auth"]})
        if logs["configured"] and logs.get("containers"):
            monitors.append({"type": "container_logs", "retain_days": logs.get("retain_days")})

    status = "blocked" if blockers else "warning" if warnings else "ok"
    summary = (
        f"Observability plan for {app} {resolved_environment}: "
        f"{len(monitors)} monitor(s), {len(blockers)} blocker(s), {len(warnings)} warning(s)."
    )
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": PLAN_KIND,
        "app": app,
        "environment": resolved_environment,
        "status": status,
        "summary": summary,
        "manifest_path": str(resolution.manifest_path) if resolution.manifest_path else None,
        "monitors": monitors,
        "health": health,
        "metrics": metrics,
        "logs": logs,
        "routes": routes,
        "blockers": blockers,
        "warnings": warnings,
        "read_only": True,
        "secrets_redacted": True,
    }
    return deep_redact(payload, safe_keys={"secrets_redacted"})


def observability_export(
    app: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Compact, redacted observability snapshot suitable for operator UI/state.

    Built from :func:`observability_status` (no probing) and reduced to scalar
    summaries: it never carries unbounded logs and never carries secret values.
    """
    status = observability_status(
        app,
        environment=environment,
        runtime_root=runtime_root,
        manifest_path=manifest_path,
    )
    snapshot = compact_observability_summary(status)
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": EXPORT_KIND,
        "app": app,
        "environment": status.get("environment"),
        "status": status.get("status"),
        "summary": status.get("summary"),
        "snapshot": snapshot,
        "secrets_redacted": True,
    }
    return deep_redact(payload, safe_keys={"secrets_redacted"})


def observability_schedule_run(
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifests_dir: Path = REPO_ROOT / "manifests",
    *,
    probe_http: bool = False,
    check_docker: bool = False,
    http_timeout: float = _DEFAULT_HTTP_TIMEOUT,
) -> Dict[str, Any]:
    """Run a cron-friendly observability sweep across all registered manifests.

    This is not a daemon or scheduler. It is a deterministic command target for
    cron/systemd/operator UI: scan the manifest registry, call
    :func:`observability_status` for each app, write a timestamped run artifact
    plus ``latest.json``, and return a compact aggregate receipt.
    """
    started_at = utc_now()
    runtime_root = Path(runtime_root)
    manifests_dir = Path(manifests_dir)
    warnings: List[Dict[str, str]] = []
    blockers: List[Dict[str, str]] = []
    entries: List[Dict[str, Any]] = []

    try:
        from .operator_reports import manifest_registry

        registry = manifest_registry(manifests_dir, runtime_root)
    except Exception as exc:  # noqa: BLE001 - schedule run should report, not crash
        registry = {"manifests": [], "errors": [{"path": str(manifests_dir), "error": type(exc).__name__}]}

    for error in registry.get("errors", []) if isinstance(registry, dict) else []:
        if isinstance(error, dict):
            warnings.append(
                schema_issue(
                    "manifest_registry_error",
                    str(error.get("error") or "Manifest could not be loaded."),
                    str(error.get("path")) if error.get("path") else None,
                )
            )

    manifests = registry.get("manifests", []) if isinstance(registry, dict) else []
    for manifest_entry in manifests if isinstance(manifests, list) else []:
        if not isinstance(manifest_entry, dict):
            continue
        app = manifest_entry.get("app")
        if not isinstance(app, str) or not app:
            continue
        environment = manifest_entry.get("environment") if isinstance(manifest_entry.get("environment"), str) else None
        manifest_path_value = manifest_entry.get("manifest_path")
        manifest_path = Path(manifest_path_value) if isinstance(manifest_path_value, str) else None
        status = observability_status(
            app,
            environment=environment,
            runtime_root=runtime_root,
            manifest_path=manifest_path,
            probe_http=probe_http,
            check_docker=check_docker,
            http_timeout=http_timeout,
        )
        compact = compact_observability_summary(status)
        entries.append(
            {
                "app": app,
                "environment": status.get("environment"),
                "manifest_path": str(manifest_path) if manifest_path is not None else None,
                "status": status.get("status"),
                "summary": status.get("summary"),
                "snapshot": compact,
            }
        )
        for blocker in status.get("blockers", []) if isinstance(status.get("blockers"), list) else []:
            if isinstance(blocker, dict):
                blockers.append(
                    schema_issue(
                        str(blocker.get("code") or "observability_blocker"),
                        f"{app}: {blocker.get('message') or 'Observability blocker.'}",
                        str(blocker.get("path")) if blocker.get("path") else None,
                    )
                )

    warning_apps = sum(1 for entry in entries if entry.get("status") == "warning")
    blocked_apps = sum(1 for entry in entries if entry.get("status") == "blocked")
    receipt_status = "blocked" if blocked_apps or blockers else "warning" if warning_apps or warnings else "succeeded"
    receipt = receipt_envelope(
        operation="observability.schedule.run",
        app=None,
        environment=None,
        status=receipt_status,
        started_at=started_at,
        completed_at=utc_now(),
        artifacts=[],
        checks=[
            {
                "name": "observability_sweep",
                "ok": receipt_status == "succeeded",
                "message": f"{len(entries)} app(s) checked; {blocked_apps} blocked, {warning_apps} warning.",
            }
        ],
        rollback={"available": False, "note": "Schedule run is read/probe oriented; only local run artifacts were written."},
        kind=SCHEDULE_RUN_KIND,
        manifests_dir=str(manifests_dir),
        probed_http=bool(probe_http),
        checked_docker=bool(check_docker),
        apps=entries,
        totals={
            "app_count": len(entries),
            "ok": sum(1 for entry in entries if entry.get("status") == "ok"),
            "warning": warning_apps,
            "blocked": blocked_apps,
        },
        blockers=blockers,
        warnings=warnings,
        summary=f"Observability schedule run checked {len(entries)} app(s): {blocked_apps} blocked, {warning_apps} warning.",
        secrets_redacted=True,
    )
    run_path = runtime_root / "observability" / "runs" / f"{receipt['operation_id']}.json"
    latest_path = runtime_root / "observability" / "latest.json"
    receipt["artifacts"] = [
        artifact(str(run_path), "observability-schedule-run", "Timestamped observability schedule run.", present=True),
        artifact(str(latest_path), "observability-latest", "Latest observability schedule run.", present=True),
    ]
    redacted = deep_redact(receipt, safe_keys={"secrets_redacted", "inputs_redacted"}, propagate=True)
    try:
        _write_json(run_path, redacted)
        _write_json(latest_path, redacted)
    except OSError:
        redacted.setdefault("warnings", []).append(
            schema_issue("observability_schedule_write_failed", f"Could not write observability run under {runtime_root}.")
        )
    return redacted


def compact_observability_summary(status: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a full status payload to a compact, scalar-only summary.

    Used by the export view and by the operator dashboard adapter so neither emits
    unbounded structures or duplicates the reduction logic.
    """
    health = status.get("health") if isinstance(status.get("health"), dict) else {}
    metrics = status.get("metrics") if isinstance(status.get("metrics"), dict) else {}
    logs = status.get("logs") if isinstance(status.get("logs"), dict) else {}
    backups = status.get("backups") if isinstance(status.get("backups"), dict) else {}
    restore_drill = status.get("restore_drill") if isinstance(status.get("restore_drill"), dict) else {}
    receipts_failures = status.get("receipts_failures") if isinstance(status.get("receipts_failures"), dict) else {}
    release = status.get("release") if isinstance(status.get("release"), dict) else {}
    routes = status.get("routes") if isinstance(status.get("routes"), dict) else {}
    blockers = status.get("blockers") if isinstance(status.get("blockers"), list) else []
    warnings = status.get("warnings") if isinstance(status.get("warnings"), list) else []
    return {
        "status": status.get("status"),
        "health_configured": bool(health.get("configured")),
        "metrics_configured": bool(metrics.get("configured")),
        "logs_configured": bool(logs.get("configured")),
        "backup_freshness": backups.get("freshness"),
        "restore_drill_count": restore_drill.get("receipt_count"),
        "receipt_failures": receipts_failures.get("count"),
        "release_present": bool(release.get("present")),
        "route_count": routes.get("count"),
        "blocker_count": len(blockers),
        "warning_count": len(warnings),
    }


def _health_summary(manifest: Any) -> Dict[str, Any]:
    health = getattr(getattr(manifest, "observability", None), "health", None)
    if health is None or health.url is None:
        return {"configured": False, "url": None, "expect_status": None, "probe": None}
    error = validate_health_url(health.url)
    summary: Dict[str, Any] = {
        "configured": True,
        "url": health.url,
        "expect_status": health.expect_status,
        "probe": None,
    }
    if error is not None:
        summary["config_error"] = error
    return summary


def _metrics_summary(manifest: Any, warnings: List[Dict[str, str]]) -> Dict[str, Any]:
    metrics = getattr(getattr(manifest, "observability", None), "metrics", None)
    if metrics is None:
        return {"configured": False, "url": None, "format": None, "auth": None}
    if metrics.url is None:
        # A declared metrics block with no endpoint is a warning, never a blocker.
        # `format: none` is a deliberate disable, not a misconfiguration.
        warnings.append(
            schema_issue(
                "observability_metrics_endpoint_missing",
                "Metrics are declared but no metrics endpoint URL is configured.",
                "observability.metrics.url",
            )
        )
    return {
        "configured": True,
        "url": metrics.url,
        "format": metrics.format,
        # auth is a mode name only; never a value.
        "auth": metrics.auth,
    }


def _logs_summary(manifest: Any) -> Dict[str, Any]:
    logs = getattr(getattr(manifest, "observability", None), "logs", None)
    if logs is None:
        return {"configured": False, "containers": None, "retain_days": None}
    return {"configured": True, "containers": logs.containers, "retain_days": logs.retain_days}


def _route_summary(manifest: Any) -> Dict[str, Any]:
    routes = getattr(manifest, "routes", None) or []
    domains = sorted({route.domain for route in routes})
    return {"count": len(routes), "domains": domains}


def _release_summary(runtime_root: Path, app: str) -> Dict[str, Any]:
    try:
        release = active_release(runtime_root, app)
    except Exception:  # noqa: BLE001 - release metadata is best-effort
        release = None
    return {
        "present": bool(release),
        "release_id": release.get("release_id") if isinstance(release, dict) else None,
    }


def _backup_summary(
    app: str,
    environment: str,
    runtime_root: Path,
    manifest_path: Optional[Path],
) -> Dict[str, Any]:
    try:
        report = backup_status_report(app, environment=environment, runtime_root=runtime_root, manifest_path=manifest_path)
    except Exception:  # noqa: BLE001 - backup status is best-effort
        return {"freshness": "unavailable", "backup_count": 0, "age_hours": None}
    freshness = report.get("freshness") if isinstance(report.get("freshness"), dict) else {}
    return {
        "freshness": freshness.get("status"),
        "age_hours": freshness.get("age_hours"),
        "threshold_hours": freshness.get("threshold_hours"),
        "backup_count": report.get("backup_count", 0),
    }


def _restore_drill_summary(runtime_root: Path, app: str) -> Dict[str, Any]:
    try:
        receipts = _restore_drill_receipts(runtime_root, app)
    except Exception:  # noqa: BLE001 - restore-drill scan is best-effort
        receipts = []
    statuses = sorted({str(item.get("status")) for item in receipts if isinstance(item, dict) and item.get("status")})
    return {
        "receipt_count": len(receipts),
        "statuses": statuses,
        "status": "ok" if receipts else "missing",
    }


def _receipt_failure_summary(runtime_root: Path, app: str, environment: Optional[str]) -> Dict[str, Any]:
    try:
        records = _receipt_records(runtime_root, app=app, environment=environment)
    except Exception:  # noqa: BLE001 - receipt scan is best-effort
        records = []
    failures = [
        {
            "receipt_id": record.get("receipt_id"),
            "operation": record.get("operation"),
            "status": record.get("status"),
        }
        for record in records
        if isinstance(record, dict) and str(record.get("status") or "").lower() in _FAILURE_STATUSES
    ]
    return {
        "count": len(failures),
        "total_receipts": len(records),
        # Cap the surfaced failure list so the summary stays bounded.
        "recent": failures[-5:],
    }


def _apply_http_probe(health: Dict[str, Any], http_timeout: float, warnings: List[Dict[str, str]]) -> None:
    """Run a single bounded HTTP GET against the declared health URL.

    Any failure (bad URL, timeout, connection error, unexpected status) records a
    warning and a ``probe`` summary. Never raises, never hangs beyond the bound.
    """
    if not health.get("configured"):
        warnings.append(
            schema_issue(
                "observability_probe_skipped",
                "HTTP probe requested but no health URL is configured.",
                "observability.health.url",
            )
        )
        return
    url = health.get("url")
    error = validate_health_url(url if isinstance(url, str) else None)
    if error is not None:
        health["probe"] = {"ok": False, "reason": "invalid_url"}
        warnings.append(schema_issue("observability_health_url_invalid", str(error), "observability.health.url"))
        return

    timeout = _bounded_timeout(http_timeout)
    expect_status = health.get("expect_status") or 200
    try:
        req = request.Request(str(url), method="GET")
        with request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - validated http(s) URL
            code = int(getattr(response, "status", 0) or response.getcode())
        ok = code == int(expect_status)
        health["probe"] = {"ok": ok, "status_code": code, "expected": int(expect_status)}
        if not ok:
            warnings.append(
                schema_issue(
                    "observability_health_unexpected_status",
                    f"Health probe returned {code}, expected {int(expect_status)}.",
                    "observability.health",
                )
            )
    except HTTPError as exc:
        # A 4xx/5xx response is the most useful unhealthy signal; preserve the
        # status code rather than collapsing it into a generic probe failure.
        code = int(exc.code)
        ok = code == int(expect_status)
        health["probe"] = {"ok": ok, "status_code": code, "expected": int(expect_status)}
        if not ok:
            warnings.append(
                schema_issue(
                    "observability_health_unexpected_status",
                    f"Health probe returned {code}, expected {int(expect_status)}.",
                    "observability.health",
                )
            )
    except Exception as exc:  # noqa: BLE001 - probe failure must never crash or hang the status call
        reason = "timeout" if _looks_like_timeout(exc) else type(exc).__name__
        health["probe"] = {"ok": False, "reason": reason}
        warnings.append(
            schema_issue(
                "observability_health_probe_failed",
                f"Health probe failed ({reason}).",
                "observability.health",
            )
        )


def _container_summary(check_docker: bool, app: str, warnings: List[Dict[str, str]]) -> Dict[str, Any]:
    if not check_docker:
        return {"checked": False, "available": None, "containers": []}
    if os.environ.get("OPHELIA_SKIP_DOCKER_STATUS") == "1":
        return {"checked": True, "available": False, "containers": []}
    docker_bin = shutil.which("docker")
    if not docker_bin:
        warnings.append(
            schema_issue(
                "observability_docker_unavailable",
                "Docker check requested but no docker binary is available.",
                "containers",
            )
        )
        return {"checked": True, "available": False, "containers": []}
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, read-only `ps`
            [docker_bin, "ps", "--filter", f"name={app}", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - docker probe must never crash or hang
        reason = "timeout" if _looks_like_timeout(exc) else type(exc).__name__
        warnings.append(schema_issue("observability_docker_probe_failed", f"Docker check failed ({reason}).", "containers"))
        return {"checked": True, "available": True, "containers": []}

    containers: List[Dict[str, str]] = []
    for line in (result.stdout or "").splitlines():
        name, _, container_status = line.partition("\t")
        if name.strip():
            containers.append({"name": name.strip(), "status": container_status.strip()})
    return {"checked": True, "available": True, "containers": containers}


def _bounded_timeout(value: Any) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return _DEFAULT_HTTP_TIMEOUT
    if timeout <= 0:
        return _DEFAULT_HTTP_TIMEOUT
    return min(timeout, _MAX_HTTP_TIMEOUT)


def _looks_like_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, URLError) and isinstance(getattr(exc, "reason", None), (TimeoutError, OSError)):
        return isinstance(exc.reason, TimeoutError) or "timed out" in str(exc.reason).lower()
    if isinstance(exc, subprocess.TimeoutExpired):
        return True
    return "timed out" in str(exc).lower()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    path.write_text(_json.dumps(payload, indent=2, sort_keys=True) + "\n")
