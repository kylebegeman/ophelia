"""Lumen ops adapter: a thin translation layer over existing Ophelia contracts.

Lumen (the ops UI/agent surface) needs a small, stable set of read-only views:
what Ophelia can do (capabilities + action descriptors) and one aggregate
dashboard of every known app/environment's health. This module is a *thin
translation layer only*. It reuses the existing, already-redaction-safe
contracts (:mod:`ophelia.command_catalog`, :mod:`ophelia.portability`,
:mod:`ophelia.receipt_index`, :mod:`ophelia.conflicts`,
:mod:`ophelia.operator_reports`) and never reimplements any business logic.

Secret safety
-------------
Every assembled payload is swept with :func:`ophelia.redaction.deep_redact` as a
final safety net. No raw env value, token, private key, DB URL, or full log is
ever emitted. The dashboard surfaces only counts, codes, statuses, and scores.

Import safety
-------------
This module must remain importable on its own with no circular import. It
imports only the read-only contract modules above; it must never import any
``ophelia.commands.*`` module at top level (those import the catalog, which can
trigger CLI-descriptor registration). The command catalog already lazily loads
CLI descriptors on demand, so :func:`action_descriptors` stays a pure delegation
to :func:`ophelia.command_catalog.catalog`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from . import command_catalog
from .api_routes import HTTP_ROUTE_PATTERNS
from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .conflicts import scan_conflicts
from .observability import compact_observability_summary, observability_status
from .operation_schema import SCHEMA_VERSION, issue
from .operator_reports import manifest_registry
from .portability import app_readiness_report, backup_status_report, traffic_status
from .receipt_index import receipt_timeline
from .redaction import deep_redact
from .state_db import state_summary

CAPABILITIES_KIND = "ophelia.lumen.capabilities"
ACTION_DESCRIPTORS_KIND = "ophelia.lumen.action_descriptors"
DASHBOARD_KIND = "ophelia.lumen.dashboard"
APPS_KIND = "ophelia.lumen.apps"

# Recent receipts surfaced per app/environment on the dashboard.
_DASHBOARD_RECEIPT_LIMIT = 10

# The named surfaces Lumen can render. These map to existing Ophelia contracts;
# this list is descriptive metadata, not a router.
_SURFACES: List[str] = [
    "catalog",
    "schema",
    "self_test",
    "providers",
    "secrets_audit",
    "readiness",
    "receipts_timeline",
    "state_db",
    "state_service",
    "policy",
    "workflows",
]


def capabilities(runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> Dict[str, Any]:
    """Manifest of what Ophelia exposes to Lumen (kind ``ophelia.lumen.capabilities``).

    Lists the command catalog (with a count), the available read-only HTTP
    endpoint path templates, the schema version, and the named Lumen surfaces.
    Carries no secret values.
    """
    descriptors = command_catalog.catalog()
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": CAPABILITIES_KIND,
        "runtime_root": str(runtime_root),
        "commands": {
            "count": len(descriptors),
            "catalog": descriptors,
        },
        "http_endpoints": list(HTTP_ROUTE_PATTERNS),
        "surfaces": list(_SURFACES),
        "summary": (
            f"Ophelia exposes {len(descriptors)} command(s), "
            f"{len(HTTP_ROUTE_PATTERNS)} read-only HTTP endpoint(s), "
            f"and {len(_SURFACES)} Lumen surface(s)."
        ),
    }
    return deep_redact(payload)


def action_descriptors() -> Dict[str, Any]:
    """Shared command catalog, framed for Lumen (kind ``ophelia.lumen.action_descriptors``).

    The ``descriptors`` are sourced directly from the shared command catalog
    (:func:`ophelia.command_catalog.catalog`) so there is no second copy of the
    action registry to drift out of sync.
    """
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": ACTION_DESCRIPTORS_KIND,
        "descriptors": command_catalog.catalog(),
    }
    return deep_redact(payload)


def app_readiness(
    app: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> Dict[str, Any]:
    """Thin wrapper over :func:`ophelia.portability.app_readiness_report`.

    The underlying report is already secret-redacted; ``deep_redact`` is applied
    as a belt-and-suspenders safety net.
    """
    return deep_redact(app_readiness_report(app, environment=environment, runtime_root=runtime_root))


def app_timeline(
    app: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> Dict[str, Any]:
    """Thin wrapper over :func:`ophelia.receipt_index.receipt_timeline`."""
    return deep_redact(
        receipt_timeline(runtime_root, app=app, environment=environment)
    )


def dashboard_data(
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifests_dir: Path = REPO_ROOT / "manifests",
) -> Dict[str, Any]:
    """Aggregate health view for every known app/environment (kind ``ophelia.lumen.dashboard``).

    For each app/environment in the manifest registry this surfaces: readiness
    status + portability score, open blocker counts/codes (codes only, never the
    full secret-bearing finding payloads), recent receipts (capped at
    :data:`_DASHBOARD_RECEIPT_LIMIT`), route-conflict counts, backup freshness,
    compact traffic status, and compact observability status.

    Resilient by construction: a failing per-app sub-report becomes a structured
    ``warnings`` entry, never a crash. The whole assembled payload is run through
    :func:`ophelia.redaction.deep_redact` as a final safety net so no secret
    value can leak even if an upstream report changes shape.
    """
    runtime_root = Path(runtime_root)
    manifests_dir = Path(manifests_dir)
    warnings: List[Dict[str, str]] = []

    registry = _safe_manifest_registry(manifests_dir, runtime_root, warnings)
    entries: List[Dict[str, Any]] = []
    for manifest_entry in registry.get("manifests", []):
        if not isinstance(manifest_entry, dict):
            continue
        app = manifest_entry.get("app")
        if not isinstance(app, str) or not app:
            continue
        environment = manifest_entry.get("environment") if isinstance(manifest_entry.get("environment"), str) else None
        manifest_path = manifest_entry.get("manifest_path") if isinstance(manifest_entry.get("manifest_path"), str) else None
        entries.append(_dashboard_app_entry(app, environment, manifest_path, runtime_root, warnings))

    # Registry-level manifest load errors become warnings, never a crash.
    for error in registry.get("errors", []):
        if isinstance(error, dict):
            warnings.append(
                issue(
                    "manifest_registry_error",
                    str(error.get("error") or "Manifest could not be loaded."),
                    str(error.get("path")) if error.get("path") else None,
                )
            )

    ready = sum(1 for entry in entries if entry.get("readiness_level") == "ready")
    blocked = sum(1 for entry in entries if entry.get("readiness_level") == "blocked")
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": DASHBOARD_KIND,
        "runtime_root": str(runtime_root),
        "manifests_dir": str(manifests_dir),
        "apps": entries,
        "state_service": state_summary(runtime_root),
        "warnings": warnings,
        "totals": {
            "app_count": len(entries),
            "ready": ready,
            "blocked": blocked,
            "warning": sum(1 for entry in entries if entry.get("readiness_level") == "warning"),
        },
        "traffic_status": _aggregate_traffic_status(entries),
        "observability": _aggregate_observability(entries),
        "summary": (
            f"{len(entries)} app/environment(s): {ready} ready, {blocked} blocked, "
            f"{len(warnings)} warning(s)."
        ),
    }
    return deep_redact(payload)


def _dashboard_app_entry(
    app: str,
    environment: Optional[str],
    manifest_path: Optional[str],
    runtime_root: Path,
    warnings: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Build one app/environment dashboard row, never raising on a sub-report failure."""
    entry: Dict[str, Any] = {
        "app": app,
        "environment": environment,
        "manifest_path": manifest_path,
        "readiness_level": None,
        "portability_score": None,
        "blockers": {"count": 0, "codes": []},
        "warnings_count": 0,
        "recent_receipts": [],
        "route_conflicts": {"count": 0, "ok": None},
        "backup_freshness": None,
        "traffic_status": None,
        "observability": None,
    }

    readiness = _safe_call(
        lambda: app_readiness_report(app, environment=environment, runtime_root=runtime_root),
        warnings,
        "readiness_unavailable",
        app,
    )
    if isinstance(readiness, dict):
        entry["readiness_level"] = readiness.get("readiness_level")
        score = readiness.get("portability_score")
        if isinstance(score, dict):
            # Surface only the scalar score + level, not the full factor payload.
            entry["portability_score"] = {
                "score": score.get("score"),
                "level": score.get("level"),
            }
        blockers = readiness.get("blockers") if isinstance(readiness.get("blockers"), list) else []
        entry["blockers"] = {
            "count": len(blockers),
            "codes": [str(item.get("code")) for item in blockers if isinstance(item, dict) and item.get("code")],
        }
        warnings_list = readiness.get("warnings") if isinstance(readiness.get("warnings"), list) else []
        entry["warnings_count"] = len(warnings_list)

    timeline = _safe_call(
        lambda: receipt_timeline(runtime_root, app=app, environment=environment),
        warnings,
        "timeline_unavailable",
        app,
    )
    if isinstance(timeline, dict):
        receipts = timeline.get("receipts") if isinstance(timeline.get("receipts"), list) else []
        entry["recent_receipts"] = [_receipt_summary(item) for item in receipts[:_DASHBOARD_RECEIPT_LIMIT]]

    conflicts = _safe_call(
        lambda: scan_conflicts(_conflict_dir(manifest_path), runtime_root=runtime_root),
        warnings,
        "route_conflicts_unavailable",
        app,
    )
    if isinstance(conflicts, dict):
        conflict_list = conflicts.get("conflicts") if isinstance(conflicts.get("conflicts"), list) else []
        entry["route_conflicts"] = {"count": len(conflict_list), "ok": bool(conflicts.get("ok"))}

    backup = _safe_call(
        lambda: backup_status_report(app, environment=environment, runtime_root=runtime_root),
        warnings,
        "backup_status_unavailable",
        app,
    )
    if isinstance(backup, dict):
        freshness = backup.get("freshness")
        if isinstance(freshness, dict):
            entry["backup_freshness"] = freshness.get("status")

    # Default observability status (no HTTP/Docker probe) reduced to a compact
    # summary. Best-effort: a failure becomes a warning, never a crash.
    observability = _safe_call(
        lambda: observability_status(
            app,
            environment=environment,
            runtime_root=runtime_root,
            manifest_path=Path(manifest_path) if manifest_path else None,
        ),
        warnings,
        "observability_unavailable",
        app,
    )
    if isinstance(observability, dict):
        entry["observability"] = compact_observability_summary(observability)

    traffic = _safe_call(
        lambda: traffic_status(
            app,
            environment=environment,
            runtime_root=runtime_root,
            manifest_path=Path(manifest_path) if manifest_path else None,
        ),
        warnings,
        "traffic_status_unavailable",
        app,
    )
    if isinstance(traffic, dict):
        entry["traffic_status"] = _compact_traffic_summary(traffic)

    return entry


def _compact_traffic_summary(status: Dict[str, Any]) -> Dict[str, Any]:
    latest_apply = status.get("latest_apply") if isinstance(status.get("latest_apply"), dict) else {}
    latest_rollback = status.get("latest_rollback") if isinstance(status.get("latest_rollback"), dict) else {}
    route_conflicts = status.get("route_conflicts") if isinstance(status.get("route_conflicts"), list) else []
    route_ownership = status.get("route_ownership") if isinstance(status.get("route_ownership"), list) else []
    blockers = status.get("blockers") if isinstance(status.get("blockers"), list) else []
    target_health = status.get("target_health") if isinstance(status.get("target_health"), dict) else {}
    return {
        "status": status.get("status"),
        "latest_apply_status": latest_apply.get("status"),
        "latest_apply_completed_at": latest_apply.get("completed_at"),
        "latest_rollback_status": latest_rollback.get("status"),
        "provider_mutation_performed": bool(latest_apply.get("provider_mutation_performed")),
        "route_count": len(route_ownership),
        "route_conflict_count": len(route_conflicts),
        "target_health_ok": target_health.get("ok"),
        "blocker_count": len(blockers),
    }


def _aggregate_observability(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    summaries = [entry.get("observability") for entry in entries if isinstance(entry.get("observability"), dict)]
    statuses = _count_values(summary.get("status") for summary in summaries)
    warning_count = sum(int(summary.get("warning_count") or 0) for summary in summaries)
    blocker_count = sum(int(summary.get("blocker_count") or 0) for summary in summaries)
    return {
        "app_count": len(summaries),
        "status": "blocked" if blocker_count else "warning" if warning_count else "ok",
        "statuses": statuses,
        "health_configured": sum(1 for summary in summaries if summary.get("health_configured")),
        "metrics_configured": sum(1 for summary in summaries if summary.get("metrics_configured")),
        "logs_configured": sum(1 for summary in summaries if summary.get("logs_configured")),
        "receipt_failures": sum(int(summary.get("receipt_failures") or 0) for summary in summaries),
        "warning_count": warning_count,
        "blocker_count": blocker_count,
    }


def _aggregate_traffic_status(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    summaries = [entry.get("traffic_status") for entry in entries if isinstance(entry.get("traffic_status"), dict)]
    statuses = _count_values(summary.get("status") for summary in summaries)
    blocker_count = sum(int(summary.get("blocker_count") or 0) for summary in summaries)
    route_conflict_count = sum(int(summary.get("route_conflict_count") or 0) for summary in summaries)
    return {
        "app_count": len(summaries),
        "status": "blocked" if blocker_count else "warning" if route_conflict_count else "ok",
        "statuses": statuses,
        "with_latest_apply": sum(1 for summary in summaries if summary.get("latest_apply_status")),
        "with_latest_rollback": sum(1 for summary in summaries if summary.get("latest_rollback_status")),
        "provider_mutations_performed": sum(1 for summary in summaries if summary.get("provider_mutation_performed")),
        "route_conflict_count": route_conflict_count,
        "blocker_count": blocker_count,
    }


def _count_values(values) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for value in values:
        if value is None:
            continue
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _receipt_summary(receipt: Any) -> Dict[str, Any]:
    """Compact, secret-free receipt summary for the dashboard."""
    if not isinstance(receipt, dict):
        return {}
    return {
        "receipt_id": receipt.get("receipt_id"),
        "operation": receipt.get("operation"),
        "status": receipt.get("status"),
        "started_at": receipt.get("started_at"),
        "rollback_available": bool(receipt.get("rollback_available")),
    }


def _conflict_dir(manifest_path: Optional[str]) -> Path:
    """Directory to scan for route conflicts for one app.

    ``scan_conflicts`` globs a directory; use the app manifest's parent when
    known, otherwise fall back to the repo manifests directory.
    """
    if manifest_path:
        return Path(manifest_path).parent
    return REPO_ROOT / "manifests"


def _safe_manifest_registry(
    manifests_dir: Path,
    runtime_root: Path,
    warnings: List[Dict[str, str]],
) -> Dict[str, Any]:
    registry = _safe_call(
        lambda: manifest_registry(manifests_dir, runtime_root),
        warnings,
        "manifest_registry_unavailable",
        None,
    )
    if isinstance(registry, dict):
        return registry
    return {"manifests": [], "errors": []}


def _safe_call(
    func,
    warnings: List[Dict[str, str]],
    code: str,
    app: Optional[str],
) -> Optional[Any]:
    """Run ``func`` and convert any failure into a warning entry, never raising."""
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 - per-app sub-reports are best-effort
        message = f"{code.replace('_', ' ')}"
        if app:
            message = f"{message} for {app}"
        # Record only the exception type, never str(exc): a raw secret embedded
        # in an exception message could otherwise reach the warning un-redacted.
        warnings.append(issue(code, f"{message} (failed: {type(exc).__name__})", app))
        return None
