"""Authoritative SQLite journal for kernel operations."""

from __future__ import annotations

import hmac
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional, Tuple

from ..domain._contracts import (
    ContractValidationError,
    canonical_digest,
    canonical_json,
    digest_text,
    require_text,
)
from ..domain.events import OperationEvent
from ..domain.operations import Actor, OperationRef, OperationRequest, OperationState
from ..domain.plans import ApprovedPlanRef
from ..domain.receipts import TerminalReceipt
from .migrations import migrate


MAX_CANONICAL_JSON_BYTES = 65536
DEFAULT_OPERATION_DB_RELATIVE_PATH = Path("host-state") / "operations.db"


class OperationStoreError(RuntimeError):
    """Base error for rejected journal mutations."""


class IdempotencyConflict(OperationStoreError):
    """The same idempotency identity was reused for different normalized intent."""


class OperationConflict(OperationStoreError):
    """A journal mutation conflicts with durable operation state."""


class LeaseConflict(OperationStoreError):
    """A lease is held by another owner or the supplied fence is stale."""


class IntegrityError(OperationStoreError):
    """The operation database failed an integrity check."""


@dataclass(frozen=True)
class OperationLease:
    operation_id: str
    owner_id: str
    fencing_token: int
    expires_at: float


class SQLiteOperationJournal:
    """SQLite implementation of the authoritative OperationJournal protocol.

    A fresh connection is used per call so separate instances and threads share
    SQLite's locking semantics rather than a process-local lock.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        clock: Callable[[], float] = time.time,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.database_path = Path(database_path).expanduser()
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def beneath_runtime_root(
        cls,
        runtime_root: Path,
        **kwargs: object,
    ) -> "SQLiteOperationJournal":
        return cls(Path(runtime_root) / DEFAULT_OPERATION_DB_RELATIVE_PATH, **kwargs)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.database_path),
            timeout=self._timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = %d" % int(self._timeout_seconds * 1000))
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            journal_mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
            if journal_mode.lower() != "wal":
                raise IntegrityError("Operation database could not enable WAL mode.")
            migrate(connection)
            self._check_integrity(connection)
        finally:
            connection.close()

    @staticmethod
    def _check_integrity(connection: sqlite3.Connection) -> None:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick_check != "ok" or foreign_key_rows:
            raise IntegrityError("Operation database integrity validation failed.")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _bounded_json(value: object) -> str:
        payload = canonical_json(value)
        if len(payload.encode("utf-8")) > MAX_CANONICAL_JSON_BYTES:
            raise ContractValidationError(
                "Canonical journal payload exceeds the 65536-byte bound."
            )
        return payload

    def accept(
        self,
        actor: Actor,
        request: OperationRequest,
        approved_plan: ApprovedPlanRef,
    ) -> OperationRef:
        request_digest = request.intent_digest()
        self._validate_acceptance_binding(
            actor, request, approved_plan, request_digest
        )
        actor_json = self._bounded_json(actor)
        request_json = self._bounded_json(request)
        approval_json = self._bounded_json(approved_plan)
        plan_json = self._bounded_json(
            {
                "schema_version": approved_plan.schema_version,
                "kind": "ophelia.kernel.approved_plan_claims",
                "plan_id": approved_plan.plan_id,
                "plan_digest": approved_plan.plan_digest,
                "request_digest": approved_plan.request_digest,
                "host_id": approved_plan.host_id,
                "app": approved_plan.app,
                "environment": approved_plan.environment,
                "revision_id": approved_plan.revision_id,
                "revision_digest": approved_plan.revision_digest,
                "manifest_digest": approved_plan.manifest_digest,
                "artifact_digests": approved_plan.artifact_digests,
                "observed_state_digest": approved_plan.observed_state_digest,
                "policy_digest": approved_plan.policy_digest,
            }
        )

        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT i.request_digest, o.operation_id, o.request_id, o.state
                FROM idempotency_keys AS i
                JOIN operations AS o ON o.operation_id = i.operation_id
                WHERE i.actor_id = ? AND i.operation_class = ? AND i.idempotency_key_digest = ?
                """,
                (actor.actor_id, request.operation, digest_text(request.idempotency_key)),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise IdempotencyConflict(
                        "Idempotency key already identifies different normalized intent."
                    )
                return OperationRef(
                    operation_id=existing["operation_id"],
                    request_id=existing["request_id"],
                    state=OperationState(existing["state"]),
                )

            if approved_plan.expired(
                datetime.fromtimestamp(self._clock(), timezone.utc)
            ):
                raise OperationConflict("Approval has expired.")

            operation_id = "operation_" + uuid.uuid4().hex
            accepted_at = datetime.fromtimestamp(
                self._clock(), timezone.utc
            ).isoformat().replace("+00:00", "Z")
            connection.execute(
                """
                INSERT INTO plans(plan_id, plan_digest, request_digest, payload_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(plan_id) DO NOTHING
                """,
                (
                    approved_plan.plan_id,
                    approved_plan.plan_digest,
                    approved_plan.request_digest,
                    plan_json,
                ),
            )
            stored_plan = connection.execute(
                "SELECT plan_digest, request_digest, payload_json FROM plans WHERE plan_id = ?",
                (approved_plan.plan_id,),
            ).fetchone()
            if (
                stored_plan["plan_digest"] != approved_plan.plan_digest
                or stored_plan["request_digest"] != approved_plan.request_digest
                or stored_plan["payload_json"] != plan_json
            ):
                raise OperationConflict("Plan identity conflicts with durable plan content.")

            connection.execute(
                """
                INSERT INTO approvals(
                    decision_id, plan_id, actor_id, approval_digest, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(decision_id) DO NOTHING
                """,
                (
                    approved_plan.decision_id,
                    approved_plan.plan_id,
                    approved_plan.actor_id,
                    approved_plan.approval_digest,
                    approval_json,
                ),
            )
            stored_approval = connection.execute(
                """
                SELECT plan_id, actor_id, approval_digest, payload_json
                FROM approvals WHERE decision_id = ?
                """,
                (approved_plan.decision_id,),
            ).fetchone()
            if (
                stored_approval["plan_id"] != approved_plan.plan_id
                or stored_approval["actor_id"] != approved_plan.actor_id
                or stored_approval["approval_digest"] != approved_plan.approval_digest
                or stored_approval["payload_json"] != approval_json
            ):
                raise OperationConflict(
                    "Approval identity conflicts with durable approval content."
                )

            connection.execute(
                """
                INSERT INTO operations(
                    operation_id, request_id, actor_id, operation_class,
                    request_digest, state, host_id, app, environment, revision_id,
                    decision_id, request_json, actor_json, accepted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    request.request_id,
                    actor.actor_id,
                    request.operation,
                    request_digest,
                    OperationState.ACCEPTED.value,
                    request.host_id,
                    request.app,
                    request.environment,
                    request.revision_id,
                    approved_plan.decision_id,
                    request_json,
                    actor_json,
                    accepted_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO idempotency_keys(
                    actor_id, operation_class, idempotency_key_digest,
                    request_digest, operation_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    actor.actor_id,
                    request.operation,
                    digest_text(request.idempotency_key),
                    request_digest,
                    operation_id,
                ),
            )
            return OperationRef(
                operation_id=operation_id,
                request_id=request.request_id,
                state=OperationState.ACCEPTED,
            )

    def _validate_acceptance_binding(
        self,
        actor: Actor,
        request: OperationRequest,
        approved_plan: ApprovedPlanRef,
        request_digest: str,
    ) -> None:
        if approved_plan.actor_id != actor.actor_id:
            raise OperationConflict("Approval actor does not match authenticated actor.")
        approval_payload = approved_plan.to_dict()
        approval_payload.pop("approval_digest")
        if not hmac.compare_digest(
            approved_plan.approval_digest, canonical_digest(approval_payload)
        ):
            raise OperationConflict("Approval digest does not match its binding claims.")
        expected = (
            approved_plan.request_digest == request_digest
            and approved_plan.host_id == request.host_id
            and approved_plan.app == request.app
            and approved_plan.environment == request.environment
            and approved_plan.revision_id == request.revision_id
            and approved_plan.revision_digest == request.revision_digest
        )
        if not expected:
            raise OperationConflict("Approval does not bind the normalized request.")

    def get(self, operation_id: str) -> OperationRef:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT operation_id, request_id, state FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError(operation_id)
        return OperationRef(
            operation_id=row["operation_id"],
            request_id=row["request_id"],
            state=OperationState(row["state"]),
        )

    def append(
        self,
        event: OperationEvent,
        *,
        lease_owner: Optional[str] = None,
        fencing_token: Optional[int] = None,
    ) -> None:
        payload_json = self._bounded_json(event)
        with self._transaction() as connection:
            self._assert_fence(connection, event.operation_id, lease_owner, fencing_token)
            self._append_event(connection, event, payload_json, allow_terminal=False)

    def _append_event(
        self,
        connection: sqlite3.Connection,
        event: OperationEvent,
        payload_json: str,
        *,
        allow_terminal: bool,
    ) -> None:
        operation = connection.execute(
            """
            SELECT state, host_id, app, environment, revision_id
            FROM operations WHERE operation_id = ?
            """,
            (event.operation_id,),
        ).fetchone()
        if operation is None:
            raise OperationConflict("Cannot append an event for an unknown operation.")
        if OperationState(operation["state"]).terminal:
            raise OperationConflict("Cannot append an event after terminal commit.")
        if event.state.terminal and not allow_terminal:
            raise OperationConflict(
                "Terminal state may only be appended with an atomic receipt commit."
            )
        if (
            event.host_id != operation["host_id"]
            or event.app != operation["app"]
            or event.environment != operation["environment"]
            or (
                event.revision_id is not None
                and event.revision_id != operation["revision_id"]
            )
        ):
            raise OperationConflict("Event identity does not match its operation.")

        previous = connection.execute(
            """
            SELECT sequence, event_digest FROM operation_events
            WHERE operation_id = ? ORDER BY sequence DESC LIMIT 1
            """,
            (event.operation_id,),
        ).fetchone()
        expected_sequence = 1 if previous is None else int(previous["sequence"]) + 1
        expected_previous = None if previous is None else previous["event_digest"]
        if event.sequence != expected_sequence:
            raise OperationConflict(
                "Event sequence must be the next strict per-operation sequence."
            )
        if event.previous_event_digest != expected_previous:
            raise OperationConflict("Event digest chain does not match durable history.")

        connection.execute(
            """
            INSERT INTO operation_events(
                operation_id, sequence, event_id, event_digest, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                event.operation_id,
                event.sequence,
                event.event_id,
                canonical_digest(event),
                payload_json,
            ),
        )
        connection.execute(
            "UPDATE operations SET state = ? WHERE operation_id = ?",
            (event.state.value, event.operation_id),
        )

    def events(self, operation_id: str) -> Tuple[OperationEvent, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT payload_json FROM operation_events
                WHERE operation_id = ? ORDER BY sequence
                """,
                (operation_id,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._event_from_json(row["payload_json"]) for row in rows)

    @staticmethod
    def _event_from_json(payload_json: str) -> OperationEvent:
        payload = json.loads(payload_json)
        return OperationEvent(
            event_id=payload["event_id"],
            operation_id=payload["operation_id"],
            sequence=payload["sequence"],
            event_type=payload["event_type"],
            occurred_at=payload["occurred_at"],
            state=OperationState(payload["state"]),
            host_id=payload["host_id"],
            app=payload["app"],
            environment=payload["environment"],
            revision_id=payload.get("revision_id"),
            message=payload.get("message"),
            evidence_digests=tuple(payload.get("evidence_digests", ())),
            previous_event_digest=payload.get("previous_event_digest"),
        )

    def acquire_lease(
        self,
        operation_id: str,
        owner_id: str,
        ttl_seconds: float,
    ) -> OperationLease:
        self._validate_lease_input(owner_id, ttl_seconds)
        now = self._clock()
        with self._transaction() as connection:
            self._require_nonterminal_operation(connection, operation_id)
            row = connection.execute(
                """
                SELECT owner_id, fencing_token, expires_at
                FROM operation_leases WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if row is not None and row["owner_id"] is not None and row["expires_at"] > now:
                if row["owner_id"] != owner_id:
                    raise LeaseConflict("Operation lease is held by another owner.")
                token = int(row["fencing_token"])
            else:
                token = 1 if row is None else int(row["fencing_token"]) + 1
            expires_at = now + ttl_seconds
            connection.execute(
                """
                INSERT INTO operation_leases(
                    operation_id, owner_id, fencing_token, expires_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    fencing_token = excluded.fencing_token,
                    expires_at = excluded.expires_at
                """,
                (operation_id, owner_id, token, expires_at),
            )
            return OperationLease(operation_id, owner_id, token, expires_at)

    def heartbeat_lease(
        self,
        operation_id: str,
        owner_id: str,
        fencing_token: int,
        ttl_seconds: float,
    ) -> OperationLease:
        self._validate_lease_input(owner_id, ttl_seconds)
        now = self._clock()
        with self._transaction() as connection:
            row = self._active_lease(
                connection, operation_id, owner_id, fencing_token, now
            )
            expires_at = now + ttl_seconds
            connection.execute(
                "UPDATE operation_leases SET expires_at = ? WHERE operation_id = ?",
                (expires_at, operation_id),
            )
            return OperationLease(
                operation_id, owner_id, int(row["fencing_token"]), expires_at
            )

    def release_lease(
        self,
        operation_id: str,
        owner_id: str,
        fencing_token: int,
    ) -> None:
        now = self._clock()
        with self._transaction() as connection:
            self._active_lease(connection, operation_id, owner_id, fencing_token, now)
            connection.execute(
                """
                UPDATE operation_leases
                SET owner_id = NULL, expires_at = ?
                WHERE operation_id = ?
                """,
                (now, operation_id),
            )

    @staticmethod
    def _validate_lease_input(owner_id: str, ttl_seconds: float) -> None:
        require_text(owner_id, "owner_id", 255)
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or ttl_seconds <= 0
            or ttl_seconds > 86400
        ):
            raise ContractValidationError(
                "ttl_seconds must be greater than zero and no more than 86400."
            )

    @staticmethod
    def _require_nonterminal_operation(
        connection: sqlite3.Connection, operation_id: str
    ) -> None:
        row = connection.execute(
            "SELECT state FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise OperationConflict("Unknown operation.")
        if OperationState(row["state"]).terminal:
            raise OperationConflict("Terminal operations cannot be leased.")

    def _active_lease(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        owner_id: str,
        fencing_token: int,
        now: float,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT owner_id, fencing_token, expires_at
            FROM operation_leases WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if (
            row is None
            or row["owner_id"] != owner_id
            or int(row["fencing_token"]) != fencing_token
            or float(row["expires_at"]) <= now
        ):
            raise LeaseConflict("Lease owner or fencing token is stale.")
        return row

    def _assert_fence(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        owner_id: Optional[str],
        fencing_token: Optional[int],
    ) -> None:
        row = connection.execute(
            "SELECT 1 FROM operation_leases WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            if owner_id is not None or fencing_token is not None:
                raise LeaseConflict("No durable lease exists for this operation.")
            return
        if owner_id is None or fencing_token is None:
            raise LeaseConflict("A leased operation requires its owner and fencing token.")
        self._active_lease(
            connection, operation_id, owner_id, fencing_token, self._clock()
        )

    def commit_receipt(
        self,
        receipt: TerminalReceipt,
        *,
        lease_owner: Optional[str] = None,
        fencing_token: Optional[int] = None,
    ) -> None:
        validated = self._revalidate_receipt(receipt)
        receipt_json = self._bounded_json(validated)
        terminal_state = OperationState(validated.outcome.value)

        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM terminal_receipts WHERE operation_id = ?",
                (validated.operation_id,),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] == receipt_json:
                    return
                raise OperationConflict("Operation already has a different terminal receipt.")

            self._assert_fence(
                connection, validated.operation_id, lease_owner, fencing_token
            )
            operation = connection.execute(
                """
                SELECT o.*, a.plan_id, a.payload_json AS approval_json,
                       p.plan_digest, p.payload_json AS plan_json
                FROM operations AS o
                JOIN approvals AS a ON a.decision_id = o.decision_id
                JOIN plans AS p ON p.plan_id = a.plan_id
                WHERE o.operation_id = ?
                """,
                (validated.operation_id,),
            ).fetchone()
            if operation is None:
                raise OperationConflict("Cannot commit a receipt for an unknown operation.")
            if OperationState(operation["state"]).terminal:
                raise OperationConflict("Operation is already terminal.")
            plan_claims = json.loads(operation["plan_json"])
            if not (
                validated.operation == operation["operation_class"]
                and validated.plan_id == operation["plan_id"]
                and validated.plan_digest == operation["plan_digest"]
                and validated.decision_id == operation["decision_id"]
                and validated.host_id == operation["host_id"]
                and validated.app == operation["app"]
                and validated.environment == operation["environment"]
                and validated.desired_revision_id == operation["revision_id"]
                and validated.desired_revision_digest
                == plan_claims["revision_digest"]
                and list(validated.artifact_digests)
                == plan_claims["artifact_digests"]
            ):
                raise OperationConflict(
                    "Terminal receipt does not bind the accepted operation and plan."
                )

            previous = connection.execute(
                """
                SELECT sequence, event_digest FROM operation_events
                WHERE operation_id = ? ORDER BY sequence DESC LIMIT 1
                """,
                (validated.operation_id,),
            ).fetchone()
            sequence = 1 if previous is None else int(previous["sequence"]) + 1
            previous_digest = None if previous is None else previous["event_digest"]
            terminal_event = OperationEvent(
                event_id="event_" + uuid.uuid4().hex,
                operation_id=validated.operation_id,
                sequence=sequence,
                event_type="operation.terminal",
                occurred_at=validated.completed_at,
                state=terminal_state,
                host_id=validated.host_id,
                app=validated.app,
                environment=validated.environment,
                revision_id=validated.desired_revision_id,
                previous_event_digest=previous_digest,
            )
            self._append_event(
                connection,
                terminal_event,
                self._bounded_json(terminal_event),
                allow_terminal=True,
            )
            connection.execute(
                """
                INSERT INTO terminal_receipts(
                    receipt_id, operation_id, outcome, payload_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    validated.receipt_id,
                    validated.operation_id,
                    validated.outcome.value,
                    receipt_json,
                ),
            )

    @staticmethod
    def _revalidate_receipt(receipt: TerminalReceipt) -> TerminalReceipt:
        if not isinstance(receipt, TerminalReceipt):
            raise ContractValidationError("receipt must be a TerminalReceipt.")
        checks = tuple(replace(check) for check in receipt.verification.checks)
        verification = replace(receipt.verification, checks=checks)
        compensation = replace(receipt.compensation)
        return replace(
            receipt, verification=verification, compensation=compensation
        )

    def receipt_payload(self, operation_id: str) -> Optional[dict]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT payload_json FROM terminal_receipts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else json.loads(row["payload_json"])

    def integrity_check(self) -> None:
        connection = self._connect()
        try:
            self._check_integrity(connection)
        finally:
            connection.close()
