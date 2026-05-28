from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List


BACKUP_RELATIVE_PATHS = [
    Path("compose.yml"),
    Path("env"),
    Path("env.example"),
    Path("manifest.lock.json"),
    Path("release.json"),
    Path("addons.json"),
    Path("caddy"),
    Path("releases"),
    Path("release-bundles"),
]


def backup_plan(runtime_root: Path, app: str) -> Dict[str, object]:
    app_root = runtime_root / "apps" / app
    files = _existing_backup_files(app_root)
    manifest_lock = _load_json(app_root / "manifest.lock.json")
    addons = manifest_lock.get("addons", {}) if isinstance(manifest_lock, dict) else {}
    static_root = runtime_root / "static" / app
    warnings: List[str] = []
    blockers: List[str] = []

    if not app_root.exists():
        blockers.append(f"App runtime path does not exist: {app_root}")
    if not files:
        blockers.append(f"No backup-eligible files found for {app}.")
    if addons.get("postgres"):
        warnings.append("Postgres dump is planned as a database item, but live dump execution is not enabled by default.")

    plan = {
        "action": "backup.create",
        "app": app,
        "runtime_path": str(app_root),
        "backup_root": str(runtime_root / "backups" / "apps" / app),
        "files": files,
        "coverage": {
            "app_env": any(item["path"] == "env" for item in files),
            "rendered_config": any(item["path"] in {"compose.yml", "caddy", "manifest.lock.json"} for item in files),
            "release_metadata": any(item["path"] in {"release.json", "releases", "release-bundles"} for item in files),
            "postgres_metadata": bool(addons.get("postgres")),
            "static_assets": static_root.exists(),
        },
        "static_assets": {
            "path": str(static_root),
            "present": static_root.exists(),
        },
        "database": {
            "postgres": bool(addons.get("postgres")),
            "mode": "metadata-only-unless-enabled",
        },
        "release_metadata": {
            "current": (app_root / "release.json").exists(),
            "history": (app_root / "releases").exists(),
        },
        "warnings": warnings,
        "blockers": blockers,
        "can_apply": not blockers,
        "operator_confirmation_required": True,
        "restore_preview_supported": True,
        "destructive_restore_supported": False,
    }
    plan["confirmation_token"] = backup_token(plan)
    plan["summary"] = f"Backup {app} with {len(files)} runtime item(s) and static assets {'present' if static_root.exists() else 'absent'}."
    return plan


def create_backup(runtime_root: Path, app: str, confirm: str) -> Dict[str, object]:
    plan = backup_plan(runtime_root, app)
    if not plan["can_apply"]:
        raise RuntimeError("; ".join(str(item) for item in plan["blockers"]))
    if confirm != plan["confirmation_token"]:
        raise RuntimeError("Backup confirmation token did not match the current plan.")

    backup_id = _backup_id(app)
    backup_root = runtime_root / "backups" / "apps" / app / backup_id
    app_root = runtime_root / "apps" / app
    copied = []

    for item in plan["files"]:
        relative_path = Path(str(item["path"]))
        source = app_root / relative_path
        target = backup_root / "runtime" / relative_path
        _copy_path(source, target)
        copied.append(str(relative_path))

    static = plan["static_assets"]
    if static["present"]:
        _copy_path(Path(str(static["path"])), backup_root / "static")
        copied.append("static")

    manifest = {
        "backup_id": backup_id,
        "app": app,
        "created_at": _utc_now(),
        "runtime_root": str(runtime_root),
        "copied_items": copied,
        "database": plan["database"],
        "coverage": plan["coverage"],
        "warnings": plan["warnings"],
        "secrets_redacted_in_report": True,
        "restore_preview_supported": True,
        "destructive_restore_supported": False,
    }
    backup_root.mkdir(parents=True, exist_ok=True)
    (backup_root / "backup-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return {"summary": f"Created backup {backup_id} for {app}.", **manifest, "backup_path": str(backup_root)}


def restore_plan(runtime_root: Path, app: str, backup_id: str) -> Dict[str, object]:
    backup_root = runtime_root / "backups" / "apps" / app / backup_id
    manifest = _load_json(backup_root / "backup-manifest.json")
    blockers = []
    warnings = [
        "Restore apply creates a preview and report only; it does not overwrite active env, volumes, or runtime files.",
    ]
    if not manifest:
        blockers.append(f"Backup manifest not found: {backup_root / 'backup-manifest.json'}")

    runtime_backup = backup_root / "runtime"
    files = [
        str(path.relative_to(runtime_backup))
        for path in sorted(runtime_backup.rglob("*"))
        if path.is_file()
    ] if runtime_backup.exists() else []
    plan = {
        "action": "restore.apply",
        "app": app,
        "backup_id": backup_id,
        "backup_path": str(backup_root),
        "preview_path": str(runtime_root / "apps" / app / "restore-previews" / backup_id),
        "files": files,
        "file_count": len(files),
        "active_runtime_modified_on_apply": False,
        "operator_confirmation_required": True,
        "destructive_restore_supported": False,
        "warnings": warnings,
        "blockers": blockers,
        "can_apply": not blockers,
    }
    plan["confirmation_token"] = restore_token(plan)
    plan["summary"] = f"Restore preview for {app} from {backup_id} with {len(files)} file(s)."
    return plan


def apply_restore(runtime_root: Path, app: str, backup_id: str, confirm: str) -> Dict[str, object]:
    plan = restore_plan(runtime_root, app, backup_id)
    if not plan["can_apply"]:
        raise RuntimeError("; ".join(str(item) for item in plan["blockers"]))
    if confirm != plan["confirmation_token"]:
        raise RuntimeError("Restore confirmation token did not match the current plan.")

    backup_root = Path(str(plan["backup_path"]))
    preview_path = Path(str(plan["preview_path"]))
    runtime_backup = backup_root / "runtime"
    if runtime_backup.exists():
        shutil.copytree(runtime_backup, preview_path / "runtime", dirs_exist_ok=True)
    if (backup_root / "static").exists():
        shutil.copytree(backup_root / "static", preview_path / "static", dirs_exist_ok=True)

    report = {
        "app": app,
        "backup_id": backup_id,
        "preview_path": str(preview_path),
        "applied_at": _utc_now(),
        "active_runtime_modified": False,
        "destructive_restore_supported": False,
        "summary": plan["summary"],
        "warnings": plan["warnings"],
    }
    preview_path.mkdir(parents=True, exist_ok=True)
    (preview_path / "restore-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def backup_token(plan: Dict[str, object]) -> str:
    return _token("backup.create", plan)


def restore_token(plan: Dict[str, object]) -> str:
    return _token("restore.apply", plan)


def _existing_backup_files(app_root: Path) -> List[Dict[str, object]]:
    files = []
    for relative_path in BACKUP_RELATIVE_PATHS:
        path = app_root / relative_path
        if not path.exists():
            continue
        files.append(
            {
                "path": str(relative_path),
                "kind": "directory" if path.is_dir() else "file",
                "secret_values_redacted": relative_path == Path("env"),
            }
        )
    return files


def _copy_path(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        shutil.copy2(source, target)


def _load_json(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _token(action: str, plan: Dict[str, object]) -> str:
    payload = {
        "action": action,
        "app": plan.get("app"),
        "backup_id": plan.get("backup_id"),
        "runtime_path": plan.get("runtime_path"),
        "backup_path": plan.get("backup_path") or plan.get("backup_root"),
        "files": plan.get("files"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def _backup_id(app: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = hashlib.sha256(f"{app}:{stamp}".encode("utf-8")).hexdigest()[:8]
    return f"{stamp}-{suffix}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
