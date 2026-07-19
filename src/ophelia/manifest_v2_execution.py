"""Read-only planning and journaled execution for manifest v2."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from .domain import (
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
from .execution import ExecutionInput, JournaledExecutor, RevisionArtifactRef, SQLiteOperationJournal
from .execution.compose_backend import CommandRunner, ComposeRevisionBackend
from .execution.legacy_adapter import local_host_id
from .execution.staging import OperationStaging, tree_digest
from .manifest_v2 import ManifestV2, RouteV2, load_manifest_v2
from .manifest_v2_renderer import render_revision_bundle
from .manifest_v2_sources import manifest_v2_sources_digest, stage_manifest_v2_sources


PLAN_INDEX_RELATIVE_ROOT = Path("host-state") / "plans"
PLAN_LIFETIME = timedelta(hours=24)


def local_approval_key(runtime_root: Path, *, create: bool) -> bytes:
    """Load the host-local approval key without ever returning it in evidence."""

    root = _runtime_root(runtime_root) / "host-state" / "trust"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    path = root / "local-approval.key"
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise ValueError("Local approval key path is unsafe.")
        value = path.read_bytes()
        _validate_approval_key(value)
        os.chmod(path, 0o600)
        return value
    if not create:
        raise ValueError("Local approval key is not initialized; run manifest plan locally first.")
    value = os.urandom(32)
    _atomic_bytes(path, value)
    return value


def plan_manifest_v2(
    manifest_path: Path,
    *,
    runtime_root: Path,
    approval_key: bytes,
    host_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    operation: str = "deploy.apply",
    require_edge_runtime: bool = True,
    secret_refs_available: bool = True,
) -> Dict[str, Any]:
    """Calculate and stage a plan without changing desired or active state."""

    _validate_approval_key(approval_key)
    runtime_root = _runtime_root(runtime_root)
    manifest = load_manifest_v2(manifest_path)
    created_at = _utc_now()
    expires_at = (
        datetime.fromisoformat(created_at.replace("Z", "+00:00")) + PLAN_LIFETIME
    ).isoformat().replace("+00:00", "Z")
    revision = manifest.to_revision(created_at=created_at)
    effective_host = host_id or local_host_id()
    identity = canonical_digest(
        {
            "operation": operation,
            "host_id": effective_host,
            "revision_digest": revision.content_digest(),
            "created_at": created_at,
        }
    )[7:]
    request = OperationRequest(
        request_id="request_manifest-v2-" + identity[:20],
        operation=operation,
        host_id=effective_host,
        app=manifest.app,
        environment=manifest.environment,
        revision_id=revision.revision_id,
        revision_digest=revision.content_digest(),
        idempotency_key=(idempotency_key or "manifest-v2:%s:%s" % (operation, revision.content_digest()))[:255],
    )
    journal_active, runtime_active, observation_blockers = _observe_active(
        runtime_root, effective_host, manifest
    )
    blockers = list(observation_blockers)
    if manifest.routes and require_edge_runtime:
        edge = runtime_root / "platform" / "shared" / "compose.yml"
        if not edge.is_file() or edge.is_symlink():
            blockers.append("edge_runtime_missing")
    if manifest.secrets and not secret_refs_available:
        blockers.append("secret_bindings_unavailable")
    if any(item.backup_required for item in manifest.migrations):
        blockers.append("migration_backup_evidence_missing")
    observed_digest = canonical_digest(
        {
            "journal_active": None if journal_active is None else journal_active.to_dict(),
            "runtime_active": runtime_active,
            "edge_required": bool(manifest.routes),
            "secret_reference_count": len(manifest.secrets),
        }
    )
    staging = OperationStaging.create(runtime_root, "manifest-v2-" + uuid.uuid4().hex)
    source_inputs_digest = stage_manifest_v2_sources(
        manifest,
        manifest_path,
        staging.candidate,
    )
    candidate = render_revision_bundle(
        manifest,
        revision,
        runtime_root=runtime_root,
        secret_runtime_root=runtime_root / "run" / "secrets",
    )
    candidate_digest = "sha256:" + staging.finalize_uploaded_candidate(candidate)
    policy_digest = _manifest_v2_policy_digest(
        manifest,
        candidate_digest=candidate_digest,
        source_inputs_digest=source_inputs_digest,
        require_edge_runtime=require_edge_runtime,
        secret_refs_available=secret_refs_available,
    )
    plan = OperationPlan.create(
        request=request,
        manifest_digest=revision.manifest_digest,
        artifact_digests=revision.artifact_digests,
        observed_state_digest=observed_digest,
        policy_digest=policy_digest,
        steps=_execution_steps(revision),
        blocker_codes=tuple(sorted(set(blockers))),
        created_at=created_at,
    )
    evidence = {
        "schema_version": 1,
        "kind": "ophelia.manifest-v2-plan-evidence",
        "plan": plan.to_dict(),
        "request": request.to_dict(),
        "revision": revision.to_dict(),
        "staging_operation_id": staging.operation_id,
        "candidate_digest": candidate_digest,
        "source_inputs_digest": source_inputs_digest,
        "created_at": created_at,
        "expires_at": expires_at,
        "require_edge_runtime": require_edge_runtime,
        "secret_refs_available": secret_refs_available,
        "previous_revision_id": None if journal_active is None else journal_active.revision_id,
        "previous_revision_digest": None
        if journal_active is None
        else journal_active.revision_digest,
    }
    staging.write_evidence(
        "manifest-v2-plan.json", json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    )
    _write_plan_index(runtime_root, plan.plan_id, staging.operation_id)
    token = _confirmation_token(approval_key, plan)
    return _manifest_v2_plan_report(
        plan,
        request,
        revision,
        manifest,
        confirmation_token=token,
        expires_at=expires_at,
        previous_revision_id=None if journal_active is None else journal_active.revision_id,
        previous_revision_digest=None
        if journal_active is None
        else journal_active.revision_digest,
    )


def describe_manifest_v2_plan(
    runtime_root: Path,
    plan_id: str,
    *,
    approval_key: bytes,
) -> Dict[str, Any]:
    """Recreate the exact local plan response for an idempotent transport retry."""

    _validate_approval_key(approval_key)
    loaded = _load_plan(runtime_root, plan_id)
    return _manifest_v2_plan_report(
        loaded["plan"],
        loaded["request"],
        loaded["revision"],
        loaded["manifest"],
        confirmation_token=_confirmation_token(approval_key, loaded["plan"]),
        expires_at=loaded["expires_at"],
        previous_revision_id=loaded["previous_revision_id"],
        previous_revision_digest=loaded["previous_revision_digest"],
    )


def _manifest_v2_plan_report(
    plan: OperationPlan,
    request: OperationRequest,
    revision: Revision,
    manifest: ManifestV2,
    *,
    confirmation_token: str,
    expires_at: str,
    previous_revision_id: Optional[str],
    previous_revision_digest: Optional[str],
) -> Dict[str, Any]:
    blockers = list(plan.blocker_codes)
    return {
        "schema_version": 1,
        "kind": "ophelia.manifest-v2-plan",
        "status": "blocked" if blockers else "planned",
        "can_apply": not blockers,
        "plan_id": plan.plan_id,
        "plan_digest": plan.plan_digest(),
        "request_id": request.request_id,
        "host_id": plan.host_id,
        "app": manifest.app,
        "environment": manifest.environment,
        "revision_id": revision.revision_id,
        "revision_digest": revision.content_digest(),
        "manifest_digest": revision.manifest_digest,
        "artifact_digests": list(revision.artifact_digests),
        "previous_revision_id": previous_revision_id,
        "previous_revision_digest": previous_revision_digest,
        "steps": [item.to_dict() for item in plan.steps],
        "blockers": sorted(set(blockers)),
        "confirmation_token": None if blockers else confirmation_token,
        "expires_at": expires_at,
        "summary": (
            "Manifest v2 plan is blocked: %s." % ", ".join(sorted(set(blockers)))
            if blockers
            else "Manifest v2 revision is ready for exact, journaled activation."
        ),
    }


def apply_manifest_v2_plan(
    plan_id: str,
    *,
    runtime_root: Path,
    approval_key: bytes,
    confirmation: str,
    runner: Optional[CommandRunner] = None,
    secret_resolver: Optional[Callable[[str], str]] = None,
    external_verifier: Optional[Callable[[RouteV2], bool]] = None,
    require_edge_runtime: Optional[bool] = None,
    owner_id: str = "manifest-v2-cli-worker",
    execute: bool = True,
    actor: Optional[Actor] = None,
) -> Dict[str, Any]:
    """Accept a locally approved plan and optionally execute it immediately."""

    _validate_approval_key(approval_key)
    loaded = _load_plan(runtime_root, plan_id)
    plan = loaded["plan"]
    if not plan.approvable:
        raise ValueError("Blocked manifest v2 plans cannot be applied.")
    expected = _confirmation_token(approval_key, plan)
    if not isinstance(confirmation, str) or not hmac.compare_digest(expected, confirmation):
        raise ValueError("Manifest v2 confirmation did not match the exact plan.")
    if datetime.now(timezone.utc) >= datetime.fromisoformat(loaded["expires_at"].replace("Z", "+00:00")):
        raise ValueError("Manifest v2 plan has expired; calculate a fresh plan.")
    actor = actor or Actor(
        actor_id="actor_local-%d" % os.geteuid(),
        source="manifest-v2-cli",
        authenticated_by="unix-peer-credentials",
    )
    decision_identity = hashlib.sha256(
        (plan.plan_digest() + actor.actor_id).encode("utf-8")
    ).hexdigest()[:24]
    approval = ApprovedPlanRef.bind(
        plan,
        actor_id=actor.actor_id,
        decision_id="decision_local-" + decision_identity,
        authorization_kind=AuthorizationKind.LOCAL_OPERATOR,
        issuer="ophelia-local-cli",
        audience=plan.host_id,
        approved_at=plan.created_at,
        expires_at=loaded["expires_at"],
        approval_nonce=confirmation,
    )
    return submit_manifest_v2_plan(
        loaded,
        actor=actor,
        approval=approval,
        runtime_root=runtime_root,
        runner=runner,
        secret_resolver=secret_resolver,
        external_verifier=external_verifier,
        require_edge_runtime=require_edge_runtime,
        owner_id=owner_id,
        execute=execute,
    )


def submit_manifest_v2_plan(
    loaded_plan: Mapping[str, Any],
    *,
    actor: Actor,
    approval: ApprovedPlanRef,
    runtime_root: Path,
    runner: Optional[CommandRunner] = None,
    secret_resolver: Optional[Callable[[str], str]] = None,
    external_verifier: Optional[Callable[[RouteV2], bool]] = None,
    require_edge_runtime: Optional[bool] = None,
    owner_id: str = "opheliad-worker",
    execute: bool = False,
) -> Dict[str, Any]:
    """Canonical daemon/Lumen submission path for an already verified approval."""

    runtime_root = _runtime_root(runtime_root)
    plan = loaded_plan["plan"]
    request = loaded_plan["request"]
    revision = loaded_plan["revision"]
    if not approval.matches(plan) or approval.actor_id != actor.actor_id:
        raise ValueError("Approval does not bind the exact manifest v2 plan and actor.")
    first_artifact = revision.artifact_digests[0]
    staging = loaded_plan["staging"]
    artifact_ref = RevisionArtifactRef(
        revision_id=revision.revision_id,
        revision_digest=revision.content_digest(),
        relative_root=staging.candidate.relative_to(runtime_root).as_posix(),
        artifact_digest=first_artifact,
    )
    execution_input = ExecutionInput.bind(
        request=request,
        plan=plan,
        approved_plan=approval,
        revision=revision,
        artifact_ref=artifact_ref,
        deadline=loaded_plan["expires_at"],
    )
    journal = SQLiteOperationJournal.beneath_runtime_root(runtime_root)
    required_edge = (
        loaded_plan["require_edge_runtime"]
        if require_edge_runtime is None
        else require_edge_runtime
    )

    executor = manifest_v2_executor(
        runtime_root=runtime_root,
        journal=journal,
        runner=runner,
        secret_resolver=secret_resolver,
        external_verifier=external_verifier,
        require_edge_runtime=required_edge,
    )
    operation = executor.submit(actor, request, approval, execution_input=execution_input)
    receipt = executor.run(operation.operation_id, owner_id=owner_id) if execute else journal.receipt(operation.operation_id)
    return {
        "schema_version": 1,
        "kind": "ophelia.manifest-v2-operation",
        "operation": operation.to_dict(),
        "receipt": None if receipt is None else receipt.to_dict(),
    }


def manifest_v2_executor(
    *,
    runtime_root: Path,
    journal: Optional[SQLiteOperationJournal] = None,
    runner: Optional[CommandRunner] = None,
    secret_resolver: Optional[Callable[[str], str]] = None,
    external_verifier: Optional[Callable[[RouteV2], bool]] = None,
    require_edge_runtime: bool = True,
) -> JournaledExecutor:
    """Build the restart-safe executor used by the CLI and ``opheliad``."""

    runtime_root = _runtime_root(runtime_root)
    exact_journal = journal or SQLiteOperationJournal.beneath_runtime_root(runtime_root)

    def backend_factory(exact_input, operation_ref, fence):
        candidate_root = (runtime_root / exact_input.artifact_ref.relative_root).resolve(
            strict=False
        )
        try:
            candidate_root.relative_to(runtime_root / "staging")
        except ValueError as exc:
            raise ValueError("Manifest v2 candidate is outside operation staging.") from exc
        manifest = load_manifest_v2(candidate_root / "manifest.lock.json")
        if (
            manifest.app != exact_input.plan.app
            or manifest.environment != exact_input.plan.environment
            or manifest.canonical_digest() != exact_input.plan.manifest_digest
            or exact_input.revision.content_digest() != exact_input.plan.revision_digest
        ):
            raise ValueError("Recovered manifest v2 candidate does not match its journal input.")
        return ComposeRevisionBackend(
            manifest=manifest,
            revision=exact_input.revision,
            candidate_root=candidate_root,
            runtime_root=runtime_root,
            host_id=exact_input.plan.host_id,
            operation_id=operation_ref.operation_id,
            owner_id=fence.owner_id,
            fencing_token=fence.fencing_token,
            runner=runner,
            secret_resolver=secret_resolver,
            external_verifier=external_verifier,
            require_edge_runtime=require_edge_runtime,
        )

    return JournaledExecutor(journal=exact_journal, backend_factory=backend_factory)


def load_manifest_v2_plan(runtime_root: Path, plan_id: str) -> Dict[str, Any]:
    """Public loader used by the daemon after transport authorization."""

    return _load_plan(runtime_root, plan_id)


def _load_plan(runtime_root: Path, plan_id: str) -> Dict[str, Any]:
    runtime_root = _runtime_root(runtime_root)
    index_path = runtime_root / PLAN_INDEX_RELATIVE_ROOT / (plan_id + ".json")
    if index_path.is_symlink() or not index_path.is_file():
        raise ValueError("Unknown manifest v2 plan id.")
    index = _read_json(index_path)
    if index.get("plan_id") != plan_id:
        raise ValueError("Manifest v2 plan index is malformed.")
    staging = OperationStaging.open_confirmed(runtime_root, index["staging_operation_id"])
    evidence_path = staging.evidence / "manifest-v2-plan.json"
    if evidence_path.is_symlink() or not evidence_path.is_file():
        raise ValueError("Manifest v2 plan evidence is missing.")
    evidence = _read_json(evidence_path)
    plan = _plan_from_dict(evidence["plan"])
    request = _request_from_dict(evidence["request"])
    revision = _revision_from_dict(evidence["revision"])
    manifest = load_manifest_v2(staging.candidate / "manifest.lock.json")
    candidate_digest = "sha256:" + tree_digest(staging.candidate)
    source_inputs_digest = manifest_v2_sources_digest(manifest, staging.candidate)
    expected_policy_digest = _manifest_v2_policy_digest(
        manifest,
        candidate_digest=candidate_digest,
        source_inputs_digest=source_inputs_digest,
        require_edge_runtime=bool(evidence.get("require_edge_runtime", True)),
        secret_refs_available=bool(evidence.get("secret_refs_available", True)),
    )
    if (
        plan.plan_id != plan_id
        or request.intent_digest() != plan.request_digest
        or revision.content_digest() != plan.revision_digest
        or manifest.canonical_digest() != plan.manifest_digest
        or evidence.get("candidate_digest") != candidate_digest
        or evidence.get("source_inputs_digest") != source_inputs_digest
        or plan.policy_digest != expected_policy_digest
    ):
        raise ValueError("Manifest v2 plan evidence failed integrity validation.")
    expected_candidate = render_revision_bundle(
        manifest,
        revision,
        runtime_root=runtime_root,
        secret_runtime_root=runtime_root / "run" / "secrets",
    )
    for relative, content in expected_candidate.items():
        path = staging.candidate / relative
        if path.is_symlink() or not path.is_file() or path.read_text(encoding="utf-8") != content:
            raise ValueError("Manifest v2 candidate failed integrity validation.")
    return {
        "plan": plan,
        "request": request,
        "revision": revision,
        "manifest": manifest,
        "staging": staging,
        "expires_at": evidence["expires_at"],
        "require_edge_runtime": bool(evidence.get("require_edge_runtime", True)),
        "previous_revision_id": evidence.get("previous_revision_id"),
        "previous_revision_digest": evidence.get("previous_revision_digest"),
    }


def _observe_active(runtime_root: Path, host_id: str, manifest: ManifestV2):
    database_path = runtime_root / "host-state" / "operations.db"
    journal_active = None
    if database_path.exists():
        journal_active = SQLiteOperationJournal.beneath_runtime_root(runtime_root).active_revision(
            host_id, manifest.app, manifest.environment
        )
    active_path = (
        runtime_root
        / "apps"
        / manifest.app
        / "environments"
        / manifest.environment
        / "traffic"
        / "active.json"
    )
    runtime_active = None
    blockers = []
    if active_path.exists() or active_path.is_symlink():
        if active_path.is_symlink() or not active_path.is_file():
            blockers.append("runtime_active_evidence_unsafe")
        else:
            try:
                runtime_active = _read_json(active_path)
            except ValueError:
                blockers.append("runtime_active_evidence_malformed")
    if journal_active is None and runtime_active is not None:
        blockers.append("runtime_active_without_journal")
    if journal_active is not None and (
        runtime_active is None
        or runtime_active.get("revision_id") != journal_active.revision_id
        or runtime_active.get("revision_digest") != journal_active.revision_digest
    ):
        blockers.append("journal_runtime_active_mismatch")
    return journal_active, runtime_active, blockers


def _execution_steps(revision: Revision):
    phases = (
        (PlanPhase.STAGE, False, CompensationAction.NONE),
        (PlanPhase.PREFLIGHT, False, CompensationAction.NONE),
        (PlanPhase.START_CANDIDATE, True, CompensationAction.DELETE_CANDIDATE),
        (PlanPhase.READINESS_VERIFY, False, CompensationAction.NONE),
        (PlanPhase.SWITCH_TRAFFIC, True, CompensationAction.RESTORE_TRAFFIC),
        (PlanPhase.EXTERNAL_VERIFY, False, CompensationAction.NONE),
        (PlanPhase.DRAIN_PREVIOUS, True, CompensationAction.PRESERVE_PREVIOUS),
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
                    "revision_digest": revision.content_digest(),
                    "workloads": [item.to_dict() for item in revision.workloads],
                }
            ),
            compensation=compensation,
        )
        for index, (phase, mutates, compensation) in enumerate(phases, start=1)
    )


def _confirmation_token(key: bytes, plan: OperationPlan) -> str:
    return hmac.new(key, plan.plan_digest().encode("ascii"), hashlib.sha256).hexdigest()


def _manifest_v2_policy_digest(
    manifest: ManifestV2,
    *,
    candidate_digest: str,
    source_inputs_digest: str,
    require_edge_runtime: bool,
    secret_refs_available: bool,
) -> str:
    return canonical_digest(
        {
            "policy": "manifest-v2-default",
            "environment": manifest.environment,
            "production_images_pinned": all(
                item.image is None or "@sha256:" in item.image
                for item in manifest.artifacts
            ),
            "require_edge_runtime": require_edge_runtime,
            "secret_refs_available": secret_refs_available,
            "candidate_digest": candidate_digest,
            "source_inputs_digest": source_inputs_digest,
        }
    )


def _validate_approval_key(value: bytes) -> None:
    if not isinstance(value, bytes) or len(value) < 32:
        raise ValueError("approval_key must contain at least 32 bytes.")


def _runtime_root(value: Path) -> Path:
    path = Path(value).expanduser()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("runtime_root must be a trusted real directory.")
    return path.resolve()


def _write_plan_index(runtime_root: Path, plan_id: str, staging_operation_id: str) -> None:
    root = runtime_root / PLAN_INDEX_RELATIVE_ROOT
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = root / (plan_id + ".json")
    payload = {
        "schema_version": 1,
        "kind": "ophelia.manifest-v2-plan-index",
        "plan_id": plan_id,
        "staging_operation_id": staging_operation_id,
    }
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if target.exists():
        if target.is_symlink() or target.read_bytes() != data:
            raise ValueError("Manifest v2 plan id collision.")
        return
    _atomic_bytes(target, data)


def _atomic_bytes(path: Path, data: bytes) -> None:
    temporary = path.parent / ("." + path.name + ".tmp-" + os.urandom(6).hex())
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("JSON evidence is malformed: %s" % path) from exc
    if not isinstance(value, dict):
        raise ValueError("JSON evidence must be an object: %s" % path)
    return value


def _request_from_dict(value: Mapping[str, Any]) -> OperationRequest:
    return OperationRequest(
        request_id=value["request_id"],
        operation=value["operation"],
        host_id=value["host_id"],
        app=value["app"],
        environment=value["environment"],
        revision_id=value["revision_id"],
        revision_digest=value["revision_digest"],
        idempotency_key=value["idempotency_key"],
    )


def _plan_from_dict(value: Mapping[str, Any]) -> OperationPlan:
    steps = tuple(
        PlanStep(
            order=item["order"],
            phase=PlanPhase(item["phase"]),
            mutates_runtime=item["mutates_runtime"],
            desired_effect_digest=item["desired_effect_digest"],
            compensation=CompensationAction(item["compensation"]),
        )
        for item in value["steps"]
    )
    return OperationPlan(
        plan_id=value["plan_id"],
        request_digest=value["request_digest"],
        operation=value["operation"],
        host_id=value["host_id"],
        app=value["app"],
        environment=value["environment"],
        revision_id=value["revision_id"],
        revision_digest=value["revision_digest"],
        manifest_digest=value["manifest_digest"],
        artifact_digests=tuple(value["artifact_digests"]),
        observed_state_digest=value["observed_state_digest"],
        policy_digest=value["policy_digest"],
        steps=steps,
        blocker_codes=tuple(value["blocker_codes"]),
        created_at=value["created_at"],
    )


def _revision_from_dict(value: Mapping[str, Any]) -> Revision:
    workloads = tuple(
        Workload(
            workload_id=item["workload_id"],
            workload_kind=WorkloadKind(item["workload_kind"]),
            artifact_digest=item["artifact_digest"],
            route_ids=tuple(item.get("route_ids", ())),
            overlap_safe=bool(item.get("overlap_safe", False)),
        )
        for item in value["workloads"]
    )
    return Revision(
        revision_id=value["revision_id"],
        app=value["app"],
        environment=value["environment"],
        manifest_digest=value["manifest_digest"],
        artifact_digests=tuple(value["artifact_digests"]),
        renderer_version=value["renderer_version"],
        workloads=workloads,
        created_at=value["created_at"],
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
