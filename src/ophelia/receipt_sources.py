"""Unified, read-only discovery for Ophelia receipt storage backends.

Legacy and portability operations persist JSON files below app, backup, and
global receipt roots. The 0.6 execution kernel persists authoritative terminal
receipts in ``host-state/operations.db``. Product-operation receipts wrap that
same terminal contract in a correlated JSON file. This module projects every
backend into one small record shape without creating files or opening SQLite in
write mode.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union
from urllib.parse import quote


JOURNAL_RELATIVE_PATH = Path("host-state") / "operations.db"
PRODUCT_RECEIPT_KINDS = {
    "ophelia.product-operation-receipt",
    "ophelia.product-recovery-receipt",
}
JOURNAL_REQUIRED_COLUMNS = {
    "receipt_id",
    "operation_id",
    "outcome",
    "receipt_digest",
    "payload_json",
}


class _JournalSchemaError(Exception):
    pass


def receipt_records(
    runtime_root: Path,
    app: Optional[str] = None,
    environment: Optional[str] = None,
) -> List[Dict[str, object]]:
    """Return a deterministic projection across file and journal receipts."""
    records, _warnings = receipt_scan(runtime_root, app=app, environment=environment)
    return records


def receipt_scan(
    runtime_root: Path,
    app: Optional[str] = None,
    environment: Optional[str] = None,
) -> Tuple[List[Dict[str, object]], List[Dict[str, str]]]:
    """Return projected receipts plus structured storage warnings."""
    runtime_root = Path(runtime_root)
    file_records, file_warnings = _file_receipt_records(
        runtime_root,
        None,
        None,
    )
    warnings: List[Dict[str, str]] = [*file_warnings]
    records = [*file_records]
    journal_records, journal_warnings = _journal_receipt_records(
        runtime_root,
        None,
        None,
    )
    records.extend(journal_records)
    warnings.extend(journal_warnings)

    # A correlated product receipt may wrap a terminal receipt also present in
    # the journal. Keep one stable identity, expose both storage sources, and
    # prefer the richer digest-correlated product wrapper for display. A plain
    # journal row remains preferred over an unrelated legacy file collision.
    priorities = {"file": 1, "journal": 2, "product": 3}
    by_id: Dict[str, Dict[str, object]] = {}
    for record in records:
        receipt_id = str(record.get("receipt_id") or "")
        if not receipt_id:
            continue
        existing = by_id.get(receipt_id)
        if existing is None:
            by_id[receipt_id] = _record_with_source_metadata(record)
            continue
        current_priority = priorities.get(str(record.get("source")), 0)
        existing_priority = priorities.get(str(existing.get("source")), 0)
        prefer_current = current_priority > existing_priority or (
            current_priority == existing_priority
            and str(record.get("path") or "") < str(existing.get("path") or "")
        )
        primary = record if prefer_current else existing
        secondary = existing if prefer_current else record
        product_record = _record_for_source(existing, record, "product")
        journal_record = _record_for_source(existing, record, "journal")
        if product_record is not None and journal_record is not None:
            if product_record.get("payload_readable") is False:
                primary = journal_record
                secondary = product_record
            else:
                integrity_warning = _product_journal_integrity_warning(
                    product_record,
                    journal_record,
                )
                if integrity_warning is not None:
                    warnings.append(integrity_warning)
                    primary = journal_record
                    secondary = product_record
        elif not _receipt_payloads_equivalent(existing, record):
            warnings.append(
                _source_warning(
                    "receipt_identity_collision",
                    f"Receipt id '{receipt_id}' resolves to different payloads; a deterministic primary record was retained.",
                    str(primary.get("path") or "unknown"),
                )
            )
        by_id[receipt_id] = _merge_source_metadata(primary, secondary)

    merged_records = [
        record
        for record in by_id.values()
        if (app is None or record.get("app") == app)
        and (environment is None or record.get("environment") == environment)
    ]
    return (
        sorted(
            merged_records,
            key=lambda item: (
                str(item.get("started_at") or item.get("completed_at") or ""),
                str(item.get("receipt_id") or ""),
            ),
        ),
        warnings,
    )


def receipt_payload(record_or_path: Union[Mapping[str, object], Path, str]) -> Optional[Dict[str, Any]]:
    """Load one receipt payload from its projected record or JSON path."""
    if isinstance(record_or_path, Mapping):
        if record_or_path.get("payload_readable") is False:
            return None
        source = str(record_or_path.get("source") or "")
        locator = str(record_or_path.get("path") or "")
        receipt_id = record_or_path.get("receipt_id")
        storage_path = record_or_path.get("storage_path")
        if source == "journal" or locator.startswith("sqlite:"):
            database = Path(str(storage_path)) if storage_path else _journal_path_from_locator(locator)
            if database is None or not isinstance(receipt_id, str) or not receipt_id:
                return None
            return _journal_receipt_payload(database, receipt_id)
        payload = _read_json(Path(locator))
        if _product_wrapper_integrity_warning(Path(locator), payload) is not None:
            return None
        return payload

    value = str(record_or_path)
    if value.startswith("sqlite:"):
        database = _journal_path_from_locator(value)
        receipt_id = value.rpartition("/")[2]
        if database is None or not receipt_id:
            return None
        return _journal_receipt_payload(database, receipt_id)
    path = Path(value).expanduser()
    payload = _read_json(path)
    if _product_wrapper_integrity_warning(path, payload) is not None:
        return None
    return payload


def _file_receipt_records(
    runtime_root: Path,
    app: Optional[str],
    environment: Optional[str],
) -> Tuple[List[Dict[str, object]], List[Dict[str, str]]]:
    roots: List[Tuple[Optional[str], Optional[str], Path, str]] = []
    warnings: List[Dict[str, str]] = []
    apps_root = runtime_root / "apps"
    if apps_root.exists() and not _is_safe_runtime_path(apps_root, runtime_root):
        warnings.append(
            _source_warning(
                "receipt_unsafe_path",
                "The app receipt root crosses a symlink or leaves the runtime root and was not scanned.",
                str(apps_root),
            )
        )
    elif apps_root.exists():
        for app_root in sorted(path for path in apps_root.iterdir() if path.is_dir()):
            if app and app_root.name != app:
                continue
            if not _is_safe_runtime_path(app_root, runtime_root):
                warnings.append(
                    _source_warning(
                        "receipt_unsafe_path",
                        "An app receipt directory crosses a symlink or leaves the runtime root and was not scanned.",
                        str(app_root),
                    )
                )
                continue
            roots.append((app_root.name, None, app_root / "receipts", "file"))
            roots.append((app_root.name, None, app_root / "restore-drills", "file"))
            for env_root in sorted(path for path in app_root.iterdir() if path.is_dir()):
                if environment and env_root.name != environment:
                    continue
                roots.append((app_root.name, env_root.name, env_root / "receipts", "file"))
            roots.append((app_root.name, None, app_root / "rollback-reports", "file"))

    backups_root = runtime_root / "backups" / "apps"
    if backups_root.exists() and not _is_safe_runtime_path(backups_root, runtime_root):
        warnings.append(
            _source_warning(
                "receipt_unsafe_path",
                "The backup receipt root crosses a symlink or leaves the runtime root and was not scanned.",
                str(backups_root),
            )
        )
    elif app is None or (backups_root / app).exists():
        backup_apps = (
            [backups_root / app]
            if app
            else sorted(backups_root.glob("*")) if backups_root.exists() else []
        )
        for backup_app_root in backup_apps:
            if not _is_safe_runtime_path(backup_app_root, runtime_root):
                warnings.append(
                    _source_warning(
                        "receipt_unsafe_path",
                        "A backup app directory crosses a symlink or leaves the runtime root and was not scanned.",
                        str(backup_app_root),
                    )
                )
                continue
            # Backup archives may contain arbitrary application JSON. Only the
            # fixed Ophelia metadata names are receipts; never recursively
            # index backed-up runtime or customer data.
            for metadata_name in ("backup-manifest.json", "restore-report.json"):
                for metadata_path in sorted(backup_app_root.glob(f"*/{metadata_name}")):
                    roots.append(
                        (
                            backup_app_root.name,
                            environment,
                            metadata_path,
                            "file",
                        )
                    )

    # Product-operation receipts are intentionally host-global because they
    # correlate product and kernel identities. App/environment are recovered
    # from their nested terminal receipt before filters are applied.
    roots.append((None, None, runtime_root / "receipts" / "product", "product"))

    records: List[Dict[str, object]] = []
    for owner_app, owner_env, root, default_source in roots:
        if root.is_symlink() or (
            root.exists() and not _is_safe_runtime_path(root, runtime_root)
        ):
            warnings.append(
                _source_warning(
                    "receipt_unsafe_path",
                    "A receipt discovery path crosses a symlink or leaves the runtime root and was not read.",
                    str(root),
                )
            )
            continue
        if not root.exists():
            continue
        paths = [root] if root.is_file() else sorted(root.rglob("*.json"))
        for path in paths:
            if not path.is_file():
                continue
            if not _is_safe_runtime_path(path, runtime_root):
                warnings.append(
                    _source_warning(
                        "receipt_unsafe_path",
                        "A discovered receipt crosses a symlink or leaves the runtime root and was not read.",
                        str(path),
                    )
                )
                continue
            payload = _read_json(path)
            if payload is None:
                record = {
                    "receipt_id": _receipt_id(path, {}),
                    "operation": _operation_from_receipt_path(path),
                    "status": "invalid",
                    "app": owner_app,
                    "environment": owner_env,
                    "started_at": None,
                    "completed_at": None,
                    "path": str(path),
                    "source": default_source,
                    "sources": [default_source],
                    "inputs_redacted": True,
                    "payload_readable": False,
                }
                if app is not None and record.get("app") != app:
                    continue
                if environment is not None and record.get("environment") != environment:
                    continue
                records.append(record)
                warnings.append(
                    _source_warning(
                        "receipt_unreadable",
                        "A receipt file is not readable JSON.",
                        str(path),
                    )
                )
                continue
            source = (
                "product"
                if payload.get("kind") in PRODUCT_RECEIPT_KINDS
                else default_source
            )
            product_warning = _product_wrapper_integrity_warning(path, payload)
            if product_warning is not None:
                warnings.append(product_warning)
                nested = payload.get("ophelia_receipt")
                identity_payload = nested if isinstance(nested, dict) else payload
                record = {
                    "receipt_id": _receipt_id(path, identity_payload),
                    "operation": "product.receipt",
                    "status": "invalid",
                    "app": None,
                    "environment": None,
                    "started_at": None,
                    "completed_at": None,
                    "path": str(path),
                    "source": "product",
                    "sources": ["product"],
                    "inputs_redacted": True,
                    "payload_readable": False,
                }
                if app is None and environment is None:
                    records.append(record)
                continue
            record = _record_from_payload(
                path,
                payload,
                owner_app=owner_app,
                owner_environment=owner_env,
                source=source,
            )
            if app is not None and record.get("app") != app:
                continue
            if environment is not None and record.get("environment") != environment:
                continue
            records.append(record)
    return records, warnings


def _journal_receipt_records(
    runtime_root: Path,
    app: Optional[str],
    environment: Optional[str],
) -> Tuple[List[Dict[str, object]], List[Dict[str, str]]]:
    database = runtime_root / JOURNAL_RELATIVE_PATH
    if database.exists() and not _is_safe_runtime_path(database, runtime_root):
        return [], [
            _source_warning(
                "receipt_journal_unsafe_path",
                "The kernel receipt journal path crosses a symlink or leaves the runtime root and was not opened.",
                str(database),
            )
        ]
    if not database.exists() or not database.is_file():
        return [], []
    rows, warnings = _journal_rows(database)
    records: List[Dict[str, object]] = []
    for stored_id, payload in rows:
        if payload is None:
            record = _invalid_journal_record(database, stored_id)
            if app is None and environment is None:
                records.append(record)
            continue
        contract_warning = _journal_contract_warning(database, stored_id, payload)
        if contract_warning is not None:
            warnings.append(contract_warning)
            record = _invalid_journal_record(database, stored_id)
            if app is None and environment is None:
                records.append(record)
            continue
        record = _record_from_payload(
            database,
            payload,
            source="journal",
            stored_receipt_id=stored_id,
        )
        receipt_id = str(record["receipt_id"])
        record["storage_path"] = str(database)
        record["path"] = _journal_locator(database, receipt_id)
        if app is not None and record.get("app") != app:
            continue
        if environment is not None and record.get("environment") != environment:
            continue
        records.append(record)
    return records, warnings


def _invalid_journal_record(database: Path, receipt_id: str) -> Dict[str, object]:
    return {
        "receipt_id": receipt_id,
        "operation": "kernel.receipt",
        "status": "invalid",
        "app": None,
        "environment": None,
        "started_at": None,
        "completed_at": None,
        "path": _journal_locator(database, receipt_id),
        "storage_path": str(database),
        "source": "journal",
        "sources": ["journal"],
        "inputs_redacted": True,
        "payload_readable": False,
    }


def _journal_contract_warning(
    database: Path,
    stored_id: str,
    payload: Mapping[str, object],
) -> Optional[Dict[str, str]]:
    locator = _journal_locator(database, stored_id)
    if payload.get("receipt_id") != stored_id:
        return _source_warning(
            "receipt_journal_identity_mismatch",
            "A kernel receipt row id does not match its payload identity.",
            locator,
        )
    if payload.get("kind") != "ophelia.kernel.terminal_receipt":
        return _source_warning(
            "receipt_journal_contract_invalid",
            "A kernel receipt row has an unsupported payload kind.",
            locator,
        )
    if payload.get("inputs_redacted") is not True:
        return _source_warning(
            "receipt_journal_contract_invalid",
            "A kernel receipt row does not prove inputs_redacted=true.",
            locator,
        )
    return None


def _record_with_source_metadata(record: Mapping[str, object]) -> Dict[str, object]:
    result = dict(record)
    source = str(result.get("source") or "file")
    result["sources"] = sorted(
        {
            source,
            *(
                str(item)
                for item in result.get("sources", [])
                if isinstance(item, str) and item
            ),
        }
    )
    locator = result.get("path")
    result["receipt_locators"] = [str(locator)] if locator else []
    _add_source_locator(result, record)
    return result


def _record_for_source(
    first: Mapping[str, object],
    second: Mapping[str, object],
    source: str,
) -> Optional[Mapping[str, object]]:
    if first.get("source") == source:
        return first
    if second.get("source") == source:
        return second
    return None


def _receipt_payloads_equivalent(
    first: Mapping[str, object],
    second: Mapping[str, object],
) -> bool:
    first_payload = receipt_payload(first)
    second_payload = receipt_payload(second)
    return (
        isinstance(first_payload, dict)
        and isinstance(second_payload, dict)
        and first_payload == second_payload
    )


def _product_wrapper_integrity_warning(
    path: Path,
    payload: Optional[Mapping[str, object]],
) -> Optional[Dict[str, str]]:
    if not isinstance(payload, Mapping) or payload.get("kind") not in PRODUCT_RECEIPT_KINDS:
        return None
    nested = payload.get("ophelia_receipt")
    valid = isinstance(nested, Mapping)
    if valid:
        valid = nested.get("kind") == "ophelia.kernel.terminal_receipt"
    if valid:
        valid = isinstance(nested.get("receipt_id"), str) and bool(nested.get("receipt_id"))
    if valid:
        expected_digest = payload.get("ophelia_receipt_digest")
        valid = isinstance(expected_digest, str) and expected_digest == _payload_digest(nested)
    if valid:
        valid = payload.get("operation") == nested.get("operation")
    if valid:
        nested_status = nested.get("status") or nested.get("outcome")
        valid = payload.get("status") == nested_status
    if valid and payload.get("app") is not None:
        valid = payload.get("app") == nested.get("app")
    if valid and payload.get("environment") is not None:
        valid = payload.get("environment") == nested.get("environment")
    if valid and payload.get("product_id") is not None:
        valid = payload.get("product_id") == nested.get("app")
    if valid:
        valid = payload.get("inputs_redacted") is True and nested.get("inputs_redacted") is True
    if valid:
        return None
    return _source_warning(
        "receipt_product_integrity_failed",
        "A product receipt wrapper is missing or mismatches its nested kernel receipt integrity contract.",
        str(path),
    )


def _product_journal_integrity_warning(
    product_record: Mapping[str, object],
    journal_record: Mapping[str, object],
) -> Optional[Dict[str, str]]:
    product_payload = receipt_payload(product_record)
    journal_payload = receipt_payload(journal_record)
    nested = product_payload.get("ophelia_receipt") if isinstance(product_payload, dict) else None
    valid = isinstance(nested, dict) and isinstance(journal_payload, dict) and nested == journal_payload
    if valid:
        expected_digest = product_payload.get("ophelia_receipt_digest")
        valid = isinstance(expected_digest, str) and expected_digest == _payload_digest(nested)
    if valid:
        valid = product_payload.get("operation") in (None, journal_payload.get("operation"))
    if valid:
        journal_status = journal_payload.get("status") or journal_payload.get("outcome")
        valid = product_payload.get("status") in (None, journal_status)
    if valid:
        valid = product_payload.get("app") in (None, journal_payload.get("app"))
    if valid:
        valid = product_payload.get("environment") in (
            None,
            journal_payload.get("environment"),
        )
    if valid:
        return None
    return _source_warning(
        "receipt_correlation_integrity_failed",
        "A correlated product receipt does not match its authoritative kernel journal receipt; the journal payload was retained.",
        str(journal_record.get("path") or "unknown"),
    )


def _payload_digest(payload: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return ""
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _merge_source_metadata(
    primary: Mapping[str, object],
    secondary: Mapping[str, object],
) -> Dict[str, object]:
    result = _record_with_source_metadata(primary)
    sources = {
        *(
            str(item)
            for item in result.get("sources", [])
            if isinstance(item, str) and item
        ),
        str(secondary.get("source") or "file"),
        *(
            str(item)
            for item in secondary.get("sources", [])
            if isinstance(item, str) and item
        ),
    }
    result["sources"] = sorted(source for source in sources if source)
    locators = {
        *(
            str(item)
            for item in result.get("receipt_locators", [])
            if isinstance(item, str) and item
        ),
        *(
            str(item)
            for item in secondary.get("receipt_locators", [])
            if isinstance(item, str) and item
        ),
    }
    secondary_locator = secondary.get("path")
    if secondary_locator:
        locators.add(str(secondary_locator))
    result["receipt_locators"] = sorted(locators)
    if secondary.get("payload_readable") is not False:
        result["aliases"] = sorted(
            {
                *(
                    str(item)
                    for item in result.get("aliases", [])
                    if isinstance(item, str) and item
                ),
                *(
                    str(item)
                    for item in secondary.get("aliases", [])
                    if isinstance(item, str) and item
                ),
            }
        )
    _add_source_locator(result, secondary)
    return result


def _add_source_locator(
    result: Dict[str, object],
    source_record: Mapping[str, object],
) -> None:
    source = str(source_record.get("source") or "file")
    locator = source_record.get("path")
    if source == "journal" and locator:
        result["journal_locator"] = locator
    elif source == "product" and locator:
        result["product_receipt_path"] = locator
    elif source == "file" and locator:
        result.setdefault("file_receipt_path", locator)


def _record_from_payload(
    path: Path,
    payload: Mapping[str, object],
    *,
    owner_app: Optional[str] = None,
    owner_environment: Optional[str] = None,
    source: str,
    stored_receipt_id: Optional[str] = None,
) -> Dict[str, object]:
    nested = payload.get("ophelia_receipt")
    terminal = nested if isinstance(nested, dict) else payload
    receipt_id = _receipt_id(path, terminal, stored_receipt_id)
    aliases = {
        value
        for candidate in (payload, terminal)
        for key in ("verify_id", "restore_drill_id")
        for value in (candidate.get(key),)
        if isinstance(value, str) and value and value != receipt_id
    }
    operation = payload.get("operation") or terminal.get("operation") or _operation_from_receipt_path(path)
    status = payload.get("status") or terminal.get("status") or terminal.get("outcome") or "unknown"
    return {
        "receipt_id": receipt_id,
        "operation": operation,
        "status": status,
        "app": payload.get("app") or terminal.get("app") or owner_app,
        "environment": payload.get("environment") or terminal.get("environment") or owner_environment,
        "started_at": (
            payload.get("started_at")
            or terminal.get("started_at")
            or payload.get("created_at")
            or payload.get("applied_at")
        ),
        "completed_at": (
            payload.get("completed_at")
            or terminal.get("completed_at")
            or payload.get("created_at")
            or payload.get("applied_at")
        ),
        "path": str(path),
        "source": source,
        "inputs_redacted": payload.get(
            "inputs_redacted",
            terminal.get("inputs_redacted", True),
        ),
        "aliases": sorted(aliases),
    }


def _receipt_id(
    path: Path,
    payload: Mapping[str, object],
    stored_receipt_id: Optional[str] = None,
) -> str:
    if stored_receipt_id:
        return stored_receipt_id
    for key in ("receipt_id", "operation_id", "backup_id", "rollback_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:10]
    return f"{path.stem}-{digest}"


def _operation_from_receipt_path(path: Path) -> str:
    if path.name == "backup-manifest.json":
        return "backup.create"
    if path.name == "restore-report.json":
        return "restore.apply"
    if "rollback" in path.parts:
        return "rollback.apply"
    return path.stem.replace("-", ".")


def _journal_rows(
    database: Path,
) -> Tuple[List[Tuple[str, Optional[Dict[str, Any]]]], List[Dict[str, str]]]:
    warnings: List[Dict[str, str]] = []
    connection = _open_read_only(database)
    if connection is None:
        warnings.append(
            _source_warning(
                "receipt_journal_unreadable",
                "The kernel receipt journal could not be opened read-only.",
                str(database),
            )
        )
        return [], warnings
    try:
        columns = _journal_columns(connection)
        selected_columns = _journal_selected_columns(columns)
        rows = connection.execute(
            f"SELECT {', '.join(selected_columns)} FROM terminal_receipts ORDER BY receipt_id"
        ).fetchall()
    except _JournalSchemaError as exc:
        warnings.append(
            _source_warning(
                "receipt_journal_schema_invalid",
                f"The kernel receipt journal schema is incomplete: {exc}.",
                str(database),
            )
        )
        return [], warnings
    except sqlite3.Error as exc:
        warnings.append(
            _source_warning(
                "receipt_journal_unreadable",
                f"The kernel receipt journal could not be queried: {exc}.",
                str(database),
            )
        )
        return [], warnings
    finally:
        connection.close()
    result: List[Tuple[str, Optional[Dict[str, Any]]]] = []
    for raw_row in rows:
        row = dict(zip(selected_columns, raw_row))
        stored_id = row.get("receipt_id")
        raw_payload = row.get("payload_json")
        if not isinstance(stored_id, str) or not stored_id:
            warnings.append(
                _source_warning(
                    "receipt_journal_identity_invalid",
                    "A kernel receipt row has no usable receipt id.",
                    str(database),
                )
            )
            continue
        try:
            payload = _loads_json(raw_payload)
        except (TypeError, ValueError, UnicodeDecodeError):
            warnings.append(
                _source_warning(
                    "receipt_journal_payload_invalid",
                    "A kernel receipt payload is not valid JSON.",
                    _journal_locator(database, stored_id),
                )
            )
            result.append((stored_id, None))
            continue
        if isinstance(payload, dict):
            integrity_warning = _journal_storage_integrity_warning(
                database,
                stored_id,
                payload,
                row,
            )
            if integrity_warning is not None:
                warnings.append(integrity_warning)
                result.append((stored_id, None))
                continue
            result.append((stored_id, payload))
            continue
        warnings.append(
            _source_warning(
                "receipt_journal_payload_invalid",
                "A kernel receipt payload is not a JSON object.",
                _journal_locator(database, stored_id),
            )
        )
        result.append((stored_id, None))
    return result, warnings


def _journal_receipt_payload(database: Path, receipt_id: str) -> Optional[Dict[str, Any]]:
    if not _is_safe_runtime_path(database, database.parent.parent):
        return None
    connection = _open_read_only(database)
    if connection is None:
        return None
    try:
        columns = _journal_columns(connection)
        selected_columns = _journal_selected_columns(columns)
        raw_row = connection.execute(
            f"SELECT {', '.join(selected_columns)} FROM terminal_receipts WHERE receipt_id = ?",
            (receipt_id,),
        ).fetchone()
    except (_JournalSchemaError, sqlite3.Error):
        return None
    finally:
        connection.close()
    if raw_row is None:
        return None
    row = dict(zip(selected_columns, raw_row))
    try:
        payload = _loads_json(row.get("payload_json"))
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if (
        _journal_storage_integrity_warning(
            database,
            receipt_id,
            payload,
            row,
        )
        is not None
    ):
        return None
    if _journal_contract_warning(database, receipt_id, payload) is not None:
        return None
    return payload


def _journal_columns(connection: sqlite3.Connection) -> List[str]:
    columns = [
        str(row[1])
        for row in connection.execute("PRAGMA table_info(terminal_receipts)").fetchall()
        if len(row) > 1 and isinstance(row[1], str)
    ]
    missing = sorted(JOURNAL_REQUIRED_COLUMNS.difference(columns))
    if missing:
        raise _JournalSchemaError(
            "terminal_receipts is missing " + ", ".join(missing)
        )
    return columns


def _journal_selected_columns(columns: List[str]) -> List[str]:
    return [
        column
        for column in (
            "receipt_id",
            "payload_json",
            "receipt_digest",
            "operation_id",
            "outcome",
        )
        if column in columns
    ]


def _journal_storage_integrity_warning(
    database: Path,
    receipt_id: str,
    payload: Mapping[str, object],
    row: Mapping[str, object],
) -> Optional[Dict[str, str]]:
    valid = row.get("receipt_digest") == _payload_digest(payload)
    if valid:
        valid = row.get("operation_id") == payload.get("operation_id")
    if valid:
        valid = row.get("outcome") == payload.get("outcome")
    if valid:
        return None
    return _source_warning(
        "receipt_journal_integrity_failed",
        "A kernel receipt row does not match its stored digest or authoritative columns.",
        _journal_locator(database, receipt_id),
    )


def _open_read_only(database: Path) -> Optional[sqlite3.Connection]:
    if (
        not database.exists()
        or not database.is_file()
        or not _is_safe_runtime_path(database, database.parent.parent)
    ):
        return None
    uri = f"file:{quote(str(database.resolve()))}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None


def _journal_locator(database: Path, receipt_id: str) -> str:
    return f"sqlite:{database.resolve()}#terminal_receipts/{receipt_id}"


def _journal_path_from_locator(locator: str) -> Optional[Path]:
    if not locator.startswith("sqlite:") or "#terminal_receipts/" not in locator:
        return None
    raw_path = locator[len("sqlite:") :].split("#terminal_receipts/", 1)[0]
    return Path(raw_path) if raw_path else None


def _is_safe_runtime_path(path: Path, runtime_root: Path) -> bool:
    """Reject implicit discovery through symlinks or outside ``runtime_root``."""
    try:
        root_absolute = runtime_root.absolute()
        path_absolute = path.absolute()
        relative = path_absolute.relative_to(root_absolute)
    except (OSError, RuntimeError, ValueError):
        return False

    current = root_absolute
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False

    try:
        resolved_root = runtime_root.resolve()
        resolved_path = path.resolve()
        resolved_path.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = _loads_json(path.read_bytes())
    except (OSError, TypeError, ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _loads_json(raw: object) -> object:
    return json.loads(raw, parse_constant=_reject_nonfinite_json)


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not allowed: {value}")


def _source_warning(code: str, message: str, path: str) -> Dict[str, str]:
    return {"code": code, "message": message, "path": path}
