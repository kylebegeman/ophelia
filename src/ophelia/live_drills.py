from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .hardening import production_hardening_report
from .live_readiness import live_readiness_report
from .operation_schema import SCHEMA_VERSION, issue, operation_id
from .plugin_contracts import DEFAULT_PLUGINS_DIR
from .redaction import deep_redact

LIVE_DRILL_PROFILES_KIND = "ophelia.live_drill_profiles"
LIVE_DRILL_RESULT_KIND = "ophelia.live_drill_result"
LIVE_DRILL_RUNS_KIND = "ophelia.live_drill_run_results"
DEFAULT_LIVE_DRILL_PROFILES = REPO_ROOT / "fixtures" / "app-suite" / "live-drills.yml"


def list_live_drill_profiles(profiles_path: Path = DEFAULT_LIVE_DRILL_PROFILES) -> Dict[str, Any]:
    profiles, blockers, warnings = _load_profiles(Path(profiles_path))
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": LIVE_DRILL_PROFILES_KIND,
        "operation": "live_drills.list",
        "operation_id": operation_id("live_drills.list"),
        "status": "blocked" if blockers else "warning" if warnings else "ok",
        "profiles_path": str(profiles_path),
        "read_only": True,
        "mutates_state": False,
        "profiles": [_profile_summary(profile) for profile in profiles],
        "blockers": blockers,
        "warnings": warnings,
        "summary": f"{len(profiles)} live drill profile(s) loaded.",
    }
    return _redact(result)


def run_live_drill(profile_id: str, profiles_path: Path = DEFAULT_LIVE_DRILL_PROFILES) -> Dict[str, Any]:
    profiles, blockers, warnings = _load_profiles(Path(profiles_path))
    profile = next((item for item in profiles if item["id"] == profile_id), None)
    if profile is None:
        blockers.append(issue("live_drill_profile_not_found", f"Live drill profile not found: {profile_id}", "profile"))
        return _redact(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": LIVE_DRILL_RESULT_KIND,
                "operation": "live_drills.run",
                "operation_id": operation_id("live_drills.run", profile_id),
                "profile": profile_id,
                "status": "blocked",
                "read_only": True,
                "mutates_state": False,
                "blockers": blockers,
                "warnings": warnings,
                "summary": f"Live drill profile `{profile_id}` could not be run.",
            }
        )

    paths = _profile_paths(profile)
    live = live_readiness_report(
        runtime_root=paths["runtime_root"],
        manifests_dir=paths["manifests_dir"],
        ophelia_root=paths["ophelia_root"],
        app=profile.get("app"),
        environment=profile.get("environment"),
        manifest_path=paths.get("manifest"),
        host_config=paths.get("host_config"),
        provider_config=paths.get("provider_config"),
        source_host=profile.get("source_host"),
        target_host=profile.get("target_host"),
        probe_http=bool(profile.get("probe_http")),
        check_docker=bool(profile.get("check_docker")),
        http_timeout=float(profile.get("http_timeout") or 5.0),
    )
    hardening = None
    if profile.get("include_hardening"):
        hardening = production_hardening_report(
            runtime_root=paths["runtime_root"],
            manifests_dir=paths["manifests_dir"],
            ophelia_root=paths["ophelia_root"],
            host_config=paths.get("host_config"),
            provider_config=paths.get("provider_config"),
            plugins_dir=paths.get("plugins_dir") or DEFAULT_PLUGINS_DIR,
            include_fixture_suite=bool(profile.get("include_fixture_suite")),
            fixture_root=paths.get("fixture_root") or DEFAULT_LIVE_DRILL_PROFILES.parent,
            allow_blocked_live_readiness=bool(profile.get("allow_blocked_live_readiness")),
            expected_fixture_blocked_app=str(profile.get("expected_fixture_blocked_app") or "fixture-incomplete-app"),
        )

    has_expectations = isinstance(profile.get("expected"), dict) and bool(profile.get("expected"))
    expectation_checks, expectation_blockers = _expectation_checks(profile, live, hardening)
    blockers.extend(expectation_blockers)
    child_statuses = {"live_readiness": live.get("status")}
    if hardening is not None:
        child_statuses["hardening"] = hardening.get("status")
    status = _result_status(blockers, warnings, child_statuses, has_expectations=has_expectations)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": LIVE_DRILL_RESULT_KIND,
        "operation": "live_drills.run",
        "operation_id": operation_id("live_drills.run", profile_id),
        "profile": _profile_summary(profile),
        "status": status,
        "read_only": True,
        "dry_run": True,
        "mutates_state": False,
        "confirmation_required": False,
        "paths": {key: str(value) for key, value in paths.items() if value is not None},
        "child_statuses": child_statuses,
        "expectation_checks": expectation_checks,
        "reports": {
            "live_readiness": _compact_live(live),
            "hardening": _compact_hardening(hardening) if hardening is not None else None,
        },
        "blockers": blockers,
        "warnings": warnings,
        "summary": f"Live drill `{profile_id}`: {status}.",
    }
    return _redact(payload)


def run_all_live_drills(profiles_path: Path = DEFAULT_LIVE_DRILL_PROFILES) -> Dict[str, Any]:
    profiles, blockers, warnings = _load_profiles(Path(profiles_path))
    results = [run_live_drill(str(profile["id"]), profiles_path) for profile in profiles]
    result_blockers = [result for result in results if result.get("status") == "blocked"]
    result_warnings = [result for result in results if result.get("status") == "warning"]
    status = "blocked" if blockers or result_blockers else "warning" if warnings or result_warnings else "ok"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": LIVE_DRILL_RUNS_KIND,
        "operation": "live_drills.run_all",
        "operation_id": operation_id("live_drills.run_all"),
        "status": status,
        "profiles_path": str(profiles_path),
        "read_only": True,
        "dry_run": True,
        "mutates_state": False,
        "results": results,
        "totals": {
            "profile_count": len(results),
            "ok": sum(1 for result in results if result.get("status") == "ok"),
            "warning": len(result_warnings),
            "blocked": len(result_blockers),
        },
        "blockers": blockers,
        "warnings": warnings,
        "summary": f"Ran {len(results)} live drill profile(s): {len(result_blockers)} blocked.",
    }
    return _redact(payload)


def _load_profiles(profiles_path: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], List[Dict[str, str]]]:
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    profiles_path = Path(profiles_path)
    if not profiles_path.exists():
        return [], [issue("live_drill_profiles_missing", f"Profile file not found: {profiles_path}", "profiles_path")], warnings
    try:
        raw = _read_mapping(profiles_path)
    except (OSError, ValueError) as exc:
        return [], [issue("live_drill_profiles_invalid", str(exc), "profiles_path")], warnings
    raw_profiles = raw.get("profiles")
    if not isinstance(raw_profiles, list):
        return [], [issue("live_drill_profiles_invalid", "`profiles` must be a list.", "profiles")], warnings
    profiles: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_profile in enumerate(raw_profiles):
        path = f"profiles[{index}]"
        if not isinstance(raw_profile, dict):
            blockers.append(issue("live_drill_profile_invalid", "Profile must be a mapping.", path))
            continue
        profile_id = str(raw_profile.get("id") or "").strip()
        if not profile_id:
            blockers.append(issue("live_drill_profile_id_missing", "Profile id is required.", path))
            continue
        if profile_id in seen:
            blockers.append(issue("live_drill_profile_duplicate", f"Duplicate profile id: {profile_id}", path))
            continue
        seen.add(profile_id)
        profile = dict(raw_profile)
        profile["id"] = profile_id
        profile["_profiles_dir"] = str(profiles_path.parent)
        profiles.append(profile)
    return profiles, blockers, warnings


def _read_mapping(path: Path) -> Dict[str, Any]:
    text = path.read_text()
    if path.suffix == ".json":
        payload = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except ModuleNotFoundError as exc:  # pragma: no cover - self-test covers dependency
            raise ValueError("PyYAML is required for YAML live drill profiles.") from exc
        payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise ValueError(f"Profile file must be a mapping: {path}")
    return payload


def _profile_paths(profile: Dict[str, Any]) -> Dict[str, Optional[Path]]:
    base = Path(str(profile.get("_profiles_dir") or "."))
    return {
        "runtime_root": _resolve_path(profile.get("runtime_root"), base, DEFAULT_RUNTIME_ROOT),
        "manifests_dir": _resolve_path(profile.get("manifests_dir"), base, REPO_ROOT / "manifests"),
        "ophelia_root": _resolve_path(profile.get("ophelia_root"), base, REPO_ROOT),
        "host_config": _resolve_path(profile.get("host_config"), base, None),
        "provider_config": _resolve_path(profile.get("provider_config"), base, None),
        "plugins_dir": _resolve_path(profile.get("plugins_dir"), base, None),
        "manifest": _resolve_path(profile.get("manifest"), base, None),
        "fixture_root": _resolve_path(profile.get("fixture_root"), base, None),
    }


def _resolve_path(value: Any, base: Path, default: Optional[Path]) -> Optional[Path]:
    if value is None or value == "":
        return default
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path
    return (base / path).resolve()


def _profile_summary(profile: Dict[str, Any]) -> Dict[str, Any]:
    expected = profile.get("expected") if isinstance(profile.get("expected"), dict) else {}
    return {
        "id": profile.get("id"),
        "title": profile.get("title") or profile.get("id"),
        "summary": profile.get("summary"),
        "app": profile.get("app"),
        "environment": profile.get("environment"),
        "include_hardening": bool(profile.get("include_hardening")),
        "probe_http": bool(profile.get("probe_http")),
        "check_docker": bool(profile.get("check_docker")),
        "expected": expected,
    }


def _expectation_checks(
    profile: Dict[str, Any],
    live: Dict[str, Any],
    hardening: Optional[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    expected = profile.get("expected") if isinstance(profile.get("expected"), dict) else {}
    checks: List[Dict[str, Any]] = []
    blockers: List[Dict[str, str]] = []

    def add_check(name: str, actual: Any, expected_value: Any) -> None:
        if expected_value is None:
            return
        ok = actual == expected_value
        checks.append({"name": name, "ok": ok, "expected": expected_value, "actual": actual})
        if not ok:
            blockers.append(issue("live_drill_expectation_failed", f"{name}: expected {expected_value!r}, got {actual!r}.", name))

    apps = live.get("apps") if isinstance(live.get("apps"), list) else []
    by_status = {
        "blocked_apps": sorted(str(app.get("app")) for app in apps if isinstance(app, dict) and app.get("status") == "blocked"),
        "warning_apps": sorted(str(app.get("app")) for app in apps if isinstance(app, dict) and app.get("status") == "warning"),
        "ok_apps": sorted(str(app.get("app")) for app in apps if isinstance(app, dict) and app.get("status") == "ok"),
    }
    app_statuses = {
        str(app.get("app")): str(app.get("status"))
        for app in apps
        if isinstance(app, dict) and app.get("app") is not None
    }
    totals = live.get("totals") if isinstance(live.get("totals"), dict) else {}

    add_check("live_status", live.get("status"), expected.get("live_status"))
    add_check("app_count", totals.get("app_count"), expected.get("app_count"))
    add_check("drift_count", totals.get("drift"), expected.get("drift_count"))
    add_check("blocked_apps", by_status["blocked_apps"], sorted(expected["blocked_apps"]) if isinstance(expected.get("blocked_apps"), list) else None)
    add_check("warning_apps", by_status["warning_apps"], sorted(expected["warning_apps"]) if isinstance(expected.get("warning_apps"), list) else None)
    add_check("ok_apps", by_status["ok_apps"], sorted(expected["ok_apps"]) if isinstance(expected.get("ok_apps"), list) else None)
    expected_app_statuses = expected.get("app_statuses") if isinstance(expected.get("app_statuses"), dict) else None
    add_check("app_statuses", app_statuses, expected_app_statuses)
    if hardening is not None:
        add_check("hardening_status", hardening.get("status"), expected.get("hardening_status"))
        add_check("go_no_go", hardening.get("go_no_go"), expected.get("go_no_go"))
    return checks, blockers


def _result_status(
    blockers: List[Dict[str, str]],
    warnings: List[Dict[str, str]],
    child_statuses: Dict[str, Any],
    *,
    has_expectations: bool,
) -> str:
    if blockers:
        return "blocked"
    if warnings:
        return "warning"
    if not has_expectations:
        if any(status == "blocked" for status in child_statuses.values()):
            return "blocked"
        if any(status == "warning" for status in child_statuses.values()):
            return "warning"
    if any(status == "blocked" for status in child_statuses.values()):
        return "ok"
    if any(status == "warning" for status in child_statuses.values()):
        return "ok"
    return "ok"


def _compact_live(report: Dict[str, Any]) -> Dict[str, Any]:
    apps = report.get("apps") if isinstance(report.get("apps"), list) else []
    return {
        "kind": report.get("kind"),
        "status": report.get("status"),
        "summary": report.get("summary"),
        "totals": report.get("totals") if isinstance(report.get("totals"), dict) else {},
        "apps": [
            {
                "app": app.get("app"),
                "environment": app.get("environment"),
                "status": app.get("status"),
                "drift_detected": bool(app.get("drift_detected")),
                "blocker_count": len(app.get("blockers", [])) if isinstance(app.get("blockers"), list) else 0,
                "warning_count": len(app.get("warnings", [])) if isinstance(app.get("warnings"), list) else 0,
            }
            for app in apps
            if isinstance(app, dict)
        ],
    }


def _compact_hardening(report: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if report is None:
        return None
    return {
        "kind": report.get("kind"),
        "status": report.get("status"),
        "go_no_go": report.get("go_no_go"),
        "summary": report.get("summary"),
        "blocker_count": len(report.get("blockers", [])) if isinstance(report.get("blockers"), list) else 0,
        "warning_count": len(report.get("warnings", [])) if isinstance(report.get("warnings"), list) else 0,
    }


def _redact(payload: Dict[str, Any]) -> Dict[str, Any]:
    return deep_redact(
        payload,
        safe_keys={"check_docker", "confirmation_required", "dry_run", "mutates_state", "ok", "probe_http", "read_only"},
        propagate=True,
    )
