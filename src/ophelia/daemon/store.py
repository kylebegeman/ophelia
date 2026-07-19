"""Durable host, delivery, and one-shot workload state."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from ..domain import Actor, canonical_digest
from ..domain._contracts import canonical_json, digest_text, parse_utc
from ..execution import IdempotencyConflict, OperationConflict, SQLiteOperationJournal


@dataclass(frozen=True)
class WorkloadRun:
    run_id: str
    actor_id: str
    host_id: str
    app: str
    environment: str
    revision_id: str
    revision_digest: str
    workload_name: str
    workload_kind: str
    trigger_kind: str
    state: str
    accepted_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    cancellation_requested_at: Optional[str]
    fencing_token: int
    result_digest: Optional[str]
    exit_code: Optional[int]
    exit_reason: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "actor_id": self.actor_id,
            "host_id": self.host_id,
            "app": self.app,
            "environment": self.environment,
            "revision_id": self.revision_id,
            "revision_digest": self.revision_digest,
            "workload_name": self.workload_name,
            "workload_kind": self.workload_kind,
            "trigger_kind": self.trigger_kind,
            "state": self.state,
            "accepted_at": self.accepted_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "cancellation_requested_at": self.cancellation_requested_at,
            "fencing_token": self.fencing_token,
            "result_digest": self.result_digest,
            "exit_code": self.exit_code,
            "exit_reason": self.exit_reason,
        }


class DaemonStore:
    def __init__(self, journal: SQLiteOperationJournal, *, clock=time.time) -> None:
        self.journal = journal
        self._clock = clock

    def register_host(
        self,
        *,
        host_id: str,
        capabilities: Dict[str, Any],
        agent_version: str,
        protocol_version: int,
    ) -> Dict[str, Any]:
        now = self._now()
        capabilities_json = self.journal._bounded_json(capabilities)
        with self.journal._transaction() as connection:
            different_host = connection.execute(
                "SELECT host_id FROM host_state WHERE host_id != ? LIMIT 1",
                (host_id,),
            ).fetchone()
            if different_host is not None:
                raise OperationConflict(
                    "This operation journal is already bound to a different host identity."
                )
            connection.execute(
                """
                INSERT INTO host_state(
                    host_id, lifecycle_state, maintenance_mode, drained,
                    capabilities_json, agent_version, protocol_version,
                    started_at, heartbeat_at, updated_at
                ) VALUES (?, 'ready', 0, 0, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(host_id) DO UPDATE SET
                    capabilities_json = excluded.capabilities_json,
                    agent_version = excluded.agent_version,
                    protocol_version = excluded.protocol_version,
                    heartbeat_at = excluded.heartbeat_at,
                    updated_at = excluded.updated_at
                """,
                (
                    host_id,
                    capabilities_json,
                    agent_version,
                    protocol_version,
                    now,
                    now,
                    now,
                ),
            )
        return self.host(host_id)

    def plan_for_request(
        self,
        *,
        actor_id: str,
        idempotency_key: str,
        request_digest: str,
    ) -> Optional[str]:
        key_digest = digest_text(idempotency_key)
        connection = self.journal._connect()
        try:
            row = connection.execute(
                """
                SELECT request_digest, plan_id FROM daemon_plan_requests
                WHERE actor_id = ? AND idempotency_key_digest = ?
                """,
                (actor_id, key_digest),
            ).fetchone()
        finally:
            connection.close()
            self.journal._repair_permissions()
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            raise IdempotencyConflict(
                "Plan idempotency key identifies different manifest input."
            )
        return str(row["plan_id"])

    def record_plan_request(
        self,
        *,
        actor_id: str,
        idempotency_key: str,
        request_digest: str,
        plan_id: str,
    ) -> str:
        key_digest = digest_text(idempotency_key)
        with self.journal._transaction() as connection:
            connection.execute(
                """
                INSERT INTO daemon_plan_requests(
                    actor_id, idempotency_key_digest, request_digest,
                    plan_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(actor_id, idempotency_key_digest) DO NOTHING
                """,
                (actor_id, key_digest, request_digest, plan_id, self._now()),
            )
            row = connection.execute(
                """
                SELECT request_digest, plan_id FROM daemon_plan_requests
                WHERE actor_id = ? AND idempotency_key_digest = ?
                """,
                (actor_id, key_digest),
            ).fetchone()
            if row["request_digest"] != request_digest:
                raise IdempotencyConflict(
                    "Plan idempotency key identifies different manifest input."
                )
            return str(row["plan_id"])

    def heartbeat(self, host_id: str) -> None:
        now = self._now()
        with self.journal._transaction() as connection:
            updated = connection.execute(
                "UPDATE host_state SET heartbeat_at = ?, updated_at = ? WHERE host_id = ?",
                (now, now, host_id),
            ).rowcount
            if updated != 1:
                raise KeyError(host_id)

    def record_observation(
        self,
        host_id: str,
        observation: Dict[str, Any],
        *,
        retention: int,
    ) -> Dict[str, Any]:
        if (
            isinstance(retention, bool)
            or not isinstance(retention, int)
            or not 1 <= retention <= 100000
            or observation.get("host_id") != host_id
            or observation.get("status") not in {"ready", "warning", "critical"}
        ):
            raise ValueError("Host observation identity or retention is invalid.")
        observed_at = observation.get("observed_at")
        if not isinstance(observed_at, str):
            raise ValueError("Host observation timestamp is invalid.")
        parse_utc(observed_at)
        payload_json = self.journal._bounded_json(observation)
        digest = canonical_digest(observation)
        with self.journal._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO host_observations(
                    host_id, observed_at, status, observation_digest, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (host_id, observed_at, observation["status"], digest, payload_json),
            )
            observation_id = int(cursor.lastrowid)
            connection.execute(
                """
                DELETE FROM host_observations
                WHERE host_id = ? AND observation_id NOT IN (
                    SELECT observation_id FROM host_observations
                    WHERE host_id = ? ORDER BY observation_id DESC LIMIT ?
                )
                """,
                (host_id, host_id, retention),
            )
        return {
            "observation_id": observation_id,
            "observation_digest": digest,
            "observation": observation,
        }

    def latest_observation(self, host_id: str) -> Optional[Dict[str, Any]]:
        connection = self.journal._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM host_observations
                WHERE host_id = ? ORDER BY observation_id DESC LIMIT 1
                """,
                (host_id,),
            ).fetchone()
        finally:
            connection.close()
            self.journal._repair_permissions()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Host observation payload is malformed.") from exc
        if (
            canonical_json(payload) != row["payload_json"]
            or canonical_digest(payload) != row["observation_digest"]
            or payload.get("host_id") != row["host_id"]
            or payload.get("observed_at") != row["observed_at"]
            or payload.get("status") != row["status"]
        ):
            raise ValueError("Host observation evidence failed validation.")
        return {
            "observation_id": int(row["observation_id"]),
            "observation_digest": row["observation_digest"],
            "observation": payload,
        }

    def host(self, host_id: str) -> Dict[str, Any]:
        connection = self.journal._connect()
        try:
            row = connection.execute(
                "SELECT * FROM host_state WHERE host_id = ?", (host_id,)
            ).fetchone()
        finally:
            connection.close()
            self.journal._repair_permissions()
        if row is None:
            raise KeyError(host_id)
        return {
            "host_id": row["host_id"],
            "lifecycle_state": row["lifecycle_state"],
            "maintenance_mode": bool(row["maintenance_mode"]),
            "drained": bool(row["drained"]),
            "capabilities": json.loads(row["capabilities_json"]),
            "agent_version": row["agent_version"],
            "protocol_version": int(row["protocol_version"]),
            "started_at": row["started_at"],
            "heartbeat_at": row["heartbeat_at"],
            "updated_at": row["updated_at"],
        }

    def set_host_control(
        self,
        host_id: str,
        *,
        maintenance_mode: Optional[bool] = None,
        drained: Optional[bool] = None,
    ) -> Dict[str, Any]:
        if maintenance_mode is None and drained is None:
            raise ValueError("At least one host control must be supplied.")
        with self.journal._transaction() as connection:
            current = connection.execute(
                "SELECT maintenance_mode, drained FROM host_state WHERE host_id = ?",
                (host_id,),
            ).fetchone()
            if current is None:
                raise KeyError(host_id)
            maintenance = bool(current["maintenance_mode"]) if maintenance_mode is None else maintenance_mode
            drain = bool(current["drained"]) if drained is None else drained
            lifecycle = "maintenance" if maintenance else ("drained" if drain else "ready")
            connection.execute(
                """
                UPDATE host_state SET lifecycle_state = ?, maintenance_mode = ?,
                    drained = ?, updated_at = ? WHERE host_id = ?
                """,
                (lifecycle, int(maintenance), int(drain), self._now(), host_id),
            )
        return self.host(host_id)

    def accept_run(
        self,
        *,
        actor: Actor,
        host_id: str,
        app: str,
        environment: str,
        revision_id: str,
        revision_digest: str,
        workload_name: str,
        workload_kind: str,
        trigger_kind: str,
        idempotency_key: str,
        concurrency_policy: str,
    ) -> WorkloadRun:
        request = {
            "schema_version": 1,
            "kind": "ophelia.workload-run-request",
            "actor_id": actor.actor_id,
            "host_id": host_id,
            "app": app,
            "environment": environment,
            "revision_id": revision_id,
            "revision_digest": revision_digest,
            "workload_name": workload_name,
            "workload_kind": workload_kind,
            "trigger_kind": trigger_kind,
        }
        request_digest = canonical_digest(request)
        key_digest = digest_text(idempotency_key)
        accepted_at = self._now()
        with self.journal._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM workload_runs
                WHERE actor_id = ? AND idempotency_key_digest = ?
                """,
                (actor.actor_id, key_digest),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise IdempotencyConflict(
                        "Workload-run idempotency key identifies different intent."
                    )
                return self._run_from_row(existing)

            active = connection.execute(
                """
                SELECT * FROM workload_runs
                WHERE host_id = ? AND app = ? AND environment = ?
                  AND workload_name = ? AND state IN ('accepted', 'running')
                ORDER BY accepted_at LIMIT 1
                """,
                (host_id, app, environment, workload_name),
            ).fetchone()
            if active is not None and concurrency_policy == "forbid":
                raise OperationConflict(
                    "A workload run is already active and its concurrency policy forbids overlap."
                )
            if active is not None and concurrency_policy == "replace":
                self._request_cancel_tx(connection, active, actor)

            run_id = "run_" + uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO workload_runs(
                    run_id, actor_id, idempotency_key_digest, host_id, app,
                    environment, revision_id, revision_digest, workload_name,
                    workload_kind, trigger_kind, state, request_digest,
                    request_json, accepted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?)
                """,
                (
                    run_id,
                    actor.actor_id,
                    key_digest,
                    host_id,
                    app,
                    environment,
                    revision_id,
                    revision_digest,
                    workload_name,
                    workload_kind,
                    trigger_kind,
                    request_digest,
                    self.journal._bounded_json(request),
                    accepted_at,
                ),
            )
            self._append_run_event(
                connection,
                run_id,
                "accepted",
                {"state": "accepted", "actor_id": actor.actor_id},
            )
            row = connection.execute(
                "SELECT * FROM workload_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return self._run_from_row(row)

    def acquire_run(self, owner_id: str, *, lease_seconds: float = 300) -> Optional[WorkloadRun]:
        now_epoch = self._clock()
        with self.journal._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM workload_runs
                WHERE state IN ('accepted', 'running')
                  AND (lease_owner IS NULL OR lease_expires <= ?)
                ORDER BY accepted_at, run_id LIMIT 1
                """,
                (now_epoch,),
            ).fetchone()
            if row is None:
                return None
            if row["cancellation_requested_at"] is not None and row["state"] == "accepted":
                return self._complete_tx(
                    connection,
                    row,
                    state="cancelled",
                    result_digest=canonical_digest({"cancelled": True}),
                    exit_code=None,
                    exit_reason="cancelled",
                )
            token = int(row["fencing_token"]) + 1
            started_at = row["started_at"] or self._now()
            connection.execute(
                """
                UPDATE workload_runs SET state = 'running', started_at = ?,
                    lease_owner = ?, lease_expires = ?, fencing_token = ?
                WHERE run_id = ?
                """,
                (started_at, owner_id, now_epoch + lease_seconds, token, row["run_id"]),
            )
            self._append_run_event(
                connection,
                row["run_id"],
                "running",
                {"state": "running", "fencing_token": token},
            )
            claimed = connection.execute(
                "SELECT * FROM workload_runs WHERE run_id = ?", (row["run_id"],)
            ).fetchone()
            return self._run_from_row(claimed)

    def complete_run(
        self,
        run_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        state: str,
        result_digest: str,
        exit_code: Optional[int],
        exit_reason: str,
    ) -> WorkloadRun:
        if state not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("Terminal workload-run state is invalid.")
        with self.journal._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workload_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["state"] in {"succeeded", "failed", "cancelled"}:
                return self._run_from_row(row)
            if row["lease_owner"] != owner_id or int(row["fencing_token"]) != fencing_token:
                raise OperationConflict("Workload-run execution fence is stale.")
            return self._complete_tx(
                connection,
                row,
                state=state,
                result_digest=result_digest,
                exit_code=exit_code,
                exit_reason=exit_reason,
            )

    def renew_run_lease(
        self,
        run_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        lease_seconds: float = 300,
    ) -> bool:
        if lease_seconds <= 0 or lease_seconds > 3600:
            raise ValueError("Workload-run lease must be greater than zero and at most 3600 seconds.")
        with self.journal._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE workload_runs SET lease_expires = ?
                WHERE run_id = ? AND state = 'running' AND lease_owner = ?
                  AND fencing_token = ?
                """,
                (
                    self._clock() + lease_seconds,
                    run_id,
                    owner_id,
                    fencing_token,
                ),
            ).rowcount
        return updated == 1

    def cancellation_requested(self, run_id: str) -> bool:
        connection = self.journal._connect()
        try:
            row = connection.execute(
                "SELECT cancellation_requested_at FROM workload_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        finally:
            connection.close()
            self.journal._repair_permissions()
        if row is None:
            raise KeyError(run_id)
        return row[0] is not None

    def request_cancellation(self, run_id: str, actor: Actor) -> WorkloadRun:
        with self.journal._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workload_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["state"] in {"succeeded", "failed", "cancelled"}:
                return self._run_from_row(row)
            self._request_cancel_tx(connection, row, actor)
            updated = connection.execute(
                "SELECT * FROM workload_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return self._run_from_row(updated)

    def run(self, run_id: str) -> WorkloadRun:
        connection = self.journal._connect()
        try:
            row = connection.execute(
                "SELECT * FROM workload_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        finally:
            connection.close()
            self.journal._repair_permissions()
        if row is None:
            raise KeyError(run_id)
        return self._run_from_row(row)

    def list_runs(self, *, limit: int = 100) -> Tuple[WorkloadRun, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be from 1 through 1000.")
        connection = self.journal._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM workload_runs ORDER BY accepted_at DESC, run_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return tuple(self._run_from_row(row) for row in rows)
        finally:
            connection.close()
            self.journal._repair_permissions()

    def _request_cancel_tx(self, connection, row, actor: Actor) -> None:
        if row["cancellation_requested_at"] is not None:
            return
        requested_at = self._now()
        connection.execute(
            "UPDATE workload_runs SET cancellation_requested_at = ? WHERE run_id = ?",
            (requested_at, row["run_id"]),
        )
        self._append_run_event(
            connection,
            row["run_id"],
            "cancellation_requested",
            {"state": row["state"], "actor_id": actor.actor_id},
        )

    def _complete_tx(
        self,
        connection,
        row,
        *,
        state: str,
        result_digest: str,
        exit_code: Optional[int],
        exit_reason: str,
    ) -> WorkloadRun:
        completed_at = self._now()
        connection.execute(
            """
            UPDATE workload_runs SET state = ?, completed_at = ?, result_digest = ?,
                exit_code = ?, exit_reason = ?, lease_owner = NULL, lease_expires = NULL
            WHERE run_id = ?
            """,
            (state, completed_at, result_digest, exit_code, exit_reason, row["run_id"]),
        )
        self._append_run_event(
            connection,
            row["run_id"],
            state,
            {
                "state": state,
                "result_digest": result_digest,
                "exit_code": exit_code,
                "exit_reason": exit_reason,
            },
        )
        updated = connection.execute(
            "SELECT * FROM workload_runs WHERE run_id = ?", (row["run_id"],)
        ).fetchone()
        return self._run_from_row(updated)

    def _append_run_event(self, connection, run_id: str, event_type: str, detail: dict) -> None:
        previous = connection.execute(
            """
            SELECT sequence, event_digest FROM workload_run_events
            WHERE run_id = ? ORDER BY sequence DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        sequence = 1 if previous is None else int(previous["sequence"]) + 1
        payload = {
            "schema_version": 1,
            "kind": "ophelia.workload-run-event",
            "run_id": run_id,
            "sequence": sequence,
            "event_type": event_type,
            "occurred_at": self._now(),
            "previous_event_digest": None if previous is None else previous["event_digest"],
            "detail": detail,
        }
        payload_json = canonical_json(payload)
        if len(payload_json.encode("utf-8")) > 65536:
            raise ValueError("Workload-run event exceeds its storage bound.")
        event_digest = canonical_digest(payload)
        event_id = "event_" + uuid.uuid4().hex
        connection.execute(
            """
            INSERT INTO workload_run_events(
                run_id, sequence, event_id, event_digest, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, sequence, event_id, event_digest, payload_json),
        )
        connection.execute(
            """
            INSERT INTO host_events(
                event_id, event_type, operation_id, workload_run_id,
                event_digest, occurred_at, payload_json
            ) VALUES (?, 'workload', NULL, ?, ?, ?, ?)
            """,
            (event_id, run_id, event_digest, payload["occurred_at"], payload_json),
        )

    @staticmethod
    def _run_from_row(row) -> WorkloadRun:
        return WorkloadRun(
            run_id=row["run_id"],
            actor_id=row["actor_id"],
            host_id=row["host_id"],
            app=row["app"],
            environment=row["environment"],
            revision_id=row["revision_id"],
            revision_digest=row["revision_digest"],
            workload_name=row["workload_name"],
            workload_kind=row["workload_kind"],
            trigger_kind=row["trigger_kind"],
            state=row["state"],
            accepted_at=row["accepted_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            cancellation_requested_at=row["cancellation_requested_at"],
            fencing_token=int(row["fencing_token"]),
            result_digest=row["result_digest"],
            exit_code=row["exit_code"],
            exit_reason=row["exit_reason"],
        )

    def _now(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
