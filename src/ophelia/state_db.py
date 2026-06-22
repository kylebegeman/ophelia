"""Runtime state read-model backed by a local SQLite index.

This module builds and queries a *local* SQLite database that indexes what
Ophelia already wrote to the runtime root: apps, environments, manifest locks,
routes, releases, receipts, artifacts, backups, restore drills, provider
configs, and so on. The database is a **cache**, never a source of truth: it is
rebuilt deterministically from the on-disk files by :func:`rebuild_state`, and
nothing in this module ever reads from the VPS, performs SSH, or mutates the
runtime files. The only thing written is the index file itself
(``<runtime_root>/state/ophelia-state.sqlite3``), which is a local index
mutation, not a production/VPS mutation.

Secret safety
-------------
Runtime ``manifest.lock.json`` files carry *raw* env values (root ``env`` and
per-service ``services.<name>.env``). Before any manifest/payload is stored in
the database, env dicts are routed through
:func:`ophelia.portability._redact_manifest_lock`, which masks every env value
and sets ``secret_values_redacted: True``. Receipts and backups are already
redaction-safe at the source (``inputs_redacted``/``secrets_redacted_in_report``)
but each payload is additionally swept with :func:`ophelia.redaction.redact_mapping`
on any nested ``env`` mapping before it is stored. No raw env value is ever
written to the database.

Graceful degradation
---------------------
If the stdlib ``sqlite3`` module is unavailable, :data:`SQLITE_AVAILABLE` is
``False``; :func:`state_status` reports ``available: false`` and the rebuild and
query helpers return a structured blocker/status instead of raising. File-based
commands elsewhere in Ophelia are unaffected.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # pragma: no cover - exercised only on a Python build without sqlite3
    import sqlite3

    SQLITE_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive
    sqlite3 = None  # type: ignore[assignment]
    SQLITE_AVAILABLE = False

from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .operation_refs import public_resolution, resolve_receipt_ref
from .operation_schema import SCHEMA_VERSION, issue
from .operator_reports import manifest_registry, release_registry
from .portability import (
    _backup_records,
    _redact_manifest_lock,
    _restore_drill_receipts,
)
from .receipt_index import receipt_timeline
from .redaction import deep_redact, redact_mapping

STATE_SCHEMA_VERSION = 1

REBUILD_KIND = "ophelia.state_rebuild"
STATUS_KIND = "ophelia.state_status"
QUERY_KIND = "ophelia.state_query"

_DEFAULT_MANIFESTS_DIR = REPO_ROOT / "manifests"

# Tables created and populated by a rebuild, in creation order. Each table that
# stores a JSON payload keeps a canonical, already-redacted ``payload_json`` plus
# typed query columns and (where a payload is stored) a ``redacted`` flag.
_SCHEMA_STATEMENTS: List[str] = [
    "CREATE TABLE apps (app TEXT PRIMARY KEY, environments TEXT, payload_json TEXT)",
    "CREATE TABLE environments (app TEXT, environment TEXT, manifest_path TEXT, "
    "payload_json TEXT, PRIMARY KEY (app, environment))",
    "CREATE TABLE manifests (app TEXT, environment TEXT, path TEXT, "
    "payload_json TEXT, redacted INTEGER, PRIMARY KEY (app, environment, path))",
    "CREATE TABLE routes (app TEXT, environment TEXT, domain TEXT, path TEXT, "
    "service TEXT, source TEXT, payload_json TEXT)",
    "CREATE TABLE releases (app TEXT, release_id TEXT, environment TEXT, "
    "git_sha TEXT, deployed_at TEXT, active INTEGER, latest INTEGER, "
    "payload_json TEXT, PRIMARY KEY (app, release_id))",
    "CREATE TABLE receipts (receipt_id TEXT PRIMARY KEY, operation TEXT, "
    "status TEXT, app TEXT, environment TEXT, started_at TEXT, completed_at TEXT, "
    "path TEXT, rollback_available INTEGER, payload_json TEXT)",
    "CREATE TABLE artifacts (receipt_id TEXT, name TEXT, kind TEXT, path TEXT, "
    "redacted INTEGER)",
    "CREATE TABLE checks (receipt_id TEXT, name TEXT, ok INTEGER, message TEXT)",
    "CREATE TABLE backups (backup_id TEXT PRIMARY KEY, app TEXT, created_at TEXT, "
    "path TEXT, payload_json TEXT)",
    "CREATE TABLE restore_drills (drill_id TEXT, app TEXT, status TEXT, path TEXT, "
    "PRIMARY KEY (drill_id, path))",
    "CREATE TABLE provider_configs (app TEXT, environment TEXT, provider TEXT, "
    "kind TEXT, payload_json TEXT)",
    "CREATE TABLE operation_plans (operation_id TEXT PRIMARY KEY, operation TEXT, "
    "app TEXT, environment TEXT, status TEXT, path TEXT, payload_json TEXT)",
    "CREATE TABLE policy_results (operation_id TEXT, decision TEXT, app TEXT, "
    "environment TEXT, payload_json TEXT)",
    "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)",
]

# Tables dropped (newest first does not matter; no FKs) on each rebuild so a
# rerun produces identical state.
_TABLE_NAMES: List[str] = [
    "apps",
    "environments",
    "manifests",
    "routes",
    "releases",
    "receipts",
    "artifacts",
    "checks",
    "backups",
    "restore_drills",
    "provider_configs",
    "operation_plans",
    "policy_results",
    "schema_migrations",
]


def state_db_path(runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> Path:
    """Location of the local SQLite index for ``runtime_root``."""
    return Path(runtime_root) / "state" / "ophelia-state.sqlite3"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _relative_path(path: object, runtime_root: Path) -> Optional[str]:
    """Render ``path`` relative to ``runtime_root`` when possible.

    Falls back to the absolute string when the path lives outside the runtime
    root (e.g. a manifest under the repo). Returns ``None`` for empty input.
    """
    if path is None:
        return None
    text = str(path)
    if not text:
        return None
    try:
        return str(Path(text).resolve().relative_to(Path(runtime_root).resolve()))
    except (ValueError, OSError):
        return text


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _redact_payload_env(payload: Any) -> Any:
    """Deep-copy ``payload`` and mask any nested ``env`` mapping value.

    Receipts/backups are already redaction-safe at the source, but this is a
    belt-and-suspenders sweep: any dict carrying an ``env`` mapping has that
    mapping routed through :func:`redact_mapping` so a stray raw value can never
    reach the database.
    """
    cloned = json.loads(json.dumps(payload, default=str)) if payload is not None else payload
    _mask_env_in_place(cloned)
    return deep_redact(cloned, propagate=True)


def _mask_env_in_place(node: Any) -> None:
    if isinstance(node, dict):
        env = node.get("env")
        if isinstance(env, dict):
            node["env"] = redact_mapping(env)
        for value in node.values():
            _mask_env_in_place(value)
    elif isinstance(node, list):
        for item in node:
            _mask_env_in_place(item)


def _read_json_file(path: Path) -> Any:
    return json.loads(path.read_text())


# --------------------------------------------------------------------------- #
# Rebuild
# --------------------------------------------------------------------------- #


def rebuild_state(
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifests_dir: Path = _DEFAULT_MANIFESTS_DIR,
) -> Dict[str, Any]:
    """Rebuild the local SQLite index from the runtime-root files.

    Idempotent: every table is dropped and recreated inside a single
    transaction before rows are re-inserted, so repeated runs over an unchanged
    runtime root produce identical state and never raise duplicate-key errors.
    Corrupt or unreadable files become ``warnings`` and are skipped; the rebuild
    never crashes on a bad file.
    """
    runtime_root = Path(runtime_root)
    manifests_dir = Path(manifests_dir)
    db_path = state_db_path(runtime_root)

    if not SQLITE_AVAILABLE:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": REBUILD_KIND,
            "db_path": str(db_path),
            "schema_version_db": None,
            "counts": {},
            "warnings": [],
            "blockers": [
                issue(
                    "sqlite_unavailable",
                    "The stdlib sqlite3 module is unavailable; the local state index cannot be built.",
                )
            ],
            "status": "blocked",
        }

    warnings: List[Dict[str, str]] = []
    blockers: List[Dict[str, str]] = []

    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("BEGIN")
        for name in _TABLE_NAMES:
            connection.execute(f"DROP TABLE IF EXISTS {name}")
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)

        counts = _populate(connection, runtime_root, manifests_dir, warnings)

        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (STATE_SCHEMA_VERSION, _utc_now()),
        )
        connection.commit()
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        connection.rollback()
        blockers.append(issue("state_rebuild_failed", f"SQLite rebuild failed: {exc}", str(db_path)))
    finally:
        connection.close()

    status = "blocked" if blockers else "ok"
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": REBUILD_KIND,
        "db_path": str(db_path),
        "schema_version_db": None if blockers else STATE_SCHEMA_VERSION,
        "counts": counts if not blockers else {},
        "warnings": warnings,
        "blockers": blockers,
        "status": status,
    }


def _populate(
    connection: "sqlite3.Connection",
    runtime_root: Path,
    manifests_dir: Path,
    warnings: List[Dict[str, str]],
) -> Dict[str, int]:
    apps_by_environments: Dict[str, set] = {}

    _index_manifests(connection, runtime_root, apps_by_environments, warnings)
    _index_routes(connection, runtime_root, warnings)
    _index_releases(connection, runtime_root, apps_by_environments)
    _index_receipts(connection, runtime_root, warnings)
    _index_backups(connection, runtime_root, warnings)
    _index_restore_drills(connection, runtime_root, apps_by_environments)
    _index_manifest_registry(connection, manifests_dir, runtime_root, apps_by_environments, warnings)
    _index_apps(connection, apps_by_environments)

    # Report counts from actual row counts rather than running insert counters,
    # so idempotent re-inserts and id collisions can never inflate the totals.
    return {
        table: _count_rows(connection, table)
        for table in (
            "apps",
            "environments",
            "manifests",
            "routes",
            "releases",
            "receipts",
            "artifacts",
            "checks",
            "backups",
            "restore_drills",
        )
    }


def _index_manifests(
    connection: "sqlite3.Connection",
    runtime_root: Path,
    apps_by_environments: Dict[str, set],
    warnings: List[Dict[str, str]],
) -> int:
    apps_root = runtime_root / "apps"
    if not apps_root.exists():
        return 0
    count = 0
    for lock_path in sorted(apps_root.glob("*/manifest.lock.json")):
        app = lock_path.parent.name
        try:
            payload = _read_json_file(lock_path)
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(issue("manifest_unreadable", f"Could not read manifest lock: {exc}", str(lock_path)))
            continue
        if not isinstance(payload, dict):
            warnings.append(issue("manifest_unreadable", "Manifest lock is not a JSON object.", str(lock_path)))
            continue
        redacted = _redact_manifest_lock(payload)
        environment = redacted.get("environment") if isinstance(redacted.get("environment"), str) else None
        rel_path = _relative_path(lock_path, runtime_root)
        connection.execute(
            "INSERT OR REPLACE INTO manifests (app, environment, path, payload_json, redacted) "
            "VALUES (?, ?, ?, ?, ?)",
            (app, environment, rel_path, _canonical_json(redacted), 1),
        )
        apps_by_environments.setdefault(app, set())
        if environment:
            apps_by_environments[app].add(environment)
        _index_environment(connection, app, environment, rel_path, redacted)
        count += 1
    return count


def _index_environment(
    connection: "sqlite3.Connection",
    app: str,
    environment: Optional[str],
    manifest_path: Optional[str],
    redacted_lock: Dict[str, Any],
) -> None:
    payload = {
        "app": app,
        "environment": environment,
        "kind": redacted_lock.get("kind"),
        "image": redacted_lock.get("image"),
        "profile": redacted_lock.get("profile"),
    }
    connection.execute(
        "INSERT OR REPLACE INTO environments (app, environment, manifest_path, payload_json) "
        "VALUES (?, ?, ?, ?)",
        (app, environment or "", manifest_path, _canonical_json(payload)),
    )


def _index_routes(
    connection: "sqlite3.Connection",
    runtime_root: Path,
    warnings: List[Dict[str, str]],
) -> int:
    apps_root = runtime_root / "apps"
    if not apps_root.exists():
        return 0
    count = 0
    for lock_path in sorted(apps_root.glob("*/manifest.lock.json")):
        app = lock_path.parent.name
        try:
            payload = _read_json_file(lock_path)
        except (OSError, json.JSONDecodeError):
            # Already surfaced as a warning by _index_manifests.
            continue
        if not isinstance(payload, dict):
            continue
        environment = payload.get("environment") if isinstance(payload.get("environment"), str) else None
        routes = payload.get("routes")
        if not isinstance(routes, list):
            continue
        for route in routes:
            if not isinstance(route, dict):
                continue
            domain = route.get("domain")
            if not isinstance(domain, str) or not domain:
                continue
            path = route.get("path") or route.get("path_prefix")
            service = route.get("service") or route.get("upstream")
            connection.execute(
                "INSERT INTO routes (app, environment, domain, path, service, source, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    app,
                    environment,
                    domain,
                    path if isinstance(path, str) else None,
                    service if isinstance(service, str) else None,
                    "manifest.lock.json",
                    _canonical_json(route),
                ),
            )
            count += 1
    return count


def _index_releases(
    connection: "sqlite3.Connection",
    runtime_root: Path,
    apps_by_environments: Dict[str, set],
) -> int:
    registry = release_registry(runtime_root)
    count = 0
    for release in registry.get("releases", []):
        if not isinstance(release, dict):
            continue
        app = release.get("app")
        release_id = release.get("release_id")
        if not isinstance(app, str) or not isinstance(release_id, str):
            continue
        environment = release.get("environment") if isinstance(release.get("environment"), str) else None
        connection.execute(
            "INSERT OR REPLACE INTO releases (app, release_id, environment, git_sha, deployed_at, "
            "active, latest, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                app,
                release_id,
                environment,
                release.get("git_sha") if isinstance(release.get("git_sha"), str) else None,
                release.get("deployed_at") if isinstance(release.get("deployed_at"), str) else None,
                1 if release.get("active") else 0,
                1 if release.get("latest") else 0,
                _canonical_json(release),
            ),
        )
        apps_by_environments.setdefault(app, set())
        if environment:
            apps_by_environments[app].add(environment)
        count += 1
    return count


def _index_receipts(
    connection: "sqlite3.Connection",
    runtime_root: Path,
    warnings: List[Dict[str, str]],
) -> tuple[int, int, int]:
    timeline = receipt_timeline(runtime_root)
    warnings.extend(timeline.get("warnings", []))
    receipt_count = 0
    artifact_count = 0
    check_count = 0
    for entry in timeline.get("receipts", []):
        if not isinstance(entry, dict):
            continue
        receipt_id = entry.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id:
            continue
        path = entry.get("path")
        rel_path = _relative_path(path, runtime_root)
        rollback_available = 1 if entry.get("rollback_available") else 0
        connection.execute(
            "INSERT OR IGNORE INTO receipts (receipt_id, operation, status, app, environment, "
            "started_at, completed_at, path, rollback_available, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt_id,
                entry.get("operation"),
                entry.get("status"),
                entry.get("app"),
                entry.get("environment"),
                entry.get("started_at"),
                entry.get("completed_at"),
                rel_path,
                rollback_available,
                _canonical_json(_redact_payload_env(entry)),
            ),
        )
        receipt_count += 1
        artifact_count += _index_receipt_artifacts(connection, receipt_id, path, runtime_root, warnings)
        check_count += _index_receipt_checks(connection, receipt_id, path, warnings)
    return receipt_count, artifact_count, check_count


def _index_receipt_artifacts(
    connection: "sqlite3.Connection",
    receipt_id: str,
    path: object,
    runtime_root: Path,
    warnings: List[Dict[str, str]],
) -> int:
    payload = _safe_receipt_payload(path, warnings)
    if payload is None:
        return 0
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        return 0
    count = 0
    for item in artifacts:
        if isinstance(item, dict):
            name = item.get("name") or item.get("path")
            kind = item.get("kind")
            artifact_path = _relative_path(item.get("path"), runtime_root)
            redacted = 1 if item.get("redacted") else 0
        elif isinstance(item, str) and item:
            name = item
            kind = None
            artifact_path = _relative_path(item, runtime_root)
            redacted = 0
        else:
            continue
        connection.execute(
            "INSERT INTO artifacts (receipt_id, name, kind, path, redacted) VALUES (?, ?, ?, ?, ?)",
            (
                receipt_id,
                name if isinstance(name, str) else None,
                kind if isinstance(kind, str) else None,
                artifact_path,
                redacted,
            ),
        )
        count += 1
    return count


def _index_receipt_checks(
    connection: "sqlite3.Connection",
    receipt_id: str,
    path: object,
    warnings: List[Dict[str, str]],
) -> int:
    payload = _safe_receipt_payload(path, warnings)
    if payload is None:
        return 0
    checks = payload.get("checks")
    if not isinstance(checks, list):
        return 0
    count = 0
    for item in checks:
        if not isinstance(item, dict):
            continue
        connection.execute(
            "INSERT INTO checks (receipt_id, name, ok, message) VALUES (?, ?, ?, ?)",
            (
                receipt_id,
                item.get("name") if isinstance(item.get("name"), str) else None,
                1 if item.get("ok") else 0,
                item.get("message") if isinstance(item.get("message"), str) else None,
            ),
        )
        count += 1
    return count


def _safe_receipt_payload(path: object, warnings: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    file_path = Path(str(path))
    try:
        payload = _read_json_file(file_path)
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(issue("receipt_unreadable", f"Could not read receipt: {exc}", str(file_path)))
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _index_backups(
    connection: "sqlite3.Connection",
    runtime_root: Path,
    warnings: List[Dict[str, str]],
) -> int:
    backups_apps_root = runtime_root / "backups" / "apps"
    if not backups_apps_root.exists():
        return 0
    count = 0
    for app_root in sorted(path for path in backups_apps_root.iterdir() if path.is_dir()):
        for record in _backup_records(app_root):
            backup_id = record.get("backup_id")
            if not isinstance(backup_id, str) or not backup_id:
                continue
            rel_path = _relative_path(record.get("path"), runtime_root)
            stored = dict(record)
            stored["app"] = app_root.name
            stored["path"] = rel_path
            stored["manifest_path"] = _relative_path(record.get("manifest_path"), runtime_root)
            connection.execute(
                "INSERT OR REPLACE INTO backups (backup_id, app, created_at, path, payload_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    backup_id,
                    app_root.name,
                    record.get("created_at") if isinstance(record.get("created_at"), str) else None,
                    rel_path,
                    _canonical_json(_redact_payload_env(stored)),
                ),
            )
            count += 1
    return count


def _index_restore_drills(
    connection: "sqlite3.Connection",
    runtime_root: Path,
    apps_by_environments: Dict[str, set],
) -> int:
    count = 0
    apps_root = runtime_root / "apps"
    if not apps_root.exists():
        return 0
    for app_root in sorted(path for path in apps_root.iterdir() if path.is_dir()):
        app = app_root.name
        for receipt in _restore_drill_receipts(runtime_root, app):
            drill_id = receipt.get("receipt_id")
            if not isinstance(drill_id, str) or not drill_id:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO restore_drills (drill_id, app, status, path) VALUES (?, ?, ?, ?)",
                (
                    drill_id,
                    app,
                    receipt.get("status") if isinstance(receipt.get("status"), str) else None,
                    _relative_path(receipt.get("path"), runtime_root),
                ),
            )
            count += 1
        apps_by_environments.setdefault(app, set())
    return count


def _index_manifest_registry(
    connection: "sqlite3.Connection",
    manifests_dir: Path,
    runtime_root: Path,
    apps_by_environments: Dict[str, set],
    warnings: List[Dict[str, str]],
) -> None:
    if not manifests_dir.exists():
        return
    try:
        registry = manifest_registry(manifests_dir, runtime_root)
    except Exception as exc:  # noqa: BLE001 - registry is best-effort context
        warnings.append(issue("manifest_registry_unreadable", f"Could not read manifest registry: {exc}", str(manifests_dir)))
        return
    for entry in registry.get("manifests", []):
        if not isinstance(entry, dict):
            continue
        app = entry.get("app")
        if not isinstance(app, str):
            continue
        environment = entry.get("environment") if isinstance(entry.get("environment"), str) else None
        apps_by_environments.setdefault(app, set())
        if environment:
            apps_by_environments[app].add(environment)
    for error in registry.get("errors", []):
        if isinstance(error, dict):
            warnings.append(
                issue(
                    "manifest_registry_error",
                    str(error.get("error") or "Manifest could not be loaded."),
                    str(error.get("path")) if error.get("path") else None,
                )
            )


def _index_apps(connection: "sqlite3.Connection", apps_by_environments: Dict[str, set]) -> int:
    for app in sorted(apps_by_environments):
        environments = sorted(apps_by_environments[app])
        payload = {"app": app, "environments": environments}
        connection.execute(
            "INSERT OR REPLACE INTO apps (app, environments, payload_json) VALUES (?, ?, ?)",
            (app, _canonical_json(environments), _canonical_json(payload)),
        )
    return len(apps_by_environments)


def _count_rows(connection: "sqlite3.Connection", table: str) -> int:
    cursor = connection.execute(f"SELECT COUNT(*) FROM {table}")
    row = cursor.fetchone()
    return int(row[0]) if row else 0


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #


def state_status(runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> Dict[str, Any]:
    """Report whether the local index exists and matches the code schema.

    Read-only: never builds or mutates the index.
    """
    runtime_root = Path(runtime_root)
    db_path = state_db_path(runtime_root)
    base: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": STATUS_KIND,
        "available": SQLITE_AVAILABLE,
        "db_path": str(db_path),
        "exists": db_path.exists(),
        "schema_version_code": STATE_SCHEMA_VERSION,
        "schema_version_db": None,
        "needs_rebuild": True,
    }
    if not SQLITE_AVAILABLE:
        base["needs_rebuild"] = True
        base["summary"] = "sqlite3 is unavailable; the local state index cannot be used."
        return base
    if not db_path.exists():
        base["summary"] = "No local state index. Run `ship state rebuild` to create it."
        return base

    connection = sqlite3.connect(str(db_path))
    try:
        schema_version_db = _read_schema_version(connection)
        counts = _counts_from_db(connection)
    except sqlite3.Error as exc:
        base["summary"] = f"Local state index is unreadable: {exc}"
        return base
    finally:
        connection.close()

    base["schema_version_db"] = schema_version_db
    base["counts"] = counts
    base["needs_rebuild"] = schema_version_db != STATE_SCHEMA_VERSION
    if base["needs_rebuild"]:
        base["summary"] = "Local state index schema is out of date. Run `ship state rebuild`."
    else:
        base["summary"] = "Local state index is present and current."
    return base


def _read_schema_version(connection: "sqlite3.Connection") -> Optional[int]:
    try:
        cursor = connection.execute("SELECT MAX(version) FROM schema_migrations")
    except sqlite3.Error:
        return None
    row = cursor.fetchone()
    if not row or row[0] is None:
        return None
    return int(row[0])


def _counts_from_db(connection: "sqlite3.Connection") -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for table in ("apps", "environments", "manifests", "routes", "releases", "receipts", "artifacts", "backups", "restore_drills"):
        try:
            counts[table] = _count_rows(connection, table)
        except sqlite3.Error:
            counts[table] = 0
    return counts


# --------------------------------------------------------------------------- #
# Query
# --------------------------------------------------------------------------- #


def query_receipts(
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    *,
    app: Optional[str] = None,
    environment: Optional[str] = None,
    operation: Optional[str] = None,
    status: Optional[str] = None,
    ref: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Read receipts with the same content and ordering as the file timeline.

    Results match :func:`ophelia.receipt_index.receipt_timeline` ordering and
    content for the same filters. If the index is missing, returns a clear
    status telling the caller to run ``ship state rebuild`` (never auto-builds).
    """
    runtime_root = Path(runtime_root)
    db_path = state_db_path(runtime_root)
    base: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": QUERY_KIND,
        "query": "receipts",
        "available": SQLITE_AVAILABLE,
        "db_path": str(db_path),
        "filters": {
            "app": app,
            "environment": environment,
            "operation": operation,
            "status": status,
            "ref": ref,
            "limit": limit,
        },
        "receipts": [],
    }
    if not SQLITE_AVAILABLE:
        base["needs_rebuild"] = True
        base["status"] = "unavailable"
        base["summary"] = "sqlite3 is unavailable; cannot query the local state index."
        return base
    if not db_path.exists():
        base["needs_rebuild"] = True
        base["status"] = "missing"
        base["summary"] = "No local state index. Run `ship state rebuild` to create it."
        return base

    try:
        timeline = receipt_timeline(
            runtime_root,
            app=app,
            environment=environment,
            operation=operation,
            status=status,
        )
    except Exception as exc:  # noqa: BLE001 - query reports degraded state
        base["needs_rebuild"] = True
        base["status"] = "error"
        base["summary"] = f"Could not read receipt timeline: {exc}. Run `ship state rebuild`."
        return base
    receipts = timeline.get("receipts") if isinstance(timeline.get("receipts"), list) else []
    if ref:
        resolution = resolve_receipt_ref(
            ref,
            runtime_root=runtime_root,
            app=app,
            environment=environment,
            operation=operation,
            status=status,
        )
        base["resolved_ref"] = public_resolution(resolution)
        if not resolution.get("ok"):
            base["status"] = "blocked"
            base["needs_rebuild"] = False
            base["blockers"] = resolution.get("blockers", [])
            base["warnings"] = resolution.get("warnings", [])
            base["summary"] = f"Receipt reference unresolved: {ref}."
            return base
        resolved_id = str(resolution.get("resolved_id") or "")
        resolved_path = str(resolution.get("path") or "")
        receipts = [
            item
            for item in receipts
            if item.get("receipt_id") == resolved_id or item.get("path") == resolved_path
        ]
    if isinstance(limit, int) and limit > 0:
        receipts = receipts[:limit]
    base["status"] = "ok"
    base["needs_rebuild"] = False
    base["receipts"] = receipts
    base["warnings"] = timeline.get("warnings", []) if isinstance(timeline.get("warnings"), list) else []
    base["summary"] = f"{len(receipts)} receipt(s) from the receipt timeline."
    return base


__all__ = [
    "STATE_SCHEMA_VERSION",
    "SQLITE_AVAILABLE",
    "state_db_path",
    "rebuild_state",
    "state_status",
    "query_receipts",
]
