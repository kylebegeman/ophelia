from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .manifest import Manifest, ManifestError, load_manifest
from .github_providers import github_drift_snapshot
from .operation_schema import SCHEMA_VERSION
from .planning import bundle_diff
from .portability import _backup_records, _restore_drill_receipts, traffic_status
from .receipt_index import receipt_timeline
from .runtime import bundle_hash, render_bundle
from .state_db import state_status, state_summary

DRIFT_REPORT_KIND = "ophelia.drift_report"
DRIFT_SUMMARY_KIND = "ophelia.drift_summary"

_SEVERITY_RANK = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

_DRIFT_SEVERITIES = {"low", "medium", "high", "critical"}


def manifest_drift(manifest: Manifest, manifest_path: Path, runtime_root: Path) -> Dict[str, object]:
    app_root = runtime_root / "apps" / manifest.app
    diff = bundle_diff(manifest, runtime_root)
    release_metadata = _release_metadata(manifest, manifest_path, runtime_root)
    env_status = _env_status(app_root / "env.example", app_root / "env")
    snapshots: List[Dict[str, object]] = []
    findings: List[Dict[str, object]] = []

    rendered_snapshot, rendered_findings = _rendered_drift(manifest, manifest_path, runtime_root, diff)
    release_snapshot, release_findings = _release_drift(manifest, manifest_path, runtime_root, release_metadata)
    env_snapshot, env_findings = _env_drift(manifest, manifest_path, runtime_root, env_status)
    state_snapshot, state_findings = _state_drift(manifest, runtime_root)
    backup_snapshot, backup_findings = _backup_restore_drift(manifest, manifest_path, runtime_root)
    observability_snapshot, observability_findings = _observability_drift(manifest, runtime_root)
    traffic_snapshot, traffic_findings = _traffic_provider_drift(manifest, manifest_path, runtime_root)
    github_snapshot, github_findings = _github_drift(manifest, runtime_root)

    snapshots.extend(
        [
            rendered_snapshot,
            release_snapshot,
            env_snapshot,
            state_snapshot,
            backup_snapshot,
            observability_snapshot,
            traffic_snapshot,
            github_snapshot,
        ]
    )
    for group in (
        rendered_findings,
        release_findings,
        env_findings,
        state_findings,
        backup_findings,
        observability_findings,
        traffic_findings,
        github_findings,
    ):
        findings.extend(group)
    findings = _sort_findings(findings)
    severity = _highest_severity(findings)
    drifted = any(str(item.get("severity")) in _DRIFT_SEVERITIES for item in findings)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": DRIFT_REPORT_KIND,
        "app": manifest.app,
        "environment": getattr(manifest, "environment", None),
        "manifest_path": str(manifest_path),
        "runtime_path": str(app_root),
        "status": "drift" if drifted else "ok",
        "severity": severity,
        "drift": drifted,
        "snapshots": snapshots,
        "findings": findings,
        "remediation_commands": _unique_strings(
            command
            for finding in findings
            for command in finding.get("remediation_commands", [])
            if isinstance(command, str)
        ),
        "plan_candidates": _unique_dicts(
            candidate
            for finding in findings
            for candidate in finding.get("plan_candidates", [])
            if isinstance(candidate, dict)
        ),
        "rendered": {
            "changed_files": diff["changed_files"],
            "removed_files": diff["removed_files"],
            "caddy_changes": diff["caddy_changes"],
            "compose_changes": diff["compose_changes"],
        },
        "release_metadata": release_metadata,
        "env": env_status,
        "summary": _summary(manifest.app, drifted, diff, release_metadata, env_status),
    }


def all_drift(manifest_dir: Path, runtime_root: Path) -> Dict[str, object]:
    reports: List[Dict[str, object]] = []
    errors: List[Dict[str, str]] = []
    for manifest_path in sorted(manifest_dir.glob("*.ophelia.yml")) if manifest_dir.exists() else []:
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError as exc:
            errors.append({"path": str(manifest_path), "error": str(exc)})
            continue
        reports.append(manifest_drift(manifest, manifest_path, runtime_root))
    aggregate_findings = _sort_findings(
        {
            **finding,
            "app": report.get("app"),
            "environment": report.get("environment"),
        }
        for report in reports
        for finding in report.get("findings", [])
        if isinstance(finding, dict)
    )
    severity = _highest_severity(aggregate_findings)
    reported_severity = "high" if errors and _SEVERITY_RANK[severity] < _SEVERITY_RANK["high"] else severity
    drifted = any(report["drift"] for report in reports) or bool(errors)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": DRIFT_SUMMARY_KIND,
        "manifest_dir": str(manifest_dir),
        "runtime_root": str(runtime_root),
        "status": "drift" if drifted else "ok",
        "severity": reported_severity,
        "drift": drifted,
        "reports": reports,
        "errors": errors,
        "findings": aggregate_findings,
        "summary": _aggregate_summary(reports, errors, reported_severity),
    }


def _rendered_drift(
    manifest: Manifest,
    manifest_path: Path,
    runtime_root: Path,
    diff: Dict[str, object],
) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
    changed = diff.get("changed_files") if isinstance(diff.get("changed_files"), list) else []
    removed = diff.get("removed_files") if isinstance(diff.get("removed_files"), list) else []
    findings: List[Dict[str, object]] = []
    if changed or removed:
        commands = [_deploy_plan_command(manifest_path, runtime_root)]
        plan_candidates = [_plan_candidate("deploy.plan", manifest, manifest_path, commands[0])]
        for item in changed:
            if isinstance(item, dict):
                findings.append(
                    _finding(
                        "rendered_file_changed",
                        f"Rendered runtime file `{item.get('path')}` does not match the current manifest bundle.",
                        _rendered_severity(str(item.get("path") or "")),
                        "runtime",
                        str(item.get("path") or ""),
                        commands,
                        plan_candidates,
                    )
                )
        for item in removed:
            if isinstance(item, dict):
                findings.append(
                    _finding(
                        "rendered_file_removed",
                        f"Runtime file `{item.get('path')}` exists but is no longer generated by the manifest.",
                        "medium",
                        "runtime",
                        str(item.get("path") or ""),
                        commands,
                        plan_candidates,
                    )
                )
    snapshot = _snapshot(
        "manifest_vs_runtime_files",
        "drift" if findings else "clean",
        "runtime",
        f"{len(changed)} changed generated file(s), {len(removed)} removed generated file(s).",
        desired={"bundle_hash": bundle_hash(render_bundle(manifest))},
        observed={
            "runtime_path": str(runtime_root / "apps" / manifest.app),
            "changed_file_count": len(changed),
            "removed_file_count": len(removed),
        },
    )
    return snapshot, findings


def _release_drift(
    manifest: Manifest,
    manifest_path: Path,
    runtime_root: Path,
    release_metadata: Dict[str, object],
) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
    mismatches = release_metadata.get("mismatches") if isinstance(release_metadata.get("mismatches"), list) else []
    findings: List[Dict[str, object]] = []
    if mismatches:
        command = _deploy_plan_command(manifest_path, runtime_root)
        severity = "high" if not release_metadata.get("present") else "medium"
        findings.append(
            _finding(
                "release_metadata_drift",
                "Release metadata does not match the manifest-rendered bundle.",
                severity,
                "release",
                "release.json",
                [command],
                [_plan_candidate("deploy.plan", manifest, manifest_path, command)],
            )
        )
    snapshot = _snapshot(
        "release_metadata",
        "drift" if findings else "clean",
        "release",
        f"{len(mismatches)} release metadata mismatch(es).",
        desired={
            "manifest_hash": release_metadata.get("desired_manifest_hash"),
            "rendered_bundle_hash": release_metadata.get("desired_bundle_hash"),
        },
        observed={
            "present": release_metadata.get("present"),
            "release_id": release_metadata.get("release_id"),
            "manifest_hash": release_metadata.get("manifest_hash"),
            "rendered_bundle_hash": release_metadata.get("rendered_bundle_hash"),
        },
    )
    return snapshot, findings


def _env_drift(
    manifest: Manifest,
    manifest_path: Path,
    runtime_root: Path,
    env_status: Dict[str, object],
) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
    missing = env_status.get("missing_keys") if isinstance(env_status.get("missing_keys"), list) else []
    placeholder = env_status.get("placeholder_keys") if isinstance(env_status.get("placeholder_keys"), list) else []
    command = _secrets_audit_command(manifest, manifest_path, runtime_root)
    findings: List[Dict[str, object]] = []
    for key in missing:
        findings.append(
            _finding(
                "env_key_missing",
                f"Required env key `{key}` is missing from the runtime env file.",
                "high",
                "secrets",
                str(key),
                [command],
                [_plan_candidate("secrets.audit", manifest, manifest_path, command, mutates_state=False)],
            )
        )
    for key in placeholder:
        findings.append(
            _finding(
                "env_key_placeholder",
                f"Required env key `{key}` still has a placeholder value.",
                "medium",
                "secrets",
                str(key),
                [command],
                [_plan_candidate("secrets.audit", manifest, manifest_path, command, mutates_state=False)],
            )
        )
    status = "drift" if missing or placeholder else "clean"
    snapshot = _snapshot(
        "env_requirements_vs_runtime_env",
        status,
        "secrets",
        f"{len(missing)} missing env key(s), {len(placeholder)} placeholder env key(s).",
        desired={"required_keys": env_status.get("required_keys", [])},
        observed={
            "source": env_status.get("source"),
            "present_keys": env_status.get("present_keys", []),
            "missing_keys": missing,
            "placeholder_keys": placeholder,
        },
    )
    return snapshot, findings


def _state_drift(manifest: Manifest, runtime_root: Path) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
    status = state_status(runtime_root)
    findings: List[Dict[str, object]] = []
    command = _state_refresh_command(runtime_root)
    db_path = str(status.get("db_path") or runtime_root / "state" / "ophelia-state.sqlite3")
    if not status.get("available"):
        findings.append(
            _finding(
                "state_index_unavailable",
                "The SQLite state service is unavailable in this Python runtime.",
                "medium",
                "state",
                db_path,
                [],
                [],
            )
        )
    elif not status.get("exists"):
        findings.append(
            _finding(
                "state_index_missing",
                "No local state service index exists for this runtime root.",
                "medium",
                "state",
                db_path,
                [command],
                [_plan_candidate("state.refresh", manifest, None, command, mutates_state=False)],
            )
        )
    elif status.get("needs_rebuild"):
        findings.append(
            _finding(
                "state_index_rebuild_required",
                "The local state service schema or metadata is out of date.",
                "medium",
                "state",
                db_path,
                [command],
                [_plan_candidate("state.refresh", manifest, None, command, mutates_state=False)],
            )
        )
    elif status.get("needs_refresh"):
        findings.append(
            _finding(
                "state_index_stale",
                "The local state service index is stale.",
                "low",
                "state",
                db_path,
                [command],
                [_plan_candidate("state.refresh", manifest, None, command, mutates_state=False)],
            )
        )

    summary = None
    if not findings:
        summary = state_summary(runtime_root)
        apps = summary.get("apps") if isinstance(summary.get("apps"), list) else []
        app_entry = next((entry for entry in apps if isinstance(entry, dict) and entry.get("app") == manifest.app), None)
        if app_entry is None:
            findings.append(
                _finding(
                    "state_app_missing",
                    "The current app is not present in the refreshed local state service index.",
                    "medium",
                    "state",
                    manifest.app,
                    [command],
                    [_plan_candidate("state.refresh", manifest, None, command, mutates_state=False)],
                )
            )
    else:
        app_entry = None

    freshness = status.get("freshness") if isinstance(status.get("freshness"), dict) else {}
    snapshot = _snapshot(
        "runtime_root_vs_state_index",
        "drift" if any(str(item.get("severity")) in _DRIFT_SEVERITIES for item in findings) else "clean",
        "state",
        str(status.get("summary") or "State service status unavailable."),
        desired={"app": manifest.app, "schema_version": status.get("schema_version_code")},
        observed={
            "db_path": db_path,
            "exists": status.get("exists"),
            "schema_version": status.get("schema_version_db"),
            "freshness": freshness,
            "app": app_entry,
        },
    )
    return snapshot, findings


def _backup_restore_drift(
    manifest: Manifest,
    manifest_path: Path,
    runtime_root: Path,
) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
    backup_contract = manifest.data.backups
    backups_root = runtime_root / "backups" / "apps" / manifest.app
    backups = _backup_records(backups_root)
    restore_receipts = _successful_restore_receipts(runtime_root, manifest.app)
    findings: List[Dict[str, object]] = []
    backup_required = bool(backup_contract and backup_contract.required)
    restore_required = bool(backup_contract and backup_contract.restore_drill_required)
    environment = manifest.environment or "unknown"

    if backup_required and not backups:
        command = _backup_plan_command(manifest, runtime_root)
        findings.append(
            _finding(
                "backup_required_missing",
                "The manifest requires backups, but no local backup record exists for this app.",
                "high",
                "backup",
                str(backups_root),
                [command],
                [_plan_candidate("backup.plan", manifest, manifest_path, command)],
            )
        )
    if restore_required and not restore_receipts:
        command = _backup_verify_plan_command(manifest, manifest_path, runtime_root)
        findings.append(
            _finding(
                "restore_drill_required_missing",
                "The manifest requires a restore drill, but no successful restore drill or backup verification receipt exists.",
                "high",
                "restore",
                str(runtime_root / "apps" / manifest.app / "restore-drills"),
                [command],
                [_plan_candidate("backup.verify.plan", manifest, manifest_path, command, mutates_state=False)],
            )
        )

    status = "drift" if findings else "clean"
    snapshot = _snapshot(
        "backup_restore_requirements_vs_receipts",
        status,
        "backup",
        f"{len(backups)} backup record(s), {len(restore_receipts)} successful restore evidence receipt(s).",
        desired={
            "backup_required": backup_required,
            "restore_drill_required": restore_required,
            "environment": environment,
        },
        observed={
            "backup_count": len(backups),
            "latest_backup": backups[-1] if backups else None,
            "successful_restore_receipt_count": len(restore_receipts),
            "latest_restore_receipt": restore_receipts[-1] if restore_receipts else None,
        },
    )
    return snapshot, findings


def _observability_drift(manifest: Manifest, runtime_root: Path) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
    configured = _observability_configured(manifest)
    latest_path = runtime_root / "observability" / "latest.json"
    entry: Optional[Dict[str, object]] = None
    latest_payload: Dict[str, object] = {}
    if latest_path.exists():
        try:
            payload = json.loads(latest_path.read_text())
            latest_payload = payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            latest_payload = {}
        apps = latest_payload.get("apps") if isinstance(latest_payload.get("apps"), list) else []
        entry = next(
            (
                item
                for item in apps
                if isinstance(item, dict)
                and item.get("app") == manifest.app
                and (manifest.environment is None or item.get("environment") in {manifest.environment, None, "unknown"})
            ),
            None,
        )
    findings: List[Dict[str, object]] = []
    command = _observability_schedule_command(runtime_root)
    if configured and entry is None:
        findings.append(
            _finding(
                "observability_schedule_not_observed",
                "Observability is configured, but no latest schedule-run snapshot is recorded for this app.",
                "low",
                "observability",
                str(latest_path),
                [command],
                [_plan_candidate("observability.schedule.run", manifest, None, command, mutates_state=False)],
            )
        )
    elif isinstance(entry, dict) and entry.get("status") == "blocked":
        findings.append(
            _finding(
                "observability_snapshot_blocked",
                "The latest local observability schedule snapshot is blocked for this app.",
                "medium",
                "observability",
                str(latest_path),
                [_observability_status_command(manifest, runtime_root)],
                [_plan_candidate("observability.status", manifest, None, _observability_status_command(manifest, runtime_root), mutates_state=False)],
            )
        )
    snapshot = _snapshot(
        "observability_schedule_vs_manifest_registry",
        "drift" if findings else "clean" if configured else "not_configured",
        "observability",
        "Latest observability schedule run is recorded." if entry else "No latest observability schedule run entry for this app.",
        desired={"configured": configured},
        observed={
            "latest_path": str(latest_path),
            "latest_exists": latest_path.exists(),
            "operation_id": latest_payload.get("operation_id"),
            "app_entry": entry,
        },
    )
    return snapshot, findings


def _traffic_provider_drift(
    manifest: Manifest,
    manifest_path: Path,
    runtime_root: Path,
) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
    findings: List[Dict[str, object]] = []
    try:
        status = traffic_status(
            manifest.app,
            environment=manifest.environment,
            runtime_root=runtime_root,
            manifest_path=manifest_path,
        )
    except Exception as exc:  # noqa: BLE001 - drift should report degraded context
        status = {"status": "unavailable", "summary": str(exc), "latest_apply": None, "provider_plan": []}
        findings.append(
            _finding(
                "traffic_status_unavailable",
                "Traffic/provider status could not be read from local receipts.",
                "low",
                "traffic",
                manifest.app,
                [],
                [],
            )
        )

    latest_apply = status.get("latest_apply") if isinstance(status.get("latest_apply"), dict) else None
    provider_plan = status.get("provider_plan") if isinstance(status.get("provider_plan"), list) else []
    if latest_apply and latest_apply.get("status") not in {"succeeded", "ok"}:
        findings.append(
            _finding(
                "traffic_apply_not_succeeded",
                "The latest traffic apply receipt did not succeed.",
                "medium",
                "traffic",
                str(latest_apply.get("receipt_id") or manifest.app),
                [],
                [],
            )
        )
    if not latest_apply and manifest.routes:
        findings.append(
            _finding(
                "provider_observation_missing",
                "No local traffic provider apply receipt is available to compare DNS/Caddy desired state against observed provider output.",
                "info",
                "traffic",
                manifest.app,
                [],
                [],
            )
        )

    snapshot = _snapshot(
        "dns_caddy_desired_state_vs_provider_observations",
        "not_observed" if not latest_apply else "drift" if any(item.get("owner") == "traffic" and item.get("severity") != "info" for item in findings) else "clean",
        "traffic",
        str(status.get("summary") or "Traffic/provider status unavailable."),
        desired={"routes": [route.domain for route in manifest.routes]},
        observed={
            "latest_apply": latest_apply,
            "provider_plan": provider_plan,
            "status": status.get("status"),
        },
    )
    return snapshot, findings


def _github_drift(manifest: Manifest, runtime_root: Path) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
    return github_drift_snapshot(manifest, runtime_root)


def _snapshot(
    name: str,
    status: str,
    owner: str,
    summary: str,
    *,
    desired: Optional[Dict[str, object]] = None,
    observed: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    return {
        "name": name,
        "status": status,
        "owner": owner,
        "summary": summary,
        "desired": desired or {},
        "observed": observed or {},
    }


def _finding(
    code: str,
    message: str,
    severity: str,
    owner: str,
    path: Optional[str],
    remediation_commands: List[str],
    plan_candidates: List[Dict[str, object]],
) -> Dict[str, object]:
    return {
        "code": code,
        "message": message,
        "severity": severity if severity in _SEVERITY_RANK else "info",
        "owner": owner,
        "path": path,
        "remediation_commands": remediation_commands,
        "plan_candidates": plan_candidates,
    }


def _plan_candidate(
    operation: str,
    manifest: Manifest,
    manifest_path: Optional[Path],
    command: str,
    *,
    mutates_state: bool = False,
) -> Dict[str, object]:
    return {
        "operation": operation,
        "app": manifest.app,
        "environment": manifest.environment,
        "manifest_path": str(manifest_path) if manifest_path is not None else None,
        "command": command,
        "mutates_state": mutates_state,
    }


def _sort_findings(findings: object) -> List[Dict[str, object]]:
    if not isinstance(findings, list):
        findings = list(findings) if findings is not None else []
    typed = [dict(item) for item in findings if isinstance(item, dict)]
    return sorted(
        typed,
        key=lambda item: (
            -_SEVERITY_RANK.get(str(item.get("severity") or "info"), 0),
            str(item.get("owner") or ""),
            str(item.get("code") or ""),
            str(item.get("path") or ""),
        ),
    )


def _highest_severity(findings: List[Dict[str, object]]) -> str:
    if not findings:
        return "info"
    return max(
        (str(item.get("severity") or "info") for item in findings),
        key=lambda severity: _SEVERITY_RANK.get(severity, 0),
    )


def _rendered_severity(path: str) -> str:
    if path == "compose.yml" or path.startswith("caddy/") or path == "manifest.lock.json":
        return "medium"
    return "low"


def _unique_strings(values: object) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _unique_dicts(values: object) -> List[Dict[str, object]]:
    result: List[Dict[str, object]] = []
    seen = set()
    for value in values:
        key = json.dumps(value, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _successful_restore_receipts(runtime_root: Path, app: str) -> List[Dict[str, object]]:
    receipts = [
        dict(item)
        for item in _restore_drill_receipts(runtime_root, app)
        if str(item.get("status") or "").lower() in {"succeeded", "success", "ok"}
    ]
    try:
        timeline = receipt_timeline(runtime_root, app=app, operation="backup.verify.apply", status="succeeded")
    except Exception:  # noqa: BLE001 - best-effort local evidence
        timeline = {"receipts": []}
    for item in timeline.get("receipts", []) if isinstance(timeline.get("receipts"), list) else []:
        if isinstance(item, dict):
            receipts.append(dict(item))
    receipts.sort(key=lambda item: (str(item.get("completed_at") or item.get("started_at") or ""), str(item.get("receipt_id") or "")))
    return receipts


def _observability_configured(manifest: Manifest) -> bool:
    config = manifest.observability
    return bool(
        (config.health and config.health.url)
        or (config.metrics and config.metrics.url)
        or (config.logs and config.logs.containers)
    )


def _deploy_plan_command(manifest_path: Path, runtime_root: Path) -> str:
    return f"ship deploy {_quote(manifest_path)} --plan --runtime-root {_quote(runtime_root)} --json"


def _secrets_audit_command(manifest: Manifest, manifest_path: Path, runtime_root: Path) -> str:
    command = f"ship secrets audit {_quote(manifest_path)} --runtime-root {_quote(runtime_root)}"
    if manifest.environment:
        command += f" --environment {manifest.environment}"
    return command + " --json"


def _state_refresh_command(runtime_root: Path) -> str:
    return f"ship state refresh --runtime-root {_quote(runtime_root)} --json"


def _backup_plan_command(manifest: Manifest, runtime_root: Path) -> str:
    return f"ship backup plan {manifest.app} --runtime-root {_quote(runtime_root)} --json"


def _backup_verify_plan_command(manifest: Manifest, manifest_path: Path, runtime_root: Path) -> str:
    command = f"ship backup verify plan {manifest.app} --manifest {_quote(manifest_path)} --runtime-root {_quote(runtime_root)}"
    if manifest.environment:
        command += f" --environment {manifest.environment}"
    return command + " --json"


def _observability_schedule_command(runtime_root: Path) -> str:
    return f"ship observability schedule run --runtime-root {_quote(runtime_root)} --json"


def _observability_status_command(manifest: Manifest, runtime_root: Path) -> str:
    command = f"ship observability status --app {manifest.app} --runtime-root {_quote(runtime_root)}"
    if manifest.environment:
        command += f" --environment {manifest.environment}"
    return command + " --json"


def _quote(value: object) -> str:
    return shlex.quote(str(value))


def _aggregate_summary(reports: List[Dict[str, object]], errors: List[Dict[str, str]], severity: str) -> str:
    drifted = sum(1 for report in reports if report.get("drift"))
    return (
        f"{len(reports)} manifest(s) scanned, {drifted} with drift, "
        f"{len(errors)} manifest error(s), highest severity {severity}."
    )


def _release_metadata(manifest: Manifest, manifest_path: Path, runtime_root: Path) -> Dict[str, object]:
    release_path = runtime_root / "apps" / manifest.app / "release.json"
    desired_manifest_hash = _file_hash(manifest_path)
    desired_bundle_hash = bundle_hash(render_bundle(manifest))
    if not release_path.exists():
        return {
            "present": False,
            "mismatches": ["release.json missing"],
            "desired_manifest_hash": desired_manifest_hash,
            "desired_bundle_hash": desired_bundle_hash,
        }

    try:
        payload = json.loads(release_path.read_text())
    except json.JSONDecodeError:
        return {"present": True, "mismatches": ["release.json is not valid JSON"]}

    mismatches = []
    if payload.get("manifest_hash") != desired_manifest_hash:
        mismatches.append("manifest_hash")
    if payload.get("rendered_bundle_hash") != desired_bundle_hash:
        mismatches.append("rendered_bundle_hash")
    return {
        "present": True,
        "release_id": payload.get("release_id"),
        "manifest_hash": payload.get("manifest_hash"),
        "rendered_bundle_hash": payload.get("rendered_bundle_hash"),
        "desired_manifest_hash": desired_manifest_hash,
        "desired_bundle_hash": desired_bundle_hash,
        "mismatches": mismatches,
    }


def _env_status(env_example_path: Path, env_path: Path) -> Dict[str, object]:
    required = _env_keys(env_example_path)
    actual = _env_keys(env_path)
    placeholder = sorted(key for key, value in actual.items() if _is_placeholder(value))
    return {
        "source": str(env_path),
        "required_keys": sorted(required),
        "present_keys": sorted(key for key in required if key in actual),
        "missing_keys": sorted(key for key in required if key not in actual),
        "placeholder_keys": [key for key in placeholder if key in required],
    }


def _env_keys(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _summary(
    app: str,
    drifted: bool,
    diff: Dict[str, object],
    release_metadata: Dict[str, object],
    env_status: Dict[str, object],
) -> str:
    return (
        f"{app} {'has drift' if drifted else 'has no rendered drift'}: "
        f"{len(diff['changed_files'])} changed file(s), "
        f"{len(diff['removed_files'])} removed file(s), "
        f"{len(release_metadata['mismatches'])} release metadata mismatch(es), "
        f"{len(env_status['missing_keys'])} missing env key(s), "
        f"{len(env_status['placeholder_keys'])} placeholder env key(s)."
    )


def _file_hash(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered in {"", "replace-me", "changeme", "todo"} or "replace-me" in lowered
