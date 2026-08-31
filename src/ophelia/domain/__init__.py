"""Internal P0 kernel domain contracts.

The public v1 dict envelopes remain in :mod:`ophelia.operation_schema`. These
immutable values are the migration target for adapters and the canonical
executor.
"""

from ._contracts import (
    KERNEL_CONTRACT_SCHEMA_VERSION,
    ContractValidationError,
    canonical_digest,
    canonical_json,
)
from .events import OperationEvent
from .operations import Actor, OperationRef, OperationRequest, OperationState
from .plans import (
    ApprovedPlanRef,
    AuthorizationKind,
    CompensationAction,
    OperationPlan,
    PlanPhase,
    PlanStep,
)
from .receipts import (
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
from .revisions import (
    ObservedRevision,
    ObservedWorkload,
    Revision,
    RevisionState,
    Workload,
    WorkloadKind,
)

__all__ = [
    "Actor",
    "ApprovedPlanRef",
    "AuthorizationKind",
    "CheckStatus",
    "CompensationAction",
    "CompensationResult",
    "CompensationStatus",
    "ContractValidationError",
    "KERNEL_CONTRACT_SCHEMA_VERSION",
    "ObservedRevision",
    "ObservedWorkload",
    "OperationEvent",
    "OperationPlan",
    "OperationRef",
    "OperationRequest",
    "OperationState",
    "PlanPhase",
    "PlanStep",
    "ReceiptEffect",
    "ReceiptOutcome",
    "Revision",
    "RevisionState",
    "TerminalReceipt",
    "VerificationCheck",
    "VerificationResult",
    "VerificationStatus",
    "Workload",
    "WorkloadKind",
    "canonical_digest",
    "canonical_json",
]
