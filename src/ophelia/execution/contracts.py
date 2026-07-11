"""Narrow seams for the canonical P0 execution pipeline.

This module defines behavior boundaries only. It intentionally contains no
Docker, Caddy, SSH, daemon, or operation-database implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Tuple

from ..domain._contracts import (
    Contract,
    ContractValidationError,
    require_digest,
    require_digests,
    require_entity_id,
    require_text,
)
from ..domain.events import OperationEvent
from ..domain.operations import Actor, OperationRef, OperationRequest
from ..domain.plans import ApprovedPlanRef, OperationPlan, PlanPhase
from ..domain.receipts import CompensationResult, TerminalReceipt, VerificationResult
from ..domain.revisions import ObservedRevision, Revision


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
    active_revision_digest: str
    observed_state_digest: str
    evidence_digests: Tuple[str, ...]

    def __post_init__(self) -> None:
        if self.previous_revision_digest is not None:
            require_digest(self.previous_revision_digest, "previous_revision_digest")
        require_digest(self.active_revision_digest, "active_revision_digest")
        require_digest(self.observed_state_digest, "observed_state_digest")
        require_digests(self.evidence_digests, "evidence_digests")
        if not self.committed_atomically:
            raise ContractValidationError("Traffic activation results must describe an atomic commit.")


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
    ) -> OperationRef:
        ...

    def append(self, event: OperationEvent) -> None:
        ...

    def commit_receipt(self, receipt: TerminalReceipt) -> None:
        ...


class CanonicalExecutor(Protocol):
    """The sole mutating entrypoint for CLI, API, SSH, daemon, and Lumen adapters."""

    def submit(
        self,
        actor: Actor,
        request: OperationRequest,
        approved_plan: ApprovedPlanRef,
    ) -> OperationRef:
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
