from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .redaction import deep_redact, redact_command_string


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def operation_id(operation: str, app: Optional[str] = None, environment: Optional[str] = None) -> str:
    parts = [operation]
    if app:
        parts.append(app)
    if environment:
        parts.append(environment)
    parts.append(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    parts.append(secrets.token_hex(3))
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


def attach_digest(
    payload: Dict[str, Any],
    *,
    operation: Optional[str] = None,
    risk: Optional[str] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """Attach a compact, redacted operator digest to a plan/report/receipt.

    The digest is intentionally redundant with the full payload. It gives humans,
    operator UIs, and downstream agents a bounded summary of operation identity, risk,
    confirmation/rollback posture, and blocker/warning counts without requiring
    every consumer to understand every operation-specific field.
    """
    if "digest" not in payload:
        payload["digest"] = operation_digest(payload, operation=operation, risk=risk, status=status)
    return payload


def operation_digest(
    payload: Dict[str, Any],
    *,
    operation: Optional[str] = None,
    risk: Optional[str] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a scalar-only summary for an operation payload."""
    blockers = _list_value(payload.get("blockers"))
    warnings = _list_value(payload.get("warnings"))
    checks = _list_value(payload.get("checks"))
    artifacts = _list_value(payload.get("artifacts"))
    changes = _list_value(payload.get("changes"))
    exact_apply_input = payload.get("exact_apply_input")
    rollback = payload.get("rollback") if isinstance(payload.get("rollback"), dict) else {}

    confirmation_required = bool(payload.get("confirmation_required"))
    confirmation_token = payload.get("confirmation_token")
    digest: Dict[str, Any] = {
        "operation": operation or payload.get("operation"),
        "app": payload.get("app"),
        "environment": payload.get("environment"),
        "risk": risk or payload.get("risk"),
        "status": status or _digest_status(payload, blockers, warnings),
        "summary": payload.get("summary"),
        "counts": {
            "blockers": len(blockers),
            "warnings": len(warnings),
            "checks": len(checks),
            "artifacts": len(artifacts),
            "changes": len(changes),
        },
        "blocker_codes": _issue_codes(blockers),
        "warning_codes": _issue_codes(warnings),
        "confirmation": {
            "required": confirmation_required,
            "token_present": bool(confirmation_token),
            "apply_command": _apply_command_preview(exact_apply_input),
        },
        "mutation": {
            "dry_run": bool(payload.get("dry_run")),
            "changes_planned": len(changes),
            "artifacts_planned": len(artifacts),
        },
        "rollback": {
            "available": bool(rollback.get("available")),
            "note": rollback.get("note") if isinstance(rollback.get("note"), str) else None,
        },
    }
    return deep_redact(digest)


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
    return attach_digest(payload)


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
    return attach_digest(payload)


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
    return attach_digest(payload)


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


def _digest_status(payload: Dict[str, Any], blockers: List[Any], warnings: List[Any]) -> str:
    status = payload.get("status")
    if isinstance(status, str) and status:
        return status
    if blockers:
        return "blocked"
    if payload.get("confirmation_required"):
        return "awaiting_confirmation"
    if warnings:
        return "warning"
    return "ready"


def _list_value(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _issue_codes(items: List[Any]) -> List[str]:
    codes: List[str] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("code"), str):
            codes.append(item["code"])
    return codes[:10]


def _apply_command_preview(exact_apply_input: Any) -> Optional[str]:
    if not isinstance(exact_apply_input, dict):
        return None
    command = exact_apply_input.get("command")
    if isinstance(command, str) and command:
        return redact_command_string(command)
    return None
