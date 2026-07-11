from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
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
    Revision,
    RevisionState,
    TerminalReceipt,
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
    Workload,
    WorkloadKind,
    canonical_digest,
)
from ophelia.execution import (
    DEFAULT_OPERATION_DB_RELATIVE_PATH,
    ExecutionInput,
    IdempotencyConflict,
    IntegrityError,
    LeaseConflict,
    OperationConflict,
    RevisionArtifactRef,
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
DEADLINE = "2026-07-11T18:30:00Z"


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


def _execution_bundle(
    *,
    request_id: str = "request_input-1",
    idempotency_key: str = "deploy-input",
    decision_id: str = "decision_input-1",
    revision_id: str = "rev_static-1",
):
    revision = Revision(
        revision_id=revision_id,
        app="demo-service",
        environment="production",
        manifest_digest=D2,
        artifact_digests=(D3, D4),
        renderer_version="ophelia-test",
        workloads=(
            Workload(
                workload_id="site",
                workload_kind=WorkloadKind.STATIC,
                artifact_digest=D3,
                route_ids=("public",),
            ),
        ),
        created_at=CREATED,
    )
    request = _request(
        request_id=request_id,
        idempotency_key=idempotency_key,
        revision_id=revision.revision_id,
        revision_digest=revision.content_digest(),
    )
    plan = _plan(request)
    approval = ApprovedPlanRef.bind(
        plan,
        actor_id="actor_kyle",
        decision_id=decision_id,
        authorization_kind=AuthorizationKind.LUMEN_DECISION,
        issuer="fixture-control-plane",
        audience=request.host_id,
        approved_at=APPROVED,
        expires_at=EXPIRES,
        approval_nonce="raw-input-nonce-that-must-not-be-stored",
    )
    artifact_ref = RevisionArtifactRef(
        revision_id=revision.revision_id,
        revision_digest=revision.content_digest(),
        relative_root="revisions/static-1/public",
        artifact_digest=D3,
    )
    execution_input = ExecutionInput.bind(
        request=request,
        plan=plan,
        approved_plan=approval,
        revision=revision,
        artifact_ref=artifact_ref,
        deadline=DEADLINE,
    )
    return request, approval, execution_input


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
    previous_revision_id: str | None = "rev_previous",
    check_summary: str | None = None,
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
        previous_revision_id=previous_revision_id,
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
                    summary=check_summary,
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
            self.assertEqual(3, connection.execute("PRAGMA user_version").fetchone()[0])
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
                "operation_inputs",
                "operation_controls",
                "revision_lifecycle",
                "active_revisions",
                "atomic_success_commits",
                "execution_scope_leases",
            }.issubset(tables)
        )
        self.store.integrity_check()

    def test_database_symlink_is_rejected_without_changing_its_target(self) -> None:
        journal_root = self.root / "symlink-journal"
        journal_root.mkdir(mode=0o700)
        target = self.root / "outside.db"
        target.write_bytes(b"not-a-journal")
        os.chmod(target, 0o644)
        (journal_root / "operations.db").symlink_to(target)

        with self.assertRaises(IntegrityError):
            SQLiteOperationJournal(journal_root / "operations.db")

        self.assertEqual(b"not-a-journal", target.read_bytes())
        self.assertEqual(0o644, stat.S_IMODE(target.stat().st_mode))

    def test_concurrent_first_open_migrates_once_across_connections(self) -> None:
        runtime_root = self.root / "concurrent-first-open"
        barrier = threading.Barrier(8)

        def initialize(_: int) -> Path:
            barrier.wait()
            return SQLiteOperationJournal.beneath_runtime_root(
                runtime_root, clock=lambda: NOW
            ).database_path

        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(initialize, range(8)))

        self.assertEqual(1, len(set(paths)))
        reopened = SQLiteOperationJournal(paths[0], clock=lambda: NOW)
        reopened.integrity_check()
        connection = reopened._connect()
        try:
            self.assertEqual(
                3, connection.execute("PRAGMA user_version").fetchone()[0]
            )
        finally:
            connection.close()

    def test_concurrent_first_open_migrates_once_across_processes(self) -> None:
        runtime_root = self.root / "concurrent-process-first-open"
        source_root = Path(__file__).resolve().parents[1] / "src"
        script = (
            "from pathlib import Path; "
            "from ophelia.execution import SQLiteOperationJournal; "
            "SQLiteOperationJournal.beneath_runtime_root(Path(%r)).integrity_check()"
            % str(runtime_root)
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(source_root)
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        failures = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            if process.returncode != 0:
                failures.append((process.returncode, stdout, stderr))
        self.assertEqual([], failures)

        reopened = SQLiteOperationJournal.beneath_runtime_root(
            runtime_root, clock=lambda: NOW
        )
        reopened.integrity_check()

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

    def test_request_storage_digests_idempotency_key_and_repairs_modes(self) -> None:
        secret_key = "raw-idempotency-secret-material"
        request = _request(idempotency_key=secret_key)
        approval = _approval(request)
        reader = self.store._connect()
        try:
            reader.execute("SELECT count(*) FROM operations").fetchone()
            self.store.accept(_actor(), request, approval)
            connection = sqlite3.connect(str(self.store.database_path))
            try:
                request_json = connection.execute(
                    "SELECT request_json FROM operations"
                ).fetchone()[0]
            finally:
                connection.close()
            self.store._repair_permissions()

            self.assertNotIn(secret_key, request_json)
            self.assertNotIn('"idempotency_key":', request_json)
            self.assertIn('"idempotency_key_digest":', request_json)
            self.assertEqual(
                0o700,
                stat.S_IMODE(self.store.database_path.parent.stat().st_mode),
            )
            for path in (
                self.store.database_path,
                Path(str(self.store.database_path) + "-wal"),
                Path(str(self.store.database_path) + "-shm"),
            ):
                if path.exists():
                    self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
                    self.assertNotIn(secret_key.encode("utf-8"), path.read_bytes())
        finally:
            reader.close()

        os.chmod(str(self.store.database_path.parent), 0o755)
        os.chmod(str(self.store.database_path), 0o644)
        reopened = SQLiteOperationJournal(
            self.store.database_path, clock=lambda: self.now[0]
        )
        self.assertEqual(
            0o700,
            stat.S_IMODE(reopened.database_path.parent.stat().st_mode),
        )
        self.assertEqual(0o600, stat.S_IMODE(reopened.database_path.stat().st_mode))

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

    def test_event_digest_tampering_is_detected_on_read_and_reopen(self) -> None:
        operation, _, _ = self._accept()
        event = _event(
            operation.operation_id, sequence=1, event_id="event_tampered"
        )
        self.store.append(event)
        connection = sqlite3.connect(str(self.store.database_path))
        try:
            connection.execute(
                "UPDATE operation_events SET event_digest = ? WHERE operation_id = ?",
                (D6, operation.operation_id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(IntegrityError):
            self.store.events(operation.operation_id)
        with self.assertRaises(IntegrityError):
            SQLiteOperationJournal(self.store.database_path, clock=lambda: self.now[0])

    def test_recomputed_event_digest_cannot_change_accepted_identity_claims(self) -> None:
        operation, _, _ = self._accept()
        event = _event(
            operation.operation_id, sequence=1, event_id="event_rebound"
        )
        self.store.append(event)
        payload = event.to_dict()
        payload["host_id"] = "host_attacker"
        payload_json = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        connection = sqlite3.connect(str(self.store.database_path))
        try:
            connection.execute(
                """
                UPDATE operation_events
                SET payload_json = ?, event_digest = ?
                WHERE operation_id = ?
                """,
                (
                    payload_json,
                    canonical_digest(payload),
                    operation.operation_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(IntegrityError):
            self.store.events(operation.operation_id)
        with self.assertRaises(IntegrityError):
            SQLiteOperationJournal(self.store.database_path, clock=lambda: self.now[0])

    def test_previous_event_link_tampering_is_detected(self) -> None:
        operation, _, _ = self._accept()
        first = _event(
            operation.operation_id, sequence=1, event_id="event_chain-1"
        )
        second = _event(
            operation.operation_id,
            sequence=2,
            event_id="event_chain-2",
            previous_event_digest=canonical_digest(first),
        )
        self.store.append(first)
        self.store.append(second)
        payload = second.to_dict()
        payload["previous_event_digest"] = D6
        payload_json = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        connection = sqlite3.connect(str(self.store.database_path))
        try:
            connection.execute(
                """
                UPDATE operation_events
                SET payload_json = ?, event_digest = ?
                WHERE operation_id = ? AND sequence = 2
                """,
                (payload_json, canonical_digest(payload), operation.operation_id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(IntegrityError):
            self.store.events(operation.operation_id)

    def test_recomputed_plan_and_approval_claims_cannot_rebind_operation(self) -> None:
        operation, _, _ = self._accept()
        event = _event(
            operation.operation_id, sequence=1, event_id="event_plan-rebound"
        )
        self.store.append(event)
        connection = sqlite3.connect(str(self.store.database_path))
        try:
            plan_id, plan_json = connection.execute(
                "SELECT plan_id, payload_json FROM plans"
            ).fetchone()
            decision_id, approval_json = connection.execute(
                "SELECT decision_id, payload_json FROM approvals"
            ).fetchone()
            plan_payload = json.loads(plan_json)
            approval_payload = json.loads(approval_json)
            plan_payload["revision_digest"] = D2
            approval_payload["revision_digest"] = D2
            approval_binding = dict(approval_payload)
            approval_binding.pop("approval_digest")
            approval_payload["approval_digest"] = canonical_digest(approval_binding)
            connection.execute(
                "UPDATE plans SET payload_json = ? WHERE plan_id = ?",
                (
                    json.dumps(
                        plan_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    ),
                    plan_id,
                ),
            )
            connection.execute(
                """
                UPDATE approvals SET payload_json = ?, approval_digest = ?
                WHERE decision_id = ?
                """,
                (
                    json.dumps(
                        approval_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    ),
                    approval_payload["approval_digest"],
                    decision_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(IntegrityError):
            self.store.events(operation.operation_id)
        with self.assertRaises(IntegrityError):
            SQLiteOperationJournal(self.store.database_path, clock=lambda: self.now[0])

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

    def test_non_finite_lease_ttls_and_clock_samples_fail_closed(self) -> None:
        operation, _, _ = self._accept()
        for ttl in (float("nan"), float("inf"), float("-inf"), 10**1000):
            with self.subTest(ttl=ttl):
                with self.assertRaises(ContractValidationError):
                    self.store.acquire_lease(operation.operation_id, "executor-a", ttl)

        lease = self.store.acquire_lease(operation.operation_id, "executor-a", 10)
        for sample in (float("nan"), float("inf"), float("-inf"), 10**1000):
            with self.subTest(clock=sample):
                self.store._clock = lambda sample=sample: sample
                with self.assertRaises(IntegrityError):
                    self.store.heartbeat_lease(
                        operation.operation_id,
                        lease.owner_id,
                        lease.fencing_token,
                        10,
                    )
                with self.assertRaises(IntegrityError):
                    self.store.release_lease(
                        operation.operation_id,
                        lease.owner_id,
                        lease.fencing_token,
                    )

        self.store._clock = lambda: float("nan")
        request = _request(
            request_id="request_nonfinite", idempotency_key="nonfinite-clock"
        )
        with self.assertRaises(IntegrityError):
            self.store.accept(
                _actor(), request, _approval(request, decision_id="decision_nonfinite")
            )

    def test_lease_samples_time_after_waiting_for_write_lock(self) -> None:
        operation, _, _ = self._accept()
        sampled = threading.Event()
        clock_value = [NOW]

        def clock() -> float:
            sampled.set()
            return clock_value[0]

        self.store._clock = clock
        blocker = self.store._connect()
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    self.store.acquire_lease,
                    operation.operation_id,
                    "executor-delayed",
                    10,
                )
                time.sleep(0.1)
                self.assertFalse(sampled.is_set())
                clock_value[0] = NOW + 100
                blocker.commit()
                lease = future.result(timeout=5)
        finally:
            if blocker.in_transaction:
                blocker.rollback()
            blocker.close()

        self.assertEqual(NOW + 110, lease.expires_at)

        sampled.clear()
        blocker = self.store._connect()
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    self.store.heartbeat_lease,
                    operation.operation_id,
                    lease.owner_id,
                    lease.fencing_token,
                    20,
                )
                time.sleep(0.1)
                self.assertFalse(sampled.is_set())
                clock_value[0] = NOW + 105
                blocker.commit()
                heartbeat = future.result(timeout=5)
        finally:
            if blocker.in_transaction:
                blocker.rollback()
            blocker.close()

        self.assertEqual(NOW + 125, heartbeat.expires_at)

    def test_lease_renewal_never_moves_expiration_backward(self) -> None:
        operation, _, _ = self._accept()
        lease = self.store.acquire_lease(operation.operation_id, "executor-a", 100)
        self.now[0] += 10

        acquired_again = self.store.acquire_lease(
            operation.operation_id, "executor-a", 5
        )
        heartbeat = self.store.heartbeat_lease(
            operation.operation_id,
            "executor-a",
            lease.fencing_token,
            5,
        )

        self.assertEqual(lease.expires_at, acquired_again.expires_at)
        self.assertEqual(lease.expires_at, heartbeat.expires_at)

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

    def test_direct_receipt_commit_rejects_verification_summary_prose(self) -> None:
        operation, request, approval = self._accept()
        receipt = _receipt(
            operation.operation_id,
            request,
            approval,
            check_summary="Observed healthy.",
        )

        with self.assertRaisesRegex(
            ContractValidationError, "cannot contain summary prose"
        ):
            self.store.commit_receipt(receipt)

        self.assertEqual(OperationState.ACCEPTED, self.store.get(operation.operation_id).state)
        self.assertIsNone(self.store.receipt_payload(operation.operation_id))

    def test_direct_receipt_commit_accepts_digest_only_checks(self) -> None:
        operation, request, approval = self._accept()
        receipt = _receipt(operation.operation_id, request, approval)

        self.store.commit_receipt(receipt)

        payload = self.store.receipt_payload(operation.operation_id)
        self.assertIsNotNone(payload)
        self.assertIsNone(payload["verification"]["checks"][0]["summary"])

    def test_persisted_verification_summary_prose_fails_integrity(self) -> None:
        operation, request, approval = self._accept()
        self.store.commit_receipt(_receipt(operation.operation_id, request, approval))
        connection = sqlite3.connect(str(self.store.database_path))
        try:
            payload = json.loads(
                connection.execute(
                    "SELECT payload_json FROM terminal_receipts WHERE operation_id = ?",
                    (operation.operation_id,),
                ).fetchone()[0]
            )
            payload["verification"]["checks"][0]["summary"] = "Observed healthy."
            payload_json = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            connection.execute(
                """
                UPDATE terminal_receipts
                SET payload_json = ?, receipt_digest = ?
                WHERE operation_id = ?
                """,
                (payload_json, canonical_digest(payload), operation.operation_id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(IntegrityError):
            SQLiteOperationJournal(self.store.database_path, clock=lambda: self.now[0])

    def test_terminal_reconciliation_tampering_is_detected_on_reopen(self) -> None:
        operation, request, approval = self._accept()
        self.store.commit_receipt(
            _receipt(operation.operation_id, request, approval)
        )
        connection = sqlite3.connect(str(self.store.database_path))
        try:
            connection.execute(
                "UPDATE operations SET state = ? WHERE operation_id = ?",
                (OperationState.FAILED_UNCOMPENSATED.value, operation.operation_id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(IntegrityError):
            SQLiteOperationJournal(self.store.database_path, clock=lambda: self.now[0])

    def test_terminal_receipt_digest_and_contract_tampering_is_detected(self) -> None:
        operation, request, approval = self._accept()
        self.store.commit_receipt(
            _receipt(operation.operation_id, request, approval)
        )
        connection = sqlite3.connect(str(self.store.database_path))
        try:
            row = connection.execute(
                "SELECT payload_json FROM terminal_receipts WHERE operation_id = ?",
                (operation.operation_id,),
            ).fetchone()
            payload = json.loads(row[0])
            connection.execute(
                "UPDATE terminal_receipts SET receipt_digest = ? WHERE operation_id = ?",
                (D6, operation.operation_id),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(IntegrityError):
            SQLiteOperationJournal(self.store.database_path, clock=lambda: self.now[0])

        payload["inputs_redacted"] = False
        tampered_json = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        connection = sqlite3.connect(str(self.store.database_path))
        try:
            connection.execute(
                """
                UPDATE terminal_receipts
                SET receipt_digest = ?, payload_json = ?
                WHERE operation_id = ?
                """,
                (
                    canonical_digest(payload),
                    tampered_json,
                    operation.operation_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(IntegrityError):
            SQLiteOperationJournal(self.store.database_path, clock=lambda: self.now[0])

    def test_recomputed_receipt_digest_cannot_change_accepted_claims(self) -> None:
        operation, request, approval = self._accept()
        receipt = _receipt(operation.operation_id, request, approval)
        self.store.commit_receipt(receipt)
        connection = sqlite3.connect(str(self.store.database_path))
        try:
            payload = json.loads(
                connection.execute(
                    "SELECT payload_json FROM terminal_receipts WHERE operation_id = ?",
                    (operation.operation_id,),
                ).fetchone()[0]
            )
            payload["plan_id"] = "plan_attacker"
            payload_json = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            connection.execute(
                """
                UPDATE terminal_receipts
                SET payload_json = ?, receipt_digest = ?
                WHERE operation_id = ?
                """,
                (payload_json, canonical_digest(payload), operation.operation_id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(IntegrityError):
            self.store.events(operation.operation_id)
        with self.assertRaises(IntegrityError):
            self.store.receipt_payload(operation.operation_id)
        with self.assertRaises(IntegrityError):
            self.store.commit_receipt(receipt)

    def test_malformed_receipt_is_integrity_error_on_read_reopen_and_duplicate(self) -> None:
        operation, request, approval = self._accept()
        receipt = _receipt(operation.operation_id, request, approval)
        self.store.commit_receipt(receipt)
        connection = sqlite3.connect(str(self.store.database_path))
        try:
            payload = json.loads(
                connection.execute(
                    "SELECT payload_json FROM terminal_receipts WHERE operation_id = ?",
                    (operation.operation_id,),
                ).fetchone()[0]
            )
            payload.pop("verification")
            payload_json = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            connection.execute(
                """
                UPDATE terminal_receipts
                SET payload_json = ?, receipt_digest = ?
                WHERE operation_id = ?
                """,
                (payload_json, canonical_digest(payload), operation.operation_id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(IntegrityError):
            self.store.receipt_payload(operation.operation_id)
        with self.assertRaises(IntegrityError):
            self.store.commit_receipt(receipt)
        with self.assertRaises(IntegrityError):
            SQLiteOperationJournal(self.store.database_path, clock=lambda: self.now[0])

    def test_stale_owner_cannot_idempotently_recommit_existing_receipt(self) -> None:
        operation, request, approval = self._accept()
        first = self.store.acquire_lease(operation.operation_id, "executor-a", 10)
        self.now[0] += 11
        current = self.store.acquire_lease(operation.operation_id, "executor-b", 30)
        receipt = _receipt(operation.operation_id, request, approval)
        self.store.commit_receipt(
            receipt,
            lease_owner=current.owner_id,
            fencing_token=current.fencing_token,
        )

        with self.assertRaises(LeaseConflict):
            self.store.commit_receipt(
                receipt,
                lease_owner=first.owner_id,
                fencing_token=first.fencing_token,
            )
        self.store.commit_receipt(
            receipt,
            lease_owner=current.owner_id,
            fencing_token=current.fencing_token,
        )
        self.assertEqual(
            receipt.receipt_id,
            self.store.receipt_payload(operation.operation_id)["receipt_id"],
        )

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


    def test_execution_input_is_atomic_recoverable_and_idempotent(self) -> None:
        request, approval, execution_input = _execution_bundle()
        operation = self.store.accept(
            _actor(),
            request,
            approval,
            execution_input=execution_input,
        )

        self.assertEqual(
            execution_input, self.store.load_execution_input(operation.operation_id)
        )
        self.assertEqual((operation,), self.store.list_recoverable())
        retry = dataclasses.replace(request, request_id="request_input-2")
        self.assertEqual(
            operation.operation_id,
            self.store.accept(
                _actor(),
                retry,
                approval,
                execution_input=execution_input,
            ).operation_id,
        )
        with self.assertRaises(IdempotencyConflict):
            self.store.accept(_actor(), retry, approval)

        database_bytes = self.store.database_path.read_bytes()
        wal_path = Path(str(self.store.database_path) + "-wal")
        if wal_path.exists():
            database_bytes += wal_path.read_bytes()
        self.assertNotIn(b"raw-input-nonce-that-must-not-be-stored", database_bytes)

    def test_execution_deadline_must_be_future_and_within_approval(self) -> None:
        request, approval, execution_input = _execution_bundle()

        for deadline in ("2026-07-11T17:10:00Z", "2026-07-11T19:00:01Z"):
            invalid = ExecutionInput.bind(
                request=request,
                plan=execution_input.plan,
                approved_plan=approval,
                revision=execution_input.revision,
                artifact_ref=execution_input.artifact_ref,
                deadline=deadline,
            )
            with self.subTest(deadline=deadline), self.assertRaises(
                OperationConflict
            ):
                self.store.accept(
                    _actor(), request, approval, execution_input=invalid
                )

    def test_execution_input_tampering_with_recomputed_digest_is_detected(self) -> None:
        request, approval, execution_input = _execution_bundle()
        operation = self.store.accept(
            _actor(), request, approval, execution_input=execution_input
        )
        connection = sqlite3.connect(str(self.store.database_path))
        try:
            payload = json.loads(
                connection.execute(
                    "SELECT payload_json FROM operation_inputs WHERE operation_id = ?",
                    (operation.operation_id,),
                ).fetchone()[0]
            )
            payload["plan"]["host_id"] = "host_attacker"
            binding = dict(payload)
            binding.pop("input_digest")
            payload["input_digest"] = canonical_digest(binding)
            payload_json = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            connection.execute(
                """
                UPDATE operation_inputs SET input_digest = ?, payload_json = ?
                WHERE operation_id = ?
                """,
                (payload["input_digest"], payload_json, operation.operation_id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(IntegrityError):
            self.store.load_execution_input(operation.operation_id)
        with self.assertRaises(IntegrityError):
            self.store.integrity_check()

    def test_cancellation_control_and_recovery_listing_are_durable(self) -> None:
        request, approval, execution_input = _execution_bundle()
        operation = self.store.accept(
            _actor(), request, approval, execution_input=execution_input
        )
        self.assertEqual(DEADLINE, self.store.control(operation.operation_id).deadline)
        self.assertFalse(self.store.cancellation_requested(operation.operation_id))

        first = self.store.request_cancellation(operation.operation_id, _actor())
        retry = self.store.request_cancellation(
            operation.operation_id, _actor("actor_other")
        )
        self.assertTrue(first.cancellation_requested)
        self.assertEqual(first, retry)
        self.assertEqual("actor_kyle", retry.cancellation_actor.actor_id)

        lease = self.store.acquire_fence(operation.operation_id, "executor-a", 10)
        self.assertEqual((), self.store.list_recoverable())
        self.now[0] = lease.expires_at
        self.assertEqual((operation,), self.store.list_recoverable())
        for invalid in (0, -1, True, 1001):
            with self.assertRaises(ContractValidationError):
                self.store.list_recoverable(invalid)

    def test_revision_lifecycle_rejects_invalid_transition_and_stale_fence(self) -> None:
        request, approval, execution_input = _execution_bundle()
        operation = self.store.accept(
            _actor(), request, approval, execution_input=execution_input
        )
        first = self.store.acquire_fence(operation.operation_id, "executor-a", 10)
        created = self.store.append_revision_state(
            operation.operation_id, RevisionState.CREATED, fence=first
        )
        self.assertEqual(1, created.sequence)
        with self.assertRaises(OperationConflict):
            self.store.append_revision_state(
                operation.operation_id, RevisionState.READY, fence=first
            )
        self.assertEqual((created,), self.store.revision_history(operation.operation_id))

        self.now[0] += 11
        second = self.store.acquire_fence(operation.operation_id, "executor-b", 10)
        with self.assertRaises(LeaseConflict):
            self.store.append_revision_state(
                operation.operation_id, RevisionState.STAGED, fence=first
            )
        staged = self.store.append_revision_state(
            operation.operation_id, RevisionState.STAGED, fence=second
        )
        self.assertEqual(2, staged.sequence)

    def test_execution_fence_is_monotonic_across_operations_in_one_scope(self) -> None:
        first_request, first_approval, first_input = _execution_bundle()
        first = self.store.accept(
            _actor(), first_request, first_approval, execution_input=first_input
        )
        second_request, second_approval, second_input = _execution_bundle(
            request_id="request_input-2",
            idempotency_key="deploy-input-2",
            decision_id="decision_input-2",
            revision_id="rev_static-2",
        )
        second = self.store.accept(
            _actor(), second_request, second_approval, execution_input=second_input
        )

        first_fence = self.store.acquire_fence(
            first.operation_id, "executor-a", 10
        )
        with self.assertRaises(LeaseConflict):
            self.store.acquire_fence(second.operation_id, "executor-b", 10)

        self.now[0] = first_fence.expires_at
        second_fence = self.store.acquire_fence(
            second.operation_id, "executor-b", 10
        )
        self.assertGreater(
            second_fence.fencing_token, first_fence.fencing_token
        )
        with self.assertRaises(LeaseConflict):
            self.store.append_revision_state(
                first.operation_id, RevisionState.CREATED, fence=first_fence
            )
        self.store.append_revision_state(
            second.operation_id, RevisionState.CREATED, fence=second_fence
        )
        self.store.integrity_check()

    def _ready_input_operation(self, **bundle_kwargs):
        request, approval, execution_input = _execution_bundle(**bundle_kwargs)
        operation = self.store.accept(
            _actor(), request, approval, execution_input=execution_input
        )
        fence = self.store.acquire_fence(
            operation.operation_id, "executor-success", 100
        )
        for state in (
            RevisionState.CREATED,
            RevisionState.STAGED,
            RevisionState.PREFLIGHT_PASSED,
            RevisionState.STARTING,
            RevisionState.READY,
        ):
            self.store.append_revision_state(
                operation.operation_id, state, fence=fence
            )
        return operation, request, approval, execution_input, fence

    def test_active_cas_conflict_rolls_back_lifecycle_and_pointer(self) -> None:
        operation, _, _, _, fence = self._ready_input_operation()
        before = self.store.revision_history(operation.operation_id)

        with self.assertRaises(OperationConflict):
            self.store.compare_and_swap_active_revision(
                operation.operation_id, D6, fence=fence
            )

        self.assertEqual(before, self.store.revision_history(operation.operation_id))
        self.assertIsNone(
            self.store.active_revision(
                "host_example-1", "demo-service", "production"
            )
        )

    def test_atomic_success_commits_active_lifecycle_event_and_receipt(self) -> None:
        operation, request, approval, _, fence = self._ready_input_operation()
        receipt = _receipt(
            operation.operation_id,
            request,
            approval,
            previous_revision_id=None,
        )

        active = self.store.commit_success(receipt, None, fence=fence)

        self.assertEqual(request.revision_digest, active.revision_digest)
        self.assertEqual(
            RevisionState.ACTIVE,
            self.store.revision_history(operation.operation_id)[-1].state,
        )
        self.assertEqual(
            active,
            self.store.active_revision(
                request.host_id, request.app, request.environment
            ),
        )
        self.assertEqual(
            OperationState.SUCCEEDED, self.store.get(operation.operation_id).state
        )
        self.assertEqual(
            receipt.receipt_id,
            self.store.receipt_payload(operation.operation_id)["receipt_id"],
        )
        self.assertEqual(receipt, self.store.receipt(operation.operation_id))
        self.store.integrity_check()

    def test_atomic_success_rejects_verification_summary_prose(self) -> None:
        operation, request, approval, _, fence = self._ready_input_operation()
        receipt = _receipt(
            operation.operation_id,
            request,
            approval,
            previous_revision_id=None,
            check_summary="Observed healthy.",
        )

        with self.assertRaisesRegex(
            ContractValidationError, "cannot contain summary prose"
        ):
            self.store.commit_success(receipt, None, fence=fence)

        self.assertEqual(
            RevisionState.READY,
            self.store.revision_history(operation.operation_id)[-1].state,
        )
        self.assertIsNone(
            self.store.active_revision(request.host_id, request.app, request.environment)
        )

    def test_atomic_success_requires_the_exact_predecessor_revision(self) -> None:
        operation, request, approval, _, fence = self._ready_input_operation()
        receipt = _receipt(operation.operation_id, request, approval)

        with self.assertRaises(OperationConflict):
            self.store.commit_success(receipt, None, fence=fence)

        self.assertEqual(
            RevisionState.READY,
            self.store.revision_history(operation.operation_id)[-1].state,
        )
        self.assertIsNone(
            self.store.active_revision(request.host_id, request.app, request.environment)
        )

    def test_atomic_success_rechecks_cancellation_and_deadline_in_transaction(self) -> None:
        cancelled, request, approval, _, cancelled_fence = (
            self._ready_input_operation()
        )
        self.store.request_cancellation(cancelled.operation_id, _actor())
        with self.assertRaisesRegex(OperationConflict, "cancellation"):
            self.store.commit_success(
                _receipt(
                    cancelled.operation_id,
                    request,
                    approval,
                    previous_revision_id=None,
                ),
                None,
                fence=cancelled_fence,
            )
        self.store.release_fence(cancelled_fence)

        deadline, request, approval, _, deadline_fence = (
            self._ready_input_operation(
                request_id="request_deadline-2",
                idempotency_key="deploy-deadline-2",
                decision_id="decision_deadline-2",
                revision_id="rev_deadline-2",
            )
        )
        deadline_fence = self.store.heartbeat_fence(deadline_fence, 7200)
        self.now[0] = datetime.fromisoformat(
            DEADLINE.replace("Z", "+00:00")
        ).timestamp()
        with self.assertRaisesRegex(OperationConflict, "deadline"):
            self.store.commit_success(
                _receipt(
                    deadline.operation_id,
                    request,
                    approval,
                    receipt_id="receipt_deadline-2",
                    previous_revision_id=None,
                ),
                None,
                fence=deadline_fence,
            )

    def test_historical_atomic_success_survives_a_later_activation(self) -> None:
        first, first_request, first_approval, _, first_fence = (
            self._ready_input_operation()
        )
        first_receipt = _receipt(
            first.operation_id,
            first_request,
            first_approval,
            receipt_id="receipt_static-1",
            previous_revision_id=None,
        )
        first_active = self.store.commit_success(
            first_receipt, None, fence=first_fence
        )
        self.store.release_fence(first_fence)

        second, second_request, second_approval, _, second_fence = (
            self._ready_input_operation(
                request_id="request_input-2",
                idempotency_key="deploy-input-2",
                decision_id="decision_input-2",
                revision_id="rev_static-2",
            )
        )
        second_receipt = _receipt(
            second.operation_id,
            second_request,
            second_approval,
            receipt_id="receipt_static-2",
            previous_revision_id=first_request.revision_id,
        )
        second_active = self.store.commit_success(
            second_receipt,
            first_active.revision_digest,
            fence=second_fence,
        )

        self.assertEqual(first_active.generation + 1, second_active.generation)
        self.assertEqual(second.operation_id, second_active.operation_id)
        self.store.integrity_check()

    def test_v1_to_v3_migration_preserves_existing_operation(self) -> None:
        operation, _, _ = self._accept()
        connection = sqlite3.connect(str(self.store.database_path))
        try:
            connection.execute("DROP INDEX operations_recovery_lookup")
            connection.execute("DROP INDEX revision_lifecycle_latest")
            connection.execute("DROP INDEX execution_scope_lease_operation")
            for table in (
                "execution_scope_leases",
                "atomic_success_commits",
                "active_revisions",
                "revision_lifecycle",
                "operation_controls",
                "operation_inputs",
            ):
                connection.execute("DROP TABLE " + table)
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        finally:
            connection.close()

        migrated = SQLiteOperationJournal(
            self.store.database_path, clock=lambda: self.now[0]
        )
        self.assertEqual(operation, migrated.get(operation.operation_id))
        migrated.integrity_check()
        connection = migrated._connect()
        try:
            self.assertEqual(
                3, connection.execute("PRAGMA user_version").fetchone()[0]
            )
        finally:
            connection.close()

    def test_v2_migration_serializes_active_legacy_scope_lease_until_expiry(self) -> None:
        legacy, _, _ = self._accept()
        legacy_lease = self.store.acquire_lease(
            legacy.operation_id, "legacy-executor", 30
        )
        second_request, second_approval, second_input = _execution_bundle(
            request_id="request_scope-2",
            idempotency_key="deploy-scope-2",
            decision_id="decision_scope-2",
            revision_id="rev_scope-2",
        )
        second = self.store.accept(
            _actor(),
            second_request,
            second_approval,
            execution_input=second_input,
        )
        connection = sqlite3.connect(str(self.store.database_path))
        try:
            connection.execute("DROP INDEX execution_scope_lease_operation")
            connection.execute("DROP TABLE execution_scope_leases")
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        finally:
            connection.close()

        migrated = SQLiteOperationJournal(
            self.store.database_path, clock=lambda: self.now[0]
        )
        connection = migrated._connect()
        try:
            scope_row = connection.execute(
                """
                SELECT operation_id, owner_id, fencing_token, expires_at
                FROM execution_scope_leases
                WHERE host_id = ? AND app = ? AND environment = ?
                """,
                (second_request.host_id, second_request.app, second_request.environment),
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(scope_row)
        self.assertEqual(legacy.operation_id, scope_row["operation_id"])
        self.assertEqual(legacy_lease.owner_id, scope_row["owner_id"])

        with self.assertRaisesRegex(LeaseConflict, "scope"):
            migrated.acquire_fence(second.operation_id, "v3-executor", 30)

        self.now[0] = legacy_lease.expires_at
        acquired = migrated.acquire_fence(
            second.operation_id, "v3-executor", 30
        )
        self.assertEqual(second.operation_id, acquired.operation_id)
        self.assertGreater(acquired.fencing_token, legacy_lease.fencing_token)
        migrated.integrity_check()


if __name__ == "__main__":
    unittest.main()
