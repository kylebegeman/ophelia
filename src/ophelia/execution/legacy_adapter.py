"""Compatibility adapter from reviewed v1 confirmations to kernel contracts."""

from __future__ import annotations

import hashlib
import hmac
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from ..domain import (
    Actor,
    ApprovedPlanRef,
    AuthorizationKind,
    CompensationAction,
    OperationPlan,
    OperationRequest,
    PlanPhase,
    PlanStep,
    Revision,
    Workload,
    WorkloadKind,
    canonical_digest,
)
from ..domain._contracts import require_digest, require_entity_id
from ..manifest import Manifest
from .contracts import ExecutionInput, RevisionArtifactRef
from .staging import ConfirmedStaging, StagingError


_RENDERER_VERSION = "legacy-confirmed-static-v1"


@dataclass(frozen=True)
class LegacyExecutionBundle:
    """Secret-free execution input derived from one reviewed confirmation."""

    actor: Actor
    request: OperationRequest
    plan: OperationPlan
    approval: ApprovedPlanRef
    revision: Revision
    artifact_relative_root: str
    artifact_tree_digest: str
    artifact_ref: RevisionArtifactRef
    execution_input: ExecutionInput


def local_host_id(machine_identity: str | None = None) -> str:
    """Return a stable, non-secret local host identity for the legacy adapter."""

    identity = machine_identity or _machine_identity()
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"host_local-{suffix}"


def build_legacy_static_execution(
    confirmed: ConfirmedStaging,
    manifest: Manifest,
    confirmation_token: str,
    *,
    host_id: str | None = None,
    actor_uid: int | None = None,
) -> LegacyExecutionBundle:
    """Bind a reviewed static candidate into the canonical kernel vocabulary."""

    if manifest.kind != "static" or manifest.environment != "production":
        raise StagingError(
            "The journaled legacy adapter currently accepts production static deploys only."
        )
    bound_token = confirmed.binding.get("confirmation_token")
    if not isinstance(bound_token, str) or not hmac.compare_digest(
        bound_token, confirmation_token
    ):
        raise StagingError("Legacy confirmation does not match its reviewed binding.")

    payload = confirmed.binding.get("confirmation_payload")
    if not isinstance(payload, dict):
        raise StagingError("Confirmed plan payload is missing.")
    policy_digest = payload.get("policy_digest")
    if not isinstance(policy_digest, str):
        raise StagingError("Confirmed plan policy digest is missing.")
    require_digest(policy_digest, "policy_digest")

    artifact_tree_digest = _prefixed_digest(confirmed.candidate_digest)
    manifest_digest = _file_digest(confirmed.manifest_path)
    evidence = payload.get("evidence_digests")
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        raise StagingError("Confirmed plan evidence digest binding is malformed.")
    artifact_digests = tuple(
        sorted({_prefixed_digest(item) for item in evidence} | {artifact_tree_digest})
    )

    created_at = payload.get("confirmation_created_at")
    expires_at = payload.get("confirmation_expires_at")
    if not isinstance(created_at, str) or not isinstance(expires_at, str):
        raise StagingError("Confirmed plan timestamps are missing.")

    workload = Workload(
        workload_id="static",
        workload_kind=WorkloadKind.STATIC,
        artifact_digest=artifact_tree_digest,
    )
    revision = Revision.create(
        app=manifest.app,
        environment=manifest.environment,
        manifest_digest=manifest_digest,
        artifact_digests=artifact_digests,
        renderer_version=_RENDERER_VERSION,
        workloads=(workload,),
        created_at=created_at,
    )
    revision_digest = revision.content_digest()

    effective_host_id = host_id or local_host_id()
    require_entity_id(effective_host_id, "host", "host_id")
    uid = os.geteuid() if actor_uid is None else actor_uid
    if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
        raise StagingError("Authenticated Unix uid is invalid.")
    actor = Actor(
        actor_id=f"actor_local-{uid}",
        source="legacy-cli",
        authenticated_by="unix-peer-credentials",
    )

    identity_digest = canonical_digest(
        {
            "operation_id": confirmed.staging.operation_id,
            "candidate_digest": artifact_tree_digest,
            "host_id": effective_host_id,
            "revision_digest": revision_digest,
        }
    ).split(":", 1)[1]
    request = OperationRequest(
        request_id=f"request_legacy-{identity_digest[:24]}",
        operation="deploy.apply",
        host_id=effective_host_id,
        app=manifest.app,
        environment=manifest.environment,
        revision_id=revision.revision_id,
        revision_digest=revision_digest,
        idempotency_key=(
            f"legacy:{confirmed.staging.operation_id}:{artifact_tree_digest}"
        )[:255],
    )

    steps = _static_steps(revision_digest, artifact_tree_digest)
    plan = OperationPlan.create(
        request=request,
        manifest_digest=manifest_digest,
        artifact_digests=artifact_digests,
        observed_state_digest=_prefixed_digest(confirmed.baseline_digest),
        policy_digest=policy_digest,
        steps=steps,
        blocker_codes=(),
        created_at=created_at,
    )
    approval = ApprovedPlanRef.bind(
        plan,
        actor_id=actor.actor_id,
        decision_id=f"decision_legacy-{identity_digest[24:48]}",
        authorization_kind=AuthorizationKind.LEGACY_CONFIRMATION_ADAPTER,
        issuer="ophelia-legacy-cli",
        audience=effective_host_id,
        approved_at=created_at,
        expires_at=expires_at,
        approval_nonce=confirmation_token,
    )

    runtime_root = confirmed.staging.root.parent.parent
    try:
        relative_root = confirmed.staging.candidate.relative_to(runtime_root).as_posix()
    except ValueError as exc:
        raise StagingError("Confirmed candidate is outside its runtime root.") from exc
    artifact_ref = RevisionArtifactRef(
        revision_id=revision.revision_id,
        revision_digest=revision_digest,
        relative_root=relative_root,
        artifact_digest=artifact_tree_digest,
    )
    execution_input = ExecutionInput.bind(
        request=request,
        plan=plan,
        approved_plan=approval,
        revision=revision,
        artifact_ref=artifact_ref,
        deadline=expires_at,
    )
    return LegacyExecutionBundle(
        actor=actor,
        request=request,
        plan=plan,
        approval=approval,
        revision=revision,
        artifact_relative_root=relative_root,
        artifact_tree_digest=artifact_tree_digest,
        artifact_ref=artifact_ref,
        execution_input=execution_input,
    )


def _static_steps(revision_digest: str, artifact_digest: str) -> Tuple[PlanStep, ...]:
    phases = (
        (PlanPhase.STAGE, False, CompensationAction.NONE),
        (PlanPhase.PREFLIGHT, False, CompensationAction.NONE),
        (PlanPhase.START_CANDIDATE, True, CompensationAction.DELETE_CANDIDATE),
        (PlanPhase.READINESS_VERIFY, False, CompensationAction.NONE),
        (PlanPhase.SWITCH_TRAFFIC, True, CompensationAction.RESTORE_TRAFFIC),
        (PlanPhase.EXTERNAL_VERIFY, False, CompensationAction.NONE),
        (PlanPhase.DRAIN_PREVIOUS, False, CompensationAction.NONE),
        (PlanPhase.COMMIT, True, CompensationAction.RECOVER_FROM_JOURNAL),
        (PlanPhase.EMIT_RECEIPT, True, CompensationAction.RECOVER_FROM_JOURNAL),
    )
    return tuple(
        PlanStep(
            order=index,
            phase=phase,
            mutates_runtime=mutates,
            desired_effect_digest=canonical_digest(
                {
                    "phase": phase.value,
                    "revision_digest": revision_digest,
                    "artifact_digest": artifact_digest,
                }
            ),
            compensation=compensation,
        )
        for index, (phase, mutates, compensation) in enumerate(phases, start=1)
    )


def _prefixed_digest(value: str) -> str:
    digest = value if value.startswith("sha256:") else f"sha256:{value}"
    require_digest(digest, "digest")
    return digest


def _file_digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise StagingError(f"Confirmed manifest is missing or unsafe: {path}")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _machine_identity() -> str:
    machine_id = Path("/etc/machine-id")
    try:
        value = machine_id.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    return value or platform.node() or "local-host"
