"""Narrow seams for the canonical P0 execution pipeline.

This module defines behavior boundaries only. It intentionally contains no
Docker, Caddy, SSH, daemon, or operation-database implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import PurePosixPath
from typing import Optional, Protocol, Tuple

from ..domain._contracts import (
    Contract,
    ContractValidationError,
    canonical_digest,
    require_digest,
    require_digests,
    require_entity_id,
    require_enum,
    require_slug,
    require_text,
    require_utc,
)
from ..domain.events import OperationEvent
from ..domain.operations import Actor, OperationRef, OperationRequest
from ..domain.plans import ApprovedPlanRef, OperationPlan, PlanPhase
from ..domain.receipts import CompensationResult, TerminalReceipt, VerificationResult
from ..domain.revisions import ObservedRevision, Revision, RevisionState


@dataclass(frozen=True)
class RevisionArtifactRef(Contract):
    """Secret-free reference to immutable revision material below a staging root."""

    kind = "ophelia.kernel.revision_artifact_ref"

    revision_id: str
    revision_digest: str
    relative_root: str
    artifact_digest: str

    def __post_init__(self) -> None:
        require_entity_id(self.revision_id, "rev", "revision_id")
        require_digest(self.revision_digest, "revision_digest")
        require_digest(self.artifact_digest, "artifact_digest")
        require_text(self.relative_root, "relative_root", 1024)
        if "\\" in self.relative_root:
            raise ContractValidationError("relative_root must use portable forward slashes.")
        path = PurePosixPath(self.relative_root)
        if (
            path.is_absolute()
            or self.relative_root in {"", "."}
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ContractValidationError(
                "relative_root must be a normalized relative artifact root."
            )


@dataclass(frozen=True)
class ExecutionInput(Contract):
    """Exact, deterministic and secret-free input for a recoverable execution."""

    kind = "ophelia.kernel.execution_input"

    plan: OperationPlan
    approved_plan: ApprovedPlanRef
    revision: Revision
    artifact_ref: RevisionArtifactRef
    deadline: str
    input_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.plan, OperationPlan):
            raise ContractValidationError("plan must be an OperationPlan.")
        if not isinstance(self.approved_plan, ApprovedPlanRef):
            raise ContractValidationError("approved_plan must be an ApprovedPlanRef.")
        if not isinstance(self.revision, Revision):
            raise ContractValidationError("revision must be a Revision.")
        if not isinstance(self.artifact_ref, RevisionArtifactRef):
            raise ContractValidationError("artifact_ref must be a RevisionArtifactRef.")
        require_utc(self.deadline, "deadline")
        require_digest(self.input_digest, "input_digest")
        if not self.approved_plan.matches(self.plan):
            raise ContractValidationError("approved_plan must bind the exact plan.")
        if not (
            self.revision.revision_id == self.plan.revision_id
            and self.revision.content_digest() == self.plan.revision_digest
            and self.revision.app == self.plan.app
            and self.revision.environment == self.plan.environment
            and self.revision.manifest_digest == self.plan.manifest_digest
            and self.revision.artifact_digests == self.plan.artifact_digests
            and self.artifact_ref.revision_id == self.revision.revision_id
            and self.artifact_ref.revision_digest == self.revision.content_digest()
            and self.artifact_ref.artifact_digest in self.revision.artifact_digests
        ):
            raise ContractValidationError(
                "Execution input plan, approval, revision, and artifact claims must reconcile."
            )
        if self.input_digest != canonical_digest(self._binding_payload()):
            raise ContractValidationError("input_digest does not match execution input claims.")

    def _binding_payload(self) -> dict:
        payload = self.to_dict()
        payload.pop("input_digest")
        return payload

    @staticmethod
    def _digest_for(
        plan: OperationPlan,
        approved_plan: ApprovedPlanRef,
        revision: Revision,
        artifact_ref: RevisionArtifactRef,
        deadline: str,
    ) -> str:
        return canonical_digest(
            {
                "schema_version": plan.schema_version,
                "kind": ExecutionInput.kind,
                "plan": plan,
                "approved_plan": approved_plan,
                "revision": revision,
                "artifact_ref": artifact_ref,
                "deadline": deadline,
            }
        )

    @classmethod
    def bind(
        cls,
        *,
        request: OperationRequest,
        plan: OperationPlan,
        approved_plan: ApprovedPlanRef,
        revision: Revision,
        artifact_ref: RevisionArtifactRef,
        deadline: str,
    ) -> "ExecutionInput":
        if not isinstance(request, OperationRequest):
            raise ContractValidationError("request must be an OperationRequest.")
        if not (
            request.intent_digest() == plan.request_digest
            and request.operation == plan.operation
            and request.host_id == plan.host_id
            and request.app == plan.app
            and request.environment == plan.environment
            and request.revision_id == plan.revision_id
            and request.revision_digest == plan.revision_digest
        ):
            raise ContractValidationError("request must bind the exact execution plan.")
        return cls(
            plan=plan,
            approved_plan=approved_plan,
            revision=revision,
            artifact_ref=artifact_ref,
            deadline=deadline,
            input_digest=cls._digest_for(
                plan, approved_plan, revision, artifact_ref, deadline
            ),
        )


@dataclass(frozen=True)
class ExecutionFence(Contract):
    """Lease identity carried on every authoritative execution mutation."""

    kind = "ophelia.kernel.execution_fence"

    operation_id: str
    owner_id: str
    fencing_token: int
    expires_at: float

    def __post_init__(self) -> None:
        require_entity_id(self.operation_id, "operation", "operation_id")
        require_text(self.owner_id, "owner_id", 255)
        if (
            not isinstance(self.fencing_token, int)
            or isinstance(self.fencing_token, bool)
            or self.fencing_token < 1
        ):
            raise ContractValidationError("fencing_token must be a positive integer.")
        if (
            isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, (int, float))
            or not math.isfinite(float(self.expires_at))
        ):
            raise ContractValidationError("expires_at must be finite.")


@dataclass(frozen=True)
class ExecutionControl(Contract):
    kind = "ophelia.kernel.execution_control"

    operation_id: str
    deadline: Optional[str]
    cancellation_requested: bool
    cancellation_actor: Optional[Actor] = None
    cancellation_requested_at: Optional[str] = None

    def __post_init__(self) -> None:
        require_entity_id(self.operation_id, "operation", "operation_id")
        if self.deadline is not None:
            require_utc(self.deadline, "deadline")
        if not isinstance(self.cancellation_requested, bool):
            raise ContractValidationError("cancellation_requested must be boolean.")
        if self.cancellation_requested:
            if not isinstance(self.cancellation_actor, Actor):
                raise ContractValidationError(
                    "A cancellation request requires a server-derived Actor."
                )
            if self.cancellation_requested_at is None:
                raise ContractValidationError(
                    "A cancellation request requires cancellation_requested_at."
                )
            require_utc(self.cancellation_requested_at, "cancellation_requested_at")
        elif self.cancellation_actor is not None or self.cancellation_requested_at is not None:
            raise ContractValidationError(
                "Cancellation evidence cannot exist without cancellation_requested."
            )


@dataclass(frozen=True)
class RevisionLifecycleEntry(Contract):
    kind = "ophelia.kernel.revision_lifecycle_entry"

    operation_id: str
    sequence: int
    revision_id: str
    revision_digest: str
    state: RevisionState
    occurred_at: str

    def __post_init__(self) -> None:
        require_entity_id(self.operation_id, "operation", "operation_id")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 1:
            raise ContractValidationError("sequence must be a positive integer.")
        require_entity_id(self.revision_id, "rev", "revision_id")
        require_digest(self.revision_digest, "revision_digest")
        require_enum(self.state, RevisionState, "state")
        require_utc(self.occurred_at, "occurred_at")


@dataclass(frozen=True)
class ActiveRevision(Contract):
    kind = "ophelia.kernel.active_revision"

    host_id: str
    app: str
    environment: str
    operation_id: str
    revision_id: str
    revision_digest: str
    generation: int
    updated_at: str

    def __post_init__(self) -> None:
        require_entity_id(self.host_id, "host", "host_id")
        require_slug(self.app, "app")
        require_slug(self.environment, "environment")
        require_entity_id(self.operation_id, "operation", "operation_id")
        require_entity_id(self.revision_id, "rev", "revision_id")
        require_digest(self.revision_digest, "revision_digest")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 1:
            raise ContractValidationError("generation must be a positive integer.")
        require_utc(self.updated_at, "updated_at")


@dataclass(frozen=True)
class PolicyDecision(Contract):
    """Fail-closed policy evidence bound into a calculated plan."""

    kind = "ophelia.kernel.policy_decision"

    allowed: bool
    policy_digest: str
    blocker_codes: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_digest(self.policy_digest, "policy_digest")
        if not isinstance(self.blocker_codes, tuple):
            raise ContractValidationError("blocker_codes must be an immutable tuple.")
        for code in self.blocker_codes:
            require_text(code, "blocker_code", 128)
        if self.blocker_codes != tuple(sorted(set(self.blocker_codes))):
            raise ContractValidationError("blocker_codes must be sorted and contain no duplicates.")
        if self.allowed and self.blocker_codes:
            raise ContractValidationError("An allowed policy decision cannot contain blockers.")
        if not self.allowed and not self.blocker_codes:
            raise ContractValidationError("A blocked policy decision must identify a blocker.")


@dataclass(frozen=True)
class PreflightResult(Contract):
    kind = "ophelia.kernel.preflight_result"

    ok: bool
    observed_state_digest: str
    evidence_digests: Tuple[str, ...] = ()
    blocker_codes: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_digest(self.observed_state_digest, "observed_state_digest")
        require_digests(self.evidence_digests, "evidence_digests")
        if not isinstance(self.blocker_codes, tuple):
            raise ContractValidationError("blocker_codes must be an immutable tuple.")
        for code in self.blocker_codes:
            require_text(code, "blocker_code", 128)
        if self.ok and self.blocker_codes:
            raise ContractValidationError("Successful preflight cannot contain blockers.")
        if not self.ok and not self.blocker_codes:
            raise ContractValidationError("Failed preflight must identify a blocker.")


@dataclass(frozen=True)
class RuntimeHandle(Contract):
    kind = "ophelia.kernel.runtime_handle"

    backend: str
    handle_id: str
    revision_id: str
    revision_digest: str

    def __post_init__(self) -> None:
        require_text(self.backend, "backend", 128)
        require_text(self.handle_id, "handle_id", 255)
        require_entity_id(self.revision_id, "rev", "revision_id")
        require_digest(self.revision_digest, "revision_digest")


@dataclass(frozen=True)
class StopResult(Contract):
    kind = "ophelia.kernel.stop_result"

    stopped: bool
    observed_state_digest: str

    def __post_init__(self) -> None:
        require_digest(self.observed_state_digest, "observed_state_digest")


@dataclass(frozen=True)
class RemoveResult(Contract):
    kind = "ophelia.kernel.remove_result"

    removed: bool
    observed_state_digest: str

    def __post_init__(self) -> None:
        require_digest(self.observed_state_digest, "observed_state_digest")


@dataclass(frozen=True)
class LogBatch(Contract):
    """A bounded log seam that carries evidence digests, never raw output."""

    kind = "ophelia.kernel.log_batch"

    next_cursor: Optional[str]
    entry_digests: Tuple[str, ...]
    truncated: bool

    def __post_init__(self) -> None:
        if self.next_cursor is not None:
            require_text(self.next_cursor, "next_cursor", 255)
        require_digests(self.entry_digests, "entry_digests")
        if len(self.entry_digests) > 1000:
            raise ContractValidationError("A log batch may reference at most 1000 entries.")


@dataclass(frozen=True)
class TrafficActivationResult(Contract):
    kind = "ophelia.kernel.traffic_activation_result"

    committed_atomically: bool
    previous_revision_digest: Optional[str]
    active_revision_digest: Optional[str]
    observed_state_digest: str
    evidence_digests: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.committed_atomically, bool):
            raise ContractValidationError(
                "committed_atomically must be a boolean observation."
            )
        if self.previous_revision_digest is not None:
            require_digest(self.previous_revision_digest, "previous_revision_digest")
        if self.active_revision_digest is not None:
            require_digest(self.active_revision_digest, "active_revision_digest")
        require_digest(self.observed_state_digest, "observed_state_digest")
        require_digests(self.evidence_digests, "evidence_digests")


class ReadOnlyPlanner(Protocol):
    """Planner constrained to observations and operation-local staging evidence.

    Implementations must not mutate protected desired, active, provider, or
    runtime surfaces. Any permitted writes belong only to an operation-specific
    staging tree or append-only plan journal.
    """

    def plan(
        self,
        actor: Actor,
        request: OperationRequest,
        observed: ObservedRevision,
    ) -> OperationPlan:
        ...


class PolicyEnforcer(Protocol):
    """The one fail-closed policy decision seam used by every transport."""

    def evaluate(
        self,
        actor: Actor,
        request: OperationRequest,
        plan: OperationPlan,
    ) -> PolicyDecision:
        ...


class OperationJournal(Protocol):
    """Durable operation truth. Implementations append before acknowledging."""

    def accept(
        self,
        actor: Actor,
        request: OperationRequest,
        approved_plan: ApprovedPlanRef,
        *,
        execution_input: Optional[ExecutionInput] = None,
    ) -> OperationRef:
        ...

    def load_execution_input(self, operation_id: str) -> ExecutionInput:
        ...

    def control(self, operation_id: str) -> ExecutionControl:
        ...

    def request_cancellation(
        self, operation_id: str, actor: Actor
    ) -> ExecutionControl:
        ...

    def list_recoverable(self, limit: int = 100) -> Tuple[OperationRef, ...]:
        ...

    def append(self, event: OperationEvent) -> None:
        ...

    def commit_receipt(self, receipt: TerminalReceipt) -> None:
        ...

    def receipt(self, operation_id: str) -> Optional[TerminalReceipt]:
        ...


class CanonicalExecutor(Protocol):
    """The sole mutating entrypoint for CLI, API, SSH, daemon, and Lumen adapters."""

    def submit(
        self,
        actor: Actor,
        request: OperationRequest,
        approved_plan: ApprovedPlanRef,
        *,
        execution_input: ExecutionInput,
    ) -> OperationRef:
        ...

    def run(self, operation_id: str, *, owner_id: str) -> TerminalReceipt:
        ...

    def cancel(self, operation_id: str, actor: Actor) -> ExecutionControl:
        ...


class RuntimeBackend(Protocol):
    """Runtime lifecycle below cross-product authorization and policy."""

    def preflight(self, revision: Revision) -> PreflightResult:
        ...

    def start(self, revision: Revision) -> RuntimeHandle:
        ...

    def inspect(self, handle: RuntimeHandle) -> ObservedRevision:
        ...

    def stop(self, handle: RuntimeHandle, grace_seconds: int) -> StopResult:
        ...

    def remove(self, handle: RuntimeHandle) -> RemoveResult:
        ...

    def logs(self, handle: RuntimeHandle, cursor: Optional[str]) -> LogBatch:
        ...


class CandidateVerifier(Protocol):
    """Proves candidate state, rather than trusting a subprocess exit code."""

    def verify(
        self,
        revision: Revision,
        observed: ObservedRevision,
    ) -> VerificationResult:
        ...


class TrafficActivator(Protocol):
    """Atomically commits candidate traffic or restores the expected predecessor."""

    def activate(
        self,
        candidate: RuntimeHandle,
        expected_active_revision_digest: Optional[str],
    ) -> TrafficActivationResult:
        ...

    def restore(
        self,
        previous: RuntimeHandle,
        failed_candidate: RuntimeHandle,
    ) -> TrafficActivationResult:
        ...


class Compensator(Protocol):
    """Restores a verified predecessor after a failed execution phase."""

    def compensate(
        self,
        operation: OperationRef,
        failed_phase: PlanPhase,
        previous_revision: Optional[Revision],
    ) -> CompensationResult:
        ...
