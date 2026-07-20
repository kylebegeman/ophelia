"""Versioned schema for the authoritative operation journal."""

from __future__ import annotations

import sqlite3
from typing import Sequence, Tuple


SCHEMA_VERSION = 7

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
                receipt_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL CHECK (length(payload_json) <= 65536)
            )
            """,
            "CREATE INDEX operation_events_lookup ON operation_events(operation_id, sequence)",
            "CREATE INDEX operations_state_lookup ON operations(state, accepted_at)",
        ),
    ),
    (
        2,
        (
            """
            CREATE TABLE operation_inputs (
                operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id) ON DELETE CASCADE,
                input_digest TEXT NOT NULL,
                deadline TEXT NOT NULL,
                payload_json TEXT NOT NULL CHECK (length(payload_json) <= 65536)
            )
            """,
            """
            CREATE TABLE operation_controls (
                operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id) ON DELETE CASCADE,
                cancellation_actor_json TEXT CHECK (
                    cancellation_actor_json IS NULL OR length(cancellation_actor_json) <= 65536
                ),
                cancellation_requested_at TEXT
            )
            """,
            """
            CREATE TABLE revision_lifecycle (
                operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL CHECK (sequence > 0),
                revision_id TEXT NOT NULL,
                revision_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                lease_owner TEXT NOT NULL,
                fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
                payload_json TEXT NOT NULL CHECK (length(payload_json) <= 65536),
                PRIMARY KEY (operation_id, sequence)
            )
            """,
            """
            CREATE TABLE active_revisions (
                host_id TEXT NOT NULL,
                app TEXT NOT NULL,
                environment TEXT NOT NULL,
                operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                revision_id TEXT NOT NULL,
                revision_digest TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation > 0),
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL CHECK (length(payload_json) <= 65536),
                PRIMARY KEY (host_id, app, environment)
            )
            """,
            """
            CREATE TABLE atomic_success_commits (
                operation_id TEXT PRIMARY KEY REFERENCES terminal_receipts(operation_id),
                active_generation INTEGER NOT NULL CHECK (active_generation > 0)
            )
            """,
            """
            CREATE INDEX operations_recovery_lookup
            ON operations(state, accepted_at, operation_id)
            """,
            """
            CREATE INDEX revision_lifecycle_latest
            ON revision_lifecycle(operation_id, sequence DESC)
            """,
        ),
    ),
    (
        3,
        (
            """
            CREATE TABLE execution_scope_leases (
                host_id TEXT NOT NULL,
                app TEXT NOT NULL,
                environment TEXT NOT NULL,
                operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                owner_id TEXT,
                fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
                expires_at REAL NOT NULL,
                PRIMARY KEY (host_id, app, environment)
            )
            """,
            """
            CREATE INDEX execution_scope_lease_operation
            ON execution_scope_leases(operation_id)
            """,
            """
            INSERT INTO execution_scope_leases(
                host_id, app, environment, operation_id, owner_id,
                fencing_token, expires_at
            )
            SELECT o.host_id, o.app, o.environment, l.operation_id, l.owner_id,
                   l.fencing_token, l.expires_at
            FROM operation_leases AS l
            JOIN operations AS o ON o.operation_id = l.operation_id
            WHERE l.operation_id = (
                SELECT l2.operation_id
                FROM operation_leases AS l2
                JOIN operations AS o2 ON o2.operation_id = l2.operation_id
                WHERE o2.host_id = o.host_id
                  AND o2.app = o.app
                  AND o2.environment = o.environment
                ORDER BY l2.fencing_token DESC,
                         l2.expires_at DESC,
                         l2.operation_id DESC
                LIMIT 1
            )
            """,
        ),
    ),
    (
        4,
        (
            """
            CREATE TABLE host_events (
                cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                operation_id TEXT REFERENCES operations(operation_id) ON DELETE CASCADE,
                workload_run_id TEXT,
                event_digest TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL CHECK (length(payload_json) <= 65536),
                CHECK (operation_id IS NOT NULL OR workload_run_id IS NOT NULL)
            )
            """,
            """
            INSERT INTO host_events(
                event_id, event_type, operation_id, workload_run_id,
                event_digest, occurred_at, payload_json
            )
            SELECT e.event_id, 'operation', e.operation_id, NULL,
                   e.event_digest, o.accepted_at, e.payload_json
            FROM operation_events AS e
            JOIN operations AS o ON o.operation_id = e.operation_id
            ORDER BY o.accepted_at, e.operation_id, e.sequence
            """,
            """
            CREATE TABLE event_delivery (
                consumer_id TEXT PRIMARY KEY,
                acknowledged_cursor INTEGER NOT NULL DEFAULT 0
                    CHECK (acknowledged_cursor >= 0),
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE daemon_plan_requests (
                actor_id TEXT NOT NULL,
                idempotency_key_digest TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (actor_id, idempotency_key_digest)
            )
            """,
            """
            CREATE TABLE host_state (
                host_id TEXT PRIMARY KEY,
                lifecycle_state TEXT NOT NULL,
                maintenance_mode INTEGER NOT NULL DEFAULT 0
                    CHECK (maintenance_mode IN (0, 1)),
                drained INTEGER NOT NULL DEFAULT 0 CHECK (drained IN (0, 1)),
                capabilities_json TEXT NOT NULL CHECK (length(capabilities_json) <= 65536),
                agent_version TEXT NOT NULL,
                protocol_version INTEGER NOT NULL CHECK (protocol_version > 0),
                started_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE workload_runs (
                run_id TEXT PRIMARY KEY,
                actor_id TEXT NOT NULL,
                idempotency_key_digest TEXT NOT NULL,
                host_id TEXT NOT NULL,
                app TEXT NOT NULL,
                environment TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                revision_digest TEXT NOT NULL,
                workload_name TEXT NOT NULL,
                workload_kind TEXT NOT NULL,
                trigger_kind TEXT NOT NULL,
                state TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                request_json TEXT NOT NULL CHECK (length(request_json) <= 65536),
                accepted_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                cancellation_requested_at TEXT,
                lease_owner TEXT,
                lease_expires REAL,
                fencing_token INTEGER NOT NULL DEFAULT 0
                    CHECK (fencing_token >= 0),
                result_digest TEXT,
                exit_code INTEGER,
                exit_reason TEXT,
                UNIQUE (actor_id, idempotency_key_digest)
            )
            """,
            """
            CREATE TABLE workload_run_events (
                run_id TEXT NOT NULL REFERENCES workload_runs(run_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL CHECK (sequence > 0),
                event_id TEXT NOT NULL UNIQUE,
                event_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL CHECK (length(payload_json) <= 65536),
                PRIMARY KEY (run_id, sequence)
            )
            """,
            """
            CREATE INDEX host_events_delivery ON host_events(cursor)
            """,
            """
            CREATE INDEX workload_runs_state ON workload_runs(state, accepted_at)
            """,
        ),
    ),
    (
        5,
        (
            """
            CREATE TABLE agent_state (
                host_id TEXT PRIMARY KEY,
                acknowledged_command_sequence INTEGER NOT NULL DEFAULT 0
                    CHECK (acknowledged_command_sequence >= 0),
                connection_state TEXT NOT NULL DEFAULT 'disconnected',
                connected_at TEXT,
                last_exchange_at TEXT,
                last_error_type TEXT,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE agent_commands (
                command_id TEXT PRIMARY KEY,
                sequence INTEGER NOT NULL UNIQUE CHECK (sequence > 0),
                operation TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                idempotency_key_digest TEXT NOT NULL,
                envelope_digest TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL CHECK (length(payload_json) <= 65536),
                state TEXT NOT NULL,
                accepted_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                result_digest TEXT,
                result_json TEXT CHECK (
                    result_json IS NULL OR length(result_json) <= 65536
                )
            )
            """,
            """
            CREATE INDEX agent_commands_delivery
            ON agent_commands(state, sequence)
            """,
        ),
    ),
    (
        6,
        (
            """
            CREATE TABLE host_observations (
                observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                host_id TEXT NOT NULL REFERENCES host_state(host_id) ON DELETE CASCADE,
                observed_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('ready', 'warning', 'critical')),
                observation_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL CHECK (length(payload_json) <= 65536)
            )
            """,
            """
            CREATE INDEX host_observations_latest
            ON host_observations(host_id, observation_id DESC)
            """,
        ),
    ),
    (
        7,
        (
            """
            ALTER TABLE agent_commands ADD COLUMN bundle_pruned_at TEXT
            """,
        ),
    ),
)


class MigrationError(RuntimeError):
    """Raised when an operation database cannot be migrated safely."""


def migrate(connection: sqlite3.Connection) -> None:
    """Apply pending migrations in one explicit write transaction."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise MigrationError(
                "Operation database schema %d is newer than supported schema %d."
                % (current, SCHEMA_VERSION)
            )

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
