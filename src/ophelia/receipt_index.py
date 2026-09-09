"""Receipt timeline read-model.

A thin, read-only projection over the receipts that already live in the runtime
root. :func:`receipt_timeline` wraps
:func:`ophelia.receipt_sources.receipt_records` (the single source of truth for
*where* receipts live and how they sort) and layers filtering, newest-first
ordering, and two extra per-entry facets:
``artifact_paths`` (names/paths only) and ``rollback_available``.

This module never raises on a bad receipt. Storage-source diagnostics and
payloads that cannot be parsed surface as structured ``warnings`` entries.
Unreadable identities are retained when the source can still prove their id.

No secret values are read or emitted: only artifact *paths*, a rollback boolean,
and the already-redaction-safe record fields from ``_receipt_records``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DEFAULT_RUNTIME_ROOT
from .operation_schema import SCHEMA_VERSION, issue
from .receipt_sources import receipt_payload, receipt_scan

KIND = "ophelia.receipt_timeline"


def receipt_timeline(
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    *,
    app: Optional[str] = None,
    environment: Optional[str] = None,
    operation: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> Dict[str, Any]:
    """Newest-first, filtered timeline of operation receipts.

    Filters compose with AND semantics. ``operation``/``status`` are exact
    matches; ``since``/``until`` are inclusive ISO date/datetime bounds compared
    lexically against each receipt's ``started_at`` (ISO timestamps sort
    correctly as strings). Malformed receipts become ``warnings`` and are
    skipped, never raised.
    """
    records, warnings = receipt_scan(runtime_root, app=app, environment=environment)

    entries: List[Dict[str, Any]] = []
    for record in records:
        if operation is not None and record.get("operation") != operation:
            continue
        if status is not None and record.get("status") != status:
            continue
        started_at = record.get("started_at")
        if not _within_bounds(started_at, since, until):
            continue
        entry = dict(record)
        facets = _receipt_facets(record, warnings)
        entry["artifact_paths"] = facets["artifact_paths"]
        entry["rollback_available"] = facets["rollback_available"]
        entries.append(entry)

    entries.sort(
        key=lambda item: (str(item.get("started_at") or ""), str(item.get("receipt_id") or "")),
        reverse=True,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "filters": {
            "app": app,
            "environment": environment,
            "operation": operation,
            "status": status,
            "since": since,
            "until": until,
        },
        "receipts": entries,
        "warnings": warnings,
        "summary": _summary(entries, warnings),
    }


def _within_bounds(started_at: object, since: Optional[str], until: Optional[str]) -> bool:
    if since is None and until is None:
        return True
    if not isinstance(started_at, str) or not started_at:
        # An undated receipt cannot satisfy a date filter; exclude it.
        return False
    if since is not None and started_at < since:
        return False
    if until is not None and started_at > _until_ceiling(until, started_at):
        return False
    return True


def _until_ceiling(until: str, started_at: str) -> str:
    """Make a bare ``until`` date inclusive of that whole day.

    A receipt stamped ``2026-06-21T09:00:00Z`` should still match
    ``until=2026-06-21``. When ``until`` carries no time component we compare
    against the date prefix of ``started_at`` instead of the full timestamp.
    """
    if "T" not in until and len(until) == 10 and len(started_at) >= 10:
        return until + started_at[10:]
    return until


def _receipt_facets(record: Dict[str, object], warnings: List[Dict[str, str]]) -> Dict[str, Any]:
    """Read artifact paths + rollback availability defensively.

    ``receipt_records`` already tolerated unreadable files; here we parse again
    so a payload that is not valid JSON, or whose ``artifacts``/``rollback`` keys
    have the wrong shape, becomes a warning rather than a crash.
    """
    facets: Dict[str, Any] = {"artifact_paths": [], "rollback_available": False}
    if record.get("payload_readable") is False:
        return facets
    payload = receipt_payload(record)
    if not isinstance(payload, dict):
        locator = str(record.get("path") or "unknown")
        warnings.append(issue("receipt_unreadable", "Receipt payload is not readable JSON.", locator))
        return facets

    terminal = payload.get("ophelia_receipt")
    receipt = terminal if isinstance(terminal, dict) else payload
    artifacts = receipt.get("artifacts")
    if isinstance(artifacts, list):
        for item in artifacts:
            if isinstance(item, dict):
                path_value = item.get("path")
                if isinstance(path_value, str) and path_value:
                    facets["artifact_paths"].append(path_value)
            elif isinstance(item, str) and item:
                facets["artifact_paths"].append(item)

    rollback = receipt.get("rollback")
    if isinstance(rollback, dict):
        facets["rollback_available"] = bool(rollback.get("available"))
    elif (
        receipt.get("kind") == "ophelia.kernel.terminal_receipt"
        and receipt.get("operation") in {"deploy.apply", "rollback.apply"}
    ):
        facets["rollback_available"] = bool(receipt.get("previous_revision_id"))
    return facets


def _summary(entries: List[Dict[str, Any]], warnings: List[Dict[str, str]]) -> str:
    base = f"{len(entries)} receipt(s) on the timeline"
    if warnings:
        base += f", {len(warnings)} warning(s)"
    return base + "."
