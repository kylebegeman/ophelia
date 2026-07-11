from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import command_catalog
from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .live_readiness import live_readiness_report
from .lumen_adapter import console_data
from .operation_schema import SCHEMA_VERSION, issue, operation_id
from .plugin_contracts import DEFAULT_PLUGINS_DIR, plugin_inventory
from .redaction import deep_redact
from .state_db import state_status
from .workflows import list_workflow_templates

PRODUCTION_HARDENING_KIND = "ophelia.production_hardening_report"
DEFAULT_FIXTURE_ROOT = REPO_ROOT / "fixtures" / "app-suite"


def production_hardening_report(
    *,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifests_dir: Path = REPO_ROOT / "manifests",
    ophelia_root: Path = REPO_ROOT,
    host_config: Optional[Path] = None,
    provider_config: Optional[Path] = None,
    plugins_dir: Path = DEFAULT_PLUGINS_DIR,
    include_fixture_suite: bool = False,
    fixture_root: Path = DEFAULT_FIXTURE_ROOT,
    allow_blocked_live_readiness: bool = False,
    expected_fixture_blocked_app: str = "fixture-incomplete-app",
) -> Dict[str, Any]:
    """Read-only production hardening report.

    This report is a go/no-go aggregate for moving from contract work toward
    live staging/prod usage. It performs no mutation, accepts no confirmation
    token, and only composes existing read-only contracts.
    """
    runtime_root = Path(runtime_root)
    manifests_dir = Path(manifests_dir)
    ophelia_root = Path(ophelia_root)
    plugins_dir = Path(plugins_dir)
    fixture_root = Path(fixture_root)
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    checks: List[Dict[str, Any]] = []

    live = _safe_child(
        "live_readiness",
        warnings,
        lambda: live_readiness_report(
            runtime_root=runtime_root,
            manifests_dir=manifests_dir,
            ophelia_root=ophelia_root,
            host_config=host_config,
            provider_config=provider_config,
        ),
    )
    _check_child_status(
        checks,
        blockers,
        warnings,
        "live_readiness",
        live,
        allow_blocked=allow_blocked_live_readiness,
        allowed_blocked_message="Live readiness is blocked but explicitly allowed for this drill.",
    )

    console = _safe_child(
        "lumen_console",
        warnings,
        lambda: console_data(runtime_root=runtime_root, manifests_dir=manifests_dir, plugins_dir=plugins_dir),
    )
    _check_child_status(checks, blockers, warnings, "lumen_console", console)

    plugins = _safe_child("plugins", warnings, lambda: plugin_inventory(plugins_dir))
    _check_child_status(checks, blockers, warnings, "plugins", plugins)

    state = _safe_child("state_status", warnings, lambda: state_status(runtime_root))
    _check_child_status(checks, blockers, warnings, "state_status", state, warning_only=True)

    workflow_templates = _safe_child("workflow_templates", warnings, list_workflow_templates)
    template_count = len(workflow_templates.get("templates", [])) if isinstance(workflow_templates.get("templates"), list) else 0
    checks.append(
        {
            "name": "workflow_templates_available",
            "ok": template_count > 0,
            "message": f"{template_count} workflow template(s).",
        }
    )
    if template_count == 0:
        blockers.append(issue("workflow_templates_missing", "No workflow templates are available.", "workflows"))

    catalog_safety = _catalog_safety()
    checks.append(catalog_safety["check"])
    blockers.extend(catalog_safety["blockers"])

    fixture_drill = None
    if include_fixture_suite:
        fixture_drill = _fixture_suite_drill(
            fixture_root,
            expected_blocked_app=expected_fixture_blocked_app,
            warnings=warnings,
            blockers=blockers,
            checks=checks,
        )

    status = "blocked" if blockers else "warning" if warnings else "ok"
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": PRODUCTION_HARDENING_KIND,
        "operation": "production.hardening",
        "operation_id": operation_id("production.hardening"),
        "status": status,
        "go_no_go": "no_go" if blockers else "review" if warnings else "go",
        "runtime_root": str(runtime_root),
        "manifests_dir": str(manifests_dir),
        "plugins_dir": str(plugins_dir),
        "read_only": True,
        "mutates_state": False,
        "confirmation_required": False,
        "checks": checks,
        "reports": {
            "live_readiness": _compact_child(live),
            "lumen_console": _compact_console(console),
            "plugins": _compact_plugins(plugins),
            "state_status": _compact_child(state),
            "workflow_templates": {
                "kind": workflow_templates.get("kind"),
                "template_count": template_count,
            },
            "catalog_safety": catalog_safety["summary"],
            "fixture_suite": fixture_drill,
        },
        "blockers": _dedupe_issues(blockers),
        "warnings": _dedupe_issues(warnings),
        "safety": {
            "execution": "none",
            "mutation": "none",
            "confirmation_tokens_accepted": False,
            "values_redacted": True,
        },
        "summary": f"Production hardening: {status}; {len(blockers)} blocker(s), {len(warnings)} warning(s).",
    }
    return deep_redact(
        payload,
        safe_keys={"confirmation_tokens_accepted", "values_redacted", "read_only"},
        propagate=True,
    )


def _fixture_suite_drill(
    fixture_root: Path,
    *,
    expected_blocked_app: str,
    warnings: List[Dict[str, str]],
    blockers: List[Dict[str, str]],
    checks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    live = _safe_child(
        "fixture_live_readiness",
        warnings,
        lambda: live_readiness_report(
            runtime_root=fixture_root / "runtime",
            manifests_dir=fixture_root / "manifests",
            ophelia_root=REPO_ROOT,
            host_config=fixture_root / "host-inventory.yml",
            provider_config=fixture_root / "integrations.yml",
        ),
    )
    console = _safe_child(
        "fixture_console",
        warnings,
        lambda: console_data(
            runtime_root=fixture_root / "runtime",
            manifests_dir=fixture_root / "manifests",
            plugins_dir=fixture_root / "plugins",
        ),
    )
    blocked_apps = sorted(
        str(app.get("app"))
        for app in live.get("apps", [])
        if isinstance(app, dict) and app.get("status") == "blocked"
    )
    expected_ok = blocked_apps == [expected_blocked_app]
    checks.append(
        {
            "name": "fixture_suite_expected_blocked_state",
            "ok": expected_ok,
            "message": f"blocked_apps={blocked_apps}",
        }
    )
    if not expected_ok:
        blockers.append(
            issue(
                "fixture_suite_unexpected_blocked_state",
                f"Expected only `{expected_blocked_app}` to be blocked, got {blocked_apps}.",
                "fixtures/app-suite",
            )
        )
    console_apps = console.get("apps") if isinstance(console.get("apps"), list) else []
    checks.append(
        {
            "name": "fixture_console_app_count",
            "ok": len(console_apps) == 8,
            "message": f"{len(console_apps)} fixture console app row(s).",
        }
    )
    return {
        "fixture_root": str(fixture_root),
        "expected_blocked_app": expected_blocked_app,
        "blocked_apps": blocked_apps,
        "live_readiness": _compact_child(live),
        "console": _compact_console(console),
    }


def _catalog_safety() -> Dict[str, Any]:
    descriptors = command_catalog.catalog()
    blockers: List[Dict[str, str]] = []
    mutating = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict) or not descriptor.get("mutates_state"):
            continue
        mutating.append(descriptor)
        if not descriptor.get("requires_confirmation") and not descriptor.get("plan_command"):
            blockers.append(
                issue(
                    "mutating_descriptor_unguarded",
                    f"{descriptor.get('operation')} mutates state without confirmation or a plan command.",
                    str(descriptor.get("operation") or "command_catalog"),
                )
            )
    return {
        "check": {
            "name": "mutating_command_descriptors_guarded",
            "ok": not blockers,
            "message": f"{len(mutating)} mutating descriptor(s), {len(blockers)} unguarded.",
        },
        "blockers": blockers,
        "summary": {
            "command_count": len(descriptors),
            "mutating_count": len(mutating),
            "unguarded_count": len(blockers),
        },
    }


def _safe_child(name: str, warnings: List[Dict[str, str]], factory: Callable[[], Any]) -> Dict[str, Any]:
    try:
        value = factory()
    except Exception as exc:  # noqa: BLE001 - hardening report should degrade
        warnings.append(issue("hardening_child_failed", f"{name}: {type(exc).__name__}", name))
        return {"kind": "ophelia.subreport_error", "status": "blocked", "summary": f"{name} failed."}
    return value if isinstance(value, dict) else {"kind": "ophelia.subreport_error", "status": "blocked", "summary": f"{name} returned a non-object."}


def _check_child_status(
    checks: List[Dict[str, Any]],
    blockers: List[Dict[str, str]],
    warnings: List[Dict[str, str]],
    name: str,
    report: Dict[str, Any],
    *,
    allow_blocked: bool = False,
    allowed_blocked_message: str = "",
    warning_only: bool = False,
) -> None:
    status = str(report.get("status") or "unknown")
    ok = status not in {"blocked", "failed", "error"}
    checks.append({"name": name, "ok": ok or allow_blocked or warning_only, "message": status})
    if ok:
        return
    item = issue(f"{name}_blocked", f"{name} status is {status}.", name)
    if allow_blocked or warning_only:
        warnings.append(issue(item["code"], allowed_blocked_message or item["message"], name))
    else:
        blockers.append(item)


def _compact_child(report: Dict[str, Any]) -> Dict[str, Any]:
    blockers = report.get("blockers") if isinstance(report.get("blockers"), list) else []
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    return {
        "kind": report.get("kind"),
        "status": report.get("status"),
        "summary": report.get("summary"),
        "blocker_count": len(blockers),
        "warning_count": len(warnings),
        "totals": report.get("totals") if isinstance(report.get("totals"), dict) else None,
    }


def _compact_console(report: Dict[str, Any]) -> Dict[str, Any]:
    overview = report.get("overview") if isinstance(report.get("overview"), dict) else {}
    return {
        "kind": report.get("kind"),
        "app_count": len(report.get("apps", [])) if isinstance(report.get("apps"), list) else 0,
        "approval_count": len(report.get("approval_queue", [])) if isinstance(report.get("approval_queue"), list) else 0,
        "cards": overview.get("cards") if isinstance(overview.get("cards"), list) else [],
    }


def _compact_plugins(report: Dict[str, Any]) -> Dict[str, Any]:
    plugins = report.get("plugins") if isinstance(report.get("plugins"), list) else []
    return {
        "kind": report.get("kind"),
        "status": report.get("status"),
        "plugin_count": len(plugins),
        "blocker_count": len(report.get("blockers", [])) if isinstance(report.get("blockers"), list) else 0,
    }


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
