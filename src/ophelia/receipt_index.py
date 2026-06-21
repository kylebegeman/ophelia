"""Receipt timeline read-model.

A thin, read-only projection over the receipts that already live in the runtime
root. :func:`receipt_timeline` wraps :func:`ophelia.portability._receipt_records`
(the single source of truth for *where* receipts live and how they sort) and
layers filtering, newest-first ordering, and two extra per-entry facets:
``artifact_paths`` (names/paths only) and ``rollback_available``.

This module never raises on a bad receipt. ``_receipt_records`` already drops
malformed JSON silently; we re-read each receipt defensively here so a payload
that cannot be parsed (or whose ``artifacts``/``rollback`` shape is wrong)
surfaces as a structured ``warnings`` entry instead of crashing the timeline.

No secret values are read or emitted: only artifact *paths*, a rollback boolean,
and the already-redaction-safe record fields from ``_receipt_records``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DEFAULT_RUNTIME_ROOT
from .operation_schema import SCHEMA_VERSION, issue
from .portability import _receipt_records

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
    warnings: List[Dict[str, str]] = []
    records = _receipt_records(runtime_root, app=app, environment=environment)

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
        facets = _receipt_facets(Path(str(record.get("path"))), warnings)
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


def receipt_timeline_for_state(
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    *,
    app: Optional[str] = None,
    environment: Optional[str] = None,
    operation: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return only the receipt entries (no envelope), for downstream read-models."""
    return receipt_timeline(
        runtime_root,
        app=app,
        environment=environment,
        operation=operation,
        status=status,
        since=since,
        until=until,
    )["receipts"]


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


def _receipt_facets(path: Path, warnings: List[Dict[str, str]]) -> Dict[str, Any]:
    """Read artifact paths + rollback availability defensively.

    ``_receipt_records`` already tolerated unreadable files; here we parse again
    so a payload that is not valid JSON, or whose ``artifacts``/``rollback`` keys
    have the wrong shape, becomes a warning rather than a crash.
    """
    facets: Dict[str, Any] = {"artifact_paths": [], "rollback_available": False}
    try:
        raw = path.read_text()
    except OSError as exc:
        warnings.append(issue("receipt_unreadable", f"Could not read receipt: {exc}", str(path)))
        return facets
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        warnings.append(issue("receipt_unreadable", f"Receipt is not valid JSON: {exc}", str(path)))
        return facets
    if not isinstance(payload, dict):
        warnings.append(issue("receipt_unreadable", "Receipt payload is not a JSON object.", str(path)))
        return facets

    artifacts = payload.get("artifacts")
    if isinstance(artifacts, list):
        for item in artifacts:
            if isinstance(item, dict):
                name = item.get("path") or item.get("name")
                if isinstance(name, str) and name:
                    facets["artifact_paths"].append(name)
            elif isinstance(item, str) and item:
                facets["artifact_paths"].append(item)

    rollback = payload.get("rollback")
    if isinstance(rollback, dict):
        facets["rollback_available"] = bool(rollback.get("available"))
    return facets


def _summary(entries: List[Dict[str, Any]], warnings: List[Dict[str, str]]) -> str:
    base = f"{len(entries)} receipt(s) on the timeline"
    if warnings:
        base += f", {len(warnings)} unreadable receipt(s) skipped"
    return base + "."
