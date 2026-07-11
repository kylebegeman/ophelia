"""Authoritative SQLite journal for kernel operations."""

from __future__ import annotations

import hmac
import json
import math
import os
import sqlite3
import stat
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
    require_digest,
    require_text,
)
from ..domain.events import OperationEvent
from ..domain.operations import Actor, OperationRef, OperationRequest, OperationState
from ..domain.plans import ApprovedPlanRef, AuthorizationKind
from ..domain.receipts import (
    CheckStatus,
    CompensationResult,
    CompensationStatus,
    ReceiptEffect,
    ReceiptOutcome,
    TerminalReceipt,
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
)
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

    The journal directory and database files must be owned by the effective
    Ophelia UID. Descriptor-relative checks prevent final-component symlink
    traversal. As with the managed filesystem boundary, an untrusted process
    must not share that UID or rename its private journal directory.
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
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._prepare_database_file()
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
        self._repair_permissions()
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            journal_mode = self._enable_wal(connection)
            if journal_mode.lower() != "wal":
                raise IntegrityError("Operation database could not enable WAL mode.")
            migrate(connection)
            self._check_integrity(connection)
        finally:
            connection.close()
            self._repair_permissions()

    def _enable_wal(self, connection: sqlite3.Connection) -> str:
        """Serialize WAL negotiation during concurrent first open."""

        deadline = time.monotonic() + max(self._timeout_seconds, 0.0)
        delay = 0.005
        while True:
            try:
                return str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.1)

    def _open_journal_directory(self) -> int:
        required_flags = ("O_DIRECTORY", "O_NOFOLLOW")
        if any(not hasattr(os, name) for name in required_flags):
            raise IntegrityError(
                "This platform cannot safely open the operation journal directory."
            )
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(str(self.database_path.parent), flags)
        except OSError as exc:
            raise IntegrityError(
                "Operation journal directory must be a trusted real directory."
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise IntegrityError("Operation journal parent must be a directory.")
            if metadata.st_uid != os.geteuid():
                raise IntegrityError(
                    "Operation journal directory must be owned by the Ophelia UID."
                )
            os.fchmod(descriptor, 0o700)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _prepare_database_file(self) -> None:
        directory_descriptor = self._open_journal_directory()
        existing_flags = os.O_RDWR | os.O_NOFOLLOW
        existing_flags |= getattr(os, "O_CLOEXEC", 0)
        create_flags = existing_flags | os.O_CREAT | os.O_EXCL
        try:
            try:
                descriptor = os.open(
                    self.database_path.name,
                    create_flags,
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except FileExistsError:
                try:
                    descriptor = os.open(
                        self.database_path.name,
                        existing_flags,
                        dir_fd=directory_descriptor,
                    )
                except OSError as exc:
                    raise IntegrityError(
                        "Operation database must be a trusted regular file."
                    ) from exc
            except OSError as exc:
                raise IntegrityError(
                    "Operation database must be a trusted regular file."
                ) from exc
            try:
                self._repair_file_descriptor(descriptor)
            finally:
                os.close(descriptor)
        finally:
            os.close(directory_descriptor)

    @staticmethod
    def _repair_file_descriptor(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise IntegrityError("Operation journal files must be regular files.")
        if metadata.st_uid != os.geteuid():
            raise IntegrityError(
                "Operation journal files must be owned by the Ophelia UID."
            )
        os.fchmod(descriptor, 0o600)

    def _repair_permissions(self) -> None:
        directory_descriptor = self._open_journal_directory()
        flags = os.O_RDONLY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            for name in (
                self.database_path.name,
                self.database_path.name + "-wal",
                self.database_path.name + "-shm",
            ):
                try:
                    descriptor = os.open(
                        name,
                        flags,
                        dir_fd=directory_descriptor,
                    )
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise IntegrityError(
                        "Operation journal files must be trusted regular files."
                    ) from exc
                try:
                    self._repair_file_descriptor(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            os.close(directory_descriptor)

    @classmethod
    def _check_integrity(cls, connection: sqlite3.Connection) -> None:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick_check != "ok" or foreign_key_rows:
            raise IntegrityError("Operation database integrity validation failed.")
        try:
            operation_ids = connection.execute(
                "SELECT operation_id FROM operations ORDER BY operation_id"
            ).fetchall()
            for row in operation_ids:
                cls._verified_events(connection, row["operation_id"])
        except IntegrityError:
            raise
        except Exception as exc:
            raise IntegrityError(
                "Operation journal semantic integrity validation failed."
            ) from exc

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._repair_permissions()
            yield connection
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
            self._repair_permissions()

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
        idempotency_key_digest = digest_text(request.idempotency_key)
        request_payload = request.to_dict()
        request_payload.pop("idempotency_key")
        request_payload["idempotency_key_digest"] = idempotency_key_digest
        request_json = self._bounded_json(request_payload)
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
                (actor.actor_id, request.operation, idempotency_key_digest),
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

            now = self._clock_sample()
            if approved_plan.expired(
                datetime.fromtimestamp(now, timezone.utc)
            ):
                raise OperationConflict("Approval has expired.")

            operation_id = "operation_" + uuid.uuid4().hex
            accepted_at = datetime.fromtimestamp(
                now, timezone.utc
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
                    idempotency_key_digest,
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
            return self._verified_events(connection, operation_id)
        finally:
            connection.close()
            self._repair_permissions()

    @classmethod
    def _verified_events(
        cls,
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> Tuple[OperationEvent, ...]:
        operation = connection.execute(
            """
            SELECT o.*, a.plan_id, a.actor_id AS approval_actor_id,
                   a.approval_digest, a.payload_json AS approval_json,
                   p.plan_digest, p.request_digest AS plan_request_digest,
                   p.payload_json AS plan_json
            FROM operations AS o
            JOIN approvals AS a ON a.decision_id = o.decision_id
            JOIN plans AS p ON p.plan_id = a.plan_id
            WHERE o.operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if operation is None:
            raise IntegrityError("Event history references an unknown operation.")
        plan_claims, approval_claims = cls._verified_acceptance_claims(operation)

        rows = connection.execute(
            """
            SELECT sequence, event_id, event_digest, payload_json
            FROM operation_events
            WHERE operation_id = ? ORDER BY sequence
            """,
            (operation_id,),
        ).fetchall()
        events = []
        previous_digest = None
        for expected_sequence, row in enumerate(rows, start=1):
            try:
                event = cls._event_from_json(row["payload_json"])
                calculated_digest = canonical_digest(event)
            except Exception as exc:
                raise IntegrityError(
                    "Operation event payload integrity validation failed."
                ) from exc
            if (
                int(row["sequence"]) != expected_sequence
                or event.sequence != expected_sequence
                or event.operation_id != operation_id
                or event.event_id != row["event_id"]
                or event.host_id != operation["host_id"]
                or event.app != operation["app"]
                or event.environment != operation["environment"]
                or (
                    event.revision_id is not None
                    and event.revision_id != operation["revision_id"]
                )
                or row["event_digest"] != calculated_digest
                or event.previous_event_digest != previous_digest
                or canonical_json(event) != row["payload_json"]
            ):
                raise IntegrityError("Operation event chain integrity validation failed.")
            events.append(event)
            previous_digest = calculated_digest

        try:
            state = OperationState(operation["state"])
        except ValueError as exc:
            raise IntegrityError("Operation contains an unknown durable state.") from exc
        expected_state = (
            OperationState.ACCEPTED if not events else events[-1].state
        )
        if state is not expected_state:
            raise IntegrityError("Operation state does not match its final event.")

        receipt = connection.execute(
            """
            SELECT receipt_id, outcome, receipt_digest, payload_json
            FROM terminal_receipts WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        terminal_events = tuple(event for event in events if event.state.terminal)
        if receipt is None:
            if state.terminal or terminal_events:
                raise IntegrityError(
                    "Terminal operation state requires an atomic terminal receipt."
                )
            return tuple(events)

        try:
            receipt_payload = json.loads(receipt["payload_json"])
            receipt_contract = cls._receipt_from_payload(receipt_payload)
        except Exception as exc:
            raise IntegrityError("Terminal receipt payload is invalid.") from exc
        if (
            canonical_json(receipt_contract) != receipt["payload_json"]
            or canonical_digest(receipt_contract) != receipt["receipt_digest"]
            or receipt_payload.get("receipt_id") != receipt["receipt_id"]
            or receipt_payload.get("operation_id") != operation_id
            or receipt_payload.get("outcome") != receipt["outcome"]
            or receipt_contract.operation != operation["operation_class"]
            or receipt_contract.plan_id != operation["plan_id"]
            or receipt_contract.plan_digest != operation["plan_digest"]
            or receipt_contract.decision_id != operation["decision_id"]
            or receipt_contract.host_id != operation["host_id"]
            or receipt_contract.app != operation["app"]
            or receipt_contract.environment != operation["environment"]
            or receipt_contract.desired_revision_id != operation["revision_id"]
            or receipt_contract.desired_revision_digest
            != plan_claims["revision_digest"]
            or list(receipt_contract.artifact_digests)
            != plan_claims["artifact_digests"]
            or approval_claims["decision_id"] != receipt_contract.decision_id
            or not state.terminal
            or receipt["outcome"] != state.value
            or len(terminal_events) != 1
            or terminal_events[0] is not events[-1]
            or terminal_events[0].event_type != "operation.terminal"
            or terminal_events[0].state is not state
            or terminal_events[0].occurred_at != receipt_payload.get("completed_at")
        ):
            raise IntegrityError(
                "Terminal event, operation state, and receipt do not reconcile."
            )
        return tuple(events)

    @staticmethod
    def _verified_acceptance_claims(
        operation: sqlite3.Row,
    ) -> Tuple[dict, dict]:
        try:
            request_claims = json.loads(operation["request_json"])
            actor_claims = json.loads(operation["actor_json"])
            plan_claims = json.loads(operation["plan_json"])
            approval_claims = json.loads(operation["approval_json"])
            if not all(
                isinstance(value, dict)
                for value in (request_claims, actor_claims, plan_claims, approval_claims)
            ):
                raise TypeError("accepted claims must be JSON objects")
            require_digest(
                request_claims["idempotency_key_digest"],
                "idempotency_key_digest",
            )
            request_contract = OperationRequest(
                request_id=request_claims["request_id"],
                operation=request_claims["operation"],
                host_id=request_claims["host_id"],
                app=request_claims["app"],
                environment=request_claims["environment"],
                revision_id=request_claims["revision_id"],
                revision_digest=request_claims["revision_digest"],
                idempotency_key="redacted",
            )
            expected_request_claims = request_contract.to_dict()
            expected_request_claims.pop("idempotency_key")
            expected_request_claims["idempotency_key_digest"] = request_claims[
                "idempotency_key_digest"
            ]
            actor_contract = Actor(
                actor_id=actor_claims["actor_id"],
                source=actor_claims["source"],
                authenticated_by=actor_claims["authenticated_by"],
            )
            approval_contract = SQLiteOperationJournal._approved_plan_from_payload(
                approval_claims
            )
            expected_plan_claims = {
                "schema_version": approval_contract.schema_version,
                "kind": "ophelia.kernel.approved_plan_claims",
                "plan_id": approval_contract.plan_id,
                "plan_digest": approval_contract.plan_digest,
                "request_digest": approval_contract.request_digest,
                "host_id": approval_contract.host_id,
                "app": approval_contract.app,
                "environment": approval_contract.environment,
                "revision_id": approval_contract.revision_id,
                "revision_digest": approval_contract.revision_digest,
                "manifest_digest": approval_contract.manifest_digest,
                "artifact_digests": list(approval_contract.artifact_digests),
                "observed_state_digest": approval_contract.observed_state_digest,
                "policy_digest": approval_contract.policy_digest,
            }
            approval_binding = approval_contract.to_dict()
            approval_binding.pop("approval_digest")
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrityError("Accepted operation payload is invalid.") from exc

        if (
            canonical_json(expected_request_claims) != operation["request_json"]
            or canonical_json(actor_contract) != operation["actor_json"]
            or canonical_json(expected_plan_claims) != operation["plan_json"]
            or canonical_json(approval_contract) != operation["approval_json"]
            or request_contract.request_id != operation["request_id"]
            or request_contract.operation != operation["operation_class"]
            or request_contract.host_id != operation["host_id"]
            or request_contract.app != operation["app"]
            or request_contract.environment != operation["environment"]
            or request_contract.revision_id != operation["revision_id"]
            or request_contract.intent_digest() != operation["request_digest"]
            or actor_contract.actor_id != operation["actor_id"]
            or approval_contract.plan_id != operation["plan_id"]
            or approval_contract.plan_digest != operation["plan_digest"]
            or approval_contract.request_digest != operation["plan_request_digest"]
            or operation["request_digest"] != operation["plan_request_digest"]
            or approval_contract.host_id != operation["host_id"]
            or approval_contract.app != operation["app"]
            or approval_contract.environment != operation["environment"]
            or approval_contract.revision_id != operation["revision_id"]
            or approval_contract.revision_digest != request_contract.revision_digest
            or approval_contract.decision_id != operation["decision_id"]
            or approval_contract.actor_id != operation["approval_actor_id"]
            or approval_contract.approval_digest != operation["approval_digest"]
            or not isinstance(operation["approval_digest"], str)
            or not hmac.compare_digest(
                operation["approval_digest"], canonical_digest(approval_binding)
            )
        ):
            raise IntegrityError(
                "Accepted operation, plan, and approval claims do not reconcile."
            )
        return expected_plan_claims, approval_contract.to_dict()

    @staticmethod
    def _approved_plan_from_payload(payload: dict) -> ApprovedPlanRef:
        return ApprovedPlanRef(
            plan_id=payload["plan_id"],
            plan_digest=payload["plan_digest"],
            request_digest=payload["request_digest"],
            manifest_digest=payload["manifest_digest"],
            artifact_digests=tuple(payload["artifact_digests"]),
            observed_state_digest=payload["observed_state_digest"],
            policy_digest=payload["policy_digest"],
            host_id=payload["host_id"],
            app=payload["app"],
            environment=payload["environment"],
            revision_id=payload["revision_id"],
            revision_digest=payload["revision_digest"],
            actor_id=payload["actor_id"],
            decision_id=payload["decision_id"],
            authorization_kind=AuthorizationKind(payload["authorization_kind"]),
            issuer=payload["issuer"],
            audience=payload["audience"],
            approved_at=payload["approved_at"],
            expires_at=payload["expires_at"],
            nonce_digest=payload["nonce_digest"],
            approval_digest=payload["approval_digest"],
        )

    @staticmethod
    def _receipt_from_payload(payload: object) -> TerminalReceipt:
        if not isinstance(payload, dict):
            raise ContractValidationError("Terminal receipt payload must be a mapping.")
        verification_payload = payload["verification"]
        compensation_payload = payload["compensation"]
        if not isinstance(verification_payload, dict) or not isinstance(
            compensation_payload, dict
        ):
            raise ContractValidationError(
                "Terminal receipt evidence must be represented as mappings."
            )
        checks_payload = verification_payload["checks"]
        if not isinstance(checks_payload, list):
            raise ContractValidationError("Verification checks must be a list.")
        checks = tuple(
            VerificationCheck(
                name=check["name"],
                status=CheckStatus(check["status"]),
                observed_digest=check.get("observed_digest"),
                summary=check.get("summary"),
            )
            for check in checks_payload
        )
        verification = VerificationResult(
            status=VerificationStatus(verification_payload["status"]),
            observed_revision_digest=verification_payload.get(
                "observed_revision_digest"
            ),
            observed_state_digest=verification_payload.get("observed_state_digest"),
            observed_at=verification_payload["observed_at"],
            checks=checks,
        )
        compensation = CompensationResult(
            attempted=compensation_payload["attempted"],
            status=CompensationStatus(compensation_payload["status"]),
            restored_revision_id=compensation_payload.get("restored_revision_id"),
            restored_revision_digest=compensation_payload.get(
                "restored_revision_digest"
            ),
            evidence_digests=tuple(compensation_payload.get("evidence_digests", ())),
        )
        return TerminalReceipt(
            receipt_id=payload["receipt_id"],
            operation_id=payload["operation_id"],
            operation=payload["operation"],
            plan_id=payload["plan_id"],
            plan_digest=payload["plan_digest"],
            decision_id=payload["decision_id"],
            host_id=payload["host_id"],
            app=payload["app"],
            environment=payload["environment"],
            previous_revision_id=payload.get("previous_revision_id"),
            desired_revision_id=payload["desired_revision_id"],
            desired_revision_digest=payload["desired_revision_digest"],
            active_revision_id=payload.get("active_revision_id"),
            active_revision_digest=payload.get("active_revision_digest"),
            artifact_digests=tuple(payload["artifact_digests"]),
            verification=verification,
            compensation=compensation,
            effect=ReceiptEffect(payload["effect"]),
            outcome=ReceiptOutcome(payload["outcome"]),
            started_at=payload["started_at"],
            completed_at=payload["completed_at"],
            inputs_redacted=payload["inputs_redacted"],
        )

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
        with self._transaction() as connection:
            self._require_nonterminal_operation(connection, operation_id)
            now = self._clock_sample()
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
            proposed_expiration = now + ttl_seconds
            expires_at = (
                max(float(row["expires_at"]), proposed_expiration)
                if row is not None and row["owner_id"] == owner_id
                else proposed_expiration
            )
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
        with self._transaction() as connection:
            now = self._clock_sample()
            row = self._active_lease(
                connection, operation_id, owner_id, fencing_token, now
            )
            expires_at = max(float(row["expires_at"]), now + ttl_seconds)
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
        with self._transaction() as connection:
            now = self._clock_sample()
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
            or not SQLiteOperationJournal._is_finite_number(ttl_seconds)
            or ttl_seconds <= 0
            or ttl_seconds > 86400
        ):
            raise ContractValidationError(
                "ttl_seconds must be greater than zero and no more than 86400."
            )

    def _clock_sample(self) -> float:
        now = self._clock()
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not self._is_finite_number(now)
        ):
            raise IntegrityError("Operation journal clock returned a non-finite sample.")
        return float(now)

    @staticmethod
    def _is_finite_number(value: int | float) -> bool:
        try:
            return math.isfinite(float(value))
        except (OverflowError, TypeError, ValueError):
            return False

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
            connection, operation_id, owner_id, fencing_token, self._clock_sample()
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
            self._assert_fence(
                connection, validated.operation_id, lease_owner, fencing_token
            )
            existing = connection.execute(
                "SELECT payload_json FROM terminal_receipts WHERE operation_id = ?",
                (validated.operation_id,),
            ).fetchone()
            if existing is not None:
                self._verified_events(connection, validated.operation_id)
                if existing["payload_json"] == receipt_json:
                    return
                raise OperationConflict("Operation already has a different terminal receipt.")
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
                    receipt_id, operation_id, outcome, receipt_digest, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    validated.receipt_id,
                    validated.operation_id,
                    validated.outcome.value,
                    canonical_digest(validated),
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
            if row is not None:
                self._verified_events(connection, operation_id)
                try:
                    payload = json.loads(row["payload_json"])
                except Exception as exc:
                    raise IntegrityError("Terminal receipt payload is invalid.") from exc
                if not isinstance(payload, dict):
                    raise IntegrityError("Terminal receipt payload is invalid.")
                return payload
            return None
        finally:
            connection.close()

    def integrity_check(self) -> None:
        connection = self._connect()
        try:
            self._check_integrity(connection)
        finally:
            connection.close()
