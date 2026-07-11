"""Operator-console adapter: a thin translation layer over existing Ophelia contracts.

Private operator UIs need a small, stable set of read-only views:
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

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import command_catalog
from .api_routes import HTTP_ROUTE_PATTERNS
from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .conflicts import scan_conflicts
from .host_inventory import app_placement_plan, collect_host_inventory
from .observability import compact_observability_summary, observability_status
from .operation_schema import SCHEMA_VERSION, issue
from .operator_reports import manifest_registry
from .plugin_contracts import plugin_inventory as plugin_catalog
from .portability import app_readiness_report, backup_status_report, traffic_status
from .receipt_index import receipt_timeline
from .redaction import deep_redact
from .state_db import state_summary
from .workflows import list_workflow_templates

CAPABILITIES_KIND = "ophelia.lumen.capabilities"
ACTION_DESCRIPTORS_KIND = "ophelia.lumen.action_descriptors"
DASHBOARD_KIND = "ophelia.lumen.dashboard"
APPS_KIND = "ophelia.lumen.apps"
CONSOLE_KIND = "ophelia.lumen.console"

# Recent receipts surfaced per app/environment on the dashboard.
_DASHBOARD_RECEIPT_LIMIT = 10

# The named surfaces an operator console can render. These map to existing Ophelia contracts;
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
    "github_providers",
    "secret_providers",
    "host_inventory",
    "placement",
    "live_readiness",
    "plugins",
    "console",
]


def capabilities(runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> Dict[str, Any]:
    """Manifest of what Ophelia exposes to operator UIs (kind ``ophelia.lumen.capabilities``).

    Lists the command catalog (with a count), the available read-only HTTP
    endpoint path templates, the schema version, and the named operator-console surfaces.
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
        "plugins": plugin_catalog(REPO_ROOT / "plugins"),
        "summary": (
            f"Ophelia exposes {len(descriptors)} command(s), "
            f"{len(HTTP_ROUTE_PATTERNS)} read-only HTTP endpoint(s), "
            f"and {len(_SURFACES)} operator-console surface(s)."
        ),
    }
    return deep_redact(payload)


def action_descriptors() -> Dict[str, Any]:
    """Shared command catalog, framed for operator UIs (kind ``ophelia.lumen.action_descriptors``).

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


def console_data(
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifests_dir: Path = REPO_ROOT / "manifests",
    plugins_dir: Path = REPO_ROOT / "plugins",
) -> Dict[str, Any]:
    """Single read-only payload for an operator console.

    The console contract composes existing Ophelia surfaces into a bounded model
    private operator UIs can render directly: navigation, overview cards, app rows, approval
    queue, workflow summaries, plugin metadata, and safe quick actions. It does
    not execute operations and it does not include raw findings or secret values.
    """
    runtime_root = Path(runtime_root)
    manifests_dir = Path(manifests_dir)
    plugins_dir = Path(plugins_dir)
    dashboard = dashboard_data(runtime_root=runtime_root, manifests_dir=manifests_dir)
    capabilities_report = capabilities(runtime_root=runtime_root)
    plugin_report = plugin_catalog(plugins_dir)
    templates = list_workflow_templates()
    workflows = _stored_workflow_summaries(runtime_root)
    descriptors = command_catalog.catalog()
    approval_queue = _approval_queue(descriptors, workflows)
    apps = [_console_app_row(entry) for entry in dashboard.get("apps", []) if isinstance(entry, dict)]
    blockers = sum(int((entry.get("blockers") or {}).get("count") or 0) for entry in apps if isinstance(entry.get("blockers"), dict))
    warnings_count = sum(int(entry.get("warnings_count") or 0) for entry in apps)
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": CONSOLE_KIND,
        "runtime_root": str(runtime_root),
        "manifests_dir": str(manifests_dir),
        "plugins_dir": str(plugins_dir),
        "read_only": True,
        "mutates_state": False,
        "navigation": _console_navigation(),
        "overview": {
            "cards": [
                {
                    "id": "apps",
                    "label": "Apps",
                    "value": len(apps),
                    "status": _status_from_counts(blockers, warnings_count),
                },
                {
                    "id": "readiness",
                    "label": "Readiness",
                    "value": dashboard.get("totals", {}),
                    "status": "blocked" if int((dashboard.get("totals") or {}).get("blocked") or 0) else "ok",
                },
                {
                    "id": "approvals",
                    "label": "Approvals",
                    "value": len(approval_queue),
                    "status": "warning" if approval_queue else "ok",
                },
                {
                    "id": "plugins",
                    "label": "Plugins",
                    "value": len(plugin_report.get("plugins", [])) if isinstance(plugin_report.get("plugins"), list) else 0,
                    "status": plugin_report.get("status"),
                },
                {
                    "id": "workflows",
                    "label": "Workflows",
                    "value": len(workflows),
                    "status": "warning" if any(item.get("status") in {"paused", "blocked", "failed"} for item in workflows) else "ok",
                },
            ],
            "summary": dashboard.get("summary"),
        },
        "apps": apps,
        "approval_queue": approval_queue,
        "workflows": {
            "templates": templates.get("templates", []) if isinstance(templates, dict) else [],
            "stored": workflows,
        },
        "plugins": _compact_plugin_report(plugin_report),
        "quick_actions": _quick_actions(descriptors),
        "sources": {
            "dashboard": {"kind": dashboard.get("kind"), "status": "ok", "app_count": len(apps)},
            "capabilities": {
                "kind": capabilities_report.get("kind"),
                "command_count": (capabilities_report.get("commands") or {}).get("count")
                if isinstance(capabilities_report.get("commands"), dict)
                else None,
            },
            "plugins": {"kind": plugin_report.get("kind"), "status": plugin_report.get("status")},
        },
        "safety": {
            "execution": "none",
            "mutation": "none",
            "confirmation_tokens_accepted": False,
            "values_redacted": True,
        },
        "summary": f"Operator console: {len(apps)} app(s), {len(approval_queue)} approval item(s), {len(workflows)} stored workflow(s).",
    }
    return deep_redact(payload, safe_keys={"values_redacted"}, propagate=True)


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


def host_inventory(
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifests_dir: Path = REPO_ROOT / "manifests",
) -> Dict[str, Any]:
    """Thin wrapper over read-only host inventory."""
    return deep_redact(collect_host_inventory(runtime_root, REPO_ROOT, manifests_dir))


def app_placement(
    app: str,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifests_dir: Path = REPO_ROOT / "manifests",
    source_host: Optional[str] = None,
    target_host: Optional[str] = None,
) -> Dict[str, Any]:
    """Thin wrapper over app placement planning for operator UIs."""
    return deep_redact(
        app_placement_plan(
            app,
            environment=environment,
            runtime_root=runtime_root,
            manifests_dir=manifests_dir,
            ophelia_root=REPO_ROOT,
            source_host=source_host,
            target_host=target_host,
        )
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
        lambda: app_readiness_report(
            app,
            environment=environment,
            runtime_root=runtime_root,
            manifest_path=Path(manifest_path) if manifest_path else None,
        ),
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
        lambda: backup_status_report(
            app,
            environment=environment,
            runtime_root=runtime_root,
            manifest_path=Path(manifest_path) if manifest_path else None,
        ),
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


def _console_navigation() -> List[Dict[str, str]]:
    return [
        {"id": "overview", "label": "Overview"},
        {"id": "apps", "label": "Apps"},
        {"id": "approvals", "label": "Approvals"},
        {"id": "workflows", "label": "Workflows"},
        {"id": "plugins", "label": "Plugins"},
        {"id": "activity", "label": "Activity"},
    ]


def _console_app_row(entry: Dict[str, Any]) -> Dict[str, Any]:
    score = entry.get("portability_score") if isinstance(entry.get("portability_score"), dict) else {}
    blockers = entry.get("blockers") if isinstance(entry.get("blockers"), dict) else {"count": 0, "codes": []}
    route_conflicts = entry.get("route_conflicts") if isinstance(entry.get("route_conflicts"), dict) else {}
    traffic = entry.get("traffic_status") if isinstance(entry.get("traffic_status"), dict) else {}
    observability = entry.get("observability") if isinstance(entry.get("observability"), dict) else {}
    return {
        "app": entry.get("app"),
        "environment": entry.get("environment"),
        "manifest_path": entry.get("manifest_path"),
        "readiness_level": entry.get("readiness_level"),
        "score": score.get("score"),
        "score_level": score.get("level"),
        "blockers": {
            "count": blockers.get("count", 0),
            "codes": blockers.get("codes", []),
        },
        "warnings_count": entry.get("warnings_count", 0),
        "backup_freshness": entry.get("backup_freshness"),
        "route_conflict_count": route_conflicts.get("count"),
        "traffic_status": traffic.get("status"),
        "observability_status": observability.get("status"),
        "primary_actions": [
            {
                "id": "readiness",
                "operation": "app.readiness",
                "command": f"ship app readiness {entry.get('app')} --environment {entry.get('environment') or 'staging'} --json",
                "mutates_state": False,
            },
            {
                "id": "timeline",
                "operation": "receipts.timeline",
                "command": f"ship receipts timeline --app {entry.get('app')} --environment {entry.get('environment') or 'staging'} --json",
                "mutates_state": False,
            },
        ],
    }


def _stored_workflow_summaries(runtime_root: Path) -> List[Dict[str, Any]]:
    workflows_root = Path(runtime_root) / "workflows"
    if not workflows_root.exists():
        return []
    summaries: List[Dict[str, Any]] = []
    for path in sorted(workflows_root.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            summaries.append(
                {
                    "workflow_id": path.stem,
                    "path": str(path),
                    "status": "unreadable",
                    "node_counts": {},
                    "paused_nodes": [],
                }
            )
            continue
        nodes = payload.get("nodes") if isinstance(payload.get("nodes"), list) else []
        node_counts = _count_values(node.get("status") for node in nodes if isinstance(node, dict))
        paused_nodes = [
            {
                "node_id": node.get("id"),
                "operation": node.get("operation"),
                "requires_confirmation": bool(node.get("requires_confirmation")),
                "plan_command": node.get("plan_command"),
            }
            for node in nodes
            if isinstance(node, dict) and node.get("status") == "paused_for_confirmation"
        ]
        summaries.append(
            {
                "workflow_id": payload.get("workflow_id") or path.stem,
                "template": payload.get("template"),
                "app": payload.get("app"),
                "environment": payload.get("environment"),
                "status": payload.get("workflow_status") or payload.get("status"),
                "path": str(path),
                "node_counts": node_counts,
                "paused_nodes": paused_nodes,
                "updated_at": payload.get("updated_at") or payload.get("created_at"),
            }
        )
    return summaries


def _approval_queue(descriptors: List[Dict[str, Any]], workflows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for workflow in workflows:
        for node in workflow.get("paused_nodes", []) if isinstance(workflow.get("paused_nodes"), list) else []:
            if isinstance(node, dict):
                items.append(
                    {
                        "type": "workflow_node",
                        "status": "paused_for_confirmation",
                        "workflow_id": workflow.get("workflow_id"),
                        "node_id": node.get("node_id"),
                        "operation": node.get("operation"),
                        "plan_command": node.get("plan_command"),
                        "requires_confirmation": True,
                    }
                )
    for descriptor in descriptors:
        if not isinstance(descriptor, dict) or not descriptor.get("mutates_state"):
            continue
        items.append(
            {
                "type": "command_descriptor",
                "status": "confirmation_required",
                "operation": descriptor.get("operation"),
                "command": descriptor.get("command"),
                "risk": descriptor.get("risk"),
                "plan_command": descriptor.get("plan_command"),
                "requires_confirmation": bool(descriptor.get("requires_confirmation")),
            }
        )
    return items


def _quick_actions(descriptors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    wanted = {
        "live.readiness.run",
        "state.summary",
        "host.inventory",
        "app.placement.plan",
        "plugins.catalog",
        "workflow.list",
        "lumen.dashboard-data",
    }
    actions: List[Dict[str, Any]] = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict) or descriptor.get("operation") not in wanted:
            continue
        examples = descriptor.get("examples") if isinstance(descriptor.get("examples"), list) else []
        actions.append(
            {
                "operation": descriptor.get("operation"),
                "command": descriptor.get("command"),
                "summary": descriptor.get("summary"),
                "example": examples[0] if examples else descriptor.get("command"),
                "mutates_state": False,
            }
        )
    return sorted(actions, key=lambda item: str(item.get("operation") or ""))


def _compact_plugin_report(report: Dict[str, Any]) -> Dict[str, Any]:
    plugins = []
    for plugin in report.get("plugins", []) if isinstance(report.get("plugins"), list) else []:
        if not isinstance(plugin, dict):
            continue
        counts = plugin.get("capability_counts") if isinstance(plugin.get("capability_counts"), dict) else {}
        plugins.append(
            {
                "name": plugin.get("name"),
                "version": plugin.get("version"),
                "enabled": bool(plugin.get("enabled")),
                "capabilities": [
                    {"type": str(key), "count": int(value)}
                    for key, value in sorted(counts.items())
                    if isinstance(value, int)
                ],
                "lumen_surface_count": len(plugin.get("lumen_surfaces", [])) if isinstance(plugin.get("lumen_surfaces"), list) else 0,
            }
        )
    return {
        "kind": report.get("kind"),
        "status": report.get("status"),
        "plugins_dir": report.get("plugins_dir"),
        "plugin_count": len(plugins),
        "plugins": plugins,
        "blocker_count": len(report.get("blockers", [])) if isinstance(report.get("blockers"), list) else 0,
        "warning_count": len(report.get("warnings", [])) if isinstance(report.get("warnings"), list) else 0,
    }


def _status_from_counts(blockers: int, warnings: int) -> str:
    if blockers:
        return "blocked"
    if warnings:
        return "warning"
    return "ok"


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
