"""Bounded, replayable operation events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from ._contracts import (
    Contract,
    ContractValidationError,
    optional_entity_id,
    require_digest,
    require_digests,
    require_entity_id,
    require_enum,
    require_slug,
    require_text,
    require_utc,
)
from .operations import OperationState


@dataclass(frozen=True)
class OperationEvent(Contract):
    kind = "ophelia.kernel.operation_event"

    event_id: str
    operation_id: str
    sequence: int
    event_type: str
    occurred_at: str
    state: OperationState
    host_id: str
    app: str
    environment: str
    revision_id: Optional[str] = None
    message: Optional[str] = None
    evidence_digests: Tuple[str, ...] = ()
    previous_event_digest: Optional[str] = None

    def __post_init__(self) -> None:
        require_entity_id(self.event_id, "event", "event_id")
        require_entity_id(self.operation_id, "operation", "operation_id")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 1:
            raise ContractValidationError("sequence must be a positive integer.")
        require_text(self.event_type, "event_type", 128)
        require_utc(self.occurred_at, "occurred_at")
        require_enum(self.state, OperationState, "state")
        require_entity_id(self.host_id, "host", "host_id")
        require_slug(self.app, "app")
        require_slug(self.environment, "environment")
        optional_entity_id(self.revision_id, "rev", "revision_id")
        if self.message is not None:
            require_text(self.message, "message", 2048)
        require_digests(self.evidence_digests, "evidence_digests")
        if self.previous_event_digest is not None:
            require_digest(self.previous_event_digest, "previous_event_digest")
