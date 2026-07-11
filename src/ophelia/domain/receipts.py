"""Truthful terminal receipts derived from verified observed state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from ._contracts import (
    Contract,
    ContractValidationError,
    optional_entity_id,
    parse_utc,
    require_digest,
    require_digests,
    require_entity_id,
    require_enum,
    require_operation,
    require_slug,
    require_text,
    require_utc,
)


class CheckStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class VerificationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"


class CompensationStatus(str, Enum):
    NOT_NEEDED = "not_needed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ReceiptOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED_COMPENSATED = "failed_compensated"
    FAILED_UNCOMPENSATED = "failed_uncompensated"
    CANCELLED = "cancelled"


class ReceiptEffect(str, Enum):
    PREVIEW = "preview"
    FILES_ONLY = "files_only"
    RUNTIME_ACTIVATED = "runtime_activated"
    NONE = "none"


@dataclass(frozen=True)
class VerificationCheck(Contract):
    kind = "ophelia.kernel.verification_check"

    name: str
    status: CheckStatus
    observed_digest: Optional[str] = None
    summary: Optional[str] = None

    def __post_init__(self) -> None:
        require_text(self.name, "name", 128)
        require_enum(self.status, CheckStatus, "status")
        if self.observed_digest is not None:
            require_digest(self.observed_digest, "observed_digest")
        if self.summary is not None:
            require_text(self.summary, "summary", 1024)


@dataclass(frozen=True)
class VerificationResult(Contract):
    kind = "ophelia.kernel.verification_result"

    status: VerificationStatus
    observed_revision_digest: Optional[str]
    observed_state_digest: Optional[str]
    observed_at: str
    checks: Tuple[VerificationCheck, ...]

    def __post_init__(self) -> None:
        require_enum(self.status, VerificationStatus, "status")
        if self.observed_revision_digest is not None:
            require_digest(self.observed_revision_digest, "observed_revision_digest")
        if self.observed_state_digest is not None:
            require_digest(self.observed_state_digest, "observed_state_digest")
        require_utc(self.observed_at, "observed_at")
        if not isinstance(self.checks, tuple):
            raise ContractValidationError("checks must be an immutable tuple.")
        if self.status is VerificationStatus.PASSED:
            if self.observed_revision_digest is None or self.observed_state_digest is None:
                raise ContractValidationError("Passed verification requires observed revision and state digests.")
            if any(check.status is not CheckStatus.PASSED for check in self.checks):
                raise ContractValidationError("Passed verification cannot contain failed or skipped checks.")


@dataclass(frozen=True)
class CompensationResult(Contract):
    kind = "ophelia.kernel.compensation_result"

    attempted: bool
    status: CompensationStatus
    restored_revision_id: Optional[str] = None
    restored_revision_digest: Optional[str] = None
    evidence_digests: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_enum(self.status, CompensationStatus, "status")
        optional_entity_id(self.restored_revision_id, "rev", "restored_revision_id")
        if self.restored_revision_digest is not None:
            require_digest(self.restored_revision_digest, "restored_revision_digest")
        require_digests(self.evidence_digests, "evidence_digests")
        if self.status is CompensationStatus.NOT_NEEDED and self.attempted:
            raise ContractValidationError("Compensation cannot be attempted with not_needed status.")
        if self.status is not CompensationStatus.NOT_NEEDED and not self.attempted:
            raise ContractValidationError("A compensation outcome requires attempted=true.")
        if self.status is CompensationStatus.SUCCEEDED and (
            self.restored_revision_id is None or self.restored_revision_digest is None
        ):
            raise ContractValidationError("Successful compensation must identify the restored revision.")


@dataclass(frozen=True)
class TerminalReceipt(Contract):
    """Immutable terminal evidence. Success is impossible without verified state."""

    kind = "ophelia.kernel.terminal_receipt"

    receipt_id: str
    operation_id: str
    operation: str
    plan_id: str
    plan_digest: str
    decision_id: str
    host_id: str
    app: str
    environment: str
    previous_revision_id: Optional[str]
    desired_revision_id: str
    desired_revision_digest: str
    active_revision_id: Optional[str]
    active_revision_digest: Optional[str]
    artifact_digests: Tuple[str, ...]
    verification: VerificationResult
    compensation: CompensationResult
    effect: ReceiptEffect
    outcome: ReceiptOutcome
    started_at: str
    completed_at: str
    inputs_redacted: bool = True

    def __post_init__(self) -> None:
        require_entity_id(self.receipt_id, "receipt", "receipt_id")
        require_entity_id(self.operation_id, "operation", "operation_id")
        require_operation(self.operation)
        require_entity_id(self.plan_id, "plan", "plan_id")
        require_digest(self.plan_digest, "plan_digest")
        require_entity_id(self.decision_id, "decision", "decision_id")
        require_entity_id(self.host_id, "host", "host_id")
        require_slug(self.app, "app")
        require_slug(self.environment, "environment")
        optional_entity_id(self.previous_revision_id, "rev", "previous_revision_id")
        require_entity_id(self.desired_revision_id, "rev", "desired_revision_id")
        require_digest(self.desired_revision_digest, "desired_revision_digest")
        optional_entity_id(self.active_revision_id, "rev", "active_revision_id")
        if self.active_revision_digest is not None:
            require_digest(self.active_revision_digest, "active_revision_digest")
        require_digests(self.artifact_digests, "artifact_digests")
        require_enum(self.effect, ReceiptEffect, "effect")
        require_enum(self.outcome, ReceiptOutcome, "outcome")
        require_utc(self.started_at, "started_at")
        require_utc(self.completed_at, "completed_at")
        if parse_utc(self.completed_at) < parse_utc(self.started_at):
            raise ContractValidationError("completed_at must not precede started_at.")
        if not self.inputs_redacted:
            raise ContractValidationError("Kernel receipts must declare inputs_redacted=true.")

        if self.outcome is ReceiptOutcome.SUCCEEDED:
            if self.effect is not ReceiptEffect.RUNTIME_ACTIVATED:
                raise ContractValidationError("A successful kernel receipt must report runtime_activated.")
            if self.verification.status is not VerificationStatus.PASSED:
                raise ContractValidationError("A successful operation requires passed verification.")
            observed = self.verification.observed_revision_digest
            if not (
                self.active_revision_id == self.desired_revision_id
                and self.active_revision_digest == self.desired_revision_digest
                and observed == self.desired_revision_digest
            ):
                raise ContractValidationError(
                    "Success requires desired, active, and observed revision digests to match."
                )
            if self.compensation.status is CompensationStatus.FAILED:
                raise ContractValidationError("A successful receipt cannot contain failed compensation.")

        if self.outcome is ReceiptOutcome.FAILED_COMPENSATED:
            if self.compensation.status is not CompensationStatus.SUCCEEDED:
                raise ContractValidationError("failed_compensated requires successful compensation.")
            if (
                self.active_revision_id != self.compensation.restored_revision_id
                or self.active_revision_digest != self.compensation.restored_revision_digest
            ):
                raise ContractValidationError("The active revision must match the compensated revision.")

        if self.outcome is ReceiptOutcome.FAILED_UNCOMPENSATED:
            if self.compensation.status is CompensationStatus.SUCCEEDED:
                raise ContractValidationError("failed_uncompensated cannot claim successful compensation.")
