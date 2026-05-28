from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .runtime import current_release_id, load_release


ROLLBACK_FILES = [
    Path("compose.yml"),
    Path("caddy"),
    Path("env.example"),
    Path("manifest.lock.json"),
]


def rollback_plan(runtime_root: Path, app: str, release_id: str) -> Dict[str, object]:
    target = load_release(runtime_root, app, release_id)
    current_id = current_release_id(runtime_root, app)
    app_root = runtime_root / "apps" / app
    bundle_root = _bundle_root(app_root, target)
    changes = _planned_changes(app_root, bundle_root)
    verification_plan = _verification_plan(bundle_root, app, target)
    blockers: List[str] = []
    warnings: List[str] = []

    if current_id == target.get("release_id"):
        warnings.append("Target release is already the current release pointer.")
    if not bundle_root.exists():
        blockers.append(f"Rendered bundle snapshot is missing: {bundle_root}")
    if not changes:
        warnings.append("No generated files are available to restore from the target release.")

    plan = {
        "action": "deploy.rollback.apply",
        "app": app,
        "environment": target.get("environment"),
        "current_release_id": current_id,
        "target_release_id": target.get("release_id", release_id),
        "target_deployed_at": target.get("deployed_at"),
        "bundle_path": str(bundle_root),
        "changes": changes,
        "post_apply_verification": verification_plan,
        "traffic_switching": {
            "managed_by_ophelia": False,
            "mode": "file-level generated config restore",
        },
        "warnings": warnings,
        "blockers": blockers,
        "can_apply": not blockers,
    }
    plan["confirmation_token"] = rollback_token(plan)
    plan["summary"] = (
        f"Rollback {app} from {current_id or 'unknown'} to "
        f"{plan['target_release_id']} with {len(changes)} generated file changes."
    )
    return plan


def rollback_token(plan: Dict[str, object]) -> str:
    payload = {
        "action": "deploy.rollback.apply",
        "app": plan.get("app"),
        "current_release_id": plan.get("current_release_id"),
        "target_release_id": plan.get("target_release_id"),
        "bundle_path": plan.get("bundle_path"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def apply_rollback(runtime_root: Path, app: str, release_id: str, confirm: str) -> Dict[str, object]:
    plan = rollback_plan(runtime_root, app, release_id)
    if not plan["can_apply"]:
        raise RuntimeError("; ".join(str(item) for item in plan["blockers"]))
    if confirm != plan["confirmation_token"]:
        raise RuntimeError("Rollback confirmation token did not match the current plan.")

    app_root = runtime_root / "apps" / app
    bundle_root = Path(str(plan["bundle_path"]))
    restored: List[str] = []
    for change in plan["changes"]:
        relative_path = Path(str(change["path"]))
        source = bundle_root / relative_path
        target = app_root / relative_path
        _copy_path(source, target)
        restored.append(str(relative_path))

    shared_caddy_updates = _restore_shared_caddy(runtime_root, app, bundle_root, restored)

    target_release = load_release(runtime_root, app, release_id)
    current_pointer = dict(target_release)
    current_pointer["rollback_applied_at"] = _utc_now()
    current_pointer["rollback_from_release_id"] = plan.get("current_release_id")
    current_pointer["source"] = "rollback"
    current_pointer["applied"] = True
    current_pointer["verified"] = None
    current_pointer["apply"] = {
        "status": "applied",
        "ok": True,
        "applied": True,
        "phase": "rollback_restore",
        "restored_files": restored,
    }
    current_pointer["verification"] = {
        "status": "not_run",
        "ok": None,
        "verified": None,
        "results": [],
        "recommended_command": plan["post_apply_verification"]["recommended_command"],
    }
    (app_root / "release.json").write_text(json.dumps(current_pointer, indent=2, sort_keys=True) + "\n")

    report_id = f"{_compact_stamp()}-to-{plan['target_release_id']}"
    report = {
        "report_id": report_id,
        "app": app,
        "environment": plan.get("environment"),
        "current_release_id_before": plan.get("current_release_id"),
        "target_release_id": plan.get("target_release_id"),
        "applied_at": _utc_now(),
        "restored_files": restored,
        "shared_caddy_updated": bool(shared_caddy_updates),
        "shared_caddy_updates": shared_caddy_updates,
        "deleted_files": [],
        "verification": {
            "status": "not_run",
            "checks": plan["post_apply_verification"]["checks"],
            "recommended_command": plan["post_apply_verification"]["recommended_command"],
        },
        "traffic_switching": plan["traffic_switching"],
        "summary": plan["summary"],
    }
    reports_root = app_root / "rollback-reports"
    reports_root.mkdir(parents=True, exist_ok=True)
    (reports_root / f"{report_id}.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def _bundle_root(app_root: Path, release: Dict[str, object]) -> Path:
    bundle_path = release.get("bundle_path")
    if isinstance(bundle_path, str) and bundle_path:
        return Path(bundle_path)
    release_id = str(release.get("release_id", ""))
    return app_root / "release-bundles" / release_id


def _planned_changes(app_root: Path, bundle_root: Path) -> List[Dict[str, object]]:
    changes: List[Dict[str, object]] = []
    for root in ROLLBACK_FILES:
        source = bundle_root / root
        if not source.exists():
            continue
        for source_file in _iter_files(source):
            relative_path = source_file.relative_to(bundle_root)
            target_file = app_root / relative_path
            changes.append(
                {
                    "path": str(relative_path),
                    "exists": target_file.exists(),
                    "changed": _file_hash(source_file) != _file_hash(target_file),
                }
            )
    return sorted(changes, key=lambda item: str(item["path"]))


def _iter_files(path: Path) -> List[Path]:
    if path.is_file():
        return [path]
    return sorted(child for child in path.rglob("*") if child.is_file())


def _file_hash(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_path(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
        return
    shutil.copy2(source, target)


def _restore_shared_caddy(runtime_root: Path, app: str, bundle_root: Path, restored: List[str]) -> List[str]:
    updates: List[str] = []
    site_source = bundle_root / "caddy" / f"{app}.caddy"
    if site_source.exists():
        site_target = runtime_root / "caddy" / "sites.d" / f"{app}.caddy"
        _copy_path(site_source, site_target)
        relative = str(site_target.relative_to(runtime_root))
        restored.append(relative)
        updates.append(relative)

    global_source = bundle_root / "caddy" / "global.d" / f"{app}.caddy"
    if global_source.exists():
        global_target = runtime_root / "caddy" / "global.d" / f"{app}.caddy"
        _copy_path(global_source, global_target)
        relative = str(global_target.relative_to(runtime_root))
        restored.append(relative)
        updates.append(relative)
    return updates


def _verification_plan(bundle_root: Path, app: str, release: Dict[str, object]) -> Dict[str, object]:
    lock_path = bundle_root / "manifest.lock.json"
    lock = _load_json(lock_path)
    checks = []
    raw_checks = lock.get("verify", []) if isinstance(lock, dict) else []
    if isinstance(raw_checks, list):
        for item in raw_checks:
            if not isinstance(item, dict):
                continue
            checks.append(
                {
                    "name": item.get("name") or item.get("url"),
                    "url": item.get("url"),
                    "expect_status": item.get("expect_status", 200),
                    "contains_required": bool(item.get("contains")),
                }
            )
    return {
        "available": bool(checks),
        "checks": checks,
        "source": str(lock_path) if lock_path.exists() else None,
        "recommended_command": f"./cli/ship verify {app}",
        "status_after_apply": "not_run",
        "note": "Rollback apply restores generated files; verification is reported but not executed automatically.",
    }


def _load_json(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _compact_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
