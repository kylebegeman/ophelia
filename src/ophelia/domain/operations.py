"""Immutable operation identities and requests for the kernel executor."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ._contracts import (
    Contract,
    canonical_digest,
    require_digest,
    require_entity_id,
    require_enum,
    require_operation,
    require_slug,
    require_text,
)


class OperationState(str, Enum):
    ACCEPTED = "accepted"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    EXECUTING = "executing"
    COMPENSATING = "compensating"
    SUCCEEDED = "succeeded"
    FAILED_COMPENSATED = "failed_compensated"
    FAILED_UNCOMPENSATED = "failed_uncompensated"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            OperationState.SUCCEEDED,
            OperationState.FAILED_COMPENSATED,
            OperationState.FAILED_UNCOMPENSATED,
            OperationState.CANCELLED,
        }


@dataclass(frozen=True)
class Actor(Contract):
    """An authenticated, server-derived actor, never a caller-supplied label."""

    kind = "ophelia.kernel.actor"

    actor_id: str
    source: str
    authenticated_by: str

    def __post_init__(self) -> None:
        require_entity_id(self.actor_id, "actor", "actor_id")
        require_text(self.source, "source", 128)
        require_text(self.authenticated_by, "authenticated_by", 128)


@dataclass(frozen=True)
class OperationRequest(Contract):
    """Normalized mutating intent accepted by the canonical executor."""

    kind = "ophelia.kernel.operation_request"

    request_id: str
    operation: str
    host_id: str
    app: str
    environment: str
    revision_id: str
    revision_digest: str
    idempotency_key: str

    def __post_init__(self) -> None:
        require_entity_id(self.request_id, "request", "request_id")
        require_operation(self.operation)
        require_entity_id(self.host_id, "host", "host_id")
        require_slug(self.app, "app")
        require_slug(self.environment, "environment")
        require_entity_id(self.revision_id, "rev", "revision_id")
        require_digest(self.revision_digest, "revision_digest")
        require_text(self.idempotency_key, "idempotency_key", 255)

    def intent_digest(self) -> str:
        """Digest normalized intent without transport retry or idempotency identity."""

        payload = self.to_dict()
        payload.pop("request_id")
        payload.pop("idempotency_key")
        return canonical_digest(payload)


@dataclass(frozen=True)
class OperationRef(Contract):
    """Durable identity returned only after the operation journal accepts work."""

    kind = "ophelia.kernel.operation_ref"

    operation_id: str
    request_id: str
    state: OperationState

    def __post_init__(self) -> None:
        require_entity_id(self.operation_id, "operation", "operation_id")
        require_entity_id(self.request_id, "request", "request_id")
        require_enum(self.state, OperationState, "state")
