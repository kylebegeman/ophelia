"""Contract-driven backup and isolated restore drills for product bundles."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import threading
import uuid
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Optional, Tuple

from .domain import (
    Actor,
    ApprovedPlanRef,
    AuthorizationKind,
    CheckStatus,
    CompensationAction,
    CompensationResult,
    CompensationStatus,
    OperationPlan,
    OperationRequest,
    PlanPhase,
    PlanStep,
    ReceiptEffect,
    ReceiptOutcome,
    Revision,
    TerminalReceipt,
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
    canonical_digest,
)
from .execution import RuntimeHandle, SQLiteOperationJournal
from .execution.legacy_adapter import local_host_id
from .execution.process_backend import ProductProcessBackend
from .manifest_v2_execution import local_approval_key
from .product_bundle import ProductOperationsBundle, load_product_operations_bundle
from .product_execution import _revision, _slug


_RECOVERY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


class ProductRecoveryError(RuntimeError):
    """A backup or isolated restore could not meet its recovery contract."""


class _RecoveryFenceHeartbeat:
    """Keep a long recovery copy fenced without persisting secret inputs."""

    def __init__(self, journal, fence, *, ttl_seconds: float = 300.0) -> None:
        self.journal = journal
        self.fence = fence
        self.ttl_seconds = ttl_seconds
        self._stopped = threading.Event()
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"recovery-fence-{fence.operation_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join(timeout=5.0)

    def check(self) -> None:
        if self._error is not None:
            raise ProductRecoveryError(
                "Recovery operation lost its journal lease heartbeat."
            ) from self._error

    def _run(self) -> None:
        while not self._stopped.wait(min(30.0, self.ttl_seconds / 3.0)):
            try:
                self.fence = self.journal.heartbeat_fence(
                    self.fence, self.ttl_seconds
                )
            except BaseException as exc:
                self._error = exc
                self._stopped.set()
                return


def product_backup_plan(
    bundle_root: Path,
    *,
    runtime_root: Path,
    backup_id: str,
    dataset_bindings: Mapping[str, Path],
    environment_values: Optional[Mapping[str, str]] = None,
    host_id: Optional[str] = None,
) -> Dict[str, Any]:
    bundle = load_product_operations_bundle(bundle_root)
    environment_values = dict(environment_values or {})
    _recovery_id(backup_id, "backup_id")
    material = _active_material(runtime_root, bundle, host_id)
    resolved, blockers = _resolve_datasets(
        bundle,
        material["data_root"],
        dataset_bindings,
        require_sources=True,
        environment_values=environment_values,
    )
    blockers.extend(_configuration_blockers(bundle, environment_values))
    payload = _recovery_confirmation_payload(
        "backup.apply",
        bundle,
        str(material["host_id"]),
        str(material["revision"].revision_id),
        str(material["revision"].content_digest()),
        backup_id,
        resolved,
        configuration_names=tuple(sorted(environment_values)),
        configuration_digest=_configuration_value_digest(environment_values),
        active_generation=int(material["active_generation"]),
        active_operation_id=str(material["active_operation_id"]),
    )
    return {
        "schema_version": 1,
        "kind": "ophelia.plan",
        "operation": "backup.apply",
        "status": "blocked" if blockers else "planned",
        "can_apply": not blockers,
        "product_id": bundle.product_id,
        "release_id": bundle.release_id,
        "backup_id": backup_id,
        "bundle_digest": bundle.bundle_digest,
        "active_revision_id": material["revision"].revision_id,
        "active_revision_digest": material["revision"].content_digest(),
        "active_revision_generation": material["active_generation"],
        "datasets": [
            {
                "id": item["id"],
                "kind": item["kind"],
                "binding": item["binding"],
                "backup": item["backup"],
                "source_present": item["id"] in resolved,
            }
            for item in bundle.recovery["datasets"]
        ],
        "quiescence_required": any(
            item["quiescence"] == "application-stop"
            for item in bundle.recovery["datasets"]
            if item["backup"] != "excluded"
        ),
        "required_configuration_names": sorted(
            item["name"]
            for item in bundle.release.get("configuration", [])
            if item["required"]
        ),
        "provided_configuration_names": sorted(environment_values),
        "blockers": blockers,
        "warnings": [],
        "confirmation_token": (
            None
            if blockers
            else _confirmation_token(
                local_approval_key(Path(runtime_root), create=True),
                payload,
            )
        ),
        "summary": (
            f"Product backup {backup_id} is blocked."
            if blockers
            else f"Product backup {backup_id} is ready for confirmed execution."
        ),
    }


def create_product_backup(
    bundle_root: Path,
    *,
    runtime_root: Path,
    backup_id: str,
    dataset_bindings: Mapping[str, Path],
    environment_values: Mapping[str, str],
    confirm: str,
    host_id: Optional[str] = None,
    owner_id: str = "product-backup-worker",
) -> Dict[str, Any]:
    plan_report = product_backup_plan(
        bundle_root,
        runtime_root=runtime_root,
        backup_id=backup_id,
        dataset_bindings=dataset_bindings,
        environment_values=environment_values,
        host_id=host_id,
    )
    _require_confirmation(plan_report, confirm)
    bundle = load_product_operations_bundle(bundle_root)
    material = _active_material(runtime_root, bundle, host_id)
    _require_recovery_material(plan_report, material)
    revision = material["revision"]
    resolved, blockers = _resolve_datasets(
        bundle,
        material["data_root"],
        dataset_bindings,
        require_sources=True,
        environment_values=environment_values,
    )
    if blockers:
        raise ProductRecoveryError("Backup dataset bindings changed after planning.")
    backup_root = (
        Path(runtime_root)
        / "backups"
        / "product"
        / _slug(bundle.product_id)
        / backup_id
    )
    operation, approval, journal, fence, existing = _begin_recovery_operation(
        "backup.apply",
        bundle,
        revision,
        str(material["host_id"]),
        confirm,
        backup_id,
        runtime_root,
        owner_id,
    )
    if existing is not None:
        return _existing_backup_receipt(
            runtime_root,
            bundle,
            revision,
            backup_id,
            backup_root,
            existing,
            approval,
        )
    started_at = _utc_now()
    backend = _backend_for_material(
        material,
        runtime_root,
        environment_values,
        operation.operation_id,
        fence.owner_id,
        fence.fencing_token,
    )
    handle = backend.handle_for(revision)
    temporary = backup_root.parent / ("." + backup_id + ".tmp")
    resumed = False
    heartbeat = _RecoveryFenceHeartbeat(journal, fence)
    heartbeat.start()
    try:
        if backup_root.exists() or backup_root.is_symlink():
            manifest = _load_backup_manifest(
                backup_root,
                bundle,
                expected_backup_id=backup_id,
                expected_revision=revision,
            )
            if (
                manifest["backup_id"] != backup_id
                or manifest["revision_id"] != revision.revision_id
                or manifest["revision_digest"] != revision.content_digest()
            ):
                raise ProductRecoveryError(
                    "Published backup evidence belongs to another operation."
                )
            records = list(manifest["datasets"])
            _remove_owned_temporary(temporary)
        else:
            _remove_owned_temporary(temporary)
            requires_stop = any(
                item["quiescence"] == "application-stop"
                for item in bundle.recovery["datasets"]
                if item["backup"] != "excluded"
            )
            if requires_stop:
                stopped = backend.stop(handle, backend.shutdown_seconds())
                if not stopped.stopped:
                    raise ProductRecoveryError("Active product process did not quiesce.")
            temporary.mkdir(mode=0o700, parents=True)
            records = []
            datasets = {item["id"]: item for item in bundle.recovery["datasets"]}
            for dataset_id in bundle.recovery["backup_order"]:
                item = datasets[dataset_id]
                if item["backup"] == "excluded" or dataset_id not in resolved:
                    continue
                target = temporary / "datasets" / _slug(dataset_id)
                if item["kind"] == "postgresql":
                    record = _backup_postgresql(
                        item,
                        environment_values.get("DATABASE_URL"),
                        target,
                    )
                else:
                    record = _backup_dataset(item, resolved[dataset_id], target)
                records.append(record)
            manifest = {
                "schema_version": 1,
                "kind": "ophelia.product-backup",
                "backup_id": backup_id,
                "product_id": bundle.product_id,
                "release_id": bundle.release_id,
                "bundle_digest": bundle.bundle_digest,
                "recovery_contract_digest": bundle.recovery["contract_digest"],
                "recovery_document_digest": bundle.document_digests["recovery-contract"],
                "revision_id": revision.revision_id,
                "revision_digest": revision.content_digest(),
                "created_at": _utc_now(),
                "datasets": records,
                "inputs_redacted": True,
            }
            _write_json(temporary / "backup-manifest.json", manifest)
            os.replace(temporary, backup_root)
            _sync_directory(backup_root.parent)
        heartbeat.check()
        resumed = _resume_active_product(backend, revision, handle)
        if not resumed:
            raise ProductRecoveryError("Product did not return healthy after backup quiescence.")
        manifest_digest = _digest_file(backup_root / "backup-manifest.json")
        verification = _passed_verification(
            revision,
            "product_backup_verified",
            canonical_digest(
                {
                    "manifest_digest": manifest_digest,
                    "dataset_digests": sorted(item["digest"] for item in records),
                    "application_resumed": True,
                }
            ),
        )
        receipt = _recovery_terminal_receipt(
            operation.operation_id,
            "backup.apply",
            approval,
            revision,
            str(material["host_id"]),
            started_at,
            verification,
            ReceiptEffect.BACKUP_CREATED,
        )
        heartbeat.check()
        journal.commit_receipt(
            receipt,
            lease_owner=fence.owner_id,
            fencing_token=fence.fencing_token,
        )
        correlated = _recovery_receipt(
            bundle,
            receipt,
            approval,
            backup_id=backup_id,
            backup_manifest_digest=manifest_digest,
            backup_path=backup_root,
        )
        _persist_recovery_receipt(runtime_root, correlated)
        return correlated
    except BaseException:
        if not resumed:
            try:
                resumed = _resume_active_product(backend, revision, handle)
            except BaseException:
                resumed = False
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        if journal.receipt(operation.operation_id) is None:
            heartbeat.check()
            failed = _failed_recovery_receipt(
                operation.operation_id,
                "backup.apply",
                approval,
                revision,
                str(material["host_id"]),
                started_at,
                compensated=resumed,
            )
            journal.commit_receipt(
                failed,
                lease_owner=fence.owner_id,
                fencing_token=fence.fencing_token,
            )
        raise
    finally:
        heartbeat.stop()
        try:
            journal.release_fence(fence)
        except Exception:
            pass


def product_restore_drill_plan(
    bundle_root: Path,
    *,
    runtime_root: Path,
    backup_id: str,
    drill_id: str,
    environment_values: Optional[Mapping[str, str]] = None,
    host_id: Optional[str] = None,
) -> Dict[str, Any]:
    bundle = load_product_operations_bundle(bundle_root)
    environment_values = dict(environment_values or {})
    _recovery_id(backup_id, "backup_id")
    _recovery_id(drill_id, "drill_id")
    material = _active_material(runtime_root, bundle, host_id)
    backup_root = Path(runtime_root) / "backups" / "product" / _slug(bundle.product_id) / backup_id
    blockers = []
    blockers.extend(_configuration_blockers(bundle, environment_values))
    try:
        manifest, backup_manifest_digest = _verified_backup_evidence(
            backup_root,
            bundle,
            expected_backup_id=backup_id,
            expected_revision=material["revision"],
            runtime_root=runtime_root,
        )
    except ProductRecoveryError as exc:
        manifest = None
        backup_manifest_digest = None
        blockers.append({"code": "product_backup_invalid", "message": str(exc)})
    if manifest is not None:
        contracts = {item["id"]: item for item in bundle.recovery["datasets"]}
        supported_provider_kinds = {"object-storage", "filesystem-or-object-store"}
        provider_datasets = sorted(
            str(item["id"])
            for item in manifest["datasets"]
            if contracts[str(item["id"])]["binding"] == "provider-selected"
            and contracts[str(item["id"])]["kind"] not in supported_provider_kinds
        )
        if provider_datasets:
            blockers.append(
                {
                    "code": "product_provider_restore_adapter_unsupported",
                    "message": "Provider-selected datasets require a supported isolated restore adapter.",
                    "datasets": provider_datasets,
                }
            )
        supported_validations = {
            "sqlite-integrity",
            "postgres-restore",
            "migration-version",
            "application-start",
            "health-probe",
            "readiness-probe",
            "provider-restore",
            "application-validation",
        }
        unsupported_validations = sorted(
            {
                check
                for item in manifest["datasets"]
                for check in contracts[str(item["id"])]["restore_validations"]
                if check not in supported_validations
            }
        )
        if unsupported_validations:
            blockers.append(
                {
                    "code": "product_restore_validation_unsupported",
                    "message": "The recovery contract declares validations this backend cannot prove.",
                    "validations": unsupported_validations,
                }
            )
        if any(
            contracts[str(item["id"])]["kind"] == "postgresql"
            for item in manifest["datasets"]
        ):
            if not _isolated_postgres_target(
                environment_values.get("DATABASE_URL"), drill_id
            ):
                blockers.append(
                    {
                        "code": "product_postgres_restore_target_not_isolated",
                        "message": "PostgreSQL restore drills require a DATABASE_URL whose database name ends with the drill-specific isolated suffix.",
                    }
                )
            if shutil.which("pg_restore") is None or shutil.which("psql") is None:
                blockers.append(
                    {
                        "code": "product_postgres_tools_missing",
                        "message": "PostgreSQL restore drills require pg_restore and psql.",
                    }
                )
    resolved = {
        str(item["id"]): backup_root / str(item["relative_path"])
        for item in ([] if manifest is None else manifest["datasets"])
    }
    payload = _recovery_confirmation_payload(
        "restore-drill.apply",
        bundle,
        str(material["host_id"]),
        material["revision"].revision_id,
        material["revision"].content_digest(),
        drill_id,
        resolved,
        backup_id=backup_id,
        configuration_names=tuple(sorted(environment_values)),
        configuration_digest=_configuration_value_digest(environment_values),
        active_generation=int(material["active_generation"]),
        active_operation_id=str(material["active_operation_id"]),
        backup_manifest_digest=backup_manifest_digest,
        backup_dataset_digests={
            str(item["id"]): str(item["digest"])
            for item in ([] if manifest is None else manifest["datasets"])
        },
    )
    return {
        "schema_version": 1,
        "kind": "ophelia.plan",
        "operation": "restore-drill.apply",
        "status": "blocked" if blockers else "planned",
        "can_apply": not blockers,
        "product_id": bundle.product_id,
        "release_id": bundle.release_id,
        "backup_id": backup_id,
        "drill_id": drill_id,
        "bundle_digest": bundle.bundle_digest,
        "backup_manifest_digest": backup_manifest_digest,
        "active_revision_id": material["revision"].revision_id,
        "active_revision_digest": material["revision"].content_digest(),
        "active_revision_generation": material["active_generation"],
        "active_runtime_modified": False,
        "isolated_target": True,
        "required_configuration_names": sorted(
            item["name"]
            for item in bundle.release.get("configuration", [])
            if item["required"]
        ),
        "provided_configuration_names": sorted(environment_values),
        "blockers": blockers,
        "warnings": [],
        "confirmation_token": (
            None
            if blockers
            else _confirmation_token(
                local_approval_key(Path(runtime_root), create=True),
                payload,
            )
        ),
        "summary": (
            f"Restore drill {drill_id} is blocked."
            if blockers
            else f"Restore drill {drill_id} is ready for confirmed execution."
        ),
    }


def apply_product_restore_drill(
    bundle_root: Path,
    *,
    runtime_root: Path,
    backup_id: str,
    drill_id: str,
    environment_values: Mapping[str, str],
    confirm: str,
    host_id: Optional[str] = None,
    owner_id: str = "product-restore-worker",
) -> Dict[str, Any]:
    plan_report = product_restore_drill_plan(
        bundle_root,
        runtime_root=runtime_root,
        backup_id=backup_id,
        drill_id=drill_id,
        environment_values=environment_values,
        host_id=host_id,
    )
    _require_confirmation(plan_report, confirm)
    bundle = load_product_operations_bundle(bundle_root)
    material = _active_material(runtime_root, bundle, host_id)
    _require_recovery_material(plan_report, material)
    revision = material["revision"]
    backup_root = Path(runtime_root) / "backups" / "product" / _slug(bundle.product_id) / backup_id
    backup_manifest = _load_backup_manifest(
        backup_root,
        bundle,
        expected_backup_id=backup_id,
        expected_revision=revision,
    )
    expected_restore_datasets = {
        str(item["id"]) for item in backup_manifest["datasets"]
    }
    backup_manifest_digest = _digest_file(
        backup_root / "backup-manifest.json"
    )
    if backup_manifest_digest != plan_report["backup_manifest_digest"]:
        raise ProductRecoveryError(
            "Product backup changed after restore confirmation."
        )
    drill_root = (
        Path(runtime_root)
        / "restore-drills"
        / "product"
        / _slug(bundle.product_id)
        / drill_id
    )
    restore_identity = canonical_digest(
        {
            "backup_id": backup_id,
            "backup_manifest_digest": backup_manifest_digest,
            "drill_id": drill_id,
        }
    )
    operation, approval, journal, fence, existing = _begin_recovery_operation(
        "restore-drill.apply",
        bundle,
        revision,
        str(material["host_id"]),
        confirm,
        restore_identity,
        runtime_root,
        owner_id,
    )
    if existing is not None:
        return _existing_restore_receipt(
            runtime_root,
            bundle,
            revision,
            backup_id,
            drill_id,
            drill_root,
            existing,
            approval,
        )
    started_at = _utc_now()
    temporary = drill_root.parent / ("." + drill_id + ".tmp")
    drill_backend = None
    drill_handle = None
    drill_stopped = True
    heartbeat = _RecoveryFenceHeartbeat(journal, fence)
    heartbeat.start()
    try:
        report = None
        if drill_root.exists() or drill_root.is_symlink():
            try:
                report = _load_restore_report(
                    drill_root,
                    bundle,
                    revision,
                    backup_id,
                    drill_id,
                    expected_dataset_ids=expected_restore_datasets,
                    expected_backup_manifest_digest=backup_manifest_digest,
                )
            except ProductRecoveryError:
                _remove_owned_tree(drill_root)
        _remove_owned_temporary(temporary)
        if report is None:
            temporary.mkdir(mode=0o700, parents=True)
            restored = []
            local_provider_roots: Dict[str, Path] = {}
            records = {item["id"]: item for item in backup_manifest["datasets"]}
            contracts = {item["id"]: item for item in bundle.recovery["datasets"]}
            for dataset_id in bundle.recovery["restore_order"]:
                if dataset_id not in records:
                    continue
                record = records[dataset_id]
                contract = contracts[dataset_id]
                source = backup_root / str(record["relative_path"])
                target = _restored_dataset_path(temporary, contract)
                _copy_verified(source, target)
                digest = _path_digest(target)
                if digest != record["digest"]:
                    raise ProductRecoveryError("Restored dataset digest does not match backup evidence.")
                if "sqlite-integrity" in contract["restore_validations"]:
                    _verify_sqlite(target)
                if contract["kind"] == "postgresql":
                    _restore_postgresql(
                        target,
                        environment_values.get("DATABASE_URL"),
                        drill_id,
                    )
                elif contract["binding"] == "provider-selected":
                    local_provider_roots[dataset_id] = target
                restored.append({"id": dataset_id, "digest": digest})
            os.replace(temporary, drill_root)
            _sync_directory(drill_root.parent)
            drill_environment = dict(environment_values)
            object_provider_roots = {
                identifier: path
                for identifier, path in local_provider_roots.items()
                if contracts[identifier]["kind"]
                in {"object-storage", "filesystem-or-object-store"}
            }
            private_objects = object_provider_roots.get("storage.object.private")
            if private_objects is None and object_provider_roots:
                private_objects = object_provider_roots[
                    sorted(object_provider_roots)[0]
                ]
            if private_objects is not None:
                drill_environment["OBJECT_STORAGE_PROVIDER"] = "local"
                drill_environment["OBJECT_STORAGE_LOCAL_ROOT"] = os.fspath(
                    drill_root / private_objects.relative_to(temporary)
                )
            drill_runtime = drill_root / "runtime"
            drill_runtime.mkdir(mode=0o700)
            drill_backend = ProductProcessBackend(
                bundle=material["bundle"],
                artifact_path=material["artifact_path"],
                candidate_root=material["revision_root"],
                runtime_root=drill_runtime,
                environment_values=drill_environment,
                host_id=str(material["host_id"]),
                operation_id=operation.operation_id,
                owner_id=fence.owner_id,
                fencing_token=fence.fencing_token,
                data_root=drill_root / "data",
            )
            drill_handle = drill_backend.start(revision)
            drill_stopped = False
            observed = drill_backend.inspect(drill_handle)
            application = drill_backend.verify(revision, observed)
            if application.status is not VerificationStatus.PASSED:
                raise ProductRecoveryError("Restored application did not pass its health probe.")
            stopped = drill_backend.stop(
                drill_handle, drill_backend.shutdown_seconds()
            )
            drill_stopped = stopped.stopped
            if not drill_stopped:
                raise ProductRecoveryError(
                    "Restored application process did not stop after validation."
                )
            report = {
                "schema_version": 1,
                "kind": "ophelia.product-restore-drill",
                "drill_id": drill_id,
                "backup_id": backup_id,
                "product_id": bundle.product_id,
                "bundle_digest": bundle.bundle_digest,
                "backup_manifest_digest": backup_manifest_digest,
                "revision_digest": revision.content_digest(),
                "datasets": restored,
                "application_health_digest": application.observed_state_digest,
                "status": "succeeded",
                "active_runtime_modified": False,
                "completed_at": _utc_now(),
                "inputs_redacted": True,
            }
            _write_json(drill_root / "restore-report.json", report)
        else:
            restored = list(report["datasets"])
        report_digest = _digest_file(drill_root / "restore-report.json")
        verification = _passed_verification(
            revision,
            "product_restore_drill_verified",
            canonical_digest(
                {
                    "report_digest": report_digest,
                    "dataset_digests": sorted(item["digest"] for item in restored),
                    "application_health_digest": report["application_health_digest"],
                }
            ),
        )
        receipt = _recovery_terminal_receipt(
            operation.operation_id,
            "restore-drill.apply",
            approval,
            revision,
            str(material["host_id"]),
            started_at,
            verification,
            ReceiptEffect.RESTORE_VERIFIED,
        )
        heartbeat.check()
        journal.commit_receipt(
            receipt,
            lease_owner=fence.owner_id,
            fencing_token=fence.fencing_token,
        )
        correlated = _recovery_receipt(
            bundle,
            receipt,
            approval,
            backup_id=backup_id,
            backup_manifest_digest=backup_manifest_digest,
            restore_drill_id=drill_id,
            restore_report_digest=report_digest,
            restore_path=drill_root,
        )
        _persist_recovery_receipt(runtime_root, correlated)
        return correlated
    except BaseException:
        if (
            not drill_stopped
            and drill_backend is not None
            and drill_handle is not None
        ):
            try:
                drill_stopped = drill_backend.stop(
                    drill_handle, drill_backend.shutdown_seconds()
                ).stopped
            except BaseException:
                drill_stopped = False
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        if journal.receipt(operation.operation_id) is None:
            heartbeat.check()
            failed = _failed_recovery_receipt(
                operation.operation_id,
                "restore-drill.apply",
                approval,
                revision,
                str(material["host_id"]),
                started_at,
                compensated=drill_stopped,
            )
            journal.commit_receipt(
                failed,
                lease_owner=fence.owner_id,
                fencing_token=fence.fencing_token,
            )
        raise
    finally:
        heartbeat.stop()
        try:
            journal.release_fence(fence)
        except Exception:
            pass


def _active_material(
    runtime_root: Path,
    expected_bundle: ProductOperationsBundle,
    host_id: Optional[str],
) -> Dict[str, Any]:
    runtime_root = Path(runtime_root).resolve()
    app = _slug(expected_bundle.product_id)
    scope = runtime_root / "apps" / app / "environments" / "production"
    active_path = scope / "traffic" / "active.json"
    active = _read_json(active_path)
    revision_id = active.get("revision_id")
    revision_digest = active.get("revision_digest")
    if not isinstance(revision_id, str) or not isinstance(revision_digest, str):
        raise ProductRecoveryError("Active product revision evidence is malformed.")
    effective_host = host_id or local_host_id()
    database_path = runtime_root / "host-state" / "operations.db"
    if not database_path.exists() or database_path.is_symlink():
        raise ProductRecoveryError("Active product journal evidence is unavailable.")
    journal_active = SQLiteOperationJournal.beneath_runtime_root(
        runtime_root
    ).active_revision(effective_host, app, "production")
    if (
        journal_active is None
        or journal_active.revision_id != revision_id
        or journal_active.revision_digest != revision_digest
    ):
        raise ProductRecoveryError(
            "Active runtime and journal revision evidence do not reconcile."
        )
    revision_root = scope / "revisions" / revision_id
    metadata = _read_json(revision_root / "ophelia-revision.json")
    bundle = load_product_operations_bundle(revision_root / ".product" / "operations")
    if bundle.product_id != expected_bundle.product_id or bundle.bundle_digest != expected_bundle.bundle_digest:
        raise ProductRecoveryError("Supplied bundle is not the active product release.")
    created_at = metadata.get("created_at")
    if not isinstance(created_at, str):
        raise ProductRecoveryError("Active product revision creation time is unavailable.")
    revision = _revision(bundle, created_at)
    if revision.revision_id != revision_id or revision.content_digest() != revision_digest:
        raise ProductRecoveryError("Active product revision does not reconcile with its bundle.")
    process = bundle.runtime["processes"][0]
    artifact = bundle.artifact(str(process["artifact_id"]))
    return {
        "bundle": bundle,
        "revision": revision,
        "revision_root": revision_root,
        "artifact_path": revision_root / str(artifact["path"]),
        "data_root": scope / "data",
        "host_id": effective_host,
        "active_generation": journal_active.generation,
        "active_operation_id": journal_active.operation_id,
    }


def _backend_for_material(
    material,
    runtime_root,
    environment_values,
    operation_id,
    owner_id,
    fencing_token,
) -> ProductProcessBackend:
    return ProductProcessBackend(
        bundle=material["bundle"],
        artifact_path=material["artifact_path"],
        candidate_root=material["revision_root"],
        runtime_root=runtime_root,
        environment_values=environment_values,
        host_id=material["host_id"],
        operation_id=operation_id,
        owner_id=owner_id,
        fencing_token=fencing_token,
    )


def _resume_active_product(
    backend: ProductProcessBackend,
    revision: Revision,
    handle: RuntimeHandle,
) -> bool:
    started = backend.start(revision)
    if started != handle:
        raise ProductRecoveryError("Resumed product handle differs from active revision.")
    activated = backend.activate(handle, revision.content_digest())
    if activated.active_revision_digest != revision.content_digest():
        raise ProductRecoveryError("Resumed product did not restore its traffic pointer.")
    return (
        backend.verify_active(revision, handle).status
        is VerificationStatus.PASSED
    )


def _configuration_blockers(
    bundle: ProductOperationsBundle,
    environment_values: Mapping[str, str],
) -> list[Dict[str, Any]]:
    blockers: list[Dict[str, Any]] = []
    declared = {
        item["name"] for item in bundle.release.get("configuration", [])
    }
    missing = sorted(
        item["name"]
        for item in bundle.release.get("configuration", [])
        if item["required"] and not environment_values.get(item["name"])
    )
    if missing:
        blockers.append(
            {
                "code": "product_configuration_missing",
                "message": "Required host configuration is missing.",
                "names": missing,
            }
        )
    unknown = sorted(set(environment_values) - declared)
    if unknown:
        blockers.append(
            {
                "code": "product_configuration_unknown",
                "message": "Host configuration contains names not declared by the release.",
                "names": unknown,
            }
        )
    if any(
        not isinstance(value, str) or "\x00" in value
        for value in environment_values.values()
    ):
        blockers.append(
            {
                "code": "product_configuration_invalid",
                "message": "Host configuration values must be strings without NUL bytes.",
            }
        )
    return blockers


def _resolve_datasets(
    bundle: ProductOperationsBundle,
    data_root: Path,
    bindings: Mapping[str, Path],
    *,
    require_sources: bool,
    environment_values: Optional[Mapping[str, str]] = None,
) -> Tuple[Dict[str, Path], list[Dict[str, str]]]:
    resolved: Dict[str, Path] = {}
    blockers = []
    declared = {item["id"] for item in bundle.recovery["datasets"]}
    unknown = sorted(set(bindings) - declared)
    environment_values = dict(environment_values or {})
    if unknown:
        blockers.append(
            {
                "code": "product_dataset_binding_unknown",
                "message": "Dataset bindings contain unknown ids.",
            }
        )
    for item in bundle.recovery["datasets"]:
        dataset_id = str(item["id"])
        if item["backup"] == "excluded":
            continue
        if item["kind"] == "postgresql":
            if not environment_values.get("DATABASE_URL"):
                blockers.append(
                    {
                        "code": "product_postgres_configuration_missing",
                        "message": "PostgreSQL backup requires host-owned DATABASE_URL configuration.",
                    }
                )
            elif shutil.which("pg_dump") is None or shutil.which("pg_restore") is None:
                blockers.append(
                    {
                        "code": "product_postgres_tools_missing",
                        "message": "PostgreSQL backup requires pg_dump and pg_restore.",
                    }
                )
            else:
                resolved[dataset_id] = Path("__ophelia_postgresql_provider__")
            continue
        if item["binding"] == "stack-owned":
            relative = item.get("path")
            if not isinstance(relative, str):
                blockers.append({"code": "product_dataset_path_missing", "message": f"Dataset {dataset_id} has no stack-owned path."})
                continue
            root = Path(data_root).resolve(strict=False)
            path = root / PurePosixPath(relative)
            cursor = root
            for part in PurePosixPath(relative).parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    blockers.append(
                        {
                            "code": "product_dataset_path_unsafe",
                            "message": f"Dataset {dataset_id} contains a symlinked path component.",
                        }
                    )
                    path = None
                    break
            if path is None:
                continue
        else:
            supplied = bindings.get(dataset_id)
            if supplied is None:
                if item["backup"] == "required":
                    blockers.append({"code": "product_dataset_binding_missing", "message": f"Dataset {dataset_id} requires a provider binding."})
                continue
            path = Path(supplied).expanduser()
        if require_sources and (path.is_symlink() or not path.exists()):
            if item["backup"] == "required":
                blockers.append({"code": "product_dataset_source_missing", "message": f"Required dataset {dataset_id} is unavailable."})
            continue
        resolved_path = path.resolve(strict=False)
        if item["binding"] == "stack-owned":
            try:
                resolved_path.relative_to(root)
            except ValueError:
                blockers.append(
                    {
                        "code": "product_dataset_path_unsafe",
                        "message": f"Dataset {dataset_id} escapes the managed data root.",
                    }
                )
                continue
        resolved[dataset_id] = resolved_path
    object_provider_ids = {
        str(item["id"])
        for item in bundle.recovery["datasets"]
        if item["binding"] == "provider-selected"
        and item["backup"] != "excluded"
        and item["kind"] in {"object-storage", "filesystem-or-object-store"}
    }
    object_provider_sources = {
        os.fspath(resolved[identifier])
        for identifier in object_provider_ids
        if identifier in resolved
    }
    if len(object_provider_sources) > 1:
        blockers.append(
            {
                "code": "product_object_provider_snapshot_inconsistent",
                "message": (
                    "Object-storage recovery datasets must bind the same "
                    "provider snapshot root."
                ),
            }
        )
    return resolved, blockers


def _backup_dataset(contract: Mapping[str, Any], source: Path, target: Path) -> Dict[str, Any]:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if source.is_symlink():
        raise ProductRecoveryError("Dataset source may not be a symlink.")
    if str(contract["kind"]).startswith("sqlite"):
        if not source.is_file():
            raise ProductRecoveryError("SQLite dataset source must be a regular file.")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # WAL-mode databases may need SQLite to recover or checkpoint their
        # sidecars before the online backup can begin. The application is
        # quiesced above, and mode=rw prevents accidental database creation
        # while permitting that SQLite-managed recovery work.
        source_connection = sqlite3.connect(_sqlite_existing_rw_uri(source), uri=True)
        destination = sqlite3.connect(target)
        try:
            source_connection.backup(destination)
        finally:
            destination.close()
            source_connection.close()
        _verify_sqlite(target)
    else:
        _copy_verified(source, target)
    return {
        "id": contract["id"],
        "kind": contract["kind"],
        "relative_path": target.relative_to(target.parents[1]).as_posix(),
        "digest": _path_digest(target),
        "validation_checks": list(contract["restore_validations"]),
    }


def _backup_postgresql(
    contract: Mapping[str, Any], database_url: Optional[str], target: Path
) -> Dict[str, Any]:
    if not database_url or "\x00" in database_url:
        raise ProductRecoveryError("PostgreSQL backup configuration is unavailable.")
    pg_dump = shutil.which("pg_dump")
    pg_restore = shutil.which("pg_restore")
    if pg_dump is None or pg_restore is None:
        raise ProductRecoveryError("PostgreSQL backup tools are unavailable.")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment = {
        name: os.environ[name]
        for name in ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR", "HOME")
        if name in os.environ
    }
    environment["PGDATABASE"] = database_url
    try:
        completed = subprocess.run(
            [pg_dump, "--format=custom", "--no-owner", "--file", os.fspath(target)],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=1800,
        )
        if completed.returncode != 0:
            raise ProductRecoveryError("PostgreSQL provider backup failed.")
        verified = subprocess.run(
            [pg_restore, "--list", os.fspath(target)],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProductRecoveryError("PostgreSQL provider backup failed.") from exc
    if verified.returncode != 0 or not target.is_file() or target.stat().st_size < 1:
        raise ProductRecoveryError("PostgreSQL backup archive failed validation.")
    return {
        "id": contract["id"],
        "kind": contract["kind"],
        "relative_path": target.relative_to(target.parents[1]).as_posix(),
        "digest": _path_digest(target),
        "validation_checks": list(contract["restore_validations"]),
    }


def _isolated_postgres_target(database_url: Optional[str], drill_id: str) -> bool:
    if not database_url or "\x00" in database_url:
        return False
    try:
        parsed = urllib.parse.urlsplit(database_url)
    except ValueError:
        return False
    database = parsed.path.lstrip("/")
    suffix = "_ophelia_drill_" + _slug(drill_id).replace("-", "_")
    return (
        parsed.scheme in {"postgres", "postgresql"}
        and bool(parsed.hostname)
        and bool(database)
        and database.endswith(suffix)
    )


def _restore_postgresql(
    archive: Path, database_url: Optional[str], drill_id: str
) -> None:
    if not _isolated_postgres_target(database_url, drill_id):
        raise ProductRecoveryError("PostgreSQL restore target is not drill-isolated.")
    pg_restore = shutil.which("pg_restore")
    psql = shutil.which("psql")
    if pg_restore is None or psql is None:
        raise ProductRecoveryError("PostgreSQL restore tools are unavailable.")
    environment = {
        name: os.environ[name]
        for name in ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR", "HOME")
        if name in os.environ
    }
    environment["PGDATABASE"] = str(database_url)
    try:
        restored = subprocess.run(
            [
                pg_restore,
                "--clean",
                "--if-exists",
                "--no-owner",
                "--exit-on-error",
                os.fspath(archive),
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=1800,
        )
        if restored.returncode != 0:
            raise ProductRecoveryError("PostgreSQL provider restore failed.")
        verified = subprocess.run(
            [
                psql,
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "--command",
                "SELECT count(*) FROM goose_db_version WHERE is_applied = true;",
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=120,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProductRecoveryError("PostgreSQL provider restore failed.") from exc
    try:
        migration_count = int(verified.stdout.strip())
    except (AttributeError, ValueError):
        migration_count = 0
    if verified.returncode != 0 or migration_count < 1:
        raise ProductRecoveryError(
            "PostgreSQL restore did not retain applied migration evidence."
        )


def _copy_verified(source: Path, target: Path) -> None:
    if source.is_symlink():
        raise ProductRecoveryError("Recovery datasets may not contain symlink roots.")
    if source.is_dir():
        for child in source.rglob("*"):
            if child.is_symlink() or not (child.is_dir() or child.is_file()):
                raise ProductRecoveryError("Recovery datasets may contain only real files and directories.")
        shutil.copytree(source, target, symlinks=False)
    elif source.is_file():
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source, target, follow_symlinks=False)
    else:
        raise ProductRecoveryError("Recovery dataset source has an unsupported type.")


def _contained_recovery_path(root: Path, relative: str) -> Path:
    relative_path = PurePosixPath(relative)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise ProductRecoveryError("Recovery evidence contains an unsafe path.")
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ProductRecoveryError("Recovery evidence root is unavailable or unsafe.")
    resolved_root = root.resolve()
    cursor = root
    for part in relative_path.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ProductRecoveryError(
                "Recovery evidence path contains a symlink."
            )
    try:
        cursor.resolve(strict=True).relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise ProductRecoveryError(
            "Recovery evidence path escapes its managed root."
        ) from exc
    return cursor


def _restored_dataset_path(
    root: Path, contract: Mapping[str, Any]
) -> Path:
    if contract["kind"] == "postgresql":
        return Path(root) / "providers" / _slug(str(contract["id"]))
    if contract["binding"] == "stack-owned":
        relative = contract.get("path")
        if not isinstance(relative, str):
            raise ProductRecoveryError(
                "Stack-owned recovery dataset has no managed path."
            )
        return Path(root) / "data" / PurePosixPath(relative)
    return Path(root) / "providers" / _slug(str(contract["id"]))


def _load_backup_manifest(
    root: Path,
    bundle: ProductOperationsBundle,
    *,
    expected_backup_id: Optional[str] = None,
    expected_revision: Optional[Revision] = None,
) -> Mapping[str, Any]:
    if root.is_symlink() or not root.is_dir():
        raise ProductRecoveryError("Product backup root is unavailable or unsafe.")
    manifest = _read_json(root / "backup-manifest.json")
    required = {
        "schema_version", "kind", "backup_id", "product_id", "release_id",
        "bundle_digest", "recovery_contract_digest", "recovery_document_digest",
        "revision_id", "revision_digest", "created_at", "datasets", "inputs_redacted",
    }
    if (
        set(manifest) != required
        or manifest.get("schema_version") != 1
        or manifest.get("kind") != "ophelia.product-backup"
        or not isinstance(manifest.get("backup_id"), str)
        or _RECOVERY_ID.fullmatch(str(manifest.get("backup_id"))) is None
        or not isinstance(manifest.get("revision_id"), str)
        or not _is_digest(manifest.get("revision_digest"))
        or not isinstance(manifest.get("created_at"), str)
        or not manifest.get("created_at")
        or manifest.get("inputs_redacted") is not True
    ):
        raise ProductRecoveryError("Product backup manifest is malformed.")
    if (
        manifest["product_id"] != bundle.product_id
        or manifest["release_id"] != bundle.release_id
        or manifest["bundle_digest"] != bundle.bundle_digest
        or manifest["recovery_contract_digest"]
        != bundle.recovery["contract_digest"]
        or manifest["recovery_document_digest"]
        != bundle.document_digests["recovery-contract"]
    ):
        raise ProductRecoveryError("Product backup does not bind the active recovery contract.")
    if (
        expected_backup_id is not None
        and manifest["backup_id"] != expected_backup_id
    ):
        raise ProductRecoveryError("Product backup identity does not match the request.")
    if expected_revision is not None and (
        manifest["revision_id"] != expected_revision.revision_id
        or manifest["revision_digest"] != expected_revision.content_digest()
    ):
        raise ProductRecoveryError(
            "Product backup does not bind the active product revision."
        )
    if not isinstance(manifest["datasets"], list):
        raise ProductRecoveryError("Product backup datasets are malformed.")
    contracts = {
        str(item["id"]): item
        for item in bundle.recovery["datasets"]
        if item["backup"] != "excluded"
    }
    required_ids = {
        identifier
        for identifier, contract in contracts.items()
        if contract["backup"] == "required"
    }
    seen_ids = set()
    seen_paths = set()
    for item in manifest["datasets"]:
        if (
            not isinstance(item, dict)
            or set(item)
            != {"id", "kind", "relative_path", "digest", "validation_checks"}
            or item.get("id") not in contracts
            or item["id"] in seen_ids
            or not _is_digest(item.get("digest"))
        ):
            raise ProductRecoveryError("Product backup dataset evidence is malformed.")
        contract = contracts[str(item["id"])]
        expected_path = f"datasets/{_slug(str(item['id']))}"
        if (
            item.get("kind") != contract["kind"]
            or item.get("relative_path") != expected_path
            or item.get("validation_checks") != contract["restore_validations"]
            or expected_path in seen_paths
        ):
            raise ProductRecoveryError(
                "Product backup dataset evidence differs from the recovery contract."
            )
        path = _contained_recovery_path(root, expected_path)
        if _path_digest(path) != item["digest"]:
            raise ProductRecoveryError("Product backup dataset bytes do not match evidence.")
        seen_ids.add(str(item["id"]))
        seen_paths.add(expected_path)
    if not required_ids.issubset(seen_ids):
        raise ProductRecoveryError(
            "Product backup is missing a required recovery dataset."
        )
    object_provider_ids = {
        identifier
        for identifier, contract in contracts.items()
        if contract["binding"] == "provider-selected"
        and contract["kind"] in {"object-storage", "filesystem-or-object-store"}
    }
    object_provider_digests = {
        str(item["digest"])
        for item in manifest["datasets"]
        if item["id"] in object_provider_ids
    }
    if len(object_provider_digests) > 1:
        raise ProductRecoveryError(
            "Product backup object-provider snapshots do not share one identity."
        )
    return manifest


def _verified_backup_evidence(
    root: Path,
    bundle: ProductOperationsBundle,
    *,
    expected_backup_id: str,
    expected_revision: Revision,
    runtime_root: Path,
) -> Tuple[Mapping[str, Any], str]:
    """Validate backup bytes and their successful journal-correlated receipt."""

    manifest = _load_backup_manifest(
        root,
        bundle,
        expected_backup_id=expected_backup_id,
        expected_revision=expected_revision,
    )
    manifest_digest = _digest_file(Path(root) / "backup-manifest.json")
    expected_state_digest = canonical_digest(
        {
            "manifest_digest": manifest_digest,
            "dataset_digests": sorted(
                str(item["digest"]) for item in manifest["datasets"]
            ),
            "application_resumed": True,
        }
    )
    receipts_root = Path(runtime_root) / "receipts" / "product"
    if receipts_root.is_symlink() or not receipts_root.is_dir():
        raise ProductRecoveryError(
            "Product backup has no safe correlated receipt root."
        )
    journal = SQLiteOperationJournal.beneath_runtime_root(runtime_root)
    for receipt_path in receipts_root.glob("*.json"):
        try:
            value = _read_json(receipt_path)
            terminal_value = value.get("ophelia_receipt")
            if not isinstance(terminal_value, dict):
                continue
            operation_id = terminal_value.get("operation_id")
            receipt_id = terminal_value.get("receipt_id")
            if (
                not isinstance(operation_id, str)
                or not isinstance(receipt_id, str)
                or receipt_path.name != f"{receipt_id}.json"
            ):
                continue
            terminal = journal.receipt(operation_id)
            if terminal is None:
                continue
            approval = journal.approved_plan(operation_id)
            if (
                terminal.outcome is not ReceiptOutcome.SUCCEEDED
                or terminal.operation != "backup.apply"
                or terminal.effect is not ReceiptEffect.BACKUP_CREATED
                or terminal.desired_revision_id
                != expected_revision.revision_id
                or terminal.desired_revision_digest
                != expected_revision.content_digest()
                or terminal.verification.observed_state_digest
                != expected_state_digest
            ):
                continue
            expected = _recovery_receipt(
                bundle,
                terminal,
                approval,
                backup_id=expected_backup_id,
                backup_manifest_digest=manifest_digest,
                backup_path=root,
            )
            if value == expected:
                return manifest, manifest_digest
        except (OSError, ValueError, ProductRecoveryError):
            continue
    raise ProductRecoveryError(
        "Product backup has no successful journal-correlated receipt."
    )


def _load_restore_report(
    root: Path,
    bundle: ProductOperationsBundle,
    revision: Revision,
    backup_id: str,
    drill_id: str,
    *,
    expected_dataset_ids: set[str],
    expected_backup_manifest_digest: str,
) -> Mapping[str, Any]:
    if root.is_symlink() or not root.is_dir():
        raise ProductRecoveryError("Restore drill root is unavailable or unsafe.")
    report = _read_json(root / "restore-report.json")
    required = {
        "schema_version",
        "kind",
        "drill_id",
        "backup_id",
        "product_id",
        "bundle_digest",
        "backup_manifest_digest",
        "revision_digest",
        "datasets",
        "application_health_digest",
        "status",
        "active_runtime_modified",
        "completed_at",
        "inputs_redacted",
    }
    if (
        set(report) != required
        or report.get("schema_version") != 1
        or report.get("kind") != "ophelia.product-restore-drill"
        or report.get("drill_id") != drill_id
        or report.get("backup_id") != backup_id
        or report.get("product_id") != bundle.product_id
        or report.get("bundle_digest") != bundle.bundle_digest
        or report.get("backup_manifest_digest")
        != expected_backup_manifest_digest
        or report.get("revision_digest") != revision.content_digest()
        or report.get("status") != "succeeded"
        or report.get("active_runtime_modified") is not False
        or report.get("inputs_redacted") is not True
        or not _is_digest(report.get("application_health_digest"))
        or not isinstance(report.get("datasets"), list)
    ):
        raise ProductRecoveryError("Restore drill report is malformed or mismatched.")
    contracts = {item["id"]: item for item in bundle.recovery["datasets"]}
    seen = set()
    for item in report["datasets"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "digest"}
            or item.get("id") not in contracts
            or item["id"] in seen
            or not _is_digest(item.get("digest"))
        ):
            raise ProductRecoveryError("Restore drill dataset evidence is malformed.")
        seen.add(item["id"])
        contract = contracts[item["id"]]
        target = _restored_dataset_path(root, contract)
        if _path_digest(target) != item["digest"]:
            raise ProductRecoveryError("Restore drill dataset bytes differ from its report.")
    if seen != expected_dataset_ids:
        raise ProductRecoveryError(
            "Restore drill report does not cover the exact backup dataset set."
        )
    return report


def _remove_owned_temporary(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    _remove_owned_tree(path)


def _remove_owned_tree(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ProductRecoveryError("Managed recovery work path has an unsafe type.")
    for child in path.rglob("*"):
        if child.is_symlink() or not (child.is_dir() or child.is_file()):
            raise ProductRecoveryError("Managed recovery work tree has an unsafe type.")
        try:
            child.chmod(0o700 if child.is_dir() else 0o600)
        except OSError as exc:
            raise ProductRecoveryError("Managed recovery work tree is not writable.") from exc
    shutil.rmtree(path)


def _begin_recovery_operation(
    operation_name: str,
    bundle: ProductOperationsBundle,
    revision: Revision,
    host_id: str,
    confirmation: str,
    identity: str,
    runtime_root: Path,
    owner_id: str,
):
    actor = Actor(
        actor_id=f"actor_local-{os.geteuid()}",
        source="product-cli",
        authenticated_by="unix-peer-credentials",
    )
    suffix = canonical_digest(
        {
            "operation": operation_name,
            "bundle_digest": bundle.bundle_digest,
            "revision_digest": revision.content_digest(),
            "identity": identity,
        }
    )[7:]
    request = OperationRequest(
        request_id="request_recovery-" + suffix[:24],
        operation=operation_name,
        host_id=host_id,
        app=revision.app,
        environment=revision.environment,
        revision_id=revision.revision_id,
        revision_digest=revision.content_digest(),
        idempotency_key=f"product:{operation_name}:{bundle.bundle_digest}:{identity}",
    )
    created_at = _utc_now()
    steps = tuple(
        PlanStep(
            order=index,
            phase=phase,
            mutates_runtime=mutates,
            desired_effect_digest=canonical_digest(
                {
                    "operation": operation_name,
                    "phase": phase.value,
                    "revision_digest": revision.content_digest(),
                    "identity": identity,
                }
            ),
            compensation=compensation,
        )
        for index, (phase, mutates, compensation) in enumerate(
            (
                (PlanPhase.PREFLIGHT, False, CompensationAction.NONE),
                (PlanPhase.COMMIT, True, CompensationAction.RECOVER_FROM_JOURNAL),
                (PlanPhase.EMIT_RECEIPT, True, CompensationAction.RECOVER_FROM_JOURNAL),
            ),
            start=1,
        )
    )
    plan = OperationPlan.create(
        request=request,
        manifest_digest=revision.manifest_digest,
        artifact_digests=revision.artifact_digests,
        observed_state_digest=canonical_digest(
            {"active_revision_digest": revision.content_digest()}
        ),
        policy_digest=canonical_digest(
            {
                "policy": "product-recovery-v1",
                "recovery_contract_digest": bundle.recovery["contract_digest"],
            }
        ),
        steps=steps,
        blocker_codes=(),
        created_at=created_at,
    )
    expires = (
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        + timedelta(hours=2)
    ).isoformat().replace("+00:00", "Z")
    approval = ApprovedPlanRef.bind(
        plan,
        actor_id=actor.actor_id,
        decision_id="decision_recovery-" + suffix[24:48],
        authorization_kind=AuthorizationKind.LEGACY_CONFIRMATION_ADAPTER,
        issuer="ophelia-product-cli",
        audience=host_id,
        approved_at=created_at,
        expires_at=expires,
        approval_nonce=confirmation,
    )
    journal = SQLiteOperationJournal.beneath_runtime_root(runtime_root)
    operation = journal.accept(actor, request, approval)
    approval = journal.approved_plan(operation.operation_id)
    existing = journal.receipt(operation.operation_id)
    if existing is not None:
        return operation, approval, journal, None, existing
    fence = journal.acquire_fence(operation.operation_id, owner_id, 300.0)
    return operation, approval, journal, fence, None


def _recovery_terminal_receipt(
    operation_id,
    operation_name,
    approval,
    revision,
    host_id,
    started_at,
    verification,
    effect,
) -> TerminalReceipt:
    return TerminalReceipt(
        receipt_id="receipt_" + uuid.uuid4().hex,
        operation_id=operation_id,
        operation=operation_name,
        plan_id=approval.plan_id,
        plan_digest=approval.plan_digest,
        decision_id=approval.decision_id,
        host_id=host_id,
        app=revision.app,
        environment=revision.environment,
        previous_revision_id=revision.revision_id,
        desired_revision_id=revision.revision_id,
        desired_revision_digest=revision.content_digest(),
        active_revision_id=revision.revision_id,
        active_revision_digest=revision.content_digest(),
        artifact_digests=revision.artifact_digests,
        verification=verification,
        compensation=CompensationResult(
            attempted=False, status=CompensationStatus.NOT_NEEDED
        ),
        effect=effect,
        outcome=ReceiptOutcome.SUCCEEDED,
        started_at=started_at,
        completed_at=_utc_now(),
    )


def _failed_recovery_receipt(
    operation_id,
    operation_name,
    approval,
    revision,
    host_id,
    started_at,
    *,
    compensated: bool,
) -> TerminalReceipt:
    state_digest = canonical_digest(
        {"operation": operation_name, "compensated": compensated}
    )
    return TerminalReceipt(
        receipt_id="receipt_" + uuid.uuid4().hex,
        operation_id=operation_id,
        operation=operation_name,
        plan_id=approval.plan_id,
        plan_digest=approval.plan_digest,
        decision_id=approval.decision_id,
        host_id=host_id,
        app=revision.app,
        environment=revision.environment,
        previous_revision_id=revision.revision_id,
        desired_revision_id=revision.revision_id,
        desired_revision_digest=revision.content_digest(),
        active_revision_id=revision.revision_id if compensated else None,
        active_revision_digest=revision.content_digest() if compensated else None,
        artifact_digests=revision.artifact_digests,
        verification=VerificationResult(
            status=VerificationStatus.FAILED,
            observed_revision_digest=revision.content_digest() if compensated else None,
            observed_state_digest=state_digest,
            observed_at=_utc_now(),
            checks=(
                VerificationCheck(
                    name="product_recovery_operation",
                    status=CheckStatus.FAILED,
                    observed_digest=state_digest,
                ),
            ),
        ),
        compensation=CompensationResult(
            attempted=True,
            status=(
                CompensationStatus.SUCCEEDED
                if compensated
                else CompensationStatus.FAILED
            ),
            restored_revision_id=revision.revision_id if compensated else None,
            restored_revision_digest=revision.content_digest() if compensated else None,
        ),
        effect=ReceiptEffect.NONE,
        outcome=(
            ReceiptOutcome.FAILED_COMPENSATED
            if compensated
            else ReceiptOutcome.FAILED_UNCOMPENSATED
        ),
        started_at=started_at,
        completed_at=_utc_now(),
    )


def _passed_verification(revision: Revision, name: str, state_digest: str) -> VerificationResult:
    return VerificationResult(
        status=VerificationStatus.PASSED,
        observed_revision_digest=revision.content_digest(),
        observed_state_digest=state_digest,
        observed_at=_utc_now(),
        checks=(
            VerificationCheck(
                name=name,
                status=CheckStatus.PASSED,
                observed_digest=state_digest,
            ),
        ),
    )


def _recovery_receipt(
    bundle,
    receipt,
    approval: ApprovedPlanRef,
    **values,
) -> Dict[str, Any]:
    result = {
        "schema_version": 1,
        "kind": "ophelia.product-recovery-receipt",
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
    result.update(_recovery_operation_correlation(receipt, approval))
    result.update({key: os.fspath(value) if isinstance(value, Path) else value for key, value in values.items()})
    return result


def _recovery_operation_correlation(
    receipt: TerminalReceipt,
    approval: ApprovedPlanRef,
) -> Dict[str, str]:
    if (
        receipt.plan_id != approval.plan_id
        or receipt.plan_digest != approval.plan_digest
        or receipt.decision_id != approval.decision_id
    ):
        raise ProductRecoveryError(
            "Terminal recovery receipt does not match its durable approval."
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


def _persist_recovery_receipt(runtime_root: Path, value: Mapping[str, Any]) -> Path:
    receipt_id = value["ophelia_receipt"]["receipt_id"]
    root = Path(runtime_root) / "receipts" / "product"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = root / f"{receipt_id}.json"
    _write_json(target, value, exclusive=True)
    return target


def _load_recovery_receipt(runtime_root: Path, receipt_id: str) -> Dict[str, Any]:
    value = _read_json(Path(runtime_root) / "receipts" / "product" / f"{receipt_id}.json")
    return dict(value)


def _existing_backup_receipt(
    runtime_root: Path,
    bundle: ProductOperationsBundle,
    revision: Revision,
    backup_id: str,
    backup_root: Path,
    receipt: TerminalReceipt,
    approval: ApprovedPlanRef,
) -> Dict[str, Any]:
    values: Dict[str, Any] = {"backup_id": backup_id}
    if receipt.outcome is ReceiptOutcome.SUCCEEDED:
        _load_backup_manifest(
            backup_root,
            bundle,
            expected_backup_id=backup_id,
            expected_revision=revision,
        )
        values.update(
            {
                "backup_manifest_digest": _digest_file(
                    backup_root / "backup-manifest.json"
                ),
                "backup_path": backup_root,
            }
        )
    correlated = _recovery_receipt(bundle, receipt, approval, **values)
    return _reconcile_recovery_receipt(runtime_root, correlated)


def _existing_restore_receipt(
    runtime_root: Path,
    bundle: ProductOperationsBundle,
    revision: Revision,
    backup_id: str,
    drill_id: str,
    drill_root: Path,
    receipt: TerminalReceipt,
    approval: ApprovedPlanRef,
) -> Dict[str, Any]:
    values: Dict[str, Any] = {
        "backup_id": backup_id,
        "restore_drill_id": drill_id,
    }
    if receipt.outcome is ReceiptOutcome.SUCCEEDED:
        backup_root = (
            Path(runtime_root)
            / "backups"
            / "product"
            / _slug(bundle.product_id)
            / backup_id
        )
        backup_manifest = _load_backup_manifest(
            backup_root,
            bundle,
            expected_backup_id=backup_id,
            expected_revision=revision,
        )
        _load_restore_report(
            drill_root,
            bundle,
            revision,
            backup_id,
            drill_id,
            expected_dataset_ids={
                str(item["id"]) for item in backup_manifest["datasets"]
            },
            expected_backup_manifest_digest=_digest_file(
                backup_root / "backup-manifest.json"
            ),
        )
        values.update(
            {
                "backup_manifest_digest": _digest_file(
                    backup_root / "backup-manifest.json"
                ),
                "restore_report_digest": _digest_file(
                    drill_root / "restore-report.json"
                ),
                "restore_path": drill_root,
            }
        )
    correlated = _recovery_receipt(bundle, receipt, approval, **values)
    return _reconcile_recovery_receipt(runtime_root, correlated)


def _reconcile_recovery_receipt(
    runtime_root: Path, expected: Mapping[str, Any]
) -> Dict[str, Any]:
    receipt_id = str(expected["ophelia_receipt"]["receipt_id"])
    target = (
        Path(runtime_root)
        / "receipts"
        / "product"
        / f"{receipt_id}.json"
    )
    if target.exists() or target.is_symlink():
        actual = _load_recovery_receipt(runtime_root, receipt_id)
        if actual != expected:
            raise ProductRecoveryError(
                "Correlated recovery receipt differs from terminal evidence."
            )
        return actual
    _persist_recovery_receipt(runtime_root, expected)
    return dict(expected)


def _recovery_confirmation_payload(
    operation,
    bundle,
    host_id,
    revision_id,
    revision_digest,
    identity,
    datasets,
    *,
    backup_id=None,
    configuration_names=(),
    configuration_digest=None,
    active_generation=None,
    active_operation_id=None,
    backup_manifest_digest=None,
    backup_dataset_digests=None,
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "ophelia.product-recovery-confirmation",
        "operation": operation,
        "host_id": host_id,
        "product_id": bundle.product_id,
        "bundle_digest": bundle.bundle_digest,
        "recovery_contract_digest": bundle.recovery["contract_digest"],
        "revision_id": revision_id,
        "revision_digest": revision_digest,
        "identity": identity,
        "backup_id": backup_id,
        "configuration_names": list(configuration_names),
        "configuration_digest": configuration_digest,
        "active_generation": active_generation,
        "active_operation_id": active_operation_id,
        "backup_manifest_digest": backup_manifest_digest,
        "backup_dataset_digests": dict(
            sorted((backup_dataset_digests or {}).items())
        ),
        "dataset_bindings_digest": canonical_digest(
            {key: os.fspath(value) for key, value in sorted(datasets.items())}
        ),
    }


def _require_recovery_material(
    plan: Mapping[str, Any], material: Mapping[str, Any]
) -> None:
    if (
        plan.get("active_revision_id") != material["revision"].revision_id
        or plan.get("active_revision_digest")
        != material["revision"].content_digest()
        or plan.get("active_revision_generation")
        != material["active_generation"]
    ):
        raise ProductRecoveryError(
            "Active product state changed after recovery confirmation."
        )


def _require_confirmation(plan: Mapping[str, Any], supplied: str) -> None:
    expected = plan.get("confirmation_token")
    if not isinstance(expected, str) or not supplied or not hmac.compare_digest(expected, supplied):
        raise ProductRecoveryError("Confirmation token does not match the current recovery plan.")


def _configuration_value_digest(values: Mapping[str, str]) -> str:
    encoded = json.dumps(
        {str(name): value for name, value in sorted(values.items())},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _confirmation_token(key: bytes, value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, raw, hashlib.sha256).hexdigest()


def _recovery_id(value: str, owner: str) -> None:
    if not isinstance(value, str) or _RECOVERY_ID.fullmatch(value) is None:
        raise ProductRecoveryError(f"{owner} is invalid.")


def _verify_sqlite(path: Path) -> None:
    try:
        # Backups can retain WAL journal mode without carrying transient WAL or
        # SHM sidecars. Open the already-existing managed copy read/write so
        # SQLite can initialize or recover those sidecars before integrity
        # verification. mode=rw still refuses to create a missing database.
        connection = sqlite3.connect(_sqlite_existing_rw_uri(path), uri=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ProductRecoveryError("SQLite restore integrity check failed.") from exc
    if result is None or result[0] != "ok":
        raise ProductRecoveryError("SQLite restore integrity check failed.")


def _path_digest(path: Path) -> str:
    if path.is_symlink() or not path.exists():
        raise ProductRecoveryError("Recovery artifact is missing or symlinked.")
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(b"file\0")
        _update_file_digest(digest, path)
    elif path.is_dir():
        digest.update(b"directory\0")
        for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
            if child.is_symlink():
                raise ProductRecoveryError("Recovery artifacts may not contain symlinks.")
            relative = child.relative_to(path).as_posix().encode("utf-8")
            digest.update(relative + b"\0")
            if child.is_dir():
                digest.update(b"directory\0")
            elif child.is_file():
                digest.update(b"file\0")
                _update_file_digest(digest, child)
                digest.update(b"\0")
            else:
                raise ProductRecoveryError("Recovery artifact contains a special file.")
    else:
        raise ProductRecoveryError("Recovery artifact has an unsupported type.")
    return "sha256:" + digest.hexdigest()


def _read_json(path: Path) -> Mapping[str, Any]:
    descriptor = _open_stable_file(path, max_size=4 << 20)
    try:
        metadata = os.fstat(descriptor)
        raw = bytearray()
        while len(raw) <= 4 << 20:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, (4 << 20) + 1 - len(raw)),
            )
            if not chunk:
                break
            raw.extend(chunk)
        _require_stable_file(descriptor, metadata)
        if len(raw) > 4 << 20:
            raise ProductRecoveryError(
                "Recovery evidence is unavailable or unsafe."
            )
        value = json.loads(
            bytes(raw).decode("utf-8"),
            object_pairs_hook=_strict_json_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductRecoveryError("Recovery evidence is malformed.") from exc
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise ProductRecoveryError("Recovery evidence must be an object.")
    return value


def _write_json(path: Path, value: Mapping[str, Any], *, exclusive: bool = False) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ProductRecoveryError("Recovery evidence path is symlinked.")
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if exclusive and path.exists():
        if path.read_bytes() != data:
            raise ProductRecoveryError("Recovery receipt identity collision.")
        return
    temporary = path.parent / ("." + path.name + ".tmp-" + os.urandom(6).hex())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        if exclusive:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.is_symlink() or path.read_bytes() != data:
                    raise ProductRecoveryError("Recovery receipt identity collision.")
        else:
            os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    path.chmod(0o600)
    _sync_directory(path.parent)


def _strict_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ProductRecoveryError(f"Recovery evidence repeats object key: {key}")
        value[key] = item
    return value


def _sync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    _update_file_digest(digest, path)
    return "sha256:" + digest.hexdigest()


def _update_file_digest(digest, path: Path) -> None:
    descriptor = _open_stable_file(path)
    try:
        metadata = os.fstat(descriptor)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        _require_stable_file(descriptor, metadata)
    finally:
        os.close(descriptor)


def _open_stable_file(
    path: Path,
    *,
    max_size: Optional[int] = None,
) -> int:
    path = Path(path)
    try:
        initial = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(initial.st_mode):
            raise ProductRecoveryError(
                "Recovery digest input must be a regular file."
            )
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise ProductRecoveryError(
            "Recovery evidence is unavailable or unsafe."
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not os.path.samestat(initial, metadata)
            or (max_size is not None and metadata.st_size > max_size)
        ):
            raise ProductRecoveryError(
                "Recovery evidence changed while it was opened."
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_stable_file(descriptor: int, initial: os.stat_result) -> None:
    final = os.fstat(descriptor)
    if (
        not os.path.samestat(initial, final)
        or final.st_size != initial.st_size
        or final.st_mtime_ns != initial.st_mtime_ns
        or final.st_ctime_ns != initial.st_ctime_ns
    ):
        raise ProductRecoveryError(
            "Recovery evidence changed while it was read."
        )


def _sqlite_existing_rw_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=rw"


def _is_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
