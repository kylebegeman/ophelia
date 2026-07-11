"""Read-only plans and exact, secret-free approval bindings."""

from __future__ import annotations

import hmac
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Tuple

from ._contracts import (
    Contract,
    ContractValidationError,
    canonical_digest,
    digest_text,
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
from .operations import OperationRequest


class PlanPhase(str, Enum):
    VALIDATE = "validate"
    OBSERVE = "observe"
    PLAN = "plan"
    AUTHORIZE = "authorize"
    STAGE = "stage"
    PREFLIGHT = "preflight"
    START_CANDIDATE = "start_candidate"
    READINESS_VERIFY = "readiness_verify"
    SWITCH_TRAFFIC = "switch_traffic"
    EXTERNAL_VERIFY = "external_verify"
    DRAIN_PREVIOUS = "drain_previous"
    COMMIT = "commit"
    EMIT_RECEIPT = "emit_receipt"


class CompensationAction(str, Enum):
    NONE = "none"
    DELETE_CANDIDATE = "delete_candidate"
    STOP_CANDIDATE = "stop_candidate"
    RESTORE_TRAFFIC = "restore_traffic"
    PRESERVE_PREVIOUS = "preserve_previous"
    RECOVER_FROM_JOURNAL = "recover_from_journal"


class AuthorizationKind(str, Enum):
    LUMEN_DECISION = "lumen_decision"
    LOCAL_BREAK_GLASS = "local_break_glass"
    LEGACY_CONFIRMATION_ADAPTER = "legacy_confirmation_adapter"


@dataclass(frozen=True)
class PlanStep(Contract):
    kind = "ophelia.kernel.plan_step"

    order: int
    phase: PlanPhase
    mutates_runtime: bool
    desired_effect_digest: str
    compensation: CompensationAction

    def __post_init__(self) -> None:
        if not isinstance(self.order, int) or isinstance(self.order, bool) or self.order < 1:
            raise ContractValidationError("Plan step order must be a positive integer.")
        require_enum(self.phase, PlanPhase, "phase")
        require_enum(self.compensation, CompensationAction, "compensation")
        require_digest(self.desired_effect_digest, "desired_effect_digest")
        if self.mutates_runtime and self.compensation is CompensationAction.NONE:
            raise ContractValidationError("Every mutating plan step must declare compensation.")


@dataclass(frozen=True)
class OperationPlan(Contract):
    """A deterministic plan value. Constructing it performs no I/O."""

    kind = "ophelia.kernel.operation_plan"

    plan_id: str
    request_digest: str
    operation: str
    host_id: str
    app: str
    environment: str
    revision_id: str
    revision_digest: str
    manifest_digest: str
    artifact_digests: Tuple[str, ...]
    observed_state_digest: str
    policy_digest: str
    steps: Tuple[PlanStep, ...]
    blocker_codes: Tuple[str, ...]
    created_at: str

    def __post_init__(self) -> None:
        require_entity_id(self.plan_id, "plan", "plan_id")
        require_digest(self.request_digest, "request_digest")
        require_operation(self.operation)
        require_entity_id(self.host_id, "host", "host_id")
        require_slug(self.app, "app")
        require_slug(self.environment, "environment")
        require_entity_id(self.revision_id, "rev", "revision_id")
        require_digest(self.revision_digest, "revision_digest")
        require_digest(self.manifest_digest, "manifest_digest")
        require_digests(self.artifact_digests, "artifact_digests")
        require_digest(self.observed_state_digest, "observed_state_digest")
        require_digest(self.policy_digest, "policy_digest")
        require_utc(self.created_at, "created_at")
        if not isinstance(self.steps, tuple) or not self.steps:
            raise ContractValidationError("steps must be a non-empty immutable tuple.")
        if tuple(step.order for step in self.steps) != tuple(range(1, len(self.steps) + 1)):
            raise ContractValidationError("Plan steps must have unique contiguous order starting at one.")
        if not isinstance(self.blocker_codes, tuple):
            raise ContractValidationError("blocker_codes must be an immutable tuple.")
        for code in self.blocker_codes:
            require_text(code, "blocker_code", 128)
        if self.blocker_codes != tuple(sorted(set(self.blocker_codes))):
            raise ContractValidationError("blocker_codes must be sorted and contain no duplicates.")

    @property
    def approvable(self) -> bool:
        return not self.blocker_codes

    def plan_digest(self) -> str:
        payload = self.to_dict()
        payload.pop("plan_id")
        payload.pop("created_at")
        return canonical_digest(payload)

    @classmethod
    def create(
        cls,
        *,
        request: OperationRequest,
        manifest_digest: str,
        artifact_digests: Tuple[str, ...],
        observed_state_digest: str,
        policy_digest: str,
        steps: Tuple[PlanStep, ...],
        blocker_codes: Tuple[str, ...],
        created_at: str,
    ) -> "OperationPlan":
        provisional = cls(
            plan_id="plan_pending",
            request_digest=request.intent_digest(),
            operation=request.operation,
            host_id=request.host_id,
            app=request.app,
            environment=request.environment,
            revision_id=request.revision_id,
            revision_digest=request.revision_digest,
            manifest_digest=manifest_digest,
            artifact_digests=artifact_digests,
            observed_state_digest=observed_state_digest,
            policy_digest=policy_digest,
            steps=steps,
            blocker_codes=blocker_codes,
            created_at=created_at,
        )
        return replace(provisional, plan_id="plan_" + provisional.plan_digest()[7:31])


@dataclass(frozen=True)
class ApprovedPlanRef(Contract):
    """Durable approval evidence without the reusable approval credential."""

    kind = "ophelia.kernel.approved_plan_ref"

    plan_id: str
    plan_digest: str
    request_digest: str
    manifest_digest: str
    artifact_digests: Tuple[str, ...]
    observed_state_digest: str
    policy_digest: str
    host_id: str
    app: str
    environment: str
    revision_id: str
    revision_digest: str
    actor_id: str
    decision_id: str
    authorization_kind: AuthorizationKind
    issuer: str
    audience: str
    approved_at: str
    expires_at: str
    nonce_digest: str
    approval_digest: str

    def __post_init__(self) -> None:
        require_entity_id(self.plan_id, "plan", "plan_id")
        require_digest(self.plan_digest, "plan_digest")
        require_digest(self.request_digest, "request_digest")
        require_digest(self.manifest_digest, "manifest_digest")
        require_digests(self.artifact_digests, "artifact_digests")
        require_digest(self.observed_state_digest, "observed_state_digest")
        require_digest(self.policy_digest, "policy_digest")
        require_entity_id(self.host_id, "host", "host_id")
        require_slug(self.app, "app")
        require_slug(self.environment, "environment")
        require_entity_id(self.revision_id, "rev", "revision_id")
        require_digest(self.revision_digest, "revision_digest")
        require_entity_id(self.actor_id, "actor", "actor_id")
        require_entity_id(self.decision_id, "decision", "decision_id")
        require_enum(
            self.authorization_kind,
            AuthorizationKind,
            "authorization_kind",
        )
        require_text(self.issuer, "issuer", 255)
        require_text(self.audience, "audience", 255)
        if self.audience != self.host_id:
            raise ContractValidationError("Approval audience must match the planned host_id.")
        require_utc(self.approved_at, "approved_at")
        require_utc(self.expires_at, "expires_at")
        if parse_utc(self.expires_at) <= parse_utc(self.approved_at):
            raise ContractValidationError("expires_at must be after approved_at.")
        require_digest(self.nonce_digest, "nonce_digest")
        require_digest(self.approval_digest, "approval_digest")

    @classmethod
    def bind(
        cls,
        plan: OperationPlan,
        *,
        actor_id: str,
        decision_id: str,
        authorization_kind: AuthorizationKind,
        issuer: str,
        audience: str,
        approved_at: str,
        expires_at: str,
        approval_nonce: str,
    ) -> "ApprovedPlanRef":
        if not plan.approvable:
            raise ContractValidationError("A blocked plan cannot be approved.")
        require_text(approval_nonce, "approval_nonce", 1024)
        nonce_digest = digest_text(approval_nonce)
        provisional = cls(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest(),
            request_digest=plan.request_digest,
            manifest_digest=plan.manifest_digest,
            artifact_digests=plan.artifact_digests,
            observed_state_digest=plan.observed_state_digest,
            policy_digest=plan.policy_digest,
            host_id=plan.host_id,
            app=plan.app,
            environment=plan.environment,
            revision_id=plan.revision_id,
            revision_digest=plan.revision_digest,
            actor_id=actor_id,
            decision_id=decision_id,
            authorization_kind=authorization_kind,
            issuer=issuer,
            audience=audience,
            approved_at=approved_at,
            expires_at=expires_at,
            nonce_digest=nonce_digest,
            approval_digest="sha256:" + ("0" * 64),
        )
        return replace(provisional, approval_digest=canonical_digest(provisional._binding_payload()))

    def _binding_payload(self) -> dict:
        payload = self.to_dict()
        payload.pop("approval_digest")
        return payload

    def matches(self, plan: OperationPlan) -> bool:
        return (
            plan.approvable
            and self.plan_id == plan.plan_id
            and self.plan_digest == plan.plan_digest()
            and self.request_digest == plan.request_digest
            and self.manifest_digest == plan.manifest_digest
            and self.artifact_digests == plan.artifact_digests
            and self.observed_state_digest == plan.observed_state_digest
            and self.policy_digest == plan.policy_digest
            and self.host_id == plan.host_id
            and self.app == plan.app
            and self.environment == plan.environment
            and self.revision_id == plan.revision_id
            and self.revision_digest == plan.revision_digest
            and hmac.compare_digest(
                self.approval_digest,
                canonical_digest(self._binding_payload()),
            )
        )

    def verifies_nonce(self, approval_nonce: str) -> bool:
        return hmac.compare_digest(self.nonce_digest, digest_text(approval_nonce))

    def expired(self, now: Optional[datetime] = None) -> bool:
        current = now or datetime.now(timezone.utc)
        return current.astimezone(timezone.utc) >= parse_utc(self.expires_at)
