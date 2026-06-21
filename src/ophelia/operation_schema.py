from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def operation_id(operation: str, app: Optional[str] = None, environment: Optional[str] = None) -> str:
    parts = [operation]
    if app:
        parts.append(app)
    if environment:
        parts.append(environment)
    parts.append(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    return ".".join(_slug(part) for part in parts if part)


def issue(code: str, message: str, path: Optional[str] = None) -> Dict[str, str]:
    payload = {"code": code, "message": message}
    if path:
        payload["path"] = path
    return payload


def artifact(path: str, kind: str, description: Optional[str] = None, present: Optional[bool] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"path": path, "kind": kind}
    if description:
        payload["description"] = description
    if present is not None:
        payload["present"] = present
    return payload


def diff_artifact(
    name: str,
    path: object,
    description: Optional[str] = None,
    redacted: bool = True,
    media_type: str = "text/x-diff",
) -> Dict[str, Any]:
    """Describe a redacted diff written to disk (e.g. a rendered Compose diff).

    The artifact carries only the *path* to the diff file, never inline diff
    content, so a plan/report that embeds it stays secret-free in JSON. The diff
    file itself must already have been redacted before being written.
    """
    payload: Dict[str, Any] = {
        "name": name,
        "kind": "ophelia.artifact.diff",
        "media_type": media_type,
        "path": str(path),
        "redacted": redacted,
    }
    if description is not None:
        payload["description"] = description
    return payload


def plan_envelope(
    operation: str,
    app: Optional[str],
    environment: Optional[str],
    summary: str,
    blockers: List[Any],
    warnings: List[Any],
    checks: List[Dict[str, Any]],
    artifacts: List[Dict[str, Any]],
    confirmation_required: bool,
    confirmation_token: Optional[str] = None,
    exact_apply_input: Optional[Dict[str, Any]] = None,
    risk: str = "medium",
    changes: Optional[List[Dict[str, Any]]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "ophelia.plan",
        "operation": operation,
        "operation_id": operation_id(operation, app, environment),
        "app": app,
        "environment": environment,
        "risk": risk,
        "dry_run": True,
        "summary": summary,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
        "changes": changes or [],
        "artifacts": artifacts,
        "confirmation_required": confirmation_required,
        "confirmation_token": confirmation_token,
        "exact_apply_input": exact_apply_input,
    }
    payload.update(extra)
    return payload


def report_envelope(
    operation: str,
    app: Optional[str],
    environment: Optional[str],
    summary: str,
    blockers: Optional[List[Any]] = None,
    warnings: Optional[List[Any]] = None,
    checks: Optional[List[Dict[str, Any]]] = None,
    artifacts: Optional[List[Dict[str, Any]]] = None,
    status: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "ophelia.report",
        "operation": operation,
        "operation_id": operation_id(operation, app, environment),
        "status": status or ("blocked" if blockers else "ok"),
        "app": app,
        "environment": environment,
        "summary": summary,
        "blockers": blockers or [],
        "warnings": warnings or [],
        "checks": checks or [],
        "artifacts": artifacts or [],
        "inputs_redacted": True,
    }
    payload.update(extra)
    return payload


def receipt_envelope(
    operation: str,
    app: Optional[str],
    environment: Optional[str],
    status: str,
    started_at: str,
    completed_at: str,
    artifacts: Optional[List[Dict[str, Any]]] = None,
    checks: Optional[List[Dict[str, Any]]] = None,
    rollback: Optional[Dict[str, Any]] = None,
    plan_operation_id: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "ophelia.receipt",
        "operation": operation,
        "operation_id": operation_id(operation, app, environment),
        "plan_operation_id": plan_operation_id,
        "status": status,
        "app": app,
        "environment": environment,
        "started_at": started_at,
        "completed_at": completed_at,
        "actor": "operator",
        "inputs_redacted": True,
        "artifacts": artifacts or [],
        "checks": checks or [],
        "rollback": rollback or {"available": False, "note": "No mutation was performed."},
    }
    payload.update(extra)
    return payload


def error_envelope(
    message: str,
    code: str,
    blockers: Optional[List[Any]] = None,
    warnings: Optional[List[Any]] = None,
    next_actions: Optional[List[Any]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Standard error payload (`kind: "ophelia.error"`).

    Matches the shape already emitted by ``api.py:_error`` so CLI and HTTP error
    bodies are identical. ``blockers`` defaults to a single entry derived from
    ``code``/``message``; callers may pass a richer list.
    """
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "ophelia.error",
        "status": "failed",
        "error": message,
        "blockers": blockers if blockers is not None else [{"code": code, "message": message}],
        "warnings": warnings or [],
    }
    if next_actions is not None:
        payload["next_actions"] = next_actions
    payload.update(extra)
    return payload


def token(action: str, payload: Dict[str, Any]) -> str:
    encoded = json.dumps({"action": action, **payload}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def _slug(value: str) -> str:
    return "".join(character if character.isalnum() or character in {"-", "_"} else "-" for character in value)
