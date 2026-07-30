"""Plan and execute Forge-compatible product releases through Ophelia's kernel."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Optional, Tuple

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
from .execution.legacy_adapter import local_host_id
from .execution.process_backend import ProductProcessBackend, load_staged_product_backend
from .execution.staging import OperationStaging, StagingError
from .manifest_v2_execution import local_approval_key
from .product_bundle import (
    ProductBundleError,
    ProductOperationsBundle,
    load_product_operations_bundle,
    verify_product_artifact,
)


class ProductExecutionError(RuntimeError):
    """A product release could not be planned or executed safely."""


def product_release_plan(
    bundle_root: Path,
    artifact_path: Path,
    *,
    runtime_root: Path,
    environment_values: Mapping[str, str],
    host_id: Optional[str] = None,
    operation: str = "deploy.apply",
    precondition_evidence: Optional[Mapping[str, Path]] = None,
) -> Dict[str, Any]:
    if operation not in {"deploy.apply", "rollback.apply"}:
        raise ProductExecutionError("Product release operation is unsupported.")
    bundle = load_product_operations_bundle(bundle_root)
    artifact_id = str(bundle.runtime["processes"][0]["artifact_id"])
    blockers = []
    effective_host = host_id or local_host_id()
    current = _active_state(runtime_root, bundle.product_id, effective_host)
    predecessor_bundle = (
        None
        if current is None
        else _retained_bundle(runtime_root, bundle.product_id, current)
    )
    evidence, evidence_blockers = _precondition_evidence(precondition_evidence or {})
    blockers.extend(evidence_blockers)
    try:
        artifact = verify_product_artifact(bundle, artifact_id, artifact_path)
    except ProductBundleError as exc:
        artifact = None
        blockers.append({"code": "product_artifact_invalid", "message": str(exc)})
    required_configuration = {
        item["name"]
        for item in bundle.release.get("configuration", [])
        if item["required"]
    }
    declared_configuration = {
        item["name"] for item in bundle.release.get("configuration", [])
    }
    if predecessor_bundle is not None:
        required_configuration.update(
            item["name"]
            for item in predecessor_bundle.release.get("configuration", [])
            if item["required"]
        )
        declared_configuration.update(
            item["name"]
            for item in predecessor_bundle.release.get("configuration", [])
        )
        if not _traffic_contract_compatible(bundle, predecessor_bundle):
            blockers.append(
                {
                    "code": "product_predecessor_runtime_incompatible",
                    "message": "The active predecessor cannot be restored through the target traffic contract.",
                }
            )
    missing = sorted(
        name for name in required_configuration if not environment_values.get(name)
    )
    if missing:
        blockers.append(
            {
                "code": "product_configuration_missing",
                "message": "Required host configuration is missing.",
                "names": missing,
            }
        )
    unknown_configuration = sorted(set(environment_values) - declared_configuration)
    if unknown_configuration:
        blockers.append(
            {
                "code": "product_configuration_unknown",
                "message": "Host configuration contains names not declared by the release.",
                "names": unknown_configuration,
            }
        )
    if any(not isinstance(value, str) or "\x00" in value for value in environment_values.values()):
        blockers.append(
            {
                "code": "product_configuration_invalid",
                "message": "Host configuration values must be strings without NUL bytes.",
            }
        )
    preconditions = []
    for precondition in bundle.release["rollout"]["preconditions"]:
        status, digest = _release_precondition(
            precondition,
            runtime_root=runtime_root,
            target=bundle,
            predecessor=predecessor_bundle,
            active=current,
            evidence=evidence,
        )
        preconditions.append(
            {"id": precondition, "status": status, "evidence_digest": digest}
        )
        if status == "missing":
            blockers.append(
                {
                    "code": "product_release_precondition_missing",
                    "message": f"Release precondition {precondition} lacks current evidence.",
                    "precondition": precondition,
                }
            )
        elif status == "unsupported":
            blockers.append(
                {
                    "code": "product_release_precondition_unsupported",
                    "message": f"Release precondition {precondition} is not supported by this backend.",
                    "precondition": precondition,
                }
            )
    if operation == "rollback.apply":
        if predecessor_bundle is None:
            blockers.append(
                {
                    "code": "product_rollback_without_active_revision",
                    "message": "A product rollback requires an active revision.",
                }
            )
        elif predecessor_bundle.release["rollout"]["rollback"] != "artifact-only":
            blockers.append(
                {
                    "code": "product_rollback_policy_unsupported",
                    "message": "The active release requires coordinated data restore or does not support rollback.",
                }
            )
    artifact_digest = (
        str(bundle.artifact(artifact_id)["digest"])
        if artifact is None
        else artifact.digest
    )
    payload = {
        "schema_version": 1,
        "kind": "ophelia.product-release-confirmation",
        "operation": operation,
        "host_id": effective_host,
        "product_id": bundle.product_id,
        "release_id": bundle.release_id,
        "bundle_digest": bundle.bundle_digest,
        "release_document_digest": bundle.document_digests["release-manifest"],
        "artifact_digest": artifact_digest,
        "environment": "production",
        "configuration_names": sorted(environment_values),
        "configuration_digest": _configuration_value_digest(environment_values),
        "precondition_evidence": {
            item["id"]: item["evidence_digest"]
            for item in preconditions
            if item["evidence_digest"] is not None
        },
        "expected_active_revision_digest": (
            None if current is None else current.get("revision_digest")
        ),
        "expected_active_generation": (
            None if current is None else current.get("_journal_generation")
        ),
        "expected_active_operation_id": (
            None if current is None else current.get("_journal_operation_id")
        ),
    }
    token = _confirmation_token(
        local_approval_key(Path(runtime_root), create=True),
        payload,
    )
    return {
        "schema_version": 1,
        "kind": "ophelia.plan",
        "operation": operation,
        "status": "blocked" if blockers else "planned",
        "can_apply": not blockers,
        "product_id": bundle.product_id,
        "release_id": bundle.release_id,
        "bundle_digest": bundle.bundle_digest,
        "artifact_digest": artifact_digest,
        "host_id": effective_host,
        "environment": "production",
        "current_revision_id": None if current is None else current.get("revision_id"),
        "current_revision_digest": None if current is None else current.get("revision_digest"),
        "current_revision_generation": (
            None if current is None else current.get("_journal_generation")
        ),
        "required_configuration_names": sorted(required_configuration),
        "provided_configuration_names": sorted(environment_values),
        "migration_count": len(bundle.release.get("migrations", [])),
        "migration_behavior": bundle.release["rollout"]["migration_behavior"],
        "preconditions": preconditions,
        "blockers": blockers,
        "warnings": [],
        "confirmation_token": None if blockers else token,
        "summary": (
            f"Product release {bundle.release_id} is blocked."
            if blockers
            else f"Product release {bundle.release_id} is ready for confirmed execution."
        ),
    }


def apply_product_release(
    bundle_root: Path,
    artifact_path: Path,
    *,
    runtime_root: Path,
    environment_values: Mapping[str, str],
    confirm: str,
    host_id: Optional[str] = None,
    owner_id: str = "product-cli-worker",
    operation: str = "deploy.apply",
    precondition_evidence: Optional[Mapping[str, Path]] = None,
) -> Dict[str, Any]:
    """Execute one confirmed release and persist a correlated receipt."""

    plan_report = product_release_plan(
        bundle_root,
        artifact_path,
        runtime_root=runtime_root,
        environment_values=environment_values,
        host_id=host_id,
        operation=operation,
        precondition_evidence=precondition_evidence,
    )
    expected = plan_report.get("confirmation_token")
    if not isinstance(expected, str) or not confirm or not hmac.compare_digest(expected, confirm):
        raise ProductExecutionError("Confirmation token does not match the current product release plan.")
    bundle = load_product_operations_bundle(bundle_root)
    artifact_id = str(bundle.runtime["processes"][0]["artifact_id"])
    artifact = verify_product_artifact(bundle, artifact_id, artifact_path)
    runtime_root = Path(runtime_root)
    runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime_root = runtime_root.resolve()
    staging, created_at = _stage_release(
        runtime_root,
        bundle,
        artifact.path,
        confirm,
    )
    revision = _revision(bundle, created_at)
    effective_host = str(plan_report["host_id"])
    actor = Actor(
        actor_id=f"actor_local-{os.geteuid()}",
        source="product-cli",
        authenticated_by="unix-peer-credentials",
    )
    identity = canonical_digest(
        {
            "operation": operation,
            "host_id": effective_host,
            "revision_digest": revision.content_digest(),
            "confirmation": hashlib.sha256(confirm.encode("utf-8")).hexdigest(),
        }
    )[7:]
    request = OperationRequest(
        request_id="request_product-" + identity[:24],
        operation=operation,
        host_id=effective_host,
        app=revision.app,
        environment=revision.environment,
        revision_id=revision.revision_id,
        revision_digest=revision.content_digest(),
        idempotency_key=(
            f"product:{operation}:{bundle.bundle_digest}:{artifact.digest}:"
            f"generation:{plan_report['current_revision_generation'] or 0}"
        ),
    )
    journal = SQLiteOperationJournal.beneath_runtime_root(runtime_root)
    active = journal.active_revision(effective_host, revision.app, revision.environment)
    actual_active_digest = None if active is None else active.revision_digest
    actual_active_generation = None if active is None else active.generation
    if (
        actual_active_digest != plan_report["current_revision_digest"]
        or actual_active_generation != plan_report["current_revision_generation"]
    ):
        raise ProductExecutionError(
            "Active product state changed after release confirmation."
        )
    observed_digest = canonical_digest(
        {
            "active_revision_id": None if active is None else active.revision_id,
            "active_revision_digest": None if active is None else active.revision_digest,
            "active_revision_generation": (
                None if active is None else active.generation
            ),
        }
    )
    policy_digest = canonical_digest(
        {
            "policy": "product-operations-v1",
            "bundle_digest": bundle.bundle_digest,
            "platform_verified": True,
            "configuration_names": sorted(environment_values),
        }
    )
    steps = _execution_steps(revision.content_digest(), artifact.digest)
    plan = OperationPlan.create(
        request=request,
        manifest_digest=revision.manifest_digest,
        artifact_digests=revision.artifact_digests,
        observed_state_digest=observed_digest,
        policy_digest=policy_digest,
        steps=steps,
        blocker_codes=(),
        created_at=created_at,
    )
    approved_at = _utc_now()
    expires_at = (
        datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
        + timedelta(hours=2)
    ).isoformat().replace("+00:00", "Z")
    approval = ApprovedPlanRef.bind(
        plan,
        actor_id=actor.actor_id,
        decision_id="decision_product-" + identity[24:48],
        authorization_kind=AuthorizationKind.LEGACY_CONFIRMATION_ADAPTER,
        issuer="ophelia-product-cli",
        audience=effective_host,
        approved_at=approved_at,
        expires_at=expires_at,
        approval_nonce=confirm,
    )
    relative_root = staging.candidate.relative_to(runtime_root).as_posix()
    artifact_ref = RevisionArtifactRef(
        revision_id=revision.revision_id,
        revision_digest=revision.content_digest(),
        relative_root=relative_root,
        artifact_digest=artifact.digest,
    )
    execution_input = ExecutionInput.bind(
        request=request,
        plan=plan,
        approved_plan=approval,
        revision=revision,
        artifact_ref=artifact_ref,
        deadline=expires_at,
    )

    def backend_factory(exact_input, operation_ref, fence):
        return load_staged_product_backend(
            candidate_root=runtime_root / exact_input.artifact_ref.relative_root,
            runtime_root=runtime_root,
            environment_values=environment_values,
            host_id=exact_input.plan.host_id,
            operation_id=operation_ref.operation_id,
            owner_id=fence.owner_id,
            fencing_token=fence.fencing_token,
        )

    executor = JournaledExecutor(journal=journal, backend_factory=backend_factory)
    operation_ref = executor.submit(
        actor,
        request,
        approval,
        execution_input=execution_input,
    )
    receipt = executor.run(operation_ref.operation_id, owner_id=owner_id)
    durable_approval = journal.approved_plan(operation_ref.operation_id)
    correlated = _correlated_receipt(bundle, receipt, durable_approval)
    _persist_correlated_receipt(runtime_root, correlated)
    return correlated


def _revision(bundle: ProductOperationsBundle, created_at: str) -> Revision:
    process = bundle.runtime["processes"][0]
    artifact = bundle.artifact(str(process["artifact_id"]))
    workload = Workload(
        workload_id=_slug(str(process["id"])),
        workload_kind=WorkloadKind.WEB,
        artifact_digest=str(artifact["digest"]),
        route_ids=tuple(sorted(_slug(str(item["id"])) for item in bundle.runtime["ports"])),
        overlap_safe=True,
    )
    return Revision.create(
        app=_slug(bundle.product_id),
        environment="production",
        manifest_digest=bundle.document_digests["release-manifest"],
        artifact_digests=(str(artifact["digest"]),),
        renderer_version="product-operations-bundle-v1",
        workloads=(workload,),
        created_at=created_at,
    )


def _execution_steps(revision_digest: str, artifact_digest: str) -> Tuple[PlanStep, ...]:
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
                    "revision_digest": revision_digest,
                    "artifact_digest": artifact_digest,
                }
            ),
            compensation=compensation,
        )
        for index, (phase, mutates, compensation) in enumerate(phases, start=1)
    )


def _stage_release(
    runtime_root: Path,
    bundle: ProductOperationsBundle,
    artifact_path: Path,
    confirmation: str,
) -> Tuple[OperationStaging, str]:
    operation_id = (
        "product-"
        + hashlib.sha256(confirmation.encode("utf-8")).hexdigest()[:32]
    )
    try:
        staging = OperationStaging.create(runtime_root, operation_id)
        operations_target = staging.candidate / ".product" / "operations"
        operations_target.parent.mkdir(mode=0o700, parents=True)
        operations_target.mkdir(mode=0o700)
        shutil.copyfile(bundle.root / "bundle.json", operations_target / "bundle.json", follow_symlinks=False)
        for reference in bundle.bundle["documents"]:
            relative = PurePosixPath(str(reference["path"]))
            destination = operations_target / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(bundle.root / relative, destination, follow_symlinks=False)
        declaration = bundle.artifact(str(bundle.runtime["processes"][0]["artifact_id"]))
        target = staging.candidate / PurePosixPath(str(declaration["path"]))
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(artifact_path, target, follow_symlinks=False)
        staged_bundle = load_product_operations_bundle(operations_target)
        if staged_bundle.bundle_digest != bundle.bundle_digest:
            raise ProductExecutionError(
                "Staged product bundle differs from the approved bundle."
            )
        verify_product_artifact(staged_bundle, str(declaration["id"]), target)
        target.chmod(0o700)
        created_at = _utc_now()
        staging.write_evidence(
            "product-release.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "bundle_digest": bundle.bundle_digest,
                    "artifact_digest": declaration["digest"],
                    "created_at": created_at,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        return staging, created_at
    except StagingError:
        try:
            staging = OperationStaging.open_confirmed(runtime_root, operation_id)
            evidence = staging.evidence / "product-release.json"
            if evidence.is_symlink() or not evidence.is_file():
                raise ProductExecutionError(
                    "Existing product staging has no release evidence."
                )
            value = json.loads(evidence.read_text(encoding="utf-8"))
            if value.get("bundle_digest") != bundle.bundle_digest:
                raise ProductExecutionError(
                    "Existing product staging binds a different bundle."
                )
            declaration = bundle.artifact(
                str(bundle.runtime["processes"][0]["artifact_id"])
            )
            staged_artifact = staging.candidate / PurePosixPath(
                str(declaration["path"])
            )
            staged_bundle = load_product_operations_bundle(
                staging.candidate / ".product" / "operations"
            )
            if staged_bundle.bundle_digest != bundle.bundle_digest:
                raise ProductExecutionError(
                    "Existing product staging binds different bundle bytes."
                )
            verify_product_artifact(
                staged_bundle, str(declaration["id"]), staged_artifact
            )
            return staging, str(value["created_at"])
        except (OSError, ValueError) as exc:
            raise ProductExecutionError(
                "Existing product staging failed verification."
            ) from exc
    except (OSError, ProductBundleError) as exc:
        raise ProductExecutionError(
            "Staged product release failed verification."
        ) from exc


def _correlated_receipt(
    bundle: ProductOperationsBundle,
    receipt,
    approval: ApprovedPlanRef,
) -> Dict[str, Any]:
    result = {
        "schema_version": 1,
        "kind": "ophelia.product-operation-receipt",
        "status": receipt.outcome.value,
        "operation": receipt.operation,
        "product_id": bundle.product_id,
        "release_id": bundle.release_id,
        "bundle_digest": bundle.bundle_digest,
        "composition_digest": bundle.composition_digest,
        "runtime_contract_digest": bundle.runtime["contract_digest"],
        "runtime_document_digest": bundle.document_digests["runtime-requirements"],
        "release_manifest_digest": bundle.release["manifest_digest"],
        "release_document_digest": bundle.document_digests["release-manifest"],
        "recovery_contract_digest": bundle.recovery["contract_digest"],
        "recovery_document_digest": bundle.document_digests["recovery-contract"],
        "ophelia_receipt_digest": receipt.digest(),
        "ophelia_receipt": receipt.to_dict(),
        "inputs_redacted": True,
    }
    result.update(_operation_correlation(receipt, approval))
    return result


def _operation_correlation(receipt, approval: ApprovedPlanRef) -> Dict[str, str]:
    if (
        receipt.plan_id != approval.plan_id
        or receipt.plan_digest != approval.plan_digest
        or receipt.decision_id != approval.decision_id
    ):
        raise ProductExecutionError(
            "Terminal receipt does not match its durable approval."
        )
    return {
        "ophelia_request_digest": approval.request_digest,
        "ophelia_plan_digest": approval.plan_digest,
        "ophelia_approval_digest": approval.approval_digest,
        "ophelia_operation_digest": canonical_digest(
            {
                "operation_id": receipt.operation_id,
                "operation": receipt.operation,
                "request_digest": approval.request_digest,
                "plan_digest": approval.plan_digest,
                "approval_digest": approval.approval_digest,
            }
        ),
        "ophelia_verification_digest": receipt.verification.digest(),
    }


def _persist_correlated_receipt(runtime_root: Path, value: Mapping[str, Any]) -> Path:
    receipt = value["ophelia_receipt"]
    receipt_id = receipt["receipt_id"]
    root = Path(runtime_root) / "receipts" / "product"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = root / f"{receipt_id}.json"
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if target.exists():
        if target.is_symlink() or target.read_bytes() != data:
            raise ProductExecutionError("Correlated receipt id collision.")
        return target
    if root.is_symlink():
        raise ProductExecutionError("Correlated receipt root is symlinked.")
    temporary = root / ("." + target.name + ".tmp-" + os.urandom(6).hex())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.is_symlink() or target.read_bytes() != data:
                raise ProductExecutionError("Correlated receipt id collision.")
    finally:
        temporary.unlink(missing_ok=True)
    target.chmod(0o600)
    _sync_directory(root)
    return target


def _sync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _precondition_evidence(
    supplied: Mapping[str, Path],
) -> Tuple[Dict[str, str], list[Dict[str, Any]]]:
    evidence: Dict[str, str] = {}
    blockers: list[Dict[str, Any]] = []
    for identifier, raw_path in sorted(supplied.items()):
        if not isinstance(identifier, str) or not identifier or identifier in evidence:
            blockers.append(
                {
                    "code": "product_precondition_evidence_invalid",
                    "message": "Precondition evidence ids must be unique non-empty strings.",
                }
            )
            continue
        path = Path(raw_path)
        try:
            digest = _stable_evidence_digest(path)
        except (OSError, ValueError):
            blockers.append(
                {
                    "code": "product_precondition_evidence_invalid",
                    "message": f"Precondition evidence {identifier} is unavailable or unsafe.",
                    "precondition": identifier,
                }
            )
            continue
        evidence[identifier] = "sha256:" + digest
    return evidence, blockers


def _stable_evidence_digest(path: Path) -> str:
    initial = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(initial.st_mode):
        raise ValueError("Evidence must be a real regular file.")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not os.path.samestat(initial, metadata)
            or metadata.st_size < 1
            or metadata.st_size > 4 << 20
        ):
            raise ValueError("Evidence changed while it was opened.")
        digest = hashlib.sha256()
        remaining = (4 << 20) + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        final = os.fstat(descriptor)
        if (
            final.st_size != metadata.st_size
            or final.st_mtime_ns != metadata.st_mtime_ns
            or remaining <= 0
        ):
            raise ValueError("Evidence changed while it was read.")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _release_precondition(
    identifier: str,
    *,
    runtime_root: Path,
    target: ProductOperationsBundle,
    predecessor: Optional[ProductOperationsBundle],
    active: Optional[Mapping[str, Any]],
    evidence: Mapping[str, str],
) -> Tuple[str, Optional[str]]:
    if identifier in {"artifact-digest-verified", "configuration-valid"}:
        return "verified-at-plan", None
    if identifier in {"health-probe", "readiness-probe"}:
        return "verified-during-execution", None
    if identifier == "backup-complete":
        if active is None or predecessor is None:
            return "not-required-initial-release", None
        digest = _current_backup_evidence(runtime_root, predecessor, active)
        return ("verified", digest) if digest is not None else ("missing", None)
    if identifier == "expand-contract-compatible":
        if active is None:
            return "not-required-initial-release", None
        digest = evidence.get(identifier)
        return ("verified", digest) if digest is not None else ("missing", None)
    digest = evidence.get(identifier)
    return ("verified", digest) if digest is not None else ("unsupported", None)


def _current_backup_evidence(
    runtime_root: Path,
    predecessor: ProductOperationsBundle,
    active: Mapping[str, Any],
) -> Optional[str]:
    from .product_recovery import (
        ProductRecoveryError,
        _verified_backup_evidence,
    )

    backup_root = (
        Path(runtime_root)
        / "backups"
        / "product"
        / _slug(predecessor.product_id)
    )
    if backup_root.is_symlink() or not backup_root.is_dir():
        return None
    operation_id = active.get("_journal_operation_id")
    if not isinstance(operation_id, str):
        raise ProductExecutionError(
            "Active product journal operation identity is unavailable."
        )
    try:
        execution_input = SQLiteOperationJournal.beneath_runtime_root(
            runtime_root
        ).load_execution_input(operation_id)
    except KeyError as exc:
        raise ProductExecutionError(
            "Active product execution evidence is unavailable."
        ) from exc
    revision = execution_input.revision
    if (
        revision.revision_id != active.get("revision_id")
        or revision.content_digest() != active.get("revision_digest")
        or revision.manifest_digest
        != predecessor.document_digests["release-manifest"]
    ):
        raise ProductExecutionError(
            "Active product execution evidence differs from its retained bundle."
        )
    matches = []
    for candidate in backup_root.iterdir():
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        try:
            manifest, digest = _verified_backup_evidence(
                candidate,
                predecessor,
                expected_backup_id=candidate.name,
                expected_revision=revision,
                runtime_root=runtime_root,
            )
            matches.append((str(manifest["created_at"]), digest))
        except (OSError, ValueError, ProductRecoveryError):
            continue
    return None if not matches else max(matches)[1]


def _active_state(
    runtime_root: Path, product_id: str, host_id: str
) -> Optional[Mapping[str, Any]]:
    runtime_root = Path(runtime_root)
    app = _slug(product_id)
    path = (
        runtime_root
        / "apps"
        / app
        / "environments"
        / "production"
        / "traffic"
        / "active.json"
    )
    database_path = runtime_root / "host-state" / "operations.db"
    journal_active = None
    if database_path.exists() or database_path.is_symlink():
        journal_active = SQLiteOperationJournal.beneath_runtime_root(
            runtime_root
        ).active_revision(host_id, app, "production")
    if not path.exists():
        if journal_active is not None:
            raise ProductExecutionError(
                "The journal has an active revision but runtime traffic evidence is missing."
            )
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024:
        raise ProductExecutionError("Active product state is unsafe.")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductExecutionError("Active product state is malformed.") from exc
    legacy = {
        "schema_version", "revision_id", "revision_digest", "upstream_host",
        "upstream_port", "artifact_digest",
    }
    replicated = {
        "schema_version", "revision_id", "revision_digest", "upstreams",
        "artifact_digest",
    }
    shape_valid = isinstance(value, dict) and (
        (value.get("schema_version") == 1 and set(value) == legacy)
        or (
            value.get("schema_version") == 2
            and set(value) == replicated
            and isinstance(value.get("upstreams"), list)
            and bool(value.get("upstreams"))
        )
    )
    if not shape_valid:
        raise ProductExecutionError("Active product state is malformed.")
    if journal_active is None:
        raise ProductExecutionError(
            "Runtime traffic evidence has no matching journal active revision."
        )
    if (
        value.get("revision_id") != journal_active.revision_id
        or value.get("revision_digest") != journal_active.revision_digest
    ):
        raise ProductExecutionError(
            "Runtime traffic evidence differs from the journal active revision."
        )
    result = dict(value)
    result["_journal_operation_id"] = journal_active.operation_id
    result["_journal_generation"] = journal_active.generation
    return result


def _retained_bundle(
    runtime_root: Path,
    product_id: str,
    active: Mapping[str, Any],
) -> ProductOperationsBundle:
    revision_id = active.get("revision_id")
    if not isinstance(revision_id, str):
        raise ProductExecutionError("Active product revision id is malformed.")
    root = (
        Path(runtime_root)
        / "apps"
        / _slug(product_id)
        / "environments"
        / "production"
        / "revisions"
        / revision_id
        / ".product"
        / "operations"
    )
    try:
        bundle = load_product_operations_bundle(root)
    except ProductBundleError as exc:
        raise ProductExecutionError(
            "The active predecessor bundle is unavailable or invalid."
        ) from exc
    if bundle.product_id != product_id:
        raise ProductExecutionError("The active predecessor belongs to another product.")
    return bundle


def _traffic_contract_compatible(
    target: ProductOperationsBundle,
    predecessor: ProductOperationsBundle,
) -> bool:
    target_port = target.runtime["ports"][0]
    predecessor_port = predecessor.runtime["ports"][0]
    target_health = target.runtime["health"][0]
    predecessor_health = predecessor.runtime["health"][0]
    return (
        target_port["port"] == predecessor_port["port"]
        and target_port["protocol"] == predecessor_port["protocol"]
        and target_port["exposure"] == predecessor_port["exposure"]
        and target_health["path"] == predecessor_health["path"]
    )


def _strict_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ProductExecutionError(f"JSON evidence repeats object key: {key}")
        value[key] = item
    return value


def _slug(value: str) -> str:
    normalized = "".join(character if character.isalnum() else "-" for character in value.lower())
    normalized = "-".join(part for part in normalized.split("-") if part)
    if not normalized or not normalized[0].isalpha():
        normalized = "product-" + normalized
    if len(normalized) > 63:
        suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
        normalized = normalized[: 52].rstrip("-") + "-" + suffix
    return normalized


def _configuration_value_digest(values: Mapping[str, str]) -> str:
    encoded = json.dumps(
        {str(name): value for name, value in sorted(values.items())},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _confirmation_token(key: bytes, value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
