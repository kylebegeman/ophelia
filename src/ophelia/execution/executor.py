"""Journal-first canonical executor for recoverable revision activation."""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Callable, Optional, Protocol, Tuple

from ..domain._contracts import canonical_digest, parse_utc
from ..domain.events import OperationEvent
from ..domain.operations import Actor, OperationRef, OperationRequest, OperationState
from ..domain.plans import ApprovedPlanRef, CompensationAction, PlanPhase
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
from ..domain.revisions import (
    ObservedRevision,
    Revision,
    RevisionState,
    WorkloadKind,
)
from .contracts import (
    ActiveRevision,
    ExecutionControl,
    ExecutionFence,
    ExecutionInput,
    PreflightResult,
    RemoveResult,
    RuntimeHandle,
    StopResult,
    TrafficActivationResult,
)
from .operation_store import (
    LeaseConflict,
    OperationConflict,
    SQLiteOperationJournal,
)
from .subprocesses import ProcessFailure


class JournaledExecutorError(RuntimeError):
    """Base error for failures that cannot be represented by a terminal receipt."""


class BackendContractError(JournaledExecutorError):
    """A backend claimed an outcome that did not match the approved revision."""


class RecoverableExecutionBackend(Protocol):
    """Runtime seam required by the recoverable activation pipeline.

    ``start``, ``activate``, ``restore``, ``deactivate``, and ``remove`` must be
    reconcile-first and idempotent for the operation and scope fence supplied
    when the backend instance was created. A replay must either return the
    already-observed postcondition or restore a verified predecessor and fail.
    """

    backend_name: str

    def preflight(self, revision: Revision) -> PreflightResult:
        ...

    def start(self, revision: Revision) -> RuntimeHandle:
        ...

    def handle_for(self, revision: Revision) -> RuntimeHandle:
        ...

    def inspect(self, handle: RuntimeHandle) -> ObservedRevision:
        ...

    def verify(
        self, revision: Revision, observed: ObservedRevision
    ) -> VerificationResult:
        ...

    def activate(
        self,
        candidate: RuntimeHandle,
        expected_active_revision_digest: Optional[str],
    ) -> TrafficActivationResult:
        ...

    def verify_active(
        self, revision: Revision, handle: RuntimeHandle
    ) -> VerificationResult:
        ...

    def restore(
        self, previous: RuntimeHandle, failed_candidate: RuntimeHandle
    ) -> TrafficActivationResult:
        ...

    def deactivate(self, failed_candidate: RuntimeHandle) -> TrafficActivationResult:
        ...

    def drain_previous(
        self, previous: RuntimeHandle, candidate: RuntimeHandle
    ) -> StopResult:
        ...

    def remove(self, handle: RuntimeHandle) -> RemoveResult:
        ...


BackendFactory = Callable[
    [ExecutionInput, OperationRef, ExecutionFence], RecoverableExecutionBackend
]

_SUPPORTED_PHASES = (
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

_SUPPORTED_STEPS = {
    PlanPhase.STAGE: (False, CompensationAction.NONE),
    PlanPhase.PREFLIGHT: (False, CompensationAction.NONE),
    PlanPhase.START_CANDIDATE: (
        True,
        CompensationAction.DELETE_CANDIDATE,
    ),
    PlanPhase.READINESS_VERIFY: (False, CompensationAction.NONE),
    PlanPhase.SWITCH_TRAFFIC: (
        True,
        CompensationAction.RESTORE_TRAFFIC,
    ),
    PlanPhase.EXTERNAL_VERIFY: (False, CompensationAction.NONE),
    PlanPhase.DRAIN_PREVIOUS: (False, CompensationAction.NONE),
    PlanPhase.COMMIT: (True, CompensationAction.RECOVER_FROM_JOURNAL),
    PlanPhase.EMIT_RECEIPT: (
        True,
        CompensationAction.RECOVER_FROM_JOURNAL,
    ),
}


@dataclass(frozen=True)
class _PhaseFailure(Exception):
    phase: PlanPhase
    verification: Optional[VerificationResult] = None
    evidence_digests: Tuple[str, ...] = ()


class _CancellationRequested(Exception):
    pass


class _ExecutionDeadlineExpired(Exception):
    pass


class JournaledExecutor:
    """Execute one immutable revision through journaled, recoverable phases.

    The accepted execution input is the only source of desired state. Each
    mutating backend call is preceded by a durable event and an active lease
    fence. Backends remain responsible for enforcing the same fencing token at
    the actual runtime side-effect boundary.
    """

    def __init__(
        self,
        *,
        journal: SQLiteOperationJournal,
        backend_factory: BackendFactory,
        clock: Callable[[], float] = time.time,
        lease_ttl_seconds: float = 300.0,
    ) -> None:
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be greater than zero.")
        self.journal = journal
        self.backend_factory = backend_factory
        self._clock = clock
        self.lease_ttl_seconds = lease_ttl_seconds

    def submit(
        self,
        actor: Actor,
        request: OperationRequest,
        approved_plan: ApprovedPlanRef,
        *,
        execution_input: ExecutionInput,
    ) -> OperationRef:
        """Durably accept exact execution input before acknowledging the caller."""

        self._validate_supported_plan(execution_input)
        return self.journal.accept(
            actor,
            request,
            approved_plan,
            execution_input=execution_input,
        )

    def cancel(self, operation_id: str, actor: Actor) -> ExecutionControl:
        return self.journal.request_cancellation(operation_id, actor)

    def recover(
        self, owner_id: str, *, limit: int = 100
    ) -> Tuple[TerminalReceipt, ...]:
        receipts = []
        for operation in self.journal.list_recoverable(limit):
            receipts.append(self.run(operation.operation_id, owner_id=owner_id))
        return tuple(receipts)

    def run(self, operation_id: str, *, owner_id: str) -> TerminalReceipt:
        existing = self.journal.receipt(operation_id)
        if existing is not None:
            return existing

        try:
            fence = self.journal.acquire_fence(
                operation_id, owner_id, self.lease_ttl_seconds
            )
        except OperationConflict:
            existing = self.journal.receipt(operation_id)
            if existing is not None:
                return existing
            raise

        try:
            operation = self.journal.get(operation_id)
            execution_input = self.journal.load_execution_input(operation_id)
            self._validate_supported_plan(execution_input)
            backend = self.backend_factory(execution_input, operation, fence)
            return self._run_fenced(operation, execution_input, backend, fence)
        finally:
            try:
                self.journal.release_fence(fence)
            except LeaseConflict:
                # A long-running phase may have outlived this worker's lease.
                # The stale token must not mask a receipt or a newer owner.
                pass

    def _run_fenced(
        self,
        operation: OperationRef,
        execution_input: ExecutionInput,
        backend: RecoverableExecutionBackend,
        fence: ExecutionFence,
    ) -> TerminalReceipt:
        events = self.journal.events(operation.operation_id)
        started_at = events[0].occurred_at if events else self._utc_now()
        previous = self.journal.active_revision(
            execution_input.plan.host_id,
            execution_input.plan.app,
            execution_input.plan.environment,
        )
        candidate = backend.handle_for(execution_input.revision)
        self._validate_handle(execution_input, candidate)

        if operation.state in {
            OperationState.COMPENSATING,
            OperationState.CANCELLING,
        }:
            return self._finish_interrupted(
                operation,
                execution_input,
                backend,
                candidate,
                previous,
                fence,
                started_at,
                cancelled=operation.state is OperationState.CANCELLING,
                verification=None,
            )

        self._event(
            operation,
            "operation.executing",
            OperationState.EXECUTING,
            fence,
        )
        self._ensure_revision_state(
            operation.operation_id, RevisionState.CREATED, fence
        )

        verification: Optional[VerificationResult] = None
        try:
            fence = self._phase_stage(operation, execution_input, backend, fence)
            fence = self._phase_preflight(
                operation, execution_input, backend, fence
            )
            candidate, verification, fence = self._phase_start_candidate(
                operation, execution_input, backend, candidate, fence
            )
            verification, fence = self._phase_readiness(
                operation, execution_input, backend, candidate, fence
            )
            fence = self._phase_switch_traffic(
                operation,
                execution_input,
                backend,
                candidate,
                previous,
                fence,
            )
            verification, fence = self._phase_external_verify(
                operation, execution_input, backend, candidate, fence
            )
            fence = self._phase_drain_previous(
                operation,
                execution_input,
                backend,
                candidate,
                previous,
                fence,
            )
            self._checkpoint_control(operation.operation_id, execution_input)
            fence = self._heartbeat(fence)
            self._event(
                operation,
                f"phase.{PlanPhase.COMMIT.value}.started",
                OperationState.EXECUTING,
                fence,
            )
            receipt = self._success_receipt(
                operation,
                execution_input,
                previous,
                verification,
                started_at,
            )
            self.journal.commit_success(
                receipt,
                None if previous is None else previous.revision_digest,
                fence=fence,
            )
            return receipt
        except LeaseConflict:
            raise
        except _CancellationRequested:
            return self._finish_interrupted(
                operation,
                execution_input,
                backend,
                candidate,
                previous,
                fence,
                started_at,
                cancelled=True,
                verification=verification,
            )
        except Exception as exc:
            failed_verification = (
                exc.verification if isinstance(exc, _PhaseFailure) else verification
            )
            return self._finish_interrupted(
                operation,
                execution_input,
                backend,
                candidate,
                previous,
                fence,
                started_at,
                cancelled=False,
                verification=failed_verification,
                failure_diagnostic=self._failure_diagnostic(
                    operation.operation_id, exc
                ),
            )

    def _phase_stage(
        self,
        operation: OperationRef,
        execution_input: ExecutionInput,
        backend: RecoverableExecutionBackend,
        fence: ExecutionFence,
    ) -> ExecutionFence:
        phase = PlanPhase.STAGE
        if self._phase_completed(operation.operation_id, phase):
            return fence
        fence = self._before_phase(operation, execution_input, phase, fence)
        result = backend.preflight(execution_input.revision)
        if not result.ok:
            raise _PhaseFailure(
                phase,
                self._preflight_verification(result),
                result.evidence_digests,
            )
        self._ensure_revision_state(
            operation.operation_id, RevisionState.STAGED, fence
        )
        self._complete_phase(operation, phase, result.evidence_digests, fence)
        return fence

    def _phase_preflight(
        self,
        operation: OperationRef,
        execution_input: ExecutionInput,
        backend: RecoverableExecutionBackend,
        fence: ExecutionFence,
    ) -> ExecutionFence:
        phase = PlanPhase.PREFLIGHT
        if self._phase_completed(operation.operation_id, phase):
            return fence
        fence = self._before_phase(operation, execution_input, phase, fence)
        result = backend.preflight(execution_input.revision)
        if not result.ok:
            raise _PhaseFailure(
                phase,
                self._preflight_verification(result),
                result.evidence_digests,
            )
        self._ensure_revision_state(
            operation.operation_id, RevisionState.PREFLIGHT_PASSED, fence
        )
        self._complete_phase(operation, phase, result.evidence_digests, fence)
        return fence

    def _phase_start_candidate(
        self,
        operation: OperationRef,
        execution_input: ExecutionInput,
        backend: RecoverableExecutionBackend,
        candidate: RuntimeHandle,
        fence: ExecutionFence,
    ) -> Tuple[RuntimeHandle, VerificationResult, ExecutionFence]:
        phase = PlanPhase.START_CANDIDATE
        if not self._phase_completed(operation.operation_id, phase):
            fence = self._before_phase(operation, execution_input, phase, fence)
            self._ensure_revision_state(
                operation.operation_id, RevisionState.STARTING, fence
            )
            candidate = backend.start(execution_input.revision)
            self._validate_handle(execution_input, candidate)
        observed = backend.inspect(candidate)
        verification = backend.verify(execution_input.revision, observed)
        verification = self._sanitize_verification(verification)
        if verification.status is not VerificationStatus.PASSED:
            raise _PhaseFailure(phase, verification)
        self._ensure_revision_state(
            operation.operation_id, RevisionState.READY, fence
        )
        self._complete_phase(
            operation, phase, self._verification_evidence(verification), fence
        )
        return candidate, verification, fence

    def _phase_readiness(
        self,
        operation: OperationRef,
        execution_input: ExecutionInput,
        backend: RecoverableExecutionBackend,
        candidate: RuntimeHandle,
        fence: ExecutionFence,
    ) -> Tuple[VerificationResult, ExecutionFence]:
        phase = PlanPhase.READINESS_VERIFY
        if not self._phase_completed(operation.operation_id, phase):
            fence = self._before_phase(operation, execution_input, phase, fence)
        observed = backend.inspect(candidate)
        verification = backend.verify(execution_input.revision, observed)
        verification = self._sanitize_verification(verification)
        if verification.status is not VerificationStatus.PASSED:
            raise _PhaseFailure(phase, verification)
        self._complete_phase(
            operation, phase, self._verification_evidence(verification), fence
        )
        return verification, fence

    def _phase_switch_traffic(
        self,
        operation: OperationRef,
        execution_input: ExecutionInput,
        backend: RecoverableExecutionBackend,
        candidate: RuntimeHandle,
        previous: Optional[ActiveRevision],
        fence: ExecutionFence,
    ) -> ExecutionFence:
        phase = PlanPhase.SWITCH_TRAFFIC
        if self._phase_completed(operation.operation_id, phase):
            return fence
        fence = self._before_phase(operation, execution_input, phase, fence)
        self._ensure_revision_state(
            operation.operation_id, RevisionState.TRAFFIC_CANDIDATE, fence
        )
        result = backend.activate(
            candidate, None if previous is None else previous.revision_digest
        )
        if result.active_revision_digest != execution_input.revision.content_digest():
            raise BackendContractError(
                "Traffic backend did not activate the approved revision."
            )
        self._complete_phase(operation, phase, result.evidence_digests, fence)
        return fence

    def _phase_external_verify(
        self,
        operation: OperationRef,
        execution_input: ExecutionInput,
        backend: RecoverableExecutionBackend,
        candidate: RuntimeHandle,
        fence: ExecutionFence,
    ) -> Tuple[VerificationResult, ExecutionFence]:
        phase = PlanPhase.EXTERNAL_VERIFY
        if not self._phase_completed(operation.operation_id, phase):
            fence = self._before_phase(operation, execution_input, phase, fence)
        verification = backend.verify_active(execution_input.revision, candidate)
        verification = self._sanitize_verification(verification)
        if verification.status is not VerificationStatus.PASSED:
            raise _PhaseFailure(phase, verification)
        self._complete_phase(
            operation, phase, self._verification_evidence(verification), fence
        )
        return verification, fence

    def _phase_drain_previous(
        self,
        operation: OperationRef,
        execution_input: ExecutionInput,
        backend: RecoverableExecutionBackend,
        candidate: RuntimeHandle,
        previous: Optional[ActiveRevision],
        fence: ExecutionFence,
    ) -> ExecutionFence:
        phase = PlanPhase.DRAIN_PREVIOUS
        if self._phase_completed(operation.operation_id, phase):
            return fence
        fence = self._before_phase(operation, execution_input, phase, fence)
        evidence_digests: Tuple[str, ...] = ()
        drain = getattr(backend, "drain_previous", None)
        if previous is not None and callable(drain):
            result = drain(self._previous_handle(backend, previous), candidate)
            if not isinstance(result, StopResult) or not result.stopped:
                raise BackendContractError(
                    "Runtime backend did not verify predecessor drain."
                )
            evidence_digests = (result.observed_state_digest,)
        self._complete_phase(operation, phase, evidence_digests, fence)
        return fence

    def _before_phase(
        self,
        operation: OperationRef,
        execution_input: ExecutionInput,
        phase: PlanPhase,
        fence: ExecutionFence,
    ) -> ExecutionFence:
        self._checkpoint_control(operation.operation_id, execution_input)
        fence = self._heartbeat(fence)
        self._event(
            operation,
            f"phase.{phase.value}.started",
            OperationState.EXECUTING,
            fence,
        )
        return fence

    def _complete_phase(
        self,
        operation: OperationRef,
        phase: PlanPhase,
        evidence_digests: Tuple[str, ...],
        fence: ExecutionFence,
    ) -> None:
        self._event(
            operation,
            f"phase.{phase.value}.completed",
            OperationState.EXECUTING,
            fence,
            evidence_digests=evidence_digests,
        )

    def _finish_interrupted(
        self,
        operation: OperationRef,
        execution_input: ExecutionInput,
        backend: RecoverableExecutionBackend,
        candidate: RuntimeHandle,
        previous: Optional[ActiveRevision],
        fence: ExecutionFence,
        started_at: str,
        *,
        cancelled: bool,
        verification: Optional[VerificationResult],
        failure_diagnostic: Optional[dict[str, object]] = None,
    ) -> TerminalReceipt:
        fence = self._heartbeat(fence)
        state = (
            OperationState.CANCELLING
            if cancelled
            else OperationState.COMPENSATING
        )
        if failure_diagnostic is not None:
            self._event(
                operation,
                "operation.failure_observed",
                state,
                fence,
                message=json.dumps(
                    failure_diagnostic,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        self._event(
            operation,
            "operation.cancelling" if cancelled else "operation.compensating",
            state,
            fence,
        )
        self._ensure_revision_state(
            operation.operation_id, RevisionState.CREATED, fence
        )
        switch_started = self._event_exists(
            operation.operation_id,
            f"phase.{PlanPhase.SWITCH_TRAFFIC.value}.started",
        )
        candidate_started = self._event_exists(
            operation.operation_id,
            f"phase.{PlanPhase.START_CANDIDATE.value}.started",
        )
        compensation: CompensationResult
        try:
            evidence_digests = []
            if previous is not None and candidate_started:
                result = backend.restore(
                    self._previous_handle(backend, previous), candidate
                )
                if result.active_revision_digest != previous.revision_digest:
                    raise BackendContractError(
                        "Compensation did not restore the exact predecessor."
                    )
                evidence_digests.extend(result.evidence_digests)
            elif switch_started:
                if previous is None:
                    result = backend.deactivate(candidate)
                    if result.active_revision_digest is not None:
                        raise BackendContractError(
                            "Compensation did not verify an empty active state."
                        )
                evidence_digests.extend(result.evidence_digests)

            cleanup_required = candidate_started
            if cleanup_required:
                removal = backend.remove(candidate)
                if not removal.removed:
                    raise BackendContractError(
                        "Candidate cleanup did not verify removal."
                    )
                evidence_digests.append(removal.observed_state_digest)

            if not switch_started and not cleanup_required:
                compensation = CompensationResult(
                    attempted=not cancelled,
                    status=(
                        CompensationStatus.NOT_NEEDED
                        if cancelled
                        else CompensationStatus.SUCCEEDED
                    ),
                    restored_revision_id=(
                        None if previous is None else previous.revision_id
                    ),
                    restored_revision_digest=(
                        None if previous is None else previous.revision_digest
                    ),
                )
            else:
                compensation = CompensationResult(
                    attempted=True,
                    status=CompensationStatus.SUCCEEDED,
                    restored_revision_id=(
                        None if previous is None else previous.revision_id
                    ),
                    restored_revision_digest=(
                        None if previous is None else previous.revision_digest
                    ),
                    evidence_digests=tuple(sorted(set(evidence_digests))),
                )
        except Exception:
            compensation = CompensationResult(
                attempted=True,
                status=CompensationStatus.FAILED,
            )

        self._ensure_revision_state(
            operation.operation_id, RevisionState.FAILED, fence
        )
        if compensation.status is CompensationStatus.FAILED:
            outcome = ReceiptOutcome.FAILED_UNCOMPENSATED
            active_revision_id = None
            active_revision_digest = None
        elif cancelled:
            outcome = ReceiptOutcome.CANCELLED
            active_revision_id = None if previous is None else previous.revision_id
            active_revision_digest = (
                None if previous is None else previous.revision_digest
            )
        else:
            outcome = ReceiptOutcome.FAILED_COMPENSATED
            active_revision_id = None if previous is None else previous.revision_id
            active_revision_digest = (
                None if previous is None else previous.revision_digest
            )
        completed_at = self._utc_now()
        receipt = TerminalReceipt(
            receipt_id="receipt_" + uuid.uuid4().hex,
            operation_id=operation.operation_id,
            operation=execution_input.plan.operation,
            plan_id=execution_input.plan.plan_id,
            plan_digest=execution_input.plan.plan_digest(),
            decision_id=execution_input.approved_plan.decision_id,
            host_id=execution_input.plan.host_id,
            app=execution_input.plan.app,
            environment=execution_input.plan.environment,
            previous_revision_id=(
                None if previous is None else previous.revision_id
            ),
            desired_revision_id=execution_input.revision.revision_id,
            desired_revision_digest=execution_input.revision.content_digest(),
            active_revision_id=active_revision_id,
            active_revision_digest=active_revision_digest,
            artifact_digests=execution_input.revision.artifact_digests,
            verification=verification or self._not_run_verification(completed_at),
            compensation=compensation,
            effect=ReceiptEffect.NONE,
            outcome=outcome,
            started_at=started_at,
            completed_at=completed_at,
        )
        self.journal.commit_receipt(
            receipt,
            lease_owner=fence.owner_id,
            fencing_token=fence.fencing_token,
        )
        return receipt

    def _success_receipt(
        self,
        operation: OperationRef,
        execution_input: ExecutionInput,
        previous: Optional[ActiveRevision],
        verification: VerificationResult,
        started_at: str,
    ) -> TerminalReceipt:
        return TerminalReceipt(
            receipt_id="receipt_" + uuid.uuid4().hex,
            operation_id=operation.operation_id,
            operation=execution_input.plan.operation,
            plan_id=execution_input.plan.plan_id,
            plan_digest=execution_input.plan.plan_digest(),
            decision_id=execution_input.approved_plan.decision_id,
            host_id=execution_input.plan.host_id,
            app=execution_input.plan.app,
            environment=execution_input.plan.environment,
            previous_revision_id=(
                None if previous is None else previous.revision_id
            ),
            desired_revision_id=execution_input.revision.revision_id,
            desired_revision_digest=execution_input.revision.content_digest(),
            active_revision_id=execution_input.revision.revision_id,
            active_revision_digest=execution_input.revision.content_digest(),
            artifact_digests=execution_input.revision.artifact_digests,
            verification=self._sanitize_verification(verification),
            compensation=CompensationResult(
                attempted=False,
                status=CompensationStatus.NOT_NEEDED,
            ),
            effect=ReceiptEffect.RUNTIME_ACTIVATED,
            outcome=ReceiptOutcome.SUCCEEDED,
            started_at=started_at,
            completed_at=self._utc_now(),
        )

    def _checkpoint_control(
        self, operation_id: str, execution_input: ExecutionInput
    ) -> None:
        control = self.journal.control(operation_id)
        if control.cancellation_requested:
            raise _CancellationRequested()
        deadline = parse_utc(execution_input.deadline)
        now = datetime.fromtimestamp(self._clock_sample(), timezone.utc)
        if now >= deadline:
            raise _ExecutionDeadlineExpired()

    def _heartbeat(self, fence: ExecutionFence) -> ExecutionFence:
        return self.journal.heartbeat_fence(fence, self.lease_ttl_seconds)

    def _ensure_revision_state(
        self,
        operation_id: str,
        state: RevisionState,
        fence: ExecutionFence,
    ) -> None:
        history = self.journal.revision_history(operation_id)
        if any(entry.state is state for entry in history):
            return
        self.journal.append_revision_state(operation_id, state, fence=fence)

    def _phase_completed(self, operation_id: str, phase: PlanPhase) -> bool:
        return self._event_exists(operation_id, f"phase.{phase.value}.completed")

    def _event_exists(self, operation_id: str, event_type: str) -> bool:
        return any(
            event.event_type == event_type
            for event in self.journal.events(operation_id)
        )

    def _event(
        self,
        operation: OperationRef,
        event_type: str,
        state: OperationState,
        fence: ExecutionFence,
        *,
        evidence_digests: Tuple[str, ...] = (),
        message: Optional[str] = None,
    ) -> OperationEvent:
        events = self.journal.events(operation.operation_id)
        existing = tuple(event for event in events if event.event_type == event_type)
        if existing:
            if len(existing) != 1 or existing[0].state is not state:
                raise BackendContractError(
                    "Durable executor event identity is inconsistent."
                )
            return existing[0]
        previous_digest = None if not events else canonical_digest(events[-1])
        execution_input = self.journal.load_execution_input(operation.operation_id)
        event = OperationEvent(
            event_id="event_" + uuid.uuid4().hex,
            operation_id=operation.operation_id,
            sequence=len(events) + 1,
            event_type=event_type,
            occurred_at=self._utc_now(),
            state=state,
            host_id=execution_input.plan.host_id,
            app=execution_input.plan.app,
            environment=execution_input.plan.environment,
            revision_id=execution_input.revision.revision_id,
            message=message,
            evidence_digests=tuple(sorted(set(evidence_digests))),
            previous_event_digest=previous_digest,
        )
        self.journal.append(
            event,
            lease_owner=fence.owner_id,
            fencing_token=fence.fencing_token,
        )
        return event

    def _failure_diagnostic(
        self, operation_id: str, exc: Exception
    ) -> dict[str, object]:
        phase = (
            exc.phase.value
            if isinstance(exc, _PhaseFailure)
            else self._current_phase(operation_id)
        )
        diagnostic: dict[str, object] = {
            "schema_version": 1,
            "kind": "ophelia.kernel.failure-diagnostic",
            "phase": phase,
            "exception_type": type(exc).__name__[:128],
        }
        if isinstance(exc, ProcessFailure):
            result = exc.result
            diagnostic.update(
                {
                    "category": "external_process",
                    "code": self._process_failure_code(
                        result.stdout, result.stderr, result.exit_reason
                    ),
                    "action": self._process_action(result.argv),
                    "exit_reason": result.exit_reason,
                    "exit_code": result.exit_code,
                    "stdout_digest": canonical_digest(
                        {"stream": "stdout", "value": result.stdout}
                    ),
                    "stderr_digest": canonical_digest(
                        {"stream": "stderr", "value": result.stderr}
                    ),
                    "output_truncated": bool(
                        result.stdout_truncated or result.stderr_truncated
                    ),
                }
            )
            return diagnostic
        if isinstance(exc, _PhaseFailure):
            category = "verification"
            code = "verification_failed"
        elif isinstance(exc, _ExecutionDeadlineExpired):
            category = "execution_control"
            code = "deadline_expired"
        elif isinstance(exc, BackendContractError):
            category = "backend_contract"
            code = "backend_contract_violation"
        elif isinstance(exc, OSError):
            category = "operating_system"
            code = "os_error"
            diagnostic["errno"] = exc.errno
        else:
            category = "backend"
            code = "backend_error"
        diagnostic.update(
            {
                "category": category,
                "code": code,
                "detail_digest": canonical_digest(
                    {"exception_type": type(exc).__name__, "detail": str(exc)}
                ),
            }
        )
        return diagnostic

    def _current_phase(self, operation_id: str) -> str:
        for event in reversed(self.journal.events(operation_id)):
            prefix = "phase."
            suffix = ".started"
            if event.event_type.startswith(prefix) and event.event_type.endswith(
                suffix
            ):
                return event.event_type[len(prefix) : -len(suffix)]
        return "unknown"

    @staticmethod
    def _process_action(argv: Tuple[str, ...]) -> str:
        if not argv:
            return "external"
        executable = PurePosixPath(argv[0]).name
        if executable != "docker":
            return executable[:128] if executable else "external"
        if len(argv) > 1 and argv[1] == "compose":
            for action in (
                "up",
                "pull",
                "run",
                "exec",
                "down",
                "stop",
                "ps",
                "config",
            ):
                if action in argv[2:]:
                    return "docker.compose." + action
            return "docker.compose"
        if len(argv) > 1 and argv[1] in {
            "pull",
            "run",
            "exec",
            "inspect",
            "create",
            "start",
            "stop",
            "rm",
        }:
            return "docker." + argv[1]
        return "docker"

    @staticmethod
    def _process_failure_code(stdout: str, stderr: str, exit_reason: str) -> str:
        detail = (stdout + "\n" + stderr).lower()
        patterns = (
            ("failed to validate image signature", "image_signature_validation"),
            ("permission denied", "permission_denied"),
            ("no such file or directory", "missing_path"),
            ("no space left on device", "disk_full"),
            ("port is already allocated", "port_conflict"),
            ("address already in use", "port_conflict"),
            ("pull access denied", "registry_access_denied"),
            ("unauthorized", "registry_access_denied"),
            ("unhealthy", "workload_unhealthy"),
            ("invalid mount config", "invalid_mount"),
        )
        for needle, code in patterns:
            if needle in detail:
                return code
        if exit_reason == "timeout":
            return "process_timeout"
        if exit_reason == "cancelled":
            return "process_cancelled"
        return "process_failed"

    @staticmethod
    def _validate_handle(
        execution_input: ExecutionInput, handle: RuntimeHandle
    ) -> None:
        if not (
            handle.revision_id == execution_input.revision.revision_id
            and handle.revision_digest == execution_input.revision.content_digest()
        ):
            raise BackendContractError(
                "Runtime handle does not bind the approved revision."
            )

    @staticmethod
    def _validate_supported_plan(execution_input: ExecutionInput) -> None:
        if execution_input.plan.blocker_codes:
            raise BackendContractError(
                "Blocked plans cannot enter the canonical executor."
            )
        phases = tuple(step.phase for step in execution_input.plan.steps)
        if phases != _SUPPORTED_PHASES:
            raise BackendContractError(
                "The current executor only accepts the complete activation pipeline."
            )
        if execution_input.plan.operation not in {"deploy.apply", "rollback.apply"}:
            raise BackendContractError(
                "The current executor only accepts deploy.apply and rollback.apply operations."
            )
        workloads = execution_input.revision.workloads
        manifest_v2 = execution_input.revision.renderer_version == "manifest-v2"
        if (
            not manifest_v2
            and (
                len(workloads) != 1
                or workloads[0].workload_kind not in {WorkloadKind.STATIC, WorkloadKind.WEB}
                or workloads[0].artifact_digest != execution_input.artifact_ref.artifact_digest
                or execution_input.revision.artifact_digests != (execution_input.artifact_ref.artifact_digest,)
            )
        ):
            raise BackendContractError(
                "The current executor requires one exact static or web workload artifact."
            )
        if manifest_v2 and (
            not workloads
            or execution_input.artifact_ref.artifact_digest
            not in execution_input.revision.artifact_digests
            or any(
                workload.artifact_digest not in execution_input.revision.artifact_digests
                for workload in workloads
            )
        ):
            raise BackendContractError(
                "Manifest v2 execution requires workloads bound to exact revision artifacts."
            )
        required_steps = dict(_SUPPORTED_STEPS)
        if manifest_v2 or workloads[0].workload_kind is WorkloadKind.WEB:
            required_steps[PlanPhase.DRAIN_PREVIOUS] = (
                True,
                CompensationAction.PRESERVE_PREVIOUS,
            )
        if any(
            (step.mutates_runtime, step.compensation)
            != required_steps[step.phase]
            for step in execution_input.plan.steps
        ):
            raise BackendContractError(
                "Activation steps do not declare the required mutation and compensation semantics."
            )
        revision_digest = execution_input.revision.content_digest()
        artifact_digest = execution_input.artifact_ref.artifact_digest
        if manifest_v2:
            expected_effect_digests = tuple(
                canonical_digest(
                    {
                        "phase": step.phase.value,
                        "revision_digest": revision_digest,
                        "workloads": [item.to_dict() for item in workloads],
                    }
                )
                for step in execution_input.plan.steps
            )
        else:
            expected_effect_digests = tuple(
                JournaledExecutor._expected_static_effect_digest(
                    step.phase, revision_digest, artifact_digest
                )
                for step in execution_input.plan.steps
            )
        if tuple(
            step.desired_effect_digest for step in execution_input.plan.steps
        ) != expected_effect_digests:
            raise BackendContractError(
                "Activation steps do not declare the exact desired effects."
            )
        artifact_path = PurePosixPath(execution_input.artifact_ref.relative_root)
        if (
            len(artifact_path.parts) < 3
            or artifact_path.parts[0] != "staging"
            or artifact_path.parts[-1] != "candidate"
        ):
            raise BackendContractError(
                "Execution artifacts must reference immutable operation staging."
            )

    @staticmethod
    def _expected_static_effect_digest(
        phase: PlanPhase, revision_digest: str, artifact_digest: str
    ) -> str:
        return canonical_digest(
            {
                "phase": phase.value,
                "revision_digest": revision_digest,
                "artifact_digest": artifact_digest,
            }
        )

    @staticmethod
    def _previous_handle(
        backend: RecoverableExecutionBackend, previous: ActiveRevision
    ) -> RuntimeHandle:
        return RuntimeHandle(
            backend=backend.backend_name,
            handle_id=previous.revision_id,
            revision_id=previous.revision_id,
            revision_digest=previous.revision_digest,
        )

    @staticmethod
    def _verification_evidence(
        verification: VerificationResult,
    ) -> Tuple[str, ...]:
        values = []
        if verification.observed_state_digest is not None:
            values.append(verification.observed_state_digest)
        values.extend(
            check.observed_digest
            for check in verification.checks
            if check.observed_digest is not None
        )
        return tuple(sorted(set(values)))

    @staticmethod
    def _sanitize_verification(
        verification: VerificationResult,
    ) -> VerificationResult:
        """Drop backend prose before durable persistence.

        The P0 executor persists coded check names, statuses, and digests only.
        Backend summaries can contain command output or application secrets and
        are therefore never trusted as receipt-safe text.
        """

        return replace(
            verification,
            checks=tuple(
                replace(check, summary=None) for check in verification.checks
            ),
        )

    def _preflight_verification(
        self, result: PreflightResult
    ) -> VerificationResult:
        return VerificationResult(
            status=VerificationStatus.FAILED,
            observed_revision_digest=None,
            observed_state_digest=result.observed_state_digest,
            observed_at=self._utc_now(),
            checks=(
                VerificationCheck(
                    name="preflight",
                    status=CheckStatus.FAILED,
                    observed_digest=result.observed_state_digest,
                ),
            ),
        )

    @staticmethod
    def _not_run_verification(observed_at: str) -> VerificationResult:
        return VerificationResult(
            status=VerificationStatus.NOT_RUN,
            observed_revision_digest=None,
            observed_state_digest=None,
            observed_at=observed_at,
            checks=(),
        )

    def _utc_now(self) -> str:
        return datetime.fromtimestamp(
            self._clock_sample(), timezone.utc
        ).isoformat().replace("+00:00", "Z")

    def _clock_sample(self) -> float:
        sample = self._clock()
        if (
            isinstance(sample, bool)
            or not isinstance(sample, (int, float))
            or not math.isfinite(float(sample))
        ):
            raise JournaledExecutorError("Executor clock returned an invalid sample.")
        return float(sample)
