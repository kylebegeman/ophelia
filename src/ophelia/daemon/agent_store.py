"""Durable inbound command ordering and outbound result delivery."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from ..domain import canonical_digest
from ..domain._contracts import canonical_json, digest_text
from ..execution import IdempotencyConflict, OperationConflict, SQLiteOperationJournal


_COMMAND_ID = re.compile(r"^command_[A-Za-z0-9_-]{1,247}$")
_TERMINAL = {"succeeded", "failed"}


@dataclass(frozen=True)
class AgentCommand:
    command_id: str
    sequence: int
    operation: str
    actor_id: str
    envelope_digest: str
    payload: Dict[str, Any]
    state: str
    accepted_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    result_digest: Optional[str]
    result: Optional[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "sequence": self.sequence,
            "operation": self.operation,
            "actor_id": self.actor_id,
            "state": self.state,
            "accepted_at": self.accepted_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result_digest": self.result_digest,
            "result": self.result,
        }


class AgentStore:
    def __init__(self, journal: SQLiteOperationJournal, *, clock=time.time) -> None:
        self.journal = journal
        self._clock = clock

    def register(self, host_id: str) -> Dict[str, Any]:
        now = self._now()
        with self.journal._transaction() as connection:
            connection.execute(
                """
                INSERT INTO agent_state(host_id, connection_state, updated_at)
                VALUES (?, 'disconnected', ?)
                ON CONFLICT(host_id) DO NOTHING
                """,
                (host_id, now),
            )
        return self.state(host_id)

    def state(self, host_id: str) -> Dict[str, Any]:
        connection = self.journal._connect()
        try:
            row = connection.execute(
                "SELECT * FROM agent_state WHERE host_id = ?", (host_id,)
            ).fetchone()
        finally:
            connection.close()
            self.journal._repair_permissions()
        if row is None:
            raise KeyError(host_id)
        return {
            "host_id": row["host_id"],
            "acknowledged_command_sequence": int(
                row["acknowledged_command_sequence"]
            ),
            "connection_state": row["connection_state"],
            "connected_at": row["connected_at"],
            "last_exchange_at": row["last_exchange_at"],
            "last_error_type": row["last_error_type"],
            "updated_at": row["updated_at"],
        }

    def record_connection(
        self,
        host_id: str,
        *,
        connected: bool,
        revoked: bool = False,
        error_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = self._now()
        with self.journal._transaction() as connection:
            row = connection.execute(
                """
                SELECT connection_state, connected_at, last_exchange_at
                FROM agent_state WHERE host_id = ?
                """,
                (host_id,),
            ).fetchone()
            if row is None:
                raise KeyError(host_id)
            connection.execute(
                """
                UPDATE agent_state SET connection_state = ?,
                    connected_at = ?, last_exchange_at = ?,
                    last_error_type = ?, updated_at = ?
                WHERE host_id = ?
                """,
                (
                    "connected" if connected else ("revoked" if revoked else "disconnected"),
                    (
                        now
                        if connected and row["connection_state"] != "connected"
                        else row["connected_at"]
                    ),
                    now if connected else row["last_exchange_at"],
                    None if connected else error_type,
                    now,
                    host_id,
                ),
            )
        return self.state(host_id)

    def accept(
        self,
        *,
        command_id: str,
        sequence: int,
        operation: str,
        actor_id: str,
        idempotency_key: str,
        envelope_digest: str,
        payload: Dict[str, Any],
    ) -> AgentCommand:
        if not isinstance(command_id, str) or _COMMAND_ID.fullmatch(command_id) is None:
            raise ValueError("Agent command id is invalid.")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("Agent command sequence must be a positive integer.")
        if not isinstance(operation, str) or not operation or len(operation) > 128:
            raise ValueError("Agent command operation is invalid.")
        if not isinstance(actor_id, str) or not actor_id.startswith("actor_"):
            raise ValueError("Agent command actor is invalid.")
        if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 255:
            raise ValueError("Agent command idempotency key is invalid.")
        if not isinstance(payload, dict):
            raise ValueError("Agent command payload must be an object.")
        if not isinstance(envelope_digest, str) or re.fullmatch(
            r"sha256:[0-9a-f]{64}", envelope_digest
        ) is None:
            raise ValueError("Agent command envelope digest is invalid.")
        normalized = {
            "schema_version": 1,
            "kind": "ophelia.agent-command",
            "command_id": command_id,
            "sequence": sequence,
            "operation": operation,
            "actor_id": actor_id,
            "envelope_digest": envelope_digest,
            "payload": payload,
        }
        request_digest = canonical_digest(normalized)
        payload_json = self.journal._bounded_json(payload)
        key_digest = digest_text(idempotency_key)
        with self.journal._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM agent_commands WHERE command_id = ? OR sequence = ?",
                (command_id, sequence),
            ).fetchone()
            if existing is not None:
                if (
                    existing["command_id"] != command_id
                    or int(existing["sequence"]) != sequence
                    or existing["request_digest"] != request_digest
                    or existing["idempotency_key_digest"] != key_digest
                ):
                    raise IdempotencyConflict(
                        "Agent command identity was replayed with different content."
                    )
                return self._from_row(existing)
            maximum = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM agent_commands"
                ).fetchone()[0]
            )
            acknowledged = int(
                connection.execute(
                    "SELECT COALESCE(MAX(acknowledged_command_sequence), 0) FROM agent_state"
                ).fetchone()[0]
            )
            expected = max(maximum, acknowledged) + 1
            if sequence != expected:
                raise OperationConflict(
                    "Agent command sequence is not the next durable sequence."
                )
            accepted_at = self._now()
            connection.execute(
                """
                INSERT INTO agent_commands(
                    command_id, sequence, operation, actor_id,
                    idempotency_key_digest, envelope_digest, request_digest, payload_json,
                    state, accepted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?)
                """,
                (
                    command_id,
                    sequence,
                    operation,
                    actor_id,
                    key_digest,
                    envelope_digest,
                    request_digest,
                    payload_json,
                    accepted_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_commands WHERE command_id = ?", (command_id,)
            ).fetchone()
            return self._from_row(row)

    def pending(self, *, limit: int = 100) -> Tuple[AgentCommand, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Agent command limit must be from 1 through 1000.")
        connection = self.journal._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM agent_commands
                WHERE state IN ('accepted', 'running')
                ORDER BY sequence LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return tuple(self._from_row(row) for row in rows)
        finally:
            connection.close()
            self.journal._repair_permissions()

    def mark_running(self, command_id: str) -> AgentCommand:
        with self.journal._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agent_commands WHERE command_id = ?", (command_id,)
            ).fetchone()
            if row is None:
                raise KeyError(command_id)
            if row["state"] in _TERMINAL:
                return self._from_row(row)
            started_at = row["started_at"] or self._now()
            connection.execute(
                "UPDATE agent_commands SET state = 'running', started_at = ? WHERE command_id = ?",
                (started_at, command_id),
            )
            return self._from_row(
                connection.execute(
                    "SELECT * FROM agent_commands WHERE command_id = ?", (command_id,)
                ).fetchone()
            )

    def complete(
        self,
        command_id: str,
        *,
        succeeded: bool,
        result: Dict[str, Any],
    ) -> AgentCommand:
        result_json = self.journal._bounded_json(result)
        result_digest = canonical_digest(result)
        with self.journal._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agent_commands WHERE command_id = ?", (command_id,)
            ).fetchone()
            if row is None:
                raise KeyError(command_id)
            if row["state"] in _TERMINAL:
                if (
                    row["state"] != ("succeeded" if succeeded else "failed")
                    or row["result_digest"] != result_digest
                    or row["result_json"] != result_json
                ):
                    raise IdempotencyConflict(
                        "Terminal agent command result cannot be replaced."
                    )
                return self._from_row(row)
            connection.execute(
                """
                UPDATE agent_commands SET state = ?, completed_at = ?,
                    result_digest = ?, result_json = ? WHERE command_id = ?
                """,
                (
                    "succeeded" if succeeded else "failed",
                    self._now(),
                    result_digest,
                    result_json,
                    command_id,
                ),
            )
            return self._from_row(
                connection.execute(
                    "SELECT * FROM agent_commands WHERE command_id = ?", (command_id,)
                ).fetchone()
            )

    def pending_results(self, host_id: str, *, limit: int = 100) -> Tuple[Dict[str, Any], ...]:
        state = self.state(host_id)
        connection = self.journal._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM agent_commands
                WHERE sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (state["acknowledged_command_sequence"], limit),
            ).fetchall()
            contiguous = []
            for row in rows:
                if row["state"] not in _TERMINAL:
                    break
                contiguous.append(self._from_row(row).to_dict())
            return tuple(contiguous)
        finally:
            connection.close()
            self.journal._repair_permissions()

    def acknowledge_results(self, host_id: str, sequence: int) -> int:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("Acknowledged command sequence must be non-negative.")
        with self.journal._transaction() as connection:
            state = connection.execute(
                "SELECT acknowledged_command_sequence FROM agent_state WHERE host_id = ?",
                (host_id,),
            ).fetchone()
            if state is None:
                raise KeyError(host_id)
            current = int(state[0])
            if sequence <= current:
                return current
            incomplete = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM agent_commands
                    WHERE sequence <= ? AND state NOT IN ('succeeded', 'failed')
                    """,
                    (sequence,),
                ).fetchone()[0]
            )
            if incomplete:
                raise OperationConflict(
                    "Command acknowledgement crosses a nonterminal durable command."
                )
            connection.execute(
                """
                UPDATE agent_state SET acknowledged_command_sequence = ?,
                    updated_at = ? WHERE host_id = ?
                """,
                (sequence, self._now(), host_id),
            )
            return sequence

    def acknowledged_bundle_ids(self, sequence: int) -> Tuple[str, ...]:
        """Return terminal source-bundle commands safe to remove from the inbox."""

        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("Acknowledged command sequence must be non-negative.")
        connection = self.journal._connect()
        try:
            rows = connection.execute(
                """
                SELECT command_id FROM agent_commands
                WHERE sequence <= ?
                  AND state IN ('succeeded', 'failed')
                  AND operation IN ('manifest.plan', 'host.upgrade')
                  AND bundle_pruned_at IS NULL
                ORDER BY sequence
                """,
                (sequence,),
            ).fetchall()
            return tuple(str(row["command_id"]) for row in rows)
        finally:
            connection.close()
            self.journal._repair_permissions()

    def mark_bundle_pruned(self, command_id: str) -> None:
        """Record successful cleanup so later exchanges do not rescan it."""

        if not isinstance(command_id, str) or _COMMAND_ID.fullmatch(command_id) is None:
            raise ValueError("Agent command id is invalid.")
        with self.journal._transaction() as connection:
            row = connection.execute(
                "SELECT operation, state FROM agent_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if row is None:
                raise KeyError(command_id)
            if row["operation"] not in {"manifest.plan", "host.upgrade"} or row[
                "state"
            ] not in _TERMINAL:
                raise OperationConflict("Only terminal source bundles may be pruned.")
            connection.execute(
                """
                UPDATE agent_commands
                SET bundle_pruned_at = COALESCE(bundle_pruned_at, ?)
                WHERE command_id = ?
                """,
                (self._now(), command_id),
            )

    @staticmethod
    def _from_row(row) -> AgentCommand:
        result = None if row["result_json"] is None else json.loads(row["result_json"])
        return AgentCommand(
            command_id=row["command_id"],
            sequence=int(row["sequence"]),
            operation=row["operation"],
            actor_id=row["actor_id"],
            envelope_digest=row["envelope_digest"],
            payload=json.loads(row["payload_json"]),
            state=row["state"],
            accepted_at=row["accepted_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            result_digest=row["result_digest"],
            result=result,
        )

    def _now(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
