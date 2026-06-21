from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .operation_schema import artifact, issue, plan_envelope, receipt_envelope, token
from .redaction import deep_redact


BACKUP_RELATIVE_PATHS = [
    Path("compose.yml"),
    Path("env"),
    Path("env.example"),
    Path("manifest.lock.json"),
    Path("release.json"),
    Path("active_release.json"),
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

    coverage = {
        "app_env": any(item["path"] == "env" for item in files),
        "rendered_config": any(item["path"] in {"compose.yml", "caddy", "manifest.lock.json"} for item in files),
        "release_metadata": any(item["path"] in {"release.json", "active_release.json", "releases", "release-bundles"} for item in files),
        "postgres_metadata": bool(addons.get("postgres")),
        "static_assets": static_root.exists(),
    }
    database = {
        "postgres": bool(addons.get("postgres")),
        "mode": "metadata-only-unless-enabled",
    }
    summary = f"Backup {app} with {len(files)} runtime item(s) and static assets {'present' if static_root.exists() else 'absent'}."
    blocker_issues = [issue("backup_plan_blocked", message) for message in blockers]
    warning_issues = [issue("backup_plan_warning", message) for message in warnings]
    plan = plan_envelope(
        "backup.plan",
        app,
        None,
        summary,
        blockers=blocker_issues,
        warnings=warning_issues,
        checks=[
            {"name": "runtime_path", "ok": app_root.exists(), "message": str(app_root)},
            {"name": "backup_files", "ok": bool(files), "message": f"{len(files)} item(s)"},
            {"name": "secrets_redacted", "ok": True, "message": "Env values are not included in the plan."},
        ],
        artifacts=[artifact(str(app_root / item["path"]), str(item["kind"]), present=True) for item in files],
        confirmation_required=True,
        confirmation_token=None,
        exact_apply_input={"command": f"ship backup create {app} --runtime-root {runtime_root} --confirm CONFIRMATION_TOKEN"},
        risk="high",
        action="backup.create",
        runtime_path=str(app_root),
        backup_root=str(runtime_root / "backups" / "apps" / app),
        files=files,
        coverage=coverage,
        static_assets={
            "path": str(static_root),
            "present": static_root.exists(),
        },
        database=database,
        release_metadata={
            "current": (app_root / "release.json").exists(),
            "active": (app_root / "active_release.json").exists(),
            "history": (app_root / "releases").exists(),
        },
        can_apply=not blockers,
        operator_confirmation_required=True,
        restore_preview_supported=True,
        destructive_restore_supported=False,
        secrets_redacted=True,
    )
    plan["confirmation_token"] = backup_token(plan)
    plan["exact_apply_input"] = {
        "command": f"ship backup create {app} --runtime-root {runtime_root} --confirm {plan['confirmation_token']}"
    }
    return deep_redact(
        plan,
        safe_keys={"confirmation_token", "secrets_redacted", "secret_values_redacted"},
        propagate=True,
    )


def create_backup(runtime_root: Path, app: str, confirm: str) -> Dict[str, object]:
    started_at = _utc_now()
    plan = backup_plan(runtime_root, app)
    blockers = list(plan.get("blockers", [])) if isinstance(plan.get("blockers"), list) else []
    if confirm != plan["confirmation_token"]:
        blockers.append(issue("confirmation_token_mismatch", "Backup confirmation token did not match the current plan."))
    if blockers:
        return receipt_envelope(
            "backup.create",
            app,
            None,
            "blocked",
            started_at,
            _utc_now(),
            artifacts=list(plan.get("artifacts", [])) if isinstance(plan.get("artifacts"), list) else [],
            checks=list(plan.get("checks", [])) if isinstance(plan.get("checks"), list) else [],
            rollback={"available": False, "note": "Backup create was blocked before writing artifacts."},
            plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
            blockers=blockers,
            warnings=list(plan.get("warnings", [])) if isinstance(plan.get("warnings"), list) else [],
            copied_items=[],
            secrets_redacted=True,
            inputs_redacted=True,
        )

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

    manifest = deep_redact({
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
    }, safe_keys={"secrets_redacted_in_report", "secret_values_redacted"}, propagate=True)
    receipt_details = {
        key: value
        for key, value in manifest.items()
        if key not in {"app", "environment", "operation", "operation_id", "schema_version", "kind", "status"}
    }
    backup_root.mkdir(parents=True, exist_ok=True)
    (backup_root / "backup-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    receipt = receipt_envelope(
        "backup.create",
        app,
        None,
        "succeeded",
        started_at,
        _utc_now(),
        artifacts=[
            artifact(str(backup_root), "backup", f"Backup {backup_id}", present=True),
            artifact(str(backup_root / "backup-manifest.json"), "backup-manifest", present=True),
        ],
        checks=[
            {"name": "confirmation_token", "ok": True, "message": "Matched current backup plan."},
            {"name": "runtime_files", "ok": True, "message": f"{len(copied)} item(s) copied."},
            {"name": "secrets_redacted", "ok": True, "message": "Report payloads are redacted; backup artifacts may contain env values."},
        ],
        rollback={"available": True, "note": f"Delete backup directory `{backup_root}` to remove this backup artifact."},
        plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
        **receipt_details,
        backup_path=str(backup_root),
        summary=f"Created backup {backup_id} for {app}.",
        secrets_redacted=True,
        inputs_redacted=True,
    )
    return deep_redact(
        receipt,
        safe_keys={"secrets_redacted", "inputs_redacted", "secrets_redacted_in_report", "secret_values_redacted"},
        propagate=True,
    )


def restore_plan(runtime_root: Path, app: str, backup_id: str) -> Dict[str, object]:
    backup_root = runtime_root / "backups" / "apps" / app / backup_id
    manifest = _load_json(backup_root / "backup-manifest.json")
    blockers = []
    warnings = [
        issue(
            "restore_preview_only",
            "Restore apply creates a preview and report only; it does not overwrite active env, volumes, or runtime files.",
        ),
    ]
    if not manifest:
        blockers.append(issue("backup_manifest_missing", f"Backup manifest not found: {backup_root / 'backup-manifest.json'}"))

    runtime_backup = backup_root / "runtime"
    files = [
        str(path.relative_to(runtime_backup))
        for path in sorted(runtime_backup.rglob("*"))
        if path.is_file()
    ] if runtime_backup.exists() else []
    summary = f"Restore preview for {app} from {backup_id} with {len(files)} file(s)."
    plan = plan_envelope(
        "restore.plan",
        app,
        None,
        summary,
        blockers=blockers,
        warnings=warnings,
        checks=[
            {"name": "backup_manifest", "ok": bool(manifest), "message": str(backup_root / "backup-manifest.json")},
            {"name": "restore_preview_only", "ok": True, "message": "Active runtime will not be modified."},
        ],
        artifacts=[artifact(str(backup_root), "backup", present=backup_root.exists())],
        confirmation_required=True,
        confirmation_token=None,
        exact_apply_input={"command": f"ship restore apply {app} {backup_id} --runtime-root {runtime_root} --confirm CONFIRMATION_TOKEN"},
        risk="high",
        action="restore.apply",
        backup_id=backup_id,
        backup_path=str(backup_root),
        preview_path=str(runtime_root / "apps" / app / "restore-previews" / backup_id),
        files=files,
        file_count=len(files),
        active_runtime_modified_on_apply=False,
        operator_confirmation_required=True,
        destructive_restore_supported=False,
        can_apply=not blockers,
        secrets_redacted=True,
    )
    plan["confirmation_token"] = restore_token(plan)
    plan["exact_apply_input"] = {
        "command": f"ship restore apply {app} {backup_id} --runtime-root {runtime_root} --confirm {plan['confirmation_token']}"
    }
    return deep_redact(
        plan,
        safe_keys={"confirmation_token", "secrets_redacted", "secret_values_redacted"},
        propagate=True,
    )


def apply_restore(runtime_root: Path, app: str, backup_id: str, confirm: str) -> Dict[str, object]:
    started_at = _utc_now()
    plan = restore_plan(runtime_root, app, backup_id)
    blockers = list(plan.get("blockers", [])) if isinstance(plan.get("blockers"), list) else []
    if confirm != plan["confirmation_token"]:
        blockers.append(issue("confirmation_token_mismatch", "Restore confirmation token did not match the current plan."))
    if blockers:
        return receipt_envelope(
            "restore.apply",
            app,
            None,
            "blocked",
            started_at,
            _utc_now(),
            artifacts=list(plan.get("artifacts", [])) if isinstance(plan.get("artifacts"), list) else [],
            checks=list(plan.get("checks", [])) if isinstance(plan.get("checks"), list) else [],
            rollback={"available": False, "note": "Restore preview was blocked before writing artifacts."},
            plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
            blockers=blockers,
            warnings=list(plan.get("warnings", [])) if isinstance(plan.get("warnings"), list) else [],
            active_runtime_modified=False,
            destructive_restore_supported=False,
            secrets_redacted=True,
            inputs_redacted=True,
        )

    backup_root = Path(str(plan["backup_path"]))
    preview_path = Path(str(plan["preview_path"]))
    runtime_backup = backup_root / "runtime"
    if runtime_backup.exists():
        shutil.copytree(runtime_backup, preview_path / "runtime", dirs_exist_ok=True)
    if (backup_root / "static").exists():
        shutil.copytree(backup_root / "static", preview_path / "static", dirs_exist_ok=True)

    report = receipt_envelope(
        "restore.apply",
        app,
        None,
        "succeeded",
        started_at,
        _utc_now(),
        artifacts=[
            artifact(str(preview_path), "restore-preview", present=True),
            artifact(str(preview_path / "restore-report.json"), "restore-report", present=True),
        ],
        checks=[
            {"name": "confirmation_token", "ok": True, "message": "Matched current restore plan."},
            {"name": "active_runtime_unchanged", "ok": True, "message": "Restore wrote only the preview directory."},
        ],
        rollback={"available": True, "note": f"Delete restore preview `{preview_path}` when no longer needed."},
        plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
        backup_id=backup_id,
        preview_path=str(preview_path),
        applied_at=_utc_now(),
        active_runtime_modified=False,
        destructive_restore_supported=False,
        summary=plan["summary"],
        warnings=plan["warnings"],
        secrets_redacted=True,
        inputs_redacted=True,
    )
    preview_path.mkdir(parents=True, exist_ok=True)
    (preview_path / "restore-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return deep_redact(report, safe_keys={"secrets_redacted", "inputs_redacted"}, propagate=True)


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
    return token(action, _token_payload(action, plan))


def _token_payload(action: str, plan: Dict[str, object]) -> Dict[str, object]:
    return {
        "action": action,
        "app": plan.get("app"),
        "backup_id": plan.get("backup_id"),
        "runtime_path": plan.get("runtime_path"),
        "backup_path": plan.get("backup_path") or plan.get("backup_root"),
        "files": plan.get("files"),
    }


def _backup_id(app: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = hashlib.sha256(f"{app}:{stamp}".encode("utf-8")).hexdigest()[:8]
    return f"{stamp}-{suffix}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
