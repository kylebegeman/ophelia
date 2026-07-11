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
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional, Tuple

from ..domain._contracts import (
    ContractValidationError,
    canonical_digest,
    canonical_json,
    digest_text,
    parse_utc,
    require_digest,
    require_text,
)
from ..domain.events import OperationEvent
from ..domain.operations import Actor, OperationRef, OperationRequest, OperationState
from ..domain.plans import (
    ApprovedPlanRef,
    AuthorizationKind,
    CompensationAction,
    OperationPlan,
    PlanPhase,
    PlanStep,
)
from ..domain.revisions import Revision, RevisionState, Workload, WorkloadKind
from .contracts import (
    ActiveRevision,
    ExecutionControl,
    ExecutionFence,
    ExecutionInput,
    RevisionArtifactRef,
    RevisionLifecycleEntry,
)
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


OperationLease = ExecutionFence


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
            input_rows = connection.execute(
                "SELECT operation_id FROM operation_inputs ORDER BY operation_id"
            ).fetchall()
            for row in input_rows:
                cls._load_execution_input_tx(connection, row["operation_id"])
            control_rows = connection.execute(
                "SELECT operation_id FROM operation_controls ORDER BY operation_id"
            ).fetchall()
            for row in control_rows:
                cls._control_tx(connection, row["operation_id"])
            lifecycle_rows = connection.execute(
                """
                SELECT DISTINCT operation_id FROM revision_lifecycle
                ORDER BY operation_id
                """
            ).fetchall()
            for row in lifecycle_rows:
                execution_input = cls._load_execution_input_tx(
                    connection, row["operation_id"]
                )
                history = cls._verified_revision_history(
                    connection, row["operation_id"]
                )
                if any(
                    entry.revision_id
                    != execution_input.revision.revision_id
                    or entry.revision_digest
                    != execution_input.revision.content_digest()
                    for entry in history
                ):
                    raise IntegrityError(
                        "Revision lifecycle does not bind its execution input."
                    )
            active_rows = connection.execute(
                "SELECT * FROM active_revisions ORDER BY host_id, app, environment"
            ).fetchall()
            active_by_scope = {}
            for row in active_rows:
                active = cls._active_revision_from_row(row)
                execution_input = cls._load_execution_input_tx(
                    connection, active.operation_id
                )
                history = cls._verified_revision_history(
                    connection, active.operation_id
                )
                if (
                    canonical_json(active) != row["payload_json"]
                    or not history
                    or history[-1].state is not RevisionState.ACTIVE
                    or active.revision_id
                    != execution_input.revision.revision_id
                    or active.revision_digest
                    != execution_input.revision.content_digest()
                    or active.host_id != execution_input.plan.host_id
                    or active.app != execution_input.plan.app
                    or active.environment
                    != execution_input.plan.environment
                ):
                    raise IntegrityError(
                        "Active revision does not reconcile with lifecycle and input."
                    )
                active_by_scope[(active.host_id, active.app, active.environment)] = active
            atomic_rows = connection.execute(
                """
                SELECT a.operation_id, a.active_generation, r.outcome,
                       r.payload_json AS receipt_json,
                       o.host_id, o.app, o.environment
                FROM atomic_success_commits AS a
                JOIN terminal_receipts AS r ON r.operation_id = a.operation_id
                JOIN operations AS o ON o.operation_id = a.operation_id
                ORDER BY o.host_id, o.app, o.environment,
                         a.active_generation, a.operation_id
                """
            ).fetchall()
            latest_atomic_by_scope = {}
            for row in atomic_rows:
                scope = (row["host_id"], row["app"], row["environment"])
                previous = latest_atomic_by_scope.get(scope)
                generation = int(row["active_generation"])
                history = cls._verified_revision_history(
                    connection, row["operation_id"]
                )
                receipt = cls._receipt_from_payload(json.loads(row["receipt_json"]))
                execution_input = cls._load_execution_input_tx(
                    connection, row["operation_id"]
                )
                if (
                    row["outcome"] != ReceiptOutcome.SUCCEEDED.value
                    or not history
                    or history[-1].state is not RevisionState.ACTIVE
                    or receipt.operation_id != row["operation_id"]
                    or receipt.desired_revision_id
                    != execution_input.revision.revision_id
                    or receipt.desired_revision_digest
                    != execution_input.revision.content_digest()
                    or history[-1].revision_id != receipt.desired_revision_id
                    or history[-1].revision_digest
                    != receipt.desired_revision_digest
                    or (
                        previous is not None
                        and generation <= previous[1]
                    )
                ):
                    raise IntegrityError(
                        "Atomic success marker does not reconcile with its receipt and lifecycle."
                    )
                latest_atomic_by_scope[scope] = (row["operation_id"], generation)
            for scope, latest in latest_atomic_by_scope.items():
                active = active_by_scope.get(scope)
                if (
                    active is None
                    or active.generation < latest[1]
                    or (
                        active.generation == latest[1]
                        and active.operation_id != latest[0]
                    )
                ):
                    raise IntegrityError(
                        "Current active revision conflicts with atomic success history."
                    )
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
        *,
        execution_input: Optional[ExecutionInput] = None,
    ) -> OperationRef:
        request_digest = request.intent_digest()
        self._validate_acceptance_binding(
            actor, request, approved_plan, request_digest
        )
        if execution_input is not None:
            self._validate_execution_input(
                request, approved_plan, execution_input, request_digest
            )
        actor_json = self._bounded_json(actor)
        idempotency_key_digest = digest_text(request.idempotency_key)
        request_payload = request.to_dict()
        request_payload.pop("idempotency_key")
        request_payload["idempotency_key_digest"] = idempotency_key_digest
        request_json = self._bounded_json(request_payload)
        approval_json = self._bounded_json(approved_plan)
        input_json = (
            None if execution_input is None else self._bounded_json(execution_input)
        )
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
                SELECT i.request_digest, o.operation_id, o.request_id, o.state,
                       oi.input_digest
                FROM idempotency_keys AS i
                JOIN operations AS o ON o.operation_id = i.operation_id
                LEFT JOIN operation_inputs AS oi ON oi.operation_id = o.operation_id
                WHERE i.actor_id = ? AND i.operation_class = ? AND i.idempotency_key_digest = ?
                """,
                (actor.actor_id, request.operation, idempotency_key_digest),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise IdempotencyConflict(
                        "Idempotency key already identifies different normalized intent."
                    )
                supplied_digest = (
                    None if execution_input is None else execution_input.input_digest
                )
                if existing["input_digest"] != supplied_digest:
                    raise IdempotencyConflict(
                        "Idempotent retry must supply the identical execution input digest."
                    )
                return OperationRef(
                    operation_id=existing["operation_id"],
                    request_id=existing["request_id"],
                    state=OperationState(existing["state"]),
                )

            now = self._clock_sample()
            if execution_input is not None and parse_utc(
                execution_input.deadline
            ) <= datetime.fromtimestamp(now, timezone.utc):
                raise OperationConflict("Execution deadline has expired.")
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
            if execution_input is not None:
                connection.execute(
                    """
                    INSERT INTO operation_inputs(
                        operation_id, input_digest, deadline, payload_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        execution_input.input_digest,
                        execution_input.deadline,
                        input_json,
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

    @staticmethod
    def _validate_execution_input(
        request: OperationRequest,
        approved_plan: ApprovedPlanRef,
        execution_input: ExecutionInput,
        request_digest: str,
    ) -> None:
        if not isinstance(execution_input, ExecutionInput):
            raise ContractValidationError("execution_input must be an ExecutionInput.")
        if parse_utc(execution_input.deadline) > parse_utc(
            approved_plan.expires_at
        ):
            raise OperationConflict(
                "Execution deadline cannot outlive the approved plan."
            )
        if not (
            execution_input.plan.request_digest == request_digest
            and execution_input.plan.operation == request.operation
            and execution_input.plan.host_id == request.host_id
            and execution_input.plan.app == request.app
            and execution_input.plan.environment == request.environment
            and execution_input.plan.revision_id == request.revision_id
            and execution_input.plan.revision_digest == request.revision_digest
            and canonical_json(execution_input.approved_plan)
            == canonical_json(approved_plan)
        ):
            raise OperationConflict(
                "Execution input does not bind the accepted request and approval."
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

    @staticmethod
    def _operation_plan_from_payload(payload: dict) -> OperationPlan:
        steps_payload = payload["steps"]
        if not isinstance(steps_payload, list):
            raise ContractValidationError("Operation plan steps must be a list.")
        return OperationPlan(
            plan_id=payload["plan_id"],
            request_digest=payload["request_digest"],
            operation=payload["operation"],
            host_id=payload["host_id"],
            app=payload["app"],
            environment=payload["environment"],
            revision_id=payload["revision_id"],
            revision_digest=payload["revision_digest"],
            manifest_digest=payload["manifest_digest"],
            artifact_digests=tuple(payload["artifact_digests"]),
            observed_state_digest=payload["observed_state_digest"],
            policy_digest=payload["policy_digest"],
            steps=tuple(
                PlanStep(
                    order=step["order"],
                    phase=PlanPhase(step["phase"]),
                    mutates_runtime=step["mutates_runtime"],
                    desired_effect_digest=step["desired_effect_digest"],
                    compensation=CompensationAction(step["compensation"]),
                )
                for step in steps_payload
            ),
            blocker_codes=tuple(payload["blocker_codes"]),
            created_at=payload["created_at"],
        )

    @staticmethod
    def _revision_from_payload(payload: dict) -> Revision:
        workloads_payload = payload["workloads"]
        if not isinstance(workloads_payload, list):
            raise ContractValidationError("Revision workloads must be a list.")
        return Revision(
            revision_id=payload["revision_id"],
            app=payload["app"],
            environment=payload["environment"],
            manifest_digest=payload["manifest_digest"],
            artifact_digests=tuple(payload["artifact_digests"]),
            renderer_version=payload["renderer_version"],
            workloads=tuple(
                Workload(
                    workload_id=workload["workload_id"],
                    workload_kind=WorkloadKind(workload["workload_kind"]),
                    artifact_digest=workload["artifact_digest"],
                    route_ids=tuple(workload.get("route_ids", ())),
                    overlap_safe=workload.get("overlap_safe", False),
                )
                for workload in workloads_payload
            ),
            created_at=payload["created_at"],
        )

    @classmethod
    def _execution_input_from_payload(cls, payload: object) -> ExecutionInput:
        if not isinstance(payload, dict):
            raise ContractValidationError("Execution input payload must be a mapping.")
        artifact_payload = payload["artifact_ref"]
        if not isinstance(artifact_payload, dict):
            raise ContractValidationError("artifact_ref must be a mapping.")
        return ExecutionInput(
            plan=cls._operation_plan_from_payload(payload["plan"]),
            approved_plan=cls._approved_plan_from_payload(payload["approved_plan"]),
            revision=cls._revision_from_payload(payload["revision"]),
            artifact_ref=RevisionArtifactRef(
                revision_id=artifact_payload["revision_id"],
                revision_digest=artifact_payload["revision_digest"],
                relative_root=artifact_payload["relative_root"],
                artifact_digest=artifact_payload["artifact_digest"],
            ),
            deadline=payload["deadline"],
            input_digest=payload["input_digest"],
        )

    @staticmethod
    def _accepted_operation_row(
        connection: sqlite3.Connection, operation_id: str
    ) -> Optional[sqlite3.Row]:
        return connection.execute(
            """
            SELECT o.*, a.plan_id, a.actor_id AS approval_actor_id,
                   a.approval_digest, a.payload_json AS approval_json,
                   p.plan_digest, p.request_digest AS plan_request_digest,
                   p.payload_json AS plan_json,
                   oi.input_digest, oi.deadline, oi.payload_json AS input_json
            FROM operations AS o
            JOIN approvals AS a ON a.decision_id = o.decision_id
            JOIN plans AS p ON p.plan_id = a.plan_id
            LEFT JOIN operation_inputs AS oi ON oi.operation_id = o.operation_id
            WHERE o.operation_id = ?
            """,
            (operation_id,),
        ).fetchone()

    @classmethod
    def _load_execution_input_tx(
        cls, connection: sqlite3.Connection, operation_id: str
    ) -> ExecutionInput:
        operation = cls._accepted_operation_row(connection, operation_id)
        if operation is None or operation["input_json"] is None:
            raise KeyError(operation_id)
        try:
            cls._verified_acceptance_claims(operation)
            payload = json.loads(operation["input_json"])
            execution_input = cls._execution_input_from_payload(payload)
            request_payload = json.loads(operation["request_json"])
            request = OperationRequest(
                request_id=request_payload["request_id"],
                operation=request_payload["operation"],
                host_id=request_payload["host_id"],
                app=request_payload["app"],
                environment=request_payload["environment"],
                revision_id=request_payload["revision_id"],
                revision_digest=request_payload["revision_digest"],
                idempotency_key="redacted",
            )
            cls._validate_execution_input(
                request,
                cls._approved_plan_from_payload(
                    json.loads(operation["approval_json"])
                ),
                execution_input,
                operation["request_digest"],
            )
            if (
                execution_input.input_digest != operation["input_digest"]
                or execution_input.deadline != operation["deadline"]
                or canonical_json(execution_input) != operation["input_json"]
            ):
                raise IntegrityError(
                    "Execution input durable columns do not match canonical content."
                )
            return execution_input
        except IntegrityError:
            raise
        except Exception as exc:
            raise IntegrityError(
                "Execution input, operation, plan, approval, revision, and artifact claims do not reconcile."
            ) from exc

    def load_execution_input(self, operation_id: str) -> ExecutionInput:
        connection = self._connect()
        try:
            return self._load_execution_input_tx(connection, operation_id)
        finally:
            connection.close()
            self._repair_permissions()

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

    @staticmethod
    def _actor_from_payload(payload: object) -> Actor:
        if not isinstance(payload, dict):
            raise ContractValidationError("Actor payload must be a mapping.")
        return Actor(
            actor_id=payload["actor_id"],
            source=payload["source"],
            authenticated_by=payload["authenticated_by"],
        )

    @classmethod
    def _control_tx(
        cls, connection: sqlite3.Connection, operation_id: str
    ) -> ExecutionControl:
        operation = connection.execute(
            """
            SELECT o.operation_id, oi.deadline, c.cancellation_actor_json,
                   c.cancellation_requested_at
            FROM operations AS o
            LEFT JOIN operation_inputs AS oi ON oi.operation_id = o.operation_id
            LEFT JOIN operation_controls AS c ON c.operation_id = o.operation_id
            WHERE o.operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if operation is None:
            raise KeyError(operation_id)
        try:
            actor_json = operation["cancellation_actor_json"]
            actor = (
                None
                if actor_json is None
                else cls._actor_from_payload(json.loads(actor_json))
            )
            control = ExecutionControl(
                operation_id=operation_id,
                deadline=operation["deadline"],
                cancellation_requested=actor is not None,
                cancellation_actor=actor,
                cancellation_requested_at=operation["cancellation_requested_at"],
            )
            if actor_json is not None and canonical_json(actor) != actor_json:
                raise IntegrityError("Cancellation actor payload is not canonical.")
            return control
        except IntegrityError:
            raise
        except Exception as exc:
            raise IntegrityError("Operation control payload is invalid.") from exc

    def control(self, operation_id: str) -> ExecutionControl:
        connection = self._connect()
        try:
            return self._control_tx(connection, operation_id)
        finally:
            connection.close()

    def cancellation_requested(self, operation_id: str) -> bool:
        return self.control(operation_id).cancellation_requested

    def request_cancellation(
        self, operation_id: str, actor: Actor
    ) -> ExecutionControl:
        if not isinstance(actor, Actor):
            raise ContractValidationError(
                "cancellation actor must be a server-derived Actor."
            )
        actor_json = self._bounded_json(actor)
        with self._transaction() as connection:
            operation = connection.execute(
                "SELECT state FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise OperationConflict("Unknown operation.")
            if OperationState(operation["state"]).terminal:
                raise OperationConflict(
                    "A terminal operation cannot request cancellation."
                )
            existing = connection.execute(
                """
                SELECT cancellation_actor_json FROM operation_controls
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if existing is None:
                requested_at = datetime.fromtimestamp(
                    self._clock_sample(), timezone.utc
                ).isoformat().replace("+00:00", "Z")
                connection.execute(
                    """
                    INSERT INTO operation_controls(
                        operation_id, cancellation_actor_json,
                        cancellation_requested_at
                    ) VALUES (?, ?, ?)
                    """,
                    (operation_id, actor_json, requested_at),
                )
            return self._control_tx(connection, operation_id)

    def list_recoverable(self, limit: int = 100) -> Tuple[OperationRef, ...]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > 1000
        ):
            raise ContractValidationError(
                "limit must be an integer from 1 through 1000."
            )
        connection = self._connect()
        try:
            now = self._clock_sample()
            rows = connection.execute(
                """
                SELECT o.operation_id, o.request_id, o.state
                FROM operations AS o
                JOIN operation_inputs AS oi ON oi.operation_id = o.operation_id
                LEFT JOIN operation_leases AS l ON l.operation_id = o.operation_id
                WHERE o.state NOT IN (?, ?, ?, ?)
                  AND (l.operation_id IS NULL OR l.owner_id IS NULL OR l.expires_at <= ?)
                ORDER BY o.accepted_at, o.operation_id
                LIMIT ?
                """,
                (
                    OperationState.SUCCEEDED.value,
                    OperationState.FAILED_COMPENSATED.value,
                    OperationState.FAILED_UNCOMPENSATED.value,
                    OperationState.CANCELLED.value,
                    now,
                    limit,
                ),
            ).fetchall()
            result = []
            for row in rows:
                self._load_execution_input_tx(connection, row["operation_id"])
                result.append(
                    OperationRef(
                        operation_id=row["operation_id"],
                        request_id=row["request_id"],
                        state=OperationState(row["state"]),
                    )
                )
            return tuple(result)
        finally:
            connection.close()

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

    def acquire_fence(
        self, operation_id: str, owner_id: str, ttl_seconds: float
    ) -> ExecutionFence:
        return self.acquire_lease(operation_id, owner_id, ttl_seconds)

    def heartbeat_fence(
        self, fence: ExecutionFence, ttl_seconds: float
    ) -> ExecutionFence:
        if not isinstance(fence, ExecutionFence):
            raise ContractValidationError("fence must be an ExecutionFence.")
        return self.heartbeat_lease(
            fence.operation_id,
            fence.owner_id,
            fence.fencing_token,
            ttl_seconds,
        )

    def release_fence(self, fence: ExecutionFence) -> None:
        if not isinstance(fence, ExecutionFence):
            raise ContractValidationError("fence must be an ExecutionFence.")
        self.release_lease(
            fence.operation_id, fence.owner_id, fence.fencing_token
        )

    def _assert_execution_fence(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        fence: ExecutionFence,
    ) -> None:
        if not isinstance(fence, ExecutionFence):
            raise ContractValidationError("fence must be an ExecutionFence.")
        if fence.operation_id != operation_id:
            raise LeaseConflict("Execution fence identifies a different operation.")
        self._active_lease(
            connection,
            operation_id,
            fence.owner_id,
            fence.fencing_token,
            self._clock_sample(),
        )

    _REVISION_TRANSITIONS = {
        RevisionState.CREATED: {RevisionState.STAGED, RevisionState.FAILED},
        RevisionState.STAGED: {
            RevisionState.PREFLIGHT_PASSED,
            RevisionState.FAILED,
        },
        RevisionState.PREFLIGHT_PASSED: {
            RevisionState.STARTING,
            RevisionState.FAILED,
        },
        RevisionState.STARTING: {RevisionState.READY, RevisionState.FAILED},
        RevisionState.READY: {
            RevisionState.TRAFFIC_CANDIDATE,
            RevisionState.ACTIVE,
            RevisionState.FAILED,
        },
        RevisionState.TRAFFIC_CANDIDATE: {
            RevisionState.ACTIVE,
            RevisionState.FAILED,
        },
        RevisionState.ACTIVE: {
            RevisionState.DRAINING,
            RevisionState.ROLLBACK_STARTING,
        },
        RevisionState.DRAINING: {
            RevisionState.INACTIVE,
            RevisionState.ROLLBACK_STARTING,
        },
        RevisionState.INACTIVE: {
            RevisionState.GARBAGE_COLLECTABLE,
            RevisionState.ROLLBACK_STARTING,
        },
        RevisionState.ROLLBACK_STARTING: {
            RevisionState.ACTIVE,
            RevisionState.FAILED,
        },
        RevisionState.FAILED: {RevisionState.GARBAGE_COLLECTABLE},
        RevisionState.GARBAGE_COLLECTABLE: set(),
    }

    def _append_revision_state_tx(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        state: RevisionState,
        fence: ExecutionFence,
    ) -> RevisionLifecycleEntry:
        if not isinstance(state, RevisionState):
            raise ContractValidationError("state must be a RevisionState.")
        operation = connection.execute(
            """
            SELECT state, revision_id FROM operations WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if operation is None:
            raise OperationConflict("Unknown operation.")
        if OperationState(operation["state"]).terminal:
            raise OperationConflict(
                "A terminal operation cannot append revision lifecycle state."
            )
        execution_input = self._load_execution_input_tx(connection, operation_id)
        previous = connection.execute(
            """
            SELECT sequence, state FROM revision_lifecycle
            WHERE operation_id = ? ORDER BY sequence DESC LIMIT 1
            """,
            (operation_id,),
        ).fetchone()
        if previous is None:
            if state is not RevisionState.CREATED:
                raise OperationConflict(
                    "The first revision lifecycle state must be created."
                )
            sequence = 1
        else:
            current = RevisionState(previous["state"])
            if state not in self._REVISION_TRANSITIONS[current]:
                raise OperationConflict(
                    "Revision lifecycle transition is not permitted."
                )
            sequence = int(previous["sequence"]) + 1
        occurred_at = datetime.fromtimestamp(
            self._clock_sample(), timezone.utc
        ).isoformat().replace("+00:00", "Z")
        entry = RevisionLifecycleEntry(
            operation_id=operation_id,
            sequence=sequence,
            revision_id=execution_input.revision.revision_id,
            revision_digest=execution_input.revision.content_digest(),
            state=state,
            occurred_at=occurred_at,
        )
        connection.execute(
            """
            INSERT INTO revision_lifecycle(
                operation_id, sequence, revision_id, revision_digest, state,
                occurred_at, lease_owner, fencing_token, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                sequence,
                entry.revision_id,
                entry.revision_digest,
                state.value,
                occurred_at,
                fence.owner_id,
                fence.fencing_token,
                self._bounded_json(entry),
            ),
        )
        return entry

    def append_revision_state(
        self,
        operation_id: str,
        state: RevisionState,
        *,
        fence: ExecutionFence,
    ) -> RevisionLifecycleEntry:
        with self._transaction() as connection:
            self._assert_execution_fence(connection, operation_id, fence)
            return self._append_revision_state_tx(
                connection, operation_id, state, fence
            )

    @classmethod
    def _verified_revision_history(
        cls, connection: sqlite3.Connection, operation_id: str
    ) -> Tuple[RevisionLifecycleEntry, ...]:
        rows = connection.execute(
            """
            SELECT * FROM revision_lifecycle
            WHERE operation_id = ? ORDER BY sequence
            """,
            (operation_id,),
        ).fetchall()
        result = []
        previous_state = None
        for expected_sequence, row in enumerate(rows, start=1):
            try:
                payload = json.loads(row["payload_json"])
                entry = RevisionLifecycleEntry(
                    operation_id=payload["operation_id"],
                    sequence=payload["sequence"],
                    revision_id=payload["revision_id"],
                    revision_digest=payload["revision_digest"],
                    state=RevisionState(payload["state"]),
                    occurred_at=payload["occurred_at"],
                )
            except Exception as exc:
                raise IntegrityError(
                    "Revision lifecycle payload is invalid."
                ) from exc
            if (
                entry.operation_id != operation_id
                or entry.sequence != expected_sequence
                or int(row["sequence"]) != expected_sequence
                or entry.revision_id != row["revision_id"]
                or entry.revision_digest != row["revision_digest"]
                or entry.state.value != row["state"]
                or entry.occurred_at != row["occurred_at"]
                or canonical_json(entry) != row["payload_json"]
                or not isinstance(row["lease_owner"], str)
                or int(row["fencing_token"]) < 1
            ):
                raise IntegrityError(
                    "Revision lifecycle durable columns do not reconcile."
                )
            if previous_state is None:
                valid_transition = entry.state is RevisionState.CREATED
            else:
                valid_transition = (
                    entry.state
                    in SQLiteOperationJournal._REVISION_TRANSITIONS[previous_state]
                )
            if not valid_transition:
                raise IntegrityError(
                    "Revision lifecycle contains an invalid transition."
                )
            result.append(entry)
            previous_state = entry.state
        return tuple(result)

    def revision_history(
        self, operation_id: str
    ) -> Tuple[RevisionLifecycleEntry, ...]:
        connection = self._connect()
        try:
            self._load_execution_input_tx(connection, operation_id)
            history = self._verified_revision_history(connection, operation_id)
            execution_input = self._load_execution_input_tx(
                connection, operation_id
            )
            if any(
                entry.revision_id != execution_input.revision.revision_id
                or entry.revision_digest
                != execution_input.revision.content_digest()
                for entry in history
            ):
                raise IntegrityError(
                    "Revision lifecycle does not bind the execution input."
                )
            return history
        finally:
            connection.close()

    @staticmethod
    def _active_revision_from_row(row: sqlite3.Row) -> ActiveRevision:
        return ActiveRevision(
            host_id=row["host_id"],
            app=row["app"],
            environment=row["environment"],
            operation_id=row["operation_id"],
            revision_id=row["revision_id"],
            revision_digest=row["revision_digest"],
            generation=int(row["generation"]),
            updated_at=row["updated_at"],
        )

    def active_revision(
        self, host_id: str, app: str, environment: str
    ) -> Optional[ActiveRevision]:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM active_revisions
                WHERE host_id = ? AND app = ? AND environment = ?
                """,
                (host_id, app, environment),
            ).fetchone()
            if row is None:
                return None
            try:
                active = self._active_revision_from_row(row)
                if canonical_json(active) != row["payload_json"]:
                    raise IntegrityError(
                        "Active revision payload is not canonical."
                    )
                return active
            except IntegrityError:
                raise
            except Exception as exc:
                raise IntegrityError("Active revision payload is invalid.") from exc
        finally:
            connection.close()

    def _cas_active_revision_tx(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        expected_active_revision_digest: Optional[str],
        fence: ExecutionFence,
    ) -> ActiveRevision:
        if expected_active_revision_digest is not None:
            require_digest(
                expected_active_revision_digest,
                "expected_active_revision_digest",
            )
        operation = connection.execute(
            """
            SELECT host_id, app, environment FROM operations
            WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if operation is None:
            raise OperationConflict("Unknown operation.")
        current = connection.execute(
            """
            SELECT * FROM active_revisions
            WHERE host_id = ? AND app = ? AND environment = ?
            """,
            (operation["host_id"], operation["app"], operation["environment"]),
        ).fetchone()
        current_digest = None if current is None else current["revision_digest"]
        matches = (
            current_digest is None
            and expected_active_revision_digest is None
        ) or (
            isinstance(current_digest, str)
            and isinstance(expected_active_revision_digest, str)
            and hmac.compare_digest(
                current_digest, expected_active_revision_digest
            )
        )
        if not matches:
            raise OperationConflict(
                "Active revision compare-and-swap conflict."
            )
        execution_input = self._load_execution_input_tx(connection, operation_id)
        self._append_revision_state_tx(
            connection, operation_id, RevisionState.ACTIVE, fence
        )
        generation = 1 if current is None else int(current["generation"]) + 1
        updated_at = datetime.fromtimestamp(
            self._clock_sample(), timezone.utc
        ).isoformat().replace("+00:00", "Z")
        active = ActiveRevision(
            host_id=operation["host_id"],
            app=operation["app"],
            environment=operation["environment"],
            operation_id=operation_id,
            revision_id=execution_input.revision.revision_id,
            revision_digest=execution_input.revision.content_digest(),
            generation=generation,
            updated_at=updated_at,
        )
        connection.execute(
            """
            INSERT INTO active_revisions(
                host_id, app, environment, operation_id, revision_id,
                revision_digest, generation, updated_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(host_id, app, environment) DO UPDATE SET
                operation_id = excluded.operation_id,
                revision_id = excluded.revision_id,
                revision_digest = excluded.revision_digest,
                generation = excluded.generation,
                updated_at = excluded.updated_at,
                payload_json = excluded.payload_json
            """,
            (
                active.host_id,
                active.app,
                active.environment,
                active.operation_id,
                active.revision_id,
                active.revision_digest,
                active.generation,
                active.updated_at,
                self._bounded_json(active),
            ),
        )
        return active

    def compare_and_swap_active_revision(
        self,
        operation_id: str,
        expected_active_revision_digest: Optional[str],
        *,
        fence: ExecutionFence,
    ) -> ActiveRevision:
        with self._transaction() as connection:
            self._assert_execution_fence(connection, operation_id, fence)
            return self._cas_active_revision_tx(
                connection,
                operation_id,
                expected_active_revision_digest,
                fence,
            )

    cas_active_revision = compare_and_swap_active_revision

    def _commit_receipt_tx(
        self,
        connection: sqlite3.Connection,
        validated: TerminalReceipt,
        receipt_json: str,
    ) -> bool:
        existing = connection.execute(
            "SELECT payload_json FROM terminal_receipts WHERE operation_id = ?",
            (validated.operation_id,),
        ).fetchone()
        if existing is not None:
            self._verified_events(connection, validated.operation_id)
            if existing["payload_json"] == receipt_json:
                return False
            raise OperationConflict(
                "Operation already has a different terminal receipt."
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
            raise OperationConflict(
                "Cannot commit a receipt for an unknown operation."
            )
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
            state=OperationState(validated.outcome.value),
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
        return True

    def commit_receipt(
        self,
        receipt: TerminalReceipt,
        *,
        lease_owner: Optional[str] = None,
        fencing_token: Optional[int] = None,
    ) -> None:
        validated = self._revalidate_receipt(receipt)
        receipt_json = self._bounded_json(validated)
        with self._transaction() as connection:
            self._assert_fence(
                connection, validated.operation_id, lease_owner, fencing_token
            )
            self._commit_receipt_tx(connection, validated, receipt_json)

    def commit_success(
        self,
        receipt: TerminalReceipt,
        expected_active_revision_digest: Optional[str],
        *,
        fence: ExecutionFence,
    ) -> ActiveRevision:
        validated = self._revalidate_receipt(receipt)
        if validated.outcome is not ReceiptOutcome.SUCCEEDED:
            raise OperationConflict(
                "Atomic success requires a succeeded terminal receipt."
            )
        receipt_json = self._bounded_json(validated)
        with self._transaction() as connection:
            self._assert_execution_fence(
                connection, validated.operation_id, fence
            )
            existing = connection.execute(
                """
                SELECT payload_json FROM terminal_receipts
                WHERE operation_id = ?
                """,
                (validated.operation_id,),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != receipt_json:
                    raise OperationConflict(
                        "Operation already has a different terminal receipt."
                    )
                self._verified_events(connection, validated.operation_id)
                marker = connection.execute(
                    """
                    SELECT active_generation FROM atomic_success_commits
                    WHERE operation_id = ?
                    """,
                    (validated.operation_id,),
                ).fetchone()
                active = connection.execute(
                    """
                    SELECT * FROM active_revisions
                    WHERE host_id = ? AND app = ? AND environment = ?
                    """,
                    (validated.host_id, validated.app, validated.environment),
                ).fetchone()
                if (
                    marker is None
                    or active is None
                    or active["operation_id"] != validated.operation_id
                    or active["revision_digest"]
                    != validated.desired_revision_digest
                    or int(marker["active_generation"])
                    != int(active["generation"])
                ):
                    raise IntegrityError(
                        "Atomic success receipt does not reconcile with active revision."
                    )
                return self._active_revision_from_row(active)

            current = connection.execute(
                """
                SELECT revision_id FROM active_revisions
                WHERE host_id = ? AND app = ? AND environment = ?
                """,
                (validated.host_id, validated.app, validated.environment),
            ).fetchone()
            current_revision_id = None if current is None else current["revision_id"]
            if validated.previous_revision_id != current_revision_id:
                raise OperationConflict(
                    "Successful receipt does not bind the exact predecessor revision."
                )

            active = self._cas_active_revision_tx(
                connection,
                validated.operation_id,
                expected_active_revision_digest,
                fence,
            )
            if not (
                validated.active_revision_id == active.revision_id
                and validated.active_revision_digest == active.revision_digest
            ):
                raise OperationConflict(
                    "Successful receipt does not bind the activated revision."
                )
            self._commit_receipt_tx(connection, validated, receipt_json)
            connection.execute(
                """
                INSERT INTO atomic_success_commits(
                    operation_id, active_generation
                ) VALUES (?, ?)
                """,
                (validated.operation_id, active.generation),
            )
            return active

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
