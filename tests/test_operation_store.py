from __future__ import annotations

import dataclasses
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.domain import (
    Actor,
    ApprovedPlanRef,
    AuthorizationKind,
    CheckStatus,
    CompensationAction,
    CompensationResult,
    CompensationStatus,
    ContractValidationError,
    OperationEvent,
    OperationPlan,
    OperationRequest,
    OperationState,
    PlanPhase,
    PlanStep,
    ReceiptEffect,
    ReceiptOutcome,
    TerminalReceipt,
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
    canonical_digest,
)
from ophelia.execution import (
    DEFAULT_OPERATION_DB_RELATIVE_PATH,
    IdempotencyConflict,
    LeaseConflict,
    OperationConflict,
    SQLiteOperationJournal,
)


D1 = "sha256:" + ("1" * 64)
D2 = "sha256:" + ("2" * 64)
D3 = "sha256:" + ("3" * 64)
D4 = "sha256:" + ("4" * 64)
D5 = "sha256:" + ("5" * 64)
D6 = "sha256:" + ("6" * 64)
CREATED = "2026-07-11T17:00:00Z"
APPROVED = "2026-07-11T17:05:00Z"
EXPIRES = "2026-07-11T19:00:00Z"
NOW = datetime(2026, 7, 11, 17, 10, tzinfo=timezone.utc).timestamp()


def _actor(actor_id: str = "actor_kyle") -> Actor:
    return Actor(actor_id=actor_id, source="test", authenticated_by="fixture")


def _request(
    *,
    request_id: str = "request_example-1",
    idempotency_key: str = "deploy-example",
    revision_id: str = "rev_example-1",
    revision_digest: str = D1,
) -> OperationRequest:
    return OperationRequest(
        request_id=request_id,
        operation="deploy.apply",
        host_id="host_example-1",
        app="demo-service",
        environment="production",
        revision_id=revision_id,
        revision_digest=revision_digest,
        idempotency_key=idempotency_key,
    )


def _plan(request: OperationRequest) -> OperationPlan:
    return OperationPlan.create(
        request=request,
        manifest_digest=D2,
        artifact_digests=(D3, D4),
        observed_state_digest=D5,
        policy_digest=D6,
        steps=(
            PlanStep(
                order=1,
                phase=PlanPhase.STAGE,
                mutates_runtime=True,
                desired_effect_digest=D1,
                compensation=CompensationAction.DELETE_CANDIDATE,
            ),
        ),
        blocker_codes=(),
        created_at=CREATED,
    )


def _approval(
    request: OperationRequest,
    *,
    decision_id: str = "decision_example-1",
    actor_id: str = "actor_kyle",
) -> ApprovedPlanRef:
    return ApprovedPlanRef.bind(
        _plan(request),
        actor_id=actor_id,
        decision_id=decision_id,
        authorization_kind=AuthorizationKind.LUMEN_DECISION,
        issuer="fixture-control-plane",
        audience=request.host_id,
        approved_at=APPROVED,
        expires_at=EXPIRES,
        approval_nonce="raw-one-time-nonce-that-must-not-be-stored",
    )


def _event(
    operation_id: str,
    *,
    sequence: int,
    event_id: str,
    state: OperationState = OperationState.EXECUTING,
    previous_event_digest: str = None,
) -> OperationEvent:
    return OperationEvent(
        event_id=event_id,
        operation_id=operation_id,
        sequence=sequence,
        event_type="operation.progressed",
        occurred_at="2026-07-11T17:20:00Z",
        state=state,
        host_id="host_example-1",
        app="demo-service",
        environment="production",
        revision_id="rev_example-1",
        previous_event_digest=previous_event_digest,
    )


def _receipt(
    operation_id: str,
    request: OperationRequest,
    approval: ApprovedPlanRef,
    *,
    receipt_id: str = "receipt_example-1",
) -> TerminalReceipt:
    return TerminalReceipt(
        receipt_id=receipt_id,
        operation_id=operation_id,
        operation=request.operation,
        plan_id=approval.plan_id,
        plan_digest=approval.plan_digest,
        decision_id=approval.decision_id,
        host_id=request.host_id,
        app=request.app,
        environment=request.environment,
        previous_revision_id="rev_previous",
        desired_revision_id=request.revision_id,
        desired_revision_digest=request.revision_digest,
        active_revision_id=request.revision_id,
        active_revision_digest=request.revision_digest,
        artifact_digests=(D3, D4),
        verification=VerificationResult(
            status=VerificationStatus.PASSED,
            observed_revision_digest=request.revision_digest,
            observed_state_digest=D5,
            observed_at="2026-07-11T17:25:00Z",
            checks=(
                VerificationCheck(
                    name="external",
                    status=CheckStatus.PASSED,
                    observed_digest=D5,
                    summary="Observed healthy.",
                ),
            ),
        ),
        compensation=CompensationResult(
            attempted=False,
            status=CompensationStatus.NOT_NEEDED,
        ),
        effect=ReceiptEffect.RUNTIME_ACTIVATED,
        outcome=ReceiptOutcome.SUCCEEDED,
        started_at="2026-07-11T17:15:00Z",
        completed_at="2026-07-11T17:30:00Z",
    )


class SQLiteOperationJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.now = [NOW]
        self.store = SQLiteOperationJournal.beneath_runtime_root(
            self.root, clock=lambda: self.now[0]
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _accept(
        self,
        request: OperationRequest = None,
        approval: ApprovedPlanRef = None,
    ):
        request = request or _request()
        approval = approval or _approval(request)
        return self.store.accept(_actor(), request, approval), request, approval

    def test_default_path_is_explicit_and_database_is_hardened(self) -> None:
        self.assertEqual(
            self.root / DEFAULT_OPERATION_DB_RELATIVE_PATH,
            self.store.database_path,
        )
        self.assertNotEqual(self.root / "state.db", self.store.database_path)
        connection = self.store._connect()
        try:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0])
            self.assertEqual(2, connection.execute("PRAGMA synchronous").fetchone()[0])
            self.assertEqual(1, connection.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()
        self.assertTrue(
            {
                "plans",
                "approvals",
                "operations",
                "operation_events",
                "idempotency_keys",
                "operation_leases",
                "terminal_receipts",
            }.issubset(tables)
        )
        self.store.integrity_check()

    def test_accept_is_idempotent_across_request_retry_identity(self) -> None:
        first, request, approval = self._accept()
        retry = dataclasses.replace(request, request_id="request_example-2")
        retry_ref = self.store.accept(_actor(), retry, approval)

        self.assertEqual(first.operation_id, retry_ref.operation_id)
        self.assertEqual(first.request_id, retry_ref.request_id)
        self.assertEqual(OperationState.ACCEPTED, retry_ref.state)

    def test_idempotent_retry_returns_original_after_approval_expires(self) -> None:
        first, request, approval = self._accept()
        self.now[0] = datetime(
            2026, 7, 11, 20, 0, tzinfo=timezone.utc
        ).timestamp()

        retry = dataclasses.replace(request, request_id="request_example-2")
        self.assertEqual(
            first.operation_id,
            self.store.accept(_actor(), retry, approval).operation_id,
        )

        new_request = dataclasses.replace(
            request,
            request_id="request_example-3",
            idempotency_key="new-expired-key",
        )
        with self.assertRaises(OperationConflict):
            self.store.accept(
                _actor(),
                new_request,
                _approval(new_request, decision_id="decision_expired"),
            )

    def test_accept_rejects_tampered_approval_digest(self) -> None:
        request = _request()
        approval = dataclasses.replace(
            _approval(request), approval_digest="sha256:" + ("0" * 64)
        )

        with self.assertRaises(OperationConflict):
            self.store.accept(_actor(), request, approval)

    def test_accept_rejects_conflicting_content_for_same_identity(self) -> None:
        self._accept()
        changed = _request(
            request_id="request_example-2",
            revision_id="rev_example-2",
            revision_digest=D2,
        )
        changed_approval = _approval(changed, decision_id="decision_example-2")

        with self.assertRaises(IdempotencyConflict):
            self.store.accept(_actor(), changed, changed_approval)

    def test_acceptance_is_atomic_and_does_not_store_raw_approval_nonce(self) -> None:
        request = _request()
        approval = _approval(request)
        with self.assertRaises(OperationConflict):
            self.store.accept(_actor("actor_other"), request, approval)

        connection = sqlite3.connect(str(self.store.database_path))
        try:
            self.assertEqual(
                0, connection.execute("SELECT count(*) FROM operations").fetchone()[0]
            )
        finally:
            connection.close()

        self.store.accept(_actor(), request, approval)
        database_bytes = self.store.database_path.read_bytes()
        wal_path = Path(str(self.store.database_path) + "-wal")
        if wal_path.exists():
            database_bytes += wal_path.read_bytes()
        self.assertNotIn(b"raw-one-time-nonce-that-must-not-be-stored", database_bytes)

    def test_concurrent_accept_returns_one_durable_operation(self) -> None:
        request = _request()
        approval = _approval(request)
        barrier = threading.Barrier(8)

        def accept_once(_: int) -> str:
            barrier.wait()
            return self.store.accept(_actor(), request, approval).operation_id

        with ThreadPoolExecutor(max_workers=8) as pool:
            operation_ids = list(pool.map(accept_once, range(8)))

        self.assertEqual(1, len(set(operation_ids)))
        connection = sqlite3.connect(str(self.store.database_path))
        try:
            self.assertEqual(
                1, connection.execute("SELECT count(*) FROM operations").fetchone()[0]
            )
        finally:
            connection.close()

    def test_events_are_strictly_ordered_chained_and_recover_after_reopen(self) -> None:
        operation, _, _ = self._accept()
        first = _event(
            operation.operation_id, sequence=1, event_id="event_example-1"
        )
        self.store.append(first)
        second = _event(
            operation.operation_id,
            sequence=2,
            event_id="event_example-2",
            previous_event_digest=canonical_digest(first),
        )
        self.store.append(second)

        with self.assertRaises(OperationConflict):
            self.store.append(
                _event(
                    operation.operation_id,
                    sequence=4,
                    event_id="event_example-4",
                    previous_event_digest=canonical_digest(second),
                )
            )

        reopened = SQLiteOperationJournal(
            self.store.database_path, clock=lambda: self.now[0]
        )
        self.assertEqual((first, second), reopened.events(operation.operation_id))
        self.assertEqual(OperationState.EXECUTING, reopened.get(operation.operation_id).state)

    def test_public_append_cannot_create_terminal_state_without_receipt(self) -> None:
        operation, _, _ = self._accept()
        terminal = _event(
            operation.operation_id,
            sequence=1,
            event_id="event_invalid-terminal",
            state=OperationState.SUCCEEDED,
        )

        with self.assertRaises(OperationConflict):
            self.store.append(terminal)

        self.assertEqual(OperationState.ACCEPTED, self.store.get(operation.operation_id).state)
        self.assertEqual((), self.store.events(operation.operation_id))

    def test_append_rolls_back_state_when_event_insert_fails(self) -> None:
        first_operation, _, _ = self._accept()
        self.store.append(
            _event(
                first_operation.operation_id,
                sequence=1,
                event_id="event_duplicate",
            )
        )
        second_request = _request(
            request_id="request_example-2", idempotency_key="deploy-example-2"
        )
        second_approval = _approval(
            second_request, decision_id="decision_example-2"
        )
        second_operation, _, _ = self._accept(second_request, second_approval)

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append(
                _event(
                    second_operation.operation_id,
                    sequence=1,
                    event_id="event_duplicate",
                )
            )

        self.assertEqual(
            OperationState.ACCEPTED, self.store.get(second_operation.operation_id).state
        )
        self.assertEqual((), self.store.events(second_operation.operation_id))

    def test_expired_lease_increments_fence_and_stale_owner_cannot_append(self) -> None:
        operation, request, approval = self._accept()
        first = self.store.acquire_lease(operation.operation_id, "executor-a", 10)
        self.now[0] += 11
        second = self.store.acquire_lease(operation.operation_id, "executor-b", 10)

        self.assertEqual(first.fencing_token + 1, second.fencing_token)
        event = _event(operation.operation_id, sequence=1, event_id="event_fenced")
        with self.assertRaises(LeaseConflict):
            self.store.append(
                event,
                lease_owner=first.owner_id,
                fencing_token=first.fencing_token,
            )
        self.store.append(
            event,
            lease_owner=second.owner_id,
            fencing_token=second.fencing_token,
        )
        receipt = _receipt(operation.operation_id, request, approval)
        with self.assertRaises(LeaseConflict):
            self.store.commit_receipt(
                receipt,
                lease_owner=first.owner_id,
                fencing_token=first.fencing_token,
            )
        self.store.commit_receipt(
            receipt,
            lease_owner=second.owner_id,
            fencing_token=second.fencing_token,
        )

    def test_lease_heartbeat_release_and_concurrent_acquire_are_transactional(self) -> None:
        operation, _, _ = self._accept()
        barrier = threading.Barrier(2)

        def acquire(owner: str):
            barrier.wait()
            try:
                return self.store.acquire_lease(operation.operation_id, owner, 20)
            except LeaseConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(acquire, ("executor-a", "executor-b")))
        leases = [result for result in results if result is not None]
        self.assertEqual(1, len(leases))

        lease = leases[0]
        heartbeat = self.store.heartbeat_lease(
            lease.operation_id, lease.owner_id, lease.fencing_token, 30
        )
        self.assertGreater(heartbeat.expires_at, lease.expires_at)
        self.store.release_lease(
            lease.operation_id, lease.owner_id, lease.fencing_token
        )
        replacement = self.store.acquire_lease(
            lease.operation_id, "executor-c", 20
        )
        self.assertGreater(replacement.fencing_token, lease.fencing_token)
        with self.assertRaises(LeaseConflict):
            self.store.release_lease(
                lease.operation_id, lease.owner_id, lease.fencing_token
            )

    def test_terminal_receipt_event_state_and_receipt_commit_atomically(self) -> None:
        operation, request, approval = self._accept()
        progress = _event(
            operation.operation_id, sequence=1, event_id="event_progress"
        )
        self.store.append(progress)
        receipt = _receipt(operation.operation_id, request, approval)

        self.store.commit_receipt(receipt)

        self.assertEqual(
            OperationState.SUCCEEDED, self.store.get(operation.operation_id).state
        )
        events = self.store.events(operation.operation_id)
        self.assertEqual(2, len(events))
        self.assertEqual("operation.terminal", events[-1].event_type)
        self.assertEqual(canonical_digest(progress), events[-1].previous_event_digest)
        self.assertEqual(
            receipt.receipt_id,
            self.store.receipt_payload(operation.operation_id)["receipt_id"],
        )
        self.store.commit_receipt(receipt)
        self.assertEqual(2, len(self.store.events(operation.operation_id)))

    def test_receipt_late_insert_failure_rolls_back_terminal_event_and_state(self) -> None:
        first_operation, first_request, first_approval = self._accept()
        first_receipt = _receipt(
            first_operation.operation_id,
            first_request,
            first_approval,
            receipt_id="receipt_duplicate",
        )
        self.store.commit_receipt(first_receipt)

        second_request = _request(
            request_id="request_example-2", idempotency_key="deploy-example-2"
        )
        second_approval = _approval(
            second_request, decision_id="decision_example-2"
        )
        second_operation, _, _ = self._accept(second_request, second_approval)
        second_receipt = _receipt(
            second_operation.operation_id,
            second_request,
            second_approval,
            receipt_id="receipt_duplicate",
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.commit_receipt(second_receipt)

        self.assertEqual(
            OperationState.ACCEPTED, self.store.get(second_operation.operation_id).state
        )
        self.assertEqual((), self.store.events(second_operation.operation_id))
        self.assertIsNone(self.store.receipt_payload(second_operation.operation_id))

    def test_receipt_must_bind_operation_and_bounded_canonical_payload(self) -> None:
        operation, request, approval = self._accept()
        receipt = _receipt(operation.operation_id, request, approval)
        with self.assertRaises(OperationConflict):
            self.store.commit_receipt(
                dataclasses.replace(receipt, decision_id="decision_other")
            )

        checks = tuple(
            VerificationCheck(
                name="check-%03d" % index,
                status=CheckStatus.PASSED,
                observed_digest=D5,
                summary="x" * 1000,
            )
            for index in range(80)
        )
        oversized = dataclasses.replace(
            receipt,
            verification=dataclasses.replace(receipt.verification, checks=checks),
        )
        with self.assertRaises(ContractValidationError):
            self.store.commit_receipt(oversized)
        self.assertEqual(
            OperationState.ACCEPTED, self.store.get(operation.operation_id).state
        )


if __name__ == "__main__":
    unittest.main()
