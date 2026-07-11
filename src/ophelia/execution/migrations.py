"""Versioned schema for the authoritative operation journal."""

from __future__ import annotations

import sqlite3
from typing import Sequence, Tuple


SCHEMA_VERSION = 1

_MIGRATIONS: Sequence[Tuple[int, Tuple[str, ...]]] = (
    (
        1,
        (
            """
            CREATE TABLE plans (
                plan_id TEXT PRIMARY KEY,
                plan_digest TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL CHECK (length(payload_json) <= 65536)
            )
            """,
            """
            CREATE TABLE approvals (
                decision_id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL REFERENCES plans(plan_id),
                actor_id TEXT NOT NULL,
                approval_digest TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL CHECK (length(payload_json) <= 65536)
            )
            """,
            """
            CREATE TABLE operations (
                operation_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                operation_class TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                host_id TEXT NOT NULL,
                app TEXT NOT NULL,
                environment TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                decision_id TEXT NOT NULL REFERENCES approvals(decision_id),
                request_json TEXT NOT NULL CHECK (length(request_json) <= 65536),
                actor_json TEXT NOT NULL CHECK (length(actor_json) <= 65536),
                accepted_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE idempotency_keys (
                actor_id TEXT NOT NULL,
                operation_class TEXT NOT NULL,
                idempotency_key_digest TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
                PRIMARY KEY (actor_id, operation_class, idempotency_key_digest)
            )
            """,
            """
            CREATE TABLE operation_events (
                operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL CHECK (sequence > 0),
                event_id TEXT NOT NULL UNIQUE,
                event_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL CHECK (length(payload_json) <= 65536),
                PRIMARY KEY (operation_id, sequence)
            )
            """,
            """
            CREATE TABLE operation_leases (
                operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id) ON DELETE CASCADE,
                owner_id TEXT,
                fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
                expires_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE terminal_receipts (
                receipt_id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
                outcome TEXT NOT NULL,
                payload_json TEXT NOT NULL CHECK (length(payload_json) <= 65536)
            )
            """,
            "CREATE INDEX operation_events_lookup ON operation_events(operation_id, sequence)",
            "CREATE INDEX operations_state_lookup ON operations(state, accepted_at)",
        ),
    ),
)


class MigrationError(RuntimeError):
    """Raised when an operation database cannot be migrated safely."""


def migrate(connection: sqlite3.Connection) -> None:
    """Apply pending migrations in one explicit write transaction."""

    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise MigrationError(
            "Operation database schema %d is newer than supported schema %d."
            % (current, SCHEMA_VERSION)
        )

    connection.execute("BEGIN IMMEDIATE")
    try:
        for version, statements in _MIGRATIONS:
            if version <= current:
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute("PRAGMA user_version = %d" % version)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
