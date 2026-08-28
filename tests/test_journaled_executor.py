from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.domain import (
    Actor,
    ApprovedPlanRef,
    AuthorizationKind,
    CheckStatus,
    CompensationAction,
    OperationPlan,
    OperationRequest,
    OperationState,
    PlanPhase,
    PlanStep,
    ReceiptOutcome,
    Revision,
    RevisionState,
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
    Workload,
    WorkloadKind,
    canonical_digest,
)
from ophelia.domain.revisions import ObservedRevision, ObservedWorkload
from ophelia.execution import (
    BackendContractError,
    ExecutionInput,
    JournaledExecutor,
    LeaseConflict,
    PreflightResult,
    RemoveResult,
    RevisionArtifactRef,
    RuntimeHandle,
    SQLiteOperationJournal,
    TrafficActivationResult,
)
from ophelia.execution.subprocesses import ProcessFailure, ProcessResult


NOW = datetime(2026, 7, 11, 17, 10, tzinfo=timezone.utc).timestamp()


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class _InjectedCrash(BaseException):
    pass


@dataclass
class _RuntimeState:
    materialized_revision_id: str | None = None
    materialized_revision_digest: str | None = None
    active_revision_id: str | None = None
    active_revision_digest: str | None = None
    predecessor_revision_id: str | None = None
    predecessor_revision_digest: str | None = None
    crash_once_at: str | None = None
    preflight_ok: bool = True
    fail_preflight_call: int | None = None
    preflight_calls: int = 0
    fail_active_verification_once: bool = False
    activation_count: int = 0
    restore_count: int = 0
    remove_count: int = 0
    verification_summary: str | None = None
    fail_start_with_process: bool = False
    fail_candidate_verification_once: bool = False
    stop_predecessor_on_start: bool = False


class _FakeStaticBackend:
    backend_name = "fake-static"

    def __init__(self, execution_input: ExecutionInput, state: _RuntimeState) -> None:
        self.execution_input = execution_input
        self.state = state

    def preflight(self, revision: Revision) -> PreflightResult:
        self.state.preflight_calls += 1
        ok = self.state.preflight_ok and (
            self.state.fail_preflight_call != self.state.preflight_calls
        )
        return PreflightResult(
            ok=ok,
            observed_state_digest=_digest("preflight"),
            evidence_digests=(_digest("preflight-evidence"),),
            blocker_codes=() if ok else ("fixture_preflight_failed",),
        )

    def start(self, revision: Revision) -> RuntimeHandle:
        if self.state.stop_predecessor_on_start:
            self.state.predecessor_revision_id = self.state.active_revision_id
            self.state.predecessor_revision_digest = self.state.active_revision_digest
            self.state.active_revision_id = None
            self.state.active_revision_digest = None
        if self.state.fail_start_with_process:
            secret = "postgres://operator:super-secret@example.invalid/database"
            raise ProcessFailure(
                ProcessResult(
                    argv=("docker", "compose", "up"),
                    exit_code=1,
                    exit_reason="nonzero_exit",
                    stdout="",
                    stderr="failed to validate image signature " + secret,
                    stdout_truncated=False,
                    stderr_truncated=False,
                    duration_ms=1,
                )
            )
        self.state.materialized_revision_id = revision.revision_id
        self.state.materialized_revision_digest = revision.content_digest()
        self._crash("after_start")
        return self.handle_for(revision)

    def handle_for(self, revision: Revision) -> RuntimeHandle:
        return RuntimeHandle(
            backend=self.backend_name,
            handle_id=revision.revision_id,
            revision_id=revision.revision_id,
            revision_digest=revision.content_digest(),
        )

    def inspect(self, handle: RuntimeHandle) -> ObservedRevision:
        ready = (
            self.state.materialized_revision_id == handle.revision_id
            and self.state.materialized_revision_digest == handle.revision_digest
        )
        workload = self.execution_input.revision.workloads[0]
        return ObservedRevision(
            host_id=self.execution_input.plan.host_id,
            app=self.execution_input.plan.app,
            environment=self.execution_input.plan.environment,
            revision_id=handle.revision_id,
            revision_digest=handle.revision_digest if ready else None,
            state=RevisionState.READY if ready else RevisionState.FAILED,
            workloads=(
                ObservedWorkload(
                    workload_id=workload.workload_id,
                    workload_kind=workload.workload_kind,
                    runtime_state="materialized" if ready else "missing",
                    artifact_digest=workload.artifact_digest,
                    ready=ready,
                ),
            ),
            observed_at="2026-07-11T17:20:00Z",
        )

    def verify(
        self, revision: Revision, observed: ObservedRevision
    ) -> VerificationResult:
        if self.state.fail_candidate_verification_once:
            self.state.fail_candidate_verification_once = False
            return _verification(
                revision, False, "candidate", self.state.verification_summary
            )
        passed = (
            observed.state is RevisionState.READY
            and observed.revision_digest == revision.content_digest()
        )
        return _verification(
            revision, passed, "candidate", self.state.verification_summary
        )

    def activate(
        self,
        candidate: RuntimeHandle,
        expected_active_revision_digest: str | None,
    ) -> TrafficActivationResult:
        if (
            self.state.active_revision_id == candidate.revision_id
            and self.state.active_revision_digest == candidate.revision_digest
        ):
            return self._activation_result(
                self.state.predecessor_revision_digest,
                candidate.revision_digest,
            )
        if self.state.active_revision_digest != expected_active_revision_digest:
            raise RuntimeError("active revision changed")
        self.state.predecessor_revision_id = self.state.active_revision_id
        self.state.predecessor_revision_digest = self.state.active_revision_digest
        self.state.active_revision_id = candidate.revision_id
        self.state.active_revision_digest = candidate.revision_digest
        self.state.activation_count += 1
        self._crash("after_activate")
        return self._activation_result(
            self.state.predecessor_revision_digest,
            candidate.revision_digest,
        )

    def verify_active(
        self, revision: Revision, handle: RuntimeHandle
    ) -> VerificationResult:
        if self.state.fail_active_verification_once:
            self.state.fail_active_verification_once = False
            return _verification(
                revision, False, "active", self.state.verification_summary
            )
        passed = (
            self.state.active_revision_id == handle.revision_id
            and self.state.active_revision_digest == handle.revision_digest
        )
        return _verification(
            revision, passed, "active", self.state.verification_summary
        )

    def restore(
        self, previous: RuntimeHandle, failed_candidate: RuntimeHandle
    ) -> TrafficActivationResult:
        self.state.active_revision_id = previous.revision_id
        self.state.active_revision_digest = previous.revision_digest
        self.state.restore_count += 1
        return self._activation_result(
            failed_candidate.revision_digest, previous.revision_digest
        )

    def deactivate(self, failed_candidate: RuntimeHandle) -> TrafficActivationResult:
        self.state.active_revision_id = None
        self.state.active_revision_digest = None
        self.state.restore_count += 1
        return self._activation_result(
            failed_candidate.revision_digest, None
        )

    def remove(self, handle: RuntimeHandle) -> RemoveResult:
        self.state.remove_count += 1
        self.state.materialized_revision_id = None
        self.state.materialized_revision_digest = None
        self._crash("after_remove")
        return RemoveResult(
            removed=True,
            observed_state_digest=_digest("candidate-absent"),
        )

    def _activation_result(
        self, previous_digest: str | None, active_digest: str | None
    ) -> TrafficActivationResult:
        evidence = _digest(
            f"{self.state.active_revision_id}:{self.state.active_revision_digest}"
        )
        return TrafficActivationResult(
            committed_atomically=True,
            previous_revision_digest=previous_digest,
            active_revision_digest=active_digest,
            observed_state_digest=evidence,
            evidence_digests=(evidence,),
        )

    def _crash(self, boundary: str) -> None:
        if self.state.crash_once_at == boundary:
            self.state.crash_once_at = None
            raise _InjectedCrash(boundary)


def _verification(
    revision: Revision,
    passed: bool,
    name: str,
    summary: str | None = None,
) -> VerificationResult:
    state_digest = _digest(f"{name}:{passed}")
    return VerificationResult(
        status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
        observed_revision_digest=revision.content_digest() if passed else None,
        observed_state_digest=state_digest,
        observed_at="2026-07-11T17:25:00Z",
        checks=(
            VerificationCheck(
                name=name,
                status=CheckStatus.PASSED if passed else CheckStatus.FAILED,
                observed_digest=state_digest,
                summary=summary,
            ),
        ),
    )


def _bundle(
    suffix: str,
    *,
    deadline: str = "2026-07-11T18:30:00Z",
) -> tuple[Actor, OperationRequest, ApprovedPlanRef, ExecutionInput]:
    artifact_digest = _digest(f"artifact:{suffix}")
    revision = Revision.create(
        app="demo-static",
        environment="production",
        manifest_digest=_digest("manifest"),
        artifact_digests=(artifact_digest,),
        renderer_version="executor-test",
        workloads=(
            Workload(
                workload_id="static",
                workload_kind=WorkloadKind.STATIC,
                artifact_digest=artifact_digest,
            ),
        ),
        created_at="2026-07-11T17:00:00Z",
    )
    request = OperationRequest(
        request_id=f"request_{suffix}",
        operation="deploy.apply",
        host_id="host_executor-1",
        app=revision.app,
        environment=revision.environment,
        revision_id=revision.revision_id,
        revision_digest=revision.content_digest(),
        idempotency_key=f"deploy-{suffix}",
    )
    phases = (
        PlanPhase.STAGE,
        PlanPhase.PREFLIGHT,
        PlanPhase.START_CANDIDATE,
        PlanPhase.READINESS_VERIFY,
        PlanPhase.SWITCH_TRAFFIC,
        PlanPhase.EXTERNAL_VERIFY,
        PlanPhase.DRAIN_PREVIOUS,
        PlanPhase.COMMIT,
        PlanPhase.EMIT_RECEIPT,
    )
    compensations = {
        PlanPhase.START_CANDIDATE: CompensationAction.DELETE_CANDIDATE,
        PlanPhase.SWITCH_TRAFFIC: CompensationAction.RESTORE_TRAFFIC,
        PlanPhase.COMMIT: CompensationAction.RECOVER_FROM_JOURNAL,
        PlanPhase.EMIT_RECEIPT: CompensationAction.RECOVER_FROM_JOURNAL,
    }
    plan = OperationPlan.create(
        request=request,
        manifest_digest=revision.manifest_digest,
        artifact_digests=revision.artifact_digests,
        observed_state_digest=_digest("baseline"),
        policy_digest=_digest("policy"),
        steps=tuple(
            PlanStep(
                order=index,
                phase=phase,
                mutates_runtime=phase
                in {
                    PlanPhase.START_CANDIDATE,
                    PlanPhase.SWITCH_TRAFFIC,
                    PlanPhase.COMMIT,
                    PlanPhase.EMIT_RECEIPT,
                },
                desired_effect_digest=canonical_digest(
                    {
                        "phase": phase.value,
                        "revision_digest": revision.content_digest(),
                        "artifact_digest": artifact_digest,
                    }
                ),
                compensation=compensations.get(phase, CompensationAction.NONE),
            )
            for index, phase in enumerate(phases, start=1)
        ),
        blocker_codes=(),
        created_at="2026-07-11T17:00:00Z",
    )
    actor = Actor(
        actor_id="actor_executor",
        source="test",
        authenticated_by="fixture",
    )
    approval = ApprovedPlanRef.bind(
        plan,
        actor_id=actor.actor_id,
        decision_id=f"decision_{suffix}",
        authorization_kind=AuthorizationKind.LUMEN_DECISION,
        issuer="fixture",
        audience=request.host_id,
        approved_at="2026-07-11T17:05:00Z",
        expires_at="2026-07-11T19:00:00Z",
        approval_nonce=f"nonce-{suffix}",
    )
    artifact_ref = RevisionArtifactRef(
        revision_id=revision.revision_id,
        revision_digest=revision.content_digest(),
        relative_root=f"staging/demo-static/{suffix}/candidate",
        artifact_digest=artifact_digest,
    )
    execution_input = ExecutionInput.bind(
        request=request,
        plan=plan,
        approved_plan=approval,
        revision=revision,
        artifact_ref=artifact_ref,
        deadline=deadline,
    )
    return actor, request, approval, execution_input


class JournaledExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.now = [NOW]
        self.journal = SQLiteOperationJournal.beneath_runtime_root(
            Path(self.temporary.name), clock=lambda: self.now[0]
        )
        self.runtime = _RuntimeState()
        self.executor = JournaledExecutor(
            journal=self.journal,
            backend_factory=lambda execution_input, operation, fence: _FakeStaticBackend(
                execution_input, self.runtime
            ),
            clock=lambda: self.now[0],
            lease_ttl_seconds=60,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _submit(self, suffix: str, **kwargs):
        actor, request, approval, execution_input = _bundle(suffix, **kwargs)
        operation = self.executor.submit(
            actor, request, approval, execution_input=execution_input
        )
        return operation, actor, request, approval, execution_input

    def test_success_runs_the_canonical_phases_and_commits_active_truth(self) -> None:
        operation, _, request, _, _ = self._submit("success")

        receipt = self.executor.run(operation.operation_id, owner_id="worker-one")

        self.assertEqual(ReceiptOutcome.SUCCEEDED, receipt.outcome)
        self.assertEqual(request.revision_id, receipt.active_revision_id)
        self.assertEqual(OperationState.SUCCEEDED, self.journal.get(operation.operation_id).state)
        self.assertEqual(receipt, self.journal.receipt(operation.operation_id))
        self.assertEqual(
            RevisionState.ACTIVE,
            self.journal.revision_history(operation.operation_id)[-1].state,
        )
        event_types = [
            event.event_type for event in self.journal.events(operation.operation_id)
        ]
        for phase in (
            PlanPhase.STAGE,
            PlanPhase.PREFLIGHT,
            PlanPhase.START_CANDIDATE,
            PlanPhase.READINESS_VERIFY,
            PlanPhase.SWITCH_TRAFFIC,
            PlanPhase.EXTERNAL_VERIFY,
            PlanPhase.DRAIN_PREVIOUS,
            PlanPhase.COMMIT,
            PlanPhase.EMIT_RECEIPT,
        ):
            self.assertIn(f"phase.{phase.value}.completed", event_types)
        self.journal.integrity_check()

    def test_long_backend_phase_keeps_execution_fence_active(self) -> None:
        started = time.monotonic()
        clock = lambda: NOW + time.monotonic() - started
        journal = SQLiteOperationJournal.beneath_runtime_root(
            Path(self.temporary.name) / "slow", clock=clock
        )
        runtime = _RuntimeState()

        class SlowStartBackend(_FakeStaticBackend):
            def start(self, revision: Revision) -> RuntimeHandle:
                time.sleep(0.15)
                return super().start(revision)

        executor = JournaledExecutor(
            journal=journal,
            backend_factory=lambda execution_input, operation, fence: SlowStartBackend(
                execution_input, runtime
            ),
            clock=clock,
            lease_ttl_seconds=0.05,
            lease_heartbeat_interval_seconds=0.01,
        )
        actor, request, approval, execution_input = _bundle("slow-phase")
        operation = executor.submit(
            actor, request, approval, execution_input=execution_input
        )

        receipt = executor.run(operation.operation_id, owner_id="worker-one")

        self.assertEqual(ReceiptOutcome.SUCCEEDED, receipt.outcome)
        self.assertEqual(request.revision_id, receipt.active_revision_id)
        journal.integrity_check()

    def test_heartbeat_authority_loss_stops_execution(self) -> None:
        started = time.monotonic()
        clock = lambda: NOW + time.monotonic() - started
        journal = SQLiteOperationJournal.beneath_runtime_root(
            Path(self.temporary.name) / "lost-heartbeat", clock=clock
        )
        original_heartbeat = journal.heartbeat_fence

        def heartbeat_fence(fence, ttl_seconds):
            if threading.current_thread().name == "ophelia-execution-fence-heartbeat":
                raise LeaseConflict("fixture heartbeat authority loss")
            return original_heartbeat(fence, ttl_seconds)

        journal.heartbeat_fence = heartbeat_fence
        runtime = _RuntimeState()

        class SlowStartBackend(_FakeStaticBackend):
            def start(self, revision: Revision) -> RuntimeHandle:
                time.sleep(0.05)
                return super().start(revision)

        executor = JournaledExecutor(
            journal=journal,
            backend_factory=lambda execution_input, operation, fence: SlowStartBackend(
                execution_input, runtime
            ),
            clock=clock,
            lease_ttl_seconds=0.2,
            lease_heartbeat_interval_seconds=0.01,
        )
        actor, request, approval, execution_input = _bundle("lost-heartbeat")
        operation = executor.submit(
            actor, request, approval, execution_input=execution_input
        )

        with self.assertRaisesRegex(LeaseConflict, "heartbeat authority loss"):
            executor.run(operation.operation_id, owner_id="worker-one")

        self.assertIsNone(journal.receipt(operation.operation_id))

    def test_terminal_retry_returns_the_original_receipt_without_mutation(self) -> None:
        operation, actor, request, approval, execution_input = self._submit("retry")
        receipt = self.executor.run(operation.operation_id, owner_id="worker-one")

        retry_request = replace(request, request_id="request_retry-transport")
        retried = self.executor.submit(
            actor, retry_request, approval, execution_input=execution_input
        )

        self.assertEqual(operation.operation_id, retried.operation_id)
        self.assertEqual(
            receipt,
            self.executor.run(operation.operation_id, owner_id="worker-two"),
        )
        self.assertEqual(1, self.runtime.activation_count)

    def test_backend_verification_prose_is_never_persisted(self) -> None:
        secret = "postgres://operator:super-secret@example.invalid/database"
        self.runtime.verification_summary = secret
        operation, _, _, _, _ = self._submit("redacted")

        receipt = self.executor.run(operation.operation_id, owner_id="worker-one")

        self.assertTrue(
            all(check.summary is None for check in receipt.verification.checks)
        )
        database_bytes = self.journal.database_path.read_bytes()
        wal_path = Path(str(self.journal.database_path) + "-wal")
        if wal_path.exists():
            database_bytes += wal_path.read_bytes()
        self.assertNotIn(secret.encode(), database_bytes)

    def test_recovery_reconciles_a_crash_after_the_traffic_switch(self) -> None:
        operation, _, _, _, _ = self._submit("crash")
        self.runtime.crash_once_at = "after_activate"

        with self.assertRaises(_InjectedCrash):
            self.executor.run(operation.operation_id, owner_id="worker-one")

        self.assertIsNone(self.journal.receipt(operation.operation_id))
        receipt = self.executor.run(operation.operation_id, owner_id="worker-two")
        self.assertEqual(ReceiptOutcome.SUCCEEDED, receipt.outcome)
        self.assertEqual(1, self.runtime.activation_count)
        self.journal.integrity_check()

    def test_recovery_replays_idempotent_candidate_materialization(self) -> None:
        operation, _, _, _, _ = self._submit("start-crash")
        self.runtime.crash_once_at = "after_start"

        with self.assertRaises(_InjectedCrash):
            self.executor.run(operation.operation_id, owner_id="worker-one")

        self.assertIsNotNone(self.runtime.materialized_revision_id)
        receipt = self.executor.run(operation.operation_id, owner_id="worker-two")
        self.assertEqual(ReceiptOutcome.SUCCEEDED, receipt.outcome)
        self.assertEqual(1, self.runtime.activation_count)

    def test_recovery_finishes_idempotent_compensation_after_cleanup_crash(self) -> None:
        operation, _, _, _, _ = self._submit("compensation-crash")
        self.runtime.fail_active_verification_once = True
        self.runtime.crash_once_at = "after_remove"

        with self.assertRaises(_InjectedCrash):
            self.executor.run(operation.operation_id, owner_id="worker-one")

        self.assertEqual(
            OperationState.COMPENSATING,
            self.journal.get(operation.operation_id).state,
        )
        self.assertIsNone(self.runtime.active_revision_id)
        receipt = self.executor.run(operation.operation_id, owner_id="worker-two")
        self.assertEqual(ReceiptOutcome.FAILED_COMPENSATED, receipt.outcome)
        self.assertIsNone(receipt.active_revision_id)
        self.journal.integrity_check()

    def test_failed_external_verification_restores_the_exact_predecessor(self) -> None:
        first, _, first_request, _, _ = self._submit("first")
        self.executor.run(first.operation_id, owner_id="worker-one")

        second, _, _, _, _ = self._submit("second")
        self.runtime.fail_active_verification_once = True
        receipt = self.executor.run(second.operation_id, owner_id="worker-two")

        self.assertEqual(ReceiptOutcome.FAILED_COMPENSATED, receipt.outcome)
        self.assertEqual(first_request.revision_id, receipt.active_revision_id)
        self.assertEqual(first_request.revision_id, self.runtime.active_revision_id)
        active = self.journal.active_revision(
            first_request.host_id, first_request.app, first_request.environment
        )
        self.assertEqual(first_request.revision_id, active.revision_id)
        self.assertEqual(1, self.runtime.restore_count)
        self.journal.integrity_check()

    def test_failed_recreate_candidate_restores_predecessor_stopped_during_start(self) -> None:
        first, _, first_request, _, _ = self._submit("recreate-first")
        self.executor.run(first.operation_id, owner_id="worker-one")

        second, _, _, _, _ = self._submit("recreate-second")
        self.runtime.stop_predecessor_on_start = True
        self.runtime.fail_candidate_verification_once = True
        receipt = self.executor.run(second.operation_id, owner_id="worker-two")

        self.assertEqual(ReceiptOutcome.FAILED_COMPENSATED, receipt.outcome)
        self.assertEqual(first_request.revision_id, receipt.active_revision_id)
        self.assertEqual(first_request.revision_id, self.runtime.active_revision_id)
        self.assertEqual(1, self.runtime.restore_count)
        self.assertEqual(1, self.runtime.remove_count)
        self.journal.integrity_check()

    def test_cancellation_and_expired_deadline_finish_without_live_activation(self) -> None:
        cancelled, actor, _, _, _ = self._submit("cancelled")
        self.executor.cancel(cancelled.operation_id, actor)
        cancelled_receipt = self.executor.run(
            cancelled.operation_id, owner_id="worker-one"
        )
        self.assertEqual(ReceiptOutcome.CANCELLED, cancelled_receipt.outcome)

        expired, _, _, _, _ = self._submit(
            "expired", deadline="2026-07-11T17:11:00Z"
        )
        self.now[0] = datetime(
            2026, 7, 11, 17, 12, tzinfo=timezone.utc
        ).timestamp()
        expired_receipt = self.executor.run(
            expired.operation_id, owner_id="worker-two"
        )
        self.assertEqual(ReceiptOutcome.FAILED_COMPENSATED, expired_receipt.outcome)
        self.assertIsNone(self.runtime.active_revision_id)
        self.journal.integrity_check()

    def test_incomplete_pipeline_is_rejected_before_durable_acceptance(self) -> None:
        actor, request, _, execution_input = _bundle("incomplete")
        original = execution_input.plan
        incomplete_steps = tuple(
            replace(step, order=index)
            for index, step in enumerate(
                (
                    step
                    for step in original.steps
                    if step.phase is not PlanPhase.DRAIN_PREVIOUS
                ),
                start=1,
            )
        )
        incomplete_plan = OperationPlan.create(
            request=request,
            manifest_digest=original.manifest_digest,
            artifact_digests=original.artifact_digests,
            observed_state_digest=original.observed_state_digest,
            policy_digest=original.policy_digest,
            steps=incomplete_steps,
            blocker_codes=(),
            created_at=original.created_at,
        )
        incomplete_approval = ApprovedPlanRef.bind(
            incomplete_plan,
            actor_id=actor.actor_id,
            decision_id="decision_incomplete-plan",
            authorization_kind=AuthorizationKind.LUMEN_DECISION,
            issuer="fixture",
            audience=request.host_id,
            approved_at="2026-07-11T17:05:00Z",
            expires_at="2026-07-11T19:00:00Z",
            approval_nonce="incomplete-nonce",
        )
        incomplete_input = ExecutionInput.bind(
            request=request,
            plan=incomplete_plan,
            approved_plan=incomplete_approval,
            revision=execution_input.revision,
            artifact_ref=execution_input.artifact_ref,
            deadline=execution_input.deadline,
        )

        with self.assertRaises(BackendContractError):
            self.executor.submit(
                actor,
                request,
                incomplete_approval,
                execution_input=incomplete_input,
            )
        self.assertEqual((), self.journal.list_recoverable())

    def test_altered_static_effect_digest_is_rejected_before_durable_acceptance(self) -> None:
        actor, request, _, execution_input = _bundle("altered-effect")
        original = execution_input.plan
        altered_steps = tuple(
            replace(
                step,
                desired_effect_digest=canonical_digest(
                    {"phase": step.phase.value, "altered": True}
                ),
            )
            if step.phase is PlanPhase.SWITCH_TRAFFIC
            else step
            for step in original.steps
        )
        altered_plan = OperationPlan.create(
            request=request,
            manifest_digest=original.manifest_digest,
            artifact_digests=original.artifact_digests,
            observed_state_digest=original.observed_state_digest,
            policy_digest=original.policy_digest,
            steps=altered_steps,
            blocker_codes=(),
            created_at=original.created_at,
        )
        altered_approval = ApprovedPlanRef.bind(
            altered_plan,
            actor_id=actor.actor_id,
            decision_id="decision_altered-effect",
            authorization_kind=AuthorizationKind.LUMEN_DECISION,
            issuer="fixture",
            audience=request.host_id,
            approved_at="2026-07-11T17:05:00Z",
            expires_at="2026-07-11T19:00:00Z",
            approval_nonce="altered-effect-nonce",
        )
        altered_input = ExecutionInput.bind(
            request=request,
            plan=altered_plan,
            approved_plan=altered_approval,
            revision=execution_input.revision,
            artifact_ref=execution_input.artifact_ref,
            deadline=execution_input.deadline,
        )

        with self.assertRaisesRegex(BackendContractError, "exact desired effects"):
            self.executor.submit(
                actor,
                request,
                altered_approval,
                execution_input=altered_input,
            )
        self.assertEqual((), self.journal.list_recoverable())
        self.assertEqual(0, self.runtime.activation_count)
        self.assertEqual(0, self.runtime.remove_count)

    def test_preflight_failure_does_not_remove_unstarted_candidate(self) -> None:
        self.runtime.fail_preflight_call = 2
        operation, _, _, _, _ = self._submit("preflight-failure")

        receipt = self.executor.run(operation.operation_id, owner_id="worker-one")

        self.assertEqual(ReceiptOutcome.FAILED_COMPENSATED, receipt.outcome)
        self.assertEqual(0, self.runtime.remove_count)
        self.assertFalse(
            any(
                event.event_type
                == f"phase.{PlanPhase.START_CANDIDATE.value}.started"
                for event in self.journal.events(operation.operation_id)
            )
        )

    def test_process_failure_persists_safe_actionable_diagnostic(self) -> None:
        self.runtime.fail_start_with_process = True
        operation, _, _, _, _ = self._submit("process-failure")

        receipt = self.executor.run(operation.operation_id, owner_id="worker-one")

        self.assertEqual(ReceiptOutcome.FAILED_COMPENSATED, receipt.outcome)
        events = self.journal.events(operation.operation_id)
        failure = next(
            event for event in events if event.event_type == "operation.failure_observed"
        )
        diagnostic = json.loads(failure.message or "{}")
        self.assertEqual("start_candidate", diagnostic["phase"])
        self.assertEqual("external_process", diagnostic["category"])
        self.assertEqual("docker.compose.up", diagnostic["action"])
        self.assertEqual("image_signature_validation", diagnostic["code"])
        self.assertEqual(1, diagnostic["exit_code"])
        self.assertNotIn("super-secret", json.dumps([event.to_dict() for event in events]))
        self.assertLess(
            [event.event_type for event in events].index("operation.failure_observed"),
            [event.event_type for event in events].index("operation.compensating"),
        )


if __name__ == "__main__":
    unittest.main()
