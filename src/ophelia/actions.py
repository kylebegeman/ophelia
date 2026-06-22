from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib import request
from urllib.error import URLError
from urllib.parse import urlparse

from .backup import backup_plan, create_backup, restore_plan, apply_restore
from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .conflicts import scan_conflicts
from .drift import manifest_drift
from .explain import explain_manifest
from .inspection import doctor_report, status_report
from .manifest import ManifestError, load_manifest
from .planning import deploy_confirmation_token, deploy_plan, bundle_diff
from .portability import (
    app_readiness_report,
    app_runbook_report,
    backup_status_report,
    cutover_apply,
    cutover_plan,
    env_shape_diff_report,
    export_create,
    export_plan,
    import_apply,
    import_plan,
    isolation_plan,
    pack_explain_report,
    pack_init_report,
    pack_validation_report,
    receipt_list_report,
    receipt_show_report,
    restore_drill_apply,
    restore_drill_plan,
    traffic_apply,
    traffic_plan,
    traffic_rollback_apply,
    traffic_rollback_plan,
)
from .rollback import apply_rollback, rollback_plan
from .redaction import redact_url
from .runtime import apply_local_bundle, list_releases, load_release


MUTATING_ACTIONS = {
    "deploy.apply",
    "deploy.rollback.apply",
    "backup.create",
    "restore.apply",
    "app.export.create",
    "app.import.apply",
    "app.restore-drill.apply",
    "app.cutover.apply",
    "app.traffic.apply",
    "app.traffic.rollback.apply",
}


ACTION_DEFINITIONS: List[Dict[str, Any]] = []


def action_catalog() -> List[Dict[str, Any]]:
    config = _load_action_config()
    ttl = int(config.get("confirmation_token_ttl_seconds", 900))
    callbacks_enabled = bool(config.get("callbacks_enabled", False))
    return [
        {
            **dict(item),
            "confirmation_token_ttl_seconds": ttl if item["mutation_level"] == "mutating" else None,
            "callbacks_enabled": callbacks_enabled,
        }
        for item in ACTION_DEFINITIONS
        if _action_enabled(item["id"], config)
    ]


def action_ids() -> set[str]:
    return {item["id"] for item in ACTION_DEFINITIONS}


def validate_action_inputs(action_id: str, inputs: Dict[str, Any]) -> None:
    definition = _definition(action_id)
    schema = definition["input_schema"]
    allowed = set(schema["properties"])
    required = set(schema.get("required", []))
    unknown = sorted(set(inputs) - allowed)
    missing = sorted(key for key in required if key not in inputs)
    if unknown:
        raise ActionError(f"Unknown input field(s) for {action_id}: {', '.join(unknown)}")
    if missing:
        raise ActionError(f"Missing required input field(s) for {action_id}: {', '.join(missing)}")

    for key, value in inputs.items():
        expected = schema["properties"][key]["type"]
        if expected == "string" and not isinstance(value, str):
            raise ActionError(f"`{key}` must be a string.")
        if expected == "boolean" and not isinstance(value, bool):
            raise ActionError(f"`{key}` must be a boolean.")
        if key == "environment" and value not in {"dev", "staging", "production"}:
            raise ActionError("`environment` must be one of dev, staging, or production.")
        if key == "mode" and value not in {"rehearsal", "cutover"}:
            raise ActionError("`mode` must be one of rehearsal or cutover.")
        if key == "ttl" and (not str(value).isdigit() or int(str(value)) <= 0):
            raise ActionError("`ttl` must be a positive integer string.")
        if key == "dns_provider" and value not in {"manual", "file", "cloudflare"}:
            raise ActionError("`dns_provider` must be one of manual, file, or cloudflare.")
        if key == "caddy_provider" and value not in {"manual", "file"}:
            raise ActionError("`caddy_provider` must be one of manual or file.")
        if key == "target_health_timeout":
            try:
                timeout = float(str(value))
            except ValueError as exc:
                raise ActionError("`target_health_timeout` must be a positive number string.") from exc
            if timeout <= 0:
                raise ActionError("`target_health_timeout` must be greater than zero.")
        if key == "target_health_expect_status" and (not str(value).isdigit() or not 100 <= int(str(value)) <= 599):
            raise ActionError("`target_health_expect_status` must be an HTTP status code string.")
        if key == "target_health_url":
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ActionError("`target_health_url` must be an http(s) URL without credentials, query strings, or fragments.")
        if key.endswith("_path") or key.endswith("_root") or key == "provider_config":
            _validate_path_value(key, value)
        if key == "completion_callback_url":
            _validate_callback_url(value)


def run_action(action_id: str, inputs: Dict[str, Any], runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> Dict[str, Any]:
    validate_action_inputs(action_id, inputs)
    runtime_root = Path(inputs.get("runtime_root") or runtime_root).expanduser()
    manifest_dir = Path(inputs.get("manifest_dir") or REPO_ROOT / "manifests")

    if action_id == "manifest.validate":
        manifest = _load_manifest_input(inputs)
        return _artifact("Manifest valid.", {"ok": True, "app": manifest.app, "kind": manifest.kind})
    if action_id == "manifest.explain":
        manifest_path = Path(inputs["manifest_path"])
        return _artifact("Manifest explained.", explain_manifest(load_manifest(manifest_path), manifest_path))
    if action_id == "manifest.diff":
        manifest_path = Path(inputs["manifest_path"])
        return _artifact("Manifest diff complete.", bundle_diff(load_manifest(manifest_path), runtime_root))
    if action_id == "manifest.conflicts":
        return _artifact("Conflict scan complete.", scan_conflicts(manifest_dir))
    if action_id == "pack.validate":
        manifest_path = Path(inputs["manifest_path"])
        return _artifact("Pack validation complete.", pack_validation_report(load_manifest(manifest_path), manifest_path))
    if action_id == "pack.explain":
        manifest_path = Path(inputs["manifest_path"])
        return _artifact("Pack explain complete.", pack_explain_report(load_manifest(manifest_path), manifest_path))
    if action_id == "pack.init.preview":
        return _artifact(
            "Pack init preview complete.",
            pack_init_report(
                app=inputs["app"],
                environment=inputs.get("environment"),
                critical=bool(inputs.get("critical", False)),
                postgres=bool(inputs.get("postgres", False)),
                redis=bool(inputs.get("redis", False)),
                uploads=bool(inputs.get("uploads", False)),
                root=Path(inputs.get("directory") or "."),
                write=False,
                force=False,
                include_manifest=bool(inputs.get("include_manifest", False)),
                manifest_kind=str(inputs.get("manifest_kind") or "service"),
                domain=inputs.get("domain") if isinstance(inputs.get("domain"), str) else None,
                image=inputs.get("image") if isinstance(inputs.get("image"), str) else None,
                port=int(inputs.get("port") or 8080),
                static_root=str(inputs.get("static_root") or "public"),
            ),
        )
    if action_id == "env.diff":
        return _artifact(
            "Env diff complete.",
            env_shape_diff_report(
                inputs["app"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                manifest_path=Path(inputs["manifest_path"]) if inputs.get("manifest_path") else None,
            ),
        )
    if action_id == "deploy.plan":
        manifest_path = Path(inputs["manifest_path"])
        return _artifact("Deploy plan complete.", deploy_plan(load_manifest(manifest_path), manifest_path, runtime_root))
    if action_id == "deploy.apply":
        return _run_deploy_apply(inputs, runtime_root)
    if action_id == "deploy.rollback.plan":
        return _artifact("Rollback plan complete.", rollback_plan(runtime_root, inputs["app"], inputs["release_id"]))
    if action_id == "deploy.rollback.apply":
        return _run_confirmed_plan(
            inputs,
            lambda: rollback_plan(runtime_root, inputs["app"], inputs["release_id"]),
            lambda token: apply_rollback(runtime_root, inputs["app"], inputs["release_id"], token),
            "Rollback dry-run complete.",
            "Rollback applied.",
        )
    if action_id == "runtime.status":
        return _artifact("Runtime status complete.", status_report(runtime_root, REPO_ROOT, REPO_ROOT / "manifests"))
    if action_id == "runtime.doctor":
        return _artifact("Runtime doctor complete.", doctor_report(runtime_root, REPO_ROOT, REPO_ROOT / "manifests"))
    if action_id == "runtime.drift":
        manifest_path = Path(inputs["manifest_path"])
        manifest = load_manifest(manifest_path)
        return _artifact("Runtime drift complete.", manifest_drift(manifest, manifest_path, runtime_root))
    if action_id == "backup.plan":
        return _artifact("Backup plan complete.", backup_plan(runtime_root, inputs["app"]))
    if action_id == "backup.status":
        return _artifact(
            "Backup status complete.",
            backup_status_report(
                inputs["app"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                manifest_path=Path(inputs["manifest_path"]) if inputs.get("manifest_path") else None,
            ),
        )
    if action_id == "backup.create":
        return _run_confirmed_plan(
            inputs,
            lambda: backup_plan(runtime_root, inputs["app"]),
            lambda token: create_backup(runtime_root, inputs["app"], token),
            "Backup dry-run complete.",
            "Backup created.",
        )
    if action_id == "restore.plan":
        return _artifact("Restore plan complete.", restore_plan(runtime_root, inputs["app"], inputs["backup_id"]))
    if action_id == "restore.apply":
        return _run_confirmed_plan(
            inputs,
            lambda: restore_plan(runtime_root, inputs["app"], inputs["backup_id"]),
            lambda token: apply_restore(runtime_root, inputs["app"], inputs["backup_id"], token),
            "Restore dry-run complete.",
            "Restore preview created.",
        )
    if action_id == "release.list":
        return _artifact("Release list complete.", {"app": inputs["app"], "releases": list_releases(runtime_root, inputs["app"])})
    if action_id == "release.show":
        return _artifact("Release show complete.", load_release(runtime_root, inputs["app"], inputs["release_id"]))
    if action_id == "app.readiness":
        return _artifact(
            "App readiness complete.",
            app_readiness_report(
                inputs["app"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                manifest_path=Path(inputs["manifest_path"]) if inputs.get("manifest_path") else None,
            ),
        )
    if action_id == "app.runbook":
        return _artifact(
            "App runbook complete.",
            app_runbook_report(
                inputs["app"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                manifest_path=Path(inputs["manifest_path"]) if inputs.get("manifest_path") else None,
            ),
        )
    if action_id == "app.export.plan":
        return _artifact(
            "App export plan complete.",
            export_plan(
                inputs["app"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                manifest_path=Path(inputs["manifest_path"]) if inputs.get("manifest_path") else None,
                ophelia_root=REPO_ROOT,
                include_postgres=bool(inputs.get("include_postgres", False)),
            ),
        )
    if action_id == "app.export.create":
        return _run_confirmed_plan(
            inputs,
            lambda: export_plan(
                inputs["app"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                manifest_path=Path(inputs["manifest_path"]) if inputs.get("manifest_path") else None,
                ophelia_root=REPO_ROOT,
                include_postgres=bool(inputs.get("include_postgres", False)),
            ),
            lambda token: export_create(
                inputs["app"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                manifest_path=Path(inputs["manifest_path"]) if inputs.get("manifest_path") else None,
                confirm=token,
                ophelia_root=REPO_ROOT,
                include_postgres=bool(inputs.get("include_postgres", False)),
            ),
            "App export create dry-run complete.",
            "App export create complete.",
        )
    if action_id == "app.import.plan":
        return _artifact("App import plan complete.", import_plan(Path(inputs["source_path"]), runtime_root=runtime_root, ophelia_root=REPO_ROOT))
    if action_id == "app.import.apply":
        return _run_confirmed_plan(
            inputs,
            lambda: import_plan(
                Path(inputs["source_path"]),
                runtime_root=runtime_root,
                mode=inputs.get("mode") or "rehearsal",
                ophelia_root=REPO_ROOT,
            ),
            lambda token: import_apply(
                Path(inputs["source_path"]),
                runtime_root=runtime_root,
                mode=inputs.get("mode") or "rehearsal",
                confirm=token,
                ophelia_root=REPO_ROOT,
            ),
            "App import apply dry-run complete.",
            "App import apply complete.",
        )
    if action_id == "app.restore-drill.plan":
        return _artifact(
            "Restore drill plan complete.",
            restore_drill_plan(
                inputs["app"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                manifest_path=Path(inputs["manifest_path"]) if inputs.get("manifest_path") else None,
                source=Path(inputs["source_path"]) if inputs.get("source_path") else None,
            ),
        )
    if action_id == "app.restore-drill.apply":
        return _run_confirmed_plan(
            inputs,
            lambda: restore_drill_plan(
                inputs["app"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                manifest_path=Path(inputs["manifest_path"]) if inputs.get("manifest_path") else None,
                source=Path(inputs["source_path"]) if inputs.get("source_path") else None,
            ),
            lambda token: restore_drill_apply(
                inputs["app"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                manifest_path=Path(inputs["manifest_path"]) if inputs.get("manifest_path") else None,
                source=Path(inputs["source_path"]) if inputs.get("source_path") else None,
                confirm=token,
            ),
            "Restore drill apply dry-run complete.",
            "Restore drill apply complete.",
        )
    if action_id == "app.cutover.plan":
        return _artifact(
            "Cutover plan complete.",
            cutover_plan(
                inputs["app"],
                source_host=inputs["source_host"],
                target_host=inputs["target_host"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                manifest_path=Path(inputs["manifest_path"]) if inputs.get("manifest_path") else None,
            ),
        )
    if action_id == "app.cutover.apply":
        return _run_confirmed_plan(
            inputs,
            lambda: cutover_plan(
                inputs["app"],
                source_host=inputs["source_host"],
                target_host=inputs["target_host"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                manifest_path=Path(inputs["manifest_path"]) if inputs.get("manifest_path") else None,
            ),
            lambda token: cutover_apply(
                inputs["app"],
                source_host=inputs["source_host"],
                target_host=inputs["target_host"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                manifest_path=Path(inputs["manifest_path"]) if inputs.get("manifest_path") else None,
                confirm=token,
            ),
            "Cutover apply dry-run complete.",
            "Cutover apply complete.",
        )
    if action_id == "app.traffic.plan":
        return _artifact(
            "Traffic plan complete.",
            traffic_plan(
                inputs["app"],
                source_host=inputs["source_host"],
                target_host=inputs["target_host"],
                target_origin=inputs["target_origin"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                manifest_path=Path(inputs["manifest_path"]) if inputs.get("manifest_path") else None,
                dns_provider=inputs.get("dns_provider") or "manual",
                caddy_provider=inputs.get("caddy_provider") or "manual",
                ttl=int(inputs.get("ttl") or "300"),
                provider_config=Path(inputs["provider_config"]) if inputs.get("provider_config") else None,
                execute_provider_mutation=bool(inputs.get("execute_provider_mutation", False)),
                target_health_url=inputs.get("target_health_url"),
                run_target_health=bool(inputs.get("run_target_health", False)),
                target_health_timeout=float(inputs.get("target_health_timeout") or "10"),
                target_health_expect_status=int(inputs.get("target_health_expect_status") or "200"),
            ),
        )
    if action_id == "app.traffic.apply":
        return _run_confirmed_plan(
            inputs,
            lambda: traffic_plan(
                inputs["app"],
                source_host=inputs["source_host"],
                target_host=inputs["target_host"],
                target_origin=inputs["target_origin"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                manifest_path=Path(inputs["manifest_path"]) if inputs.get("manifest_path") else None,
                dns_provider=inputs.get("dns_provider") or "manual",
                caddy_provider=inputs.get("caddy_provider") or "manual",
                ttl=int(inputs.get("ttl") or "300"),
                provider_config=Path(inputs["provider_config"]) if inputs.get("provider_config") else None,
                execute_provider_mutation=bool(inputs.get("execute_provider_mutation", False)),
                target_health_url=inputs.get("target_health_url"),
                run_target_health=bool(inputs.get("run_target_health", False)),
                target_health_timeout=float(inputs.get("target_health_timeout") or "10"),
                target_health_expect_status=int(inputs.get("target_health_expect_status") or "200"),
            ),
            lambda token: traffic_apply(
                inputs["app"],
                source_host=inputs["source_host"],
                target_host=inputs["target_host"],
                target_origin=inputs["target_origin"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                manifest_path=Path(inputs["manifest_path"]) if inputs.get("manifest_path") else None,
                dns_provider=inputs.get("dns_provider") or "manual",
                caddy_provider=inputs.get("caddy_provider") or "manual",
                ttl=int(inputs.get("ttl") or "300"),
                provider_config=Path(inputs["provider_config"]) if inputs.get("provider_config") else None,
                execute_provider_mutation=bool(inputs.get("execute_provider_mutation", False)),
                target_health_url=inputs.get("target_health_url"),
                run_target_health=bool(inputs.get("run_target_health", False)),
                target_health_timeout=float(inputs.get("target_health_timeout") or "10"),
                target_health_expect_status=int(inputs.get("target_health_expect_status") or "200"),
                confirm=token,
            ),
            "Traffic apply dry-run complete.",
            "Traffic apply complete.",
        )
    if action_id == "app.traffic.rollback.plan":
        return _artifact(
            "Traffic rollback plan complete.",
            traffic_rollback_plan(
                inputs["app"],
                receipt_id=inputs["receipt_id"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
            ),
        )
    if action_id == "app.traffic.rollback.apply":
        return _run_confirmed_plan(
            inputs,
            lambda: traffic_rollback_plan(
                inputs["app"],
                receipt_id=inputs["receipt_id"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
            ),
            lambda token: traffic_rollback_apply(
                inputs["app"],
                receipt_id=inputs["receipt_id"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                confirm=token,
            ),
            "Traffic rollback apply dry-run complete.",
            "Traffic rollback apply complete.",
        )
    if action_id == "app.isolation.plan":
        return _artifact(
            "Isolation plan complete.",
            isolation_plan(
                inputs["app"],
                environment=inputs.get("environment"),
                runtime_root=runtime_root,
                manifest_path=Path(inputs["manifest_path"]) if inputs.get("manifest_path") else None,
            ),
        )
    if action_id == "receipts.list":
        return _artifact("Receipt list complete.", receipt_list_report(runtime_root, app=inputs.get("app"), environment=inputs.get("environment")))
    if action_id == "receipts.show":
        return _artifact("Receipt show complete.", receipt_show_report(inputs["receipt_id"], runtime_root=runtime_root))
    raise ActionError(f"Unknown action id: {action_id}")


def _run_deploy_apply(inputs: Dict[str, Any], runtime_root: Path) -> Dict[str, Any]:
    manifest_path = Path(inputs["manifest_path"])
    manifest = load_manifest(manifest_path)
    plan = deploy_plan(manifest, manifest_path, runtime_root)
    if inputs.get("dry_run", True):
        return _artifact(
            "Deploy apply dry-run complete.",
            _confirmation_payload(inputs, plan, deploy_confirmation_token(plan)),
        )
    token = inputs.get("confirm_token")
    if token != deploy_confirmation_token(plan):
        raise ActionError("Deploy apply confirmation token is missing or incorrect.")
    if plan["environment"] == "production" and not _load_action_config().get("production_apply_enabled", False):
        raise ActionError("Production deploy apply is disabled by action policy.")
    app_root = apply_local_bundle(manifest, manifest_path, runtime_root, REPO_ROOT)
    return _artifact("Deploy applied.", {"app": manifest.app, "runtime_path": str(app_root), "plan": plan})


def _run_confirmed_plan(
    inputs: Dict[str, Any],
    plan_factory,
    apply_factory,
    dry_summary: str,
    apply_summary: str,
) -> Dict[str, Any]:
    plan = plan_factory()
    if inputs.get("dry_run", True):
        return _artifact(dry_summary, _confirmation_payload(inputs, plan, plan["confirmation_token"]))
    token = str(inputs.get("confirm_token") or "")
    if token != plan["confirmation_token"]:
        raise ActionError("Confirmation token is missing or incorrect.")
    return _artifact(apply_summary, apply_factory(token))


def _artifact(summary: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "summary": summary,
        "payload": payload,
        "report_markdown": f"### {summary}\n\n```json\n{json.dumps(payload, indent=2, sort_keys=True)}\n```",
    }


def _definition(action_id: str) -> Dict[str, Any]:
    config = _load_action_config()
    for item in ACTION_DEFINITIONS:
        if item["id"] == action_id:
            if not _action_enabled(action_id, config):
                raise ActionError(f"Action is disabled by policy: {action_id}")
            return item
    raise ActionError(f"Unknown action id: {action_id}")


def _load_manifest_input(inputs: Dict[str, Any]):
    try:
        return load_manifest(Path(inputs["manifest_path"]))
    except ManifestError as exc:
        raise ActionError(str(exc)) from exc


def _validate_path_value(key: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ActionError(f"`{key}` is a malformed path.")


def _validate_callback_url(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ActionError("`completion_callback_url` must be a URL string.")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ActionError("`completion_callback_url` must be an http(s) URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ActionError("`completion_callback_url` must not contain credentials, query strings, or fragments.")


def _confirmation_payload(inputs: Dict[str, Any], plan: Dict[str, Any], token: str) -> Dict[str, Any]:
    expires_at = _confirmation_expires_at()
    apply_inputs = {key: value for key, value in inputs.items() if key != "confirm_token"}
    apply_inputs["dry_run"] = False
    apply_inputs["confirm_token"] = token
    return {
        "plan": plan,
        "required_confirmation_token": token,
        "confirmation_expires_at": expires_at,
        "exact_apply_input": apply_inputs,
    }


_PROPERTY_SCHEMAS: Dict[str, Dict[str, str]] = {
    "app": {"type": "string"},
    "environment": {"type": "string"},
    "manifest_path": {"type": "string"},
    "manifest_dir": {"type": "string"},
    "runtime_root": {"type": "string"},
    "release_id": {"type": "string"},
    "backup_id": {"type": "string"},
    "receipt_id": {"type": "string"},
    "source_path": {"type": "string"},
    "source_host": {"type": "string"},
    "target_host": {"type": "string"},
    "target_origin": {"type": "string"},
    "dns_provider": {"type": "string"},
    "caddy_provider": {"type": "string"},
    "ttl": {"type": "string"},
    "provider_config": {"type": "string"},
    "execute_provider_mutation": {"type": "boolean"},
    "target_health_url": {"type": "string"},
    "run_target_health": {"type": "boolean"},
    "target_health_timeout": {"type": "string"},
    "target_health_expect_status": {"type": "string"},
    "directory": {"type": "string"},
    "mode": {"type": "string"},
    "dry_run": {"type": "boolean"},
    "confirm_token": {"type": "string"},
    "critical": {"type": "boolean"},
    "postgres": {"type": "boolean"},
    "include_postgres": {"type": "boolean"},
    "redis": {"type": "boolean"},
    "uploads": {"type": "boolean"},
    "include_manifest": {"type": "boolean"},
    "manifest_kind": {"type": "string"},
    "domain": {"type": "string"},
    "image": {"type": "string"},
    "port": {"type": "integer"},
    "static_root": {"type": "string"},
    "completion_callback_url": {"type": "string"},
    "prism_task_id": {"type": "string"},
    "prism_run_id": {"type": "string"},
    "source_artifact_id": {"type": "string"},
    "result_artifact_id": {"type": "string"},
}

_WRAPPER_FIELDS = [
    "runtime_root",
    "completion_callback_url",
    "prism_task_id",
    "prism_run_id",
    "source_artifact_id",
    "result_artifact_id",
]

_ACTION_OPTIONAL_FIELDS: Dict[str, List[str]] = {
    "manifest.diff": ["runtime_root"],
    "manifest.conflicts": ["manifest_dir"],
    "pack.init.preview": [
        "environment",
        "critical",
        "postgres",
        "redis",
        "uploads",
        "directory",
        "include_manifest",
        "manifest_kind",
        "domain",
        "image",
        "port",
        "static_root",
    ],
    "env.diff": ["environment", "manifest_path", "runtime_root"],
    "deploy.plan": ["runtime_root"],
    "deploy.apply": ["runtime_root"],
    "deploy.rollback.plan": ["runtime_root"],
    "deploy.rollback.apply": ["runtime_root"],
    "runtime.status": ["runtime_root"],
    "runtime.doctor": ["runtime_root"],
    "runtime.drift": ["runtime_root"],
    "backup.plan": ["runtime_root"],
    "backup.status": ["environment", "manifest_path", "runtime_root"],
    "backup.create": ["runtime_root"],
    "restore.plan": ["runtime_root"],
    "restore.apply": ["runtime_root"],
    "release.list": ["runtime_root"],
    "release.show": ["runtime_root"],
    "app.readiness": ["environment", "manifest_path", "runtime_root"],
    "app.runbook": ["environment", "manifest_path", "runtime_root"],
    "app.export.plan": ["environment", "manifest_path", "runtime_root", "include_postgres"],
    "app.export.create": ["environment", "manifest_path", "runtime_root", "include_postgres"],
    "app.import.plan": ["runtime_root"],
    "app.import.apply": ["runtime_root", "mode"],
    "app.restore-drill.plan": ["environment", "manifest_path", "runtime_root", "source_path"],
    "app.restore-drill.apply": ["environment", "manifest_path", "runtime_root"],
    "app.cutover.plan": ["environment", "manifest_path", "runtime_root"],
    "app.cutover.apply": ["environment", "manifest_path", "runtime_root"],
    "app.traffic.plan": [
        "environment",
        "manifest_path",
        "runtime_root",
        "dns_provider",
        "caddy_provider",
        "ttl",
        "provider_config",
        "execute_provider_mutation",
        "target_health_url",
        "run_target_health",
        "target_health_timeout",
        "target_health_expect_status",
    ],
    "app.traffic.apply": [
        "environment",
        "manifest_path",
        "runtime_root",
        "dns_provider",
        "caddy_provider",
        "ttl",
        "provider_config",
        "execute_provider_mutation",
        "target_health_url",
        "run_target_health",
        "target_health_timeout",
        "target_health_expect_status",
    ],
    "app.traffic.rollback.plan": ["environment", "runtime_root"],
    "app.traffic.rollback.apply": ["environment", "runtime_root"],
    "app.isolation.plan": ["environment", "manifest_path", "runtime_root"],
    "receipts.list": ["app", "environment", "runtime_root"],
    "receipts.show": ["runtime_root"],
}


def _input_schema(action_id: str, required: List[str], dry_run: bool, confirm: bool) -> Dict[str, Any]:
    names = list(required)
    names.extend(_ACTION_OPTIONAL_FIELDS.get(action_id, []))
    if dry_run and "dry_run" not in names:
        names.append("dry_run")
    if confirm and "confirm_token" not in names:
        names.append("confirm_token")
    names.extend(_WRAPPER_FIELDS)

    properties: Dict[str, Dict[str, str]] = {}
    for name in names:
        if name not in properties:
            properties[name] = dict(_PROPERTY_SCHEMAS[name])
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _action(
    action_id: str,
    description: str,
    category: str,
    mutation_level: str,
    required: List[str],
    dry_run: bool = False,
    confirm: bool = False,
) -> Dict[str, Any]:
    return {
        "id": action_id,
        "description": description,
        "category": category,
        "mutation_level": mutation_level,
        "allowed_environments": ["dev", "staging", "production"],
        "input_schema": _input_schema(action_id, required, dry_run, confirm),
        "dry_run_support": dry_run,
        "confirmation_requirement": "required" if confirm else "none",
        "output_schema": {"type": "object", "required": ["summary", "payload", "report_markdown"]},
        "policy_gates": _policy_gates(action_id),
        "safety_notes": "No arbitrary shell execution. Inputs are typed and unknown fields are rejected.",
    }


def _policy_gates(action_id: str) -> List[str]:
    gates = ["manifest validation succeeds"]
    if action_id == "deploy.apply":
        gates.extend(["production apply disabled by default", "production apply requires confirmation token"])
    if action_id in MUTATING_ACTIONS:
        gates.append("per app/environment lock")
    if action_id == "restore.apply":
        gates.append("destructive restore disabled by default")
    if action_id == "app.traffic.apply":
        gates.append("DNS/Caddy provider mutation requires dry-run token, provider_config, and execute_provider_mutation")
    if action_id == "app.traffic.rollback.apply":
        gates.append("traffic rollback requires dry-run token and restorable previous provider state")
    return gates


ACTION_DEFINITIONS.extend(
    [
        _action("manifest.validate", "Validate one manifest.", "manifest", "read", ["manifest_path"]),
        _action("manifest.explain", "Explain one manifest.", "manifest", "read", ["manifest_path"]),
        _action("manifest.diff", "Diff desired rendered state against runtime.", "manifest", "read", ["manifest_path"]),
        _action("manifest.conflicts", "Scan manifests for platform conflicts.", "manifest", "read", []),
        _action("pack.validate", "Validate one portable app pack.", "pack", "read", ["manifest_path"]),
        _action("pack.explain", "Explain one portable app pack.", "pack", "read", ["manifest_path"]),
        _action("pack.init.preview", "Preview app pack scaffolding.", "pack", "read", ["app"]),
        _action("env.diff", "Compare redacted app env shape.", "portability", "read", ["app"]),
        _action("deploy.plan", "Plan a deploy.", "deploy", "read", ["manifest_path"]),
        _action("deploy.apply", "Apply a deploy.", "deploy", "mutating", ["manifest_path"], dry_run=True, confirm=True),
        _action("deploy.rollback.plan", "Plan a rollback.", "rollback", "read", ["app", "release_id"]),
        _action("deploy.rollback.apply", "Apply a rollback.", "rollback", "mutating", ["app", "release_id"], dry_run=True, confirm=True),
        _action("runtime.status", "Inspect runtime status.", "runtime", "read", []),
        _action("runtime.doctor", "Run host readiness checks.", "runtime", "read", []),
        _action("runtime.drift", "Detect drift for one manifest.", "runtime", "read", ["manifest_path"]),
        _action("backup.plan", "Plan an app backup.", "backup", "read", ["app"]),
        _action("backup.status", "Inspect backup freshness and coverage.", "backup", "read", ["app"]),
        _action("backup.create", "Create an app backup.", "backup", "mutating", ["app"], dry_run=True, confirm=True),
        _action("restore.plan", "Plan a restore preview.", "restore", "read", ["app", "backup_id"]),
        _action("restore.apply", "Create a restore preview.", "restore", "mutating", ["app", "backup_id"], dry_run=True, confirm=True),
        _action("release.list", "List app releases.", "release", "read", ["app"]),
        _action("release.show", "Show one release.", "release", "read", ["app", "release_id"]),
        _action("app.readiness", "Inspect app move readiness.", "portability", "read", ["app"]),
        _action("app.runbook", "Generate an app runbook payload.", "portability", "read", ["app"]),
        _action("app.export.plan", "Plan an app export.", "portability", "read", ["app"]),
        _action("app.export.create", "Create a metadata/runtime export bundle.", "portability", "mutating", ["app"], dry_run=True, confirm=True),
        _action("app.import.plan", "Plan an app import.", "portability", "read", ["source_path"]),
        _action("app.import.apply", "Create a rehearsal import preview.", "portability", "mutating", ["source_path"], dry_run=True, confirm=True),
        _action("app.restore-drill.plan", "Plan a restore drill.", "portability", "read", ["app"]),
        _action("app.restore-drill.apply", "Run an isolated restore drill check.", "portability", "mutating", ["app", "source_path"], dry_run=True, confirm=True),
        _action("app.cutover.plan", "Plan an app cutover.", "portability", "read", ["app", "source_host", "target_host"]),
        _action("app.cutover.apply", "Write a cutover checkpoint receipt.", "portability", "mutating", ["app", "source_host", "target_host"], dry_run=True, confirm=True),
        _action("app.traffic.plan", "Plan production traffic automation.", "traffic", "read", ["app", "source_host", "target_host", "target_origin"]),
        _action("app.traffic.apply", "Write a production traffic checkpoint receipt.", "traffic", "mutating", ["app", "source_host", "target_host", "target_origin"], dry_run=True, confirm=True),
        _action("app.traffic.rollback.plan", "Plan traffic provider rollback from a receipt.", "traffic", "read", ["app", "receipt_id"]),
        _action("app.traffic.rollback.apply", "Apply traffic provider rollback from a receipt.", "traffic", "mutating", ["app", "receipt_id"], dry_run=True, confirm=True),
        _action("app.isolation.plan", "Plan per-app isolation compatibility.", "portability", "read", ["app"]),
        _action("receipts.list", "List operation receipts.", "receipts", "read", []),
        _action("receipts.show", "Show one operation receipt.", "receipts", "read", ["receipt_id"]),
    ]
)


def _load_action_config() -> Dict[str, Any]:
    path = Path(os.environ.get("OPHELIA_ACTION_CONFIG", REPO_ROOT / "config" / "ophelia-actions.json"))
    if not path.exists():
        return {}
    try:
        config = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Action config JSON is invalid: {path}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"Action config must be a JSON object: {path}")
    return config


def _action_enabled(action_id: str, config: Dict[str, Any]) -> bool:
    enabled = config.get("enabled_actions", ["*"])
    disabled = config.get("disabled_actions", [])
    return ("*" in enabled or action_id in enabled) and action_id not in disabled


class ActionError(ValueError):
    pass


@dataclass
class JobResult:
    job: Dict[str, Any]
    existing: bool = False


def run_job(
    action_id: str,
    inputs: Dict[str, Any],
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    requested_by: str = "unknown",
    source: str = "cli",
    idempotency_key: str | None = None,
    events_json: bool = False,
) -> JobResult:
    if not isinstance(action_id, str) or not action_id:
        raise ActionError("action_id must be a non-empty string.")
    if not isinstance(inputs, dict):
        raise ActionError("inputs must be an object.")
    runtime_root = Path(inputs.get("runtime_root") or runtime_root).expanduser()
    jobs_root = runtime_root / "jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    input_hash = _input_hash(action_id, inputs)
    if idempotency_key:
        existing = _idempotent_job(jobs_root, action_id, idempotency_key, input_hash)
        if existing:
            return JobResult(_load_job(jobs_root / f"{existing}.json", existing), existing=True)

    job_id = _job_id(action_id, input_hash)
    job = {
        "job_id": job_id,
        "action_id": action_id,
        "inputs_hash": input_hash,
        "state": "queued",
        "created_at": _utc_now(),
        "started_at": None,
        "completed_at": None,
        "result": None,
        "error": None,
        "warnings": [],
        "requested_by": requested_by,
        "source": source,
        "artifact_links": _artifact_links(inputs),
    }
    events: List[Dict[str, Any]] = []
    _event(events, "job.created", job_id=job_id, action_id=action_id)
    lock = _lock_path(runtime_root, action_id, inputs)
    lock_acquired = False
    apply_confirmation_token: str | None = None
    try:
        if lock is not None:
            _acquire_lock(lock)
            lock_acquired = True
        job["state"] = "running"
        job["started_at"] = _utc_now()
        _event(events, "job.started", job_id=job_id)
        _event(events, "step.started", step="execute_action")
        if action_id in MUTATING_ACTIONS and not inputs.get("dry_run", True):
            apply_confirmation_token = _validate_confirmation_record(runtime_root, action_id, inputs)
        result = run_action(action_id, inputs, runtime_root)
        job["result"] = result
        if action_id in MUTATING_ACTIONS and inputs.get("dry_run", True):
            job["state"] = "waiting_for_confirmation"
            _register_confirmation_from_result(runtime_root, job, result, inputs)
        else:
            job["state"] = "succeeded"
            job["completed_at"] = _utc_now()
            if apply_confirmation_token:
                _consume_confirmation(runtime_root, apply_confirmation_token, job_id)
        _event(events, "job.completed", state=job["state"], summary=result["summary"])
    except Exception as exc:
        job["state"] = "failed"
        job["completed_at"] = _utc_now()
        job["error"] = str(exc)
        _event(events, "error", error=str(exc))
        _event(events, "job.completed", state="failed")
    finally:
        if lock_acquired and lock is not None and lock.exists():
            lock.unlink()

    _maybe_send_completion_callback(inputs, job, events)
    _write_job(jobs_root, job, events)
    _append_audit(runtime_root, job)
    if idempotency_key and job["state"] != "failed":
        _write_idempotency(jobs_root, action_id, idempotency_key, input_hash, job_id)
    if events_json:
        for event in events:
            print(json.dumps(event, sort_keys=True))
    return JobResult(job)


def cancel_job(runtime_root: Path, job_id: str) -> Dict[str, Any]:
    job_path = runtime_root / "jobs" / f"{job_id}.json"
    if not job_path.exists():
        raise ActionError(f"Job not found: {job_id}")
    job = _load_job(job_path, job_id)
    if job["state"] in {"queued", "waiting_for_confirmation"}:
        job["state"] = "cancelled"
        job["completed_at"] = _utc_now()
        job_path.write_text(json.dumps(job, indent=2, sort_keys=True) + "\n")
    return job


def _write_job(jobs_root: Path, job: Dict[str, Any], events: List[Dict[str, Any]]) -> None:
    (jobs_root / f"{job['job_id']}.json").write_text(json.dumps(job, indent=2, sort_keys=True) + "\n")
    (jobs_root / f"{job['job_id']}.events.ndjson").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
    )


def _event(events: List[Dict[str, Any]], event_type: str, **payload: Any) -> None:
    events.append({"type": event_type, "timestamp": _utc_now(), **payload})


def _input_hash(action_id: str, inputs: Dict[str, Any]) -> str:
    sanitized = {key: value for key, value in inputs.items() if key != "confirm_token"}
    encoded = json.dumps({"action_id": action_id, "inputs": sanitized}, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _confirmation_input_hash(action_id: str, inputs: Dict[str, Any]) -> str:
    sanitized = {
        key: value
        for key, value in inputs.items()
        if key not in {"confirm_token", "dry_run"}
    }
    encoded = json.dumps({"action_id": action_id, "inputs": sanitized}, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _job_id(action_id: str, input_hash: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{action_id.replace('.', '-')}-{input_hash[:10]}-{secrets.token_hex(3)}"


def _idempotent_job(jobs_root: Path, action_id: str, key: str, input_hash: str) -> str | None:
    index_path = jobs_root / "idempotency.json"
    if not index_path.exists():
        return None
    try:
        index = json.loads(index_path.read_text())
    except json.JSONDecodeError as exc:
        raise ActionError(f"Idempotency index is unreadable: {index_path}") from exc
    if not isinstance(index, dict):
        raise ActionError(f"Idempotency index must be a JSON object: {index_path}")
    record = index.get(key)
    if record is None:
        return None
    if not isinstance(record, dict):
        raise ActionError(f"Idempotency record is invalid for key: {key}")
    if record.get("action_id") != action_id or record.get("input_hash") != input_hash:
        raise ActionError("Idempotency key already exists with different action/input.")
    job_id = record.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ActionError(f"Idempotency record is missing a job_id for key: {key}")
    return job_id


def _load_job(path: Path, job_id: str) -> Dict[str, Any]:
    try:
        job = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ActionError(f"Job record is unreadable: {job_id}") from exc
    if not isinstance(job, dict) or not isinstance(job.get("state"), str):
        raise ActionError(f"Job record is invalid: {job_id}")
    return job


def _write_idempotency(jobs_root: Path, action_id: str, key: str, input_hash: str, job_id: str) -> None:
    index_path = jobs_root / "idempotency.json"
    try:
        index = json.loads(index_path.read_text()) if index_path.exists() else {}
    except json.JSONDecodeError as exc:
        raise ActionError(f"Idempotency index is unreadable: {index_path}") from exc
    if not isinstance(index, dict):
        raise ActionError(f"Idempotency index must be a JSON object: {index_path}")
    index[key] = {"action_id": action_id, "input_hash": input_hash, "job_id": job_id}
    index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")


def _lock_path(runtime_root: Path, action_id: str, inputs: Dict[str, Any]) -> Path | None:
    if action_id not in MUTATING_ACTIONS:
        return None
    app = inputs.get("app")
    environment = inputs.get("environment")
    if not app and inputs.get("manifest_path"):
        try:
            manifest = load_manifest(Path(inputs["manifest_path"]))
            app = manifest.app
            environment = environment or manifest.environment
        except (ManifestError, OSError, ValueError):
            app = "unknown"
    environment = environment or "unknown"
    lock_root = runtime_root / "locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    return lock_root / f"{app}-{environment}.lock"


def _acquire_lock(path: Path) -> None:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ActionError(f"Concurrent mutating job is already locked: {path.name}")
    with os.fdopen(fd, "w") as handle:
        handle.write(str(os.getpid()))


def _append_audit(runtime_root: Path, job: Dict[str, Any]) -> None:
    audit_root = runtime_root / "audit"
    audit_root.mkdir(parents=True, exist_ok=True)
    record = {
        "job_id": job["job_id"],
        "requested_by": job["requested_by"],
        "source": job["source"],
        "action_id": job["action_id"],
        "inputs_hash": job["inputs_hash"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "completed_at": job["completed_at"],
        "state": job["state"],
        "result_summary": (job.get("result") or {}).get("summary") if isinstance(job.get("result"), dict) else None,
    }
    with (audit_root / "jobs.ndjson").open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _confirmation_expires_at() -> str:
    ttl = int(_load_action_config().get("confirmation_token_ttl_seconds", 900))
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl)).replace(microsecond=0).isoformat()


def _register_confirmation_from_result(
    runtime_root: Path,
    job: Dict[str, Any],
    result: Dict[str, Any],
    inputs: Dict[str, Any],
) -> None:
    payload = result.get("payload") if isinstance(result, dict) else None
    if not isinstance(payload, dict):
        return
    token = payload.get("required_confirmation_token")
    expires_at = payload.get("confirmation_expires_at")
    if not isinstance(token, str) or not token:
        return
    confirmations_root = runtime_root / "confirmations"
    confirmations_root.mkdir(parents=True, exist_ok=True)
    record = {
        "token": token,
        "action_id": job["action_id"],
        "confirmation_input_hash": _confirmation_input_hash(str(job["action_id"]), inputs),
        "created_at": _utc_now(),
        "expires_at": expires_at,
        "dry_run_job_id": job["job_id"],
        "state": "active",
    }
    (confirmations_root / f"{token}.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")


def _validate_confirmation_record(runtime_root: Path, action_id: str, inputs: Dict[str, Any]) -> str:
    token = inputs.get("confirm_token")
    if not isinstance(token, str) or not token:
        raise ActionError("confirmation token is missing or incorrect.")
    path = runtime_root / "confirmations" / f"{token}.json"
    if not path.exists():
        raise ActionError("confirmation token was not issued by a prior dry-run job.")
    record = json.loads(path.read_text())
    if record.get("state") != "active":
        raise ActionError("confirmation token is no longer active.")
    if record.get("action_id") != action_id:
        raise ActionError("confirmation token action does not match this job.")
    expected_hash = _confirmation_input_hash(action_id, inputs)
    if record.get("confirmation_input_hash") != expected_hash:
        raise ActionError("confirmation token input hash does not match this job.")
    expires_at = record.get("expires_at")
    if isinstance(expires_at, str) and _parse_datetime(expires_at) < datetime.now(timezone.utc):
        raise ActionError("confirmation token has expired.")
    return token


def _consume_confirmation(runtime_root: Path, token: str, job_id: str) -> None:
    path = runtime_root / "confirmations" / f"{token}.json"
    if not path.exists():
        return
    record = json.loads(path.read_text())
    record["state"] = "consumed"
    record["consumed_by_job_id"] = job_id
    record["consumed_at"] = _utc_now()
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _maybe_send_completion_callback(
    inputs: Dict[str, Any],
    job: Dict[str, Any],
    events: List[Dict[str, Any]],
) -> None:
    callback_url = inputs.get("completion_callback_url")
    if not callback_url:
        return
    config = _load_action_config()
    if not config.get("callbacks_enabled", False):
        job.setdefault("warnings", []).append("Completion callback requested but callbacks are disabled by policy.")
        _event(events, "warning", warning="completion_callback_disabled")
        return
    if not _callback_destination_allowed(str(callback_url), config):
        job.setdefault("warnings", []).append("Completion callback requested but destination is not allowlisted.")
        _event(events, "warning", warning="completion_callback_not_allowlisted")
        return
    body = json.dumps(job, sort_keys=True).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    secret_name = config.get("callback_secret_env")
    secret = os.environ.get(secret_name) if isinstance(secret_name, str) else None
    if secret:
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Ophelia-Signature"] = f"sha256={signature}"
    try:
        req = request.Request(str(callback_url), data=body, headers=headers, method="POST")
        with request.urlopen(req, timeout=5) as response:
            _event(events, "command.completed", command="completion_callback", status=response.status)
    except (OSError, URLError) as exc:
        warning = f"Completion callback failed for {redact_url(str(callback_url))}: {exc.__class__.__name__}"
        job.setdefault("warnings", []).append(warning)
        _event(events, "warning", warning=warning)


def _callback_destination_allowed(callback_url: str, config: Dict[str, Any]) -> bool:
    allowed = config.get("callback_allowed_hosts", [])
    if not isinstance(allowed, list) or not all(isinstance(item, str) and item.strip() for item in allowed):
        return False
    parsed = urlparse(callback_url)
    if not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    host_port = f"{host}:{parsed.port}" if parsed.port is not None else host
    allowed_hosts = {item.strip().lower() for item in allowed}
    return host in allowed_hosts or host_port in allowed_hosts


def _artifact_links(inputs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: inputs.get(key)
        for key in ("prism_task_id", "prism_run_id", "source_artifact_id", "result_artifact_id")
        if inputs.get(key)
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
