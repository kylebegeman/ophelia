"""CLI compatibility boundary for the journaled production-static executor."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict

from ..caddy_manager import reload_caddy
from ..domain import (
    CheckStatus,
    OperationRef,
    ReceiptOutcome,
    Revision,
    TerminalReceipt,
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
    canonical_digest,
)
from ..domain._contracts import require_digest, require_entity_id
from ..manifest import Manifest
from ..runtime import DeployMetadata, static_asset_plan, static_runtime_root
from .executor import JournaledExecutor
from .legacy_adapter import LegacyExecutionBundle, build_legacy_static_execution
from .operation_store import SQLiteOperationJournal
from .staging import ConfirmedStaging
from .static_backend import StaticBackendError, StaticRuntimeBackend


CaddyReloader = Callable[..., Dict[str, object]]


@dataclass(frozen=True)
class LegacyStaticExecutionResult:
    operation: OperationRef
    receipt: TerminalReceipt
    app_root: Path
    release_id: str


def supports_journaled_static(manifest: Manifest, runtime_root: Path) -> bool:
    """Return whether this first migration slice models every live side effect."""

    return static_execution_mode(manifest, runtime_root) == "journaled"


def static_execution_mode(manifest: Manifest, runtime_root: Path) -> str:
    """Choose journaled, compatibility, or fail-closed static execution."""

    if manifest.kind != "static" or manifest.environment != "production":
        return "compatibility"

    global_caddy = (
        runtime_root
        / "caddy"
        / "global.d"
        / "ophelia-on-demand-tls.caddy"
    )
    predecessor_state, _, _ = _static_predecessor_state(manifest, runtime_root)
    if predecessor_state == "unsafe" or _path_has_symlink_parent(
        global_caddy, runtime_root
    ):
        return "blocked"
    modeled = (
        manifest.kind == "static"
        and manifest.environment == "production"
        and manifest.static_root is not None
        and not Path(manifest.static_root).is_absolute()
        and manifest.edge.on_demand_tls is None
        and not manifest.addons.postgres
        and not manifest.addons.redis
        and not global_caddy.exists()
        and not global_caddy.is_symlink()
    )
    journal_has_managed_state = False
    if predecessor_state in {"empty", "legacy"}:
        try:
            journal_has_managed_state = _journal_scope_has_managed_state(
                manifest, runtime_root
            )
        except (OSError, sqlite3.Error):
            return "blocked"
    if not modeled:
        if predecessor_state == "managed" or journal_has_managed_state:
            return "blocked"
        return "compatibility"
    if predecessor_state == "legacy":
        if journal_has_managed_state:
            return "blocked"
        return "compatibility"
    return "journaled"


def execute_confirmed_static(
    *,
    confirmed: ConfirmedStaging,
    manifest: Manifest,
    confirmation_token: str,
    runtime_root: Path,
    ophelia_root: Path,
    deploy_metadata: DeployMetadata,
    caddy_reloader: CaddyReloader = reload_caddy,
    blocking_external_verifier: Callable[[], Dict[str, object]] | None = None,
    fault_injector: Callable[[str], None] | None = None,
    owner_id: str | None = None,
) -> LegacyStaticExecutionResult:
    """Run one reviewed production-static candidate through the kernel.

    Caddy validation and reload are part of traffic activation. Their raw output
    is never admitted to the operation journal; only bounded result fields are
    reduced into an evidence digest. The optional verifier is release-blocking;
    warning-only verification belongs after the kernel commit.
    """

    if static_execution_mode(manifest, runtime_root) != "journaled":
        raise StaticBackendError(
            "Static deployment requires side effects or predecessor migration "
            "outside the journaled compatibility slice."
        )

    bundle = build_legacy_static_execution(
        confirmed,
        manifest,
        confirmation_token,
    )
    (
        predecessor_state,
        live_operation_id,
        live_revision_digest,
    ) = _static_predecessor_state(
        manifest, runtime_root
    )
    database_path = runtime_root / "host-state" / "operations.db"
    if predecessor_state == "managed" and (
        database_path.is_symlink() or not database_path.is_file()
    ):
        raise StaticBackendError(
            "Managed static predecessor has no authoritative operation journal."
        )
    journal = SQLiteOperationJournal.beneath_runtime_root(runtime_root)
    _require_reconciled_predecessor(
        journal,
        bundle,
        predecessor_state,
        live_operation_id,
        live_revision_digest,
    )

    def commit_traffic() -> str:
        report = caddy_reloader(
            runtime_root=runtime_root,
            ophelia_root=ophelia_root,
            timeout=30,
            validate_first=True,
        )
        if (
            report.get("ok") is not True
            or report.get("validated") is not True
            or report.get("reloaded") is not True
            or report.get("container_verified") is not True
        ):
            raise StaticBackendError(
                "Caddy validation or reload did not reach a verified success state."
            )
        errors = report.get("errors")
        error_items = errors if isinstance(errors, list) else []
        error_codes = tuple(
            sorted(
                str(item.get("code"))
                for item in error_items
                if isinstance(item, dict) and item.get("code")
            )
        )
        return canonical_digest(
            {
                "kind": report.get("kind"),
                "ok": report.get("ok"),
                "container": report.get("container"),
                "container_id": report.get("container_id"),
                "container_verified": report.get("container_verified"),
                "validated": report.get("validated"),
                "reloaded": report.get("reloaded"),
                "returncode": report.get("returncode"),
                "error_codes": error_codes,
            }
        )

    def backend_factory(execution_input, operation, fence):
        active_verifier = (
            None
            if blocking_external_verifier is None
            else lambda revision, _handle: _verification_result(
                blocking_external_verifier(), revision
            )
        )
        return StaticRuntimeBackend(
            manifest=manifest,
            manifest_path=confirmed.manifest_path,
            candidate_root=confirmed.staging.candidate,
            generated_files=list(confirmed.generated_files),
            runtime_root=runtime_root,
            deploy_metadata=deploy_metadata,
            expected_candidate_digest=confirmed.candidate_digest,
            expected_bundle_hash=confirmed.rendered_bundle_hash,
            expected_baseline_digest=confirmed.baseline_digest,
            host_id=execution_input.plan.host_id,
            operation_id=operation.operation_id,
            owner_id=fence.owner_id,
            fencing_token=fence.fencing_token,
            fault_injector=fault_injector,
            traffic_committer=commit_traffic,
            external_verifier=active_verifier,
        )

    executor = JournaledExecutor(
        journal=journal,
        backend_factory=backend_factory,
    )
    operation = executor.submit(
        bundle.actor,
        bundle.request,
        bundle.approval,
        execution_input=bundle.execution_input,
    )
    receipt = executor.run(
        operation.operation_id,
        owner_id=owner_id or f"cli-{os.getpid()}",
    )
    if not deploy_metadata.release_id:
        raise StaticBackendError("Confirmed static deployment has no release id.")
    if receipt.outcome is ReceiptOutcome.SUCCEEDED:
        try:
            _project_legacy_release(
                journal=journal,
                operation=operation,
                receipt=receipt,
                confirmed=confirmed,
                manifest=manifest,
                runtime_root=runtime_root,
                deploy_metadata=deploy_metadata,
                externally_verified=blocking_external_verifier is not None,
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            raise StaticBackendError(
                "Kernel activation succeeded but compatibility projection failed."
            ) from exc
    return LegacyStaticExecutionResult(
        operation=operation,
        receipt=receipt,
        app_root=runtime_root / "apps" / manifest.app,
        release_id=deploy_metadata.release_id,
    )


def _project_legacy_release(
    *,
    journal: SQLiteOperationJournal,
    operation: OperationRef,
    receipt: TerminalReceipt,
    confirmed: ConfirmedStaging,
    manifest: Manifest,
    runtime_root: Path,
    deploy_metadata: DeployMetadata,
    externally_verified: bool,
) -> None:
    active = journal.active_revision(
        receipt.host_id,
        receipt.app,
        receipt.environment,
    )
    if (
        active is None
        or active.operation_id != operation.operation_id
        or active.revision_digest != receipt.desired_revision_digest
    ):
        raise StaticBackendError(
            "Kernel active state changed before compatibility projection."
        )
    release_id = deploy_metadata.release_id
    if not release_id:
        raise StaticBackendError("Compatibility projection requires a release id.")
    app_root = runtime_root / "apps" / manifest.app
    bundle_root = app_root / "release-bundles" / release_id
    manifest_lock = bundle_root / "manifest.lock.json"
    if (
        _path_has_symlink_parent(manifest_lock, runtime_root)
        or manifest_lock.is_symlink()
        or not manifest_lock.is_file()
    ):
        raise StaticBackendError(
            "Immutable manifest is unavailable for compatibility projection."
        )
    manifest_bytes = manifest_lock.read_bytes()
    lock_path = app_root / ".legacy-projection.lock"
    if _path_has_symlink_parent(lock_path, runtime_root) or lock_path.is_symlink():
        raise StaticBackendError("Compatibility projection lock is unsafe.")
    app_root.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(
        lock_path,
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise StaticBackendError(
                "Compatibility projection lock must be a regular file."
            )
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        active = journal.active_revision(
            receipt.host_id,
            receipt.app,
            receipt.environment,
        )
        if (
            active is None
            or active.operation_id != operation.operation_id
            or active.revision_digest != receipt.desired_revision_digest
        ):
            raise StaticBackendError(
                "Kernel active state changed during compatibility projection."
            )
        existing = _read_projection(app_root / "active_release.json")
        existing_kernel = existing.get("kernel")
        existing_generation = (
            existing_kernel.get("active_generation")
            if isinstance(existing_kernel, dict)
            else None
        )
        if (
            isinstance(existing_generation, int)
            and existing_generation > active.generation
        ):
            raise StaticBackendError(
                "Compatibility projection cannot replace a newer generation."
            )
        existing_release_id = existing.get("release_id")
        if existing_release_id == release_id:
            previous_release_id = existing.get("previous_release_id")
        else:
            previous_release_id = existing_release_id
        verification = (
            {
                "status": "passed",
                "ok": True,
                "verified": True,
                "results": [],
                "source": "kernel_receipt",
                "receipt_id": receipt.receipt_id,
            }
            if externally_verified
            else {"status": "not_run", "ok": None, "results": []}
        )
        static_release = (
            static_runtime_root(runtime_root, manifest)
            / "releases"
            / release_id
        )
        static_plan = static_asset_plan(manifest, manifest_lock, runtime_root)
        static_assets = {
            **static_plan,
            "release_root": str(static_release),
            "runtime_current": str(
                static_runtime_root(runtime_root, manifest) / "current"
            ),
            "release_digest": static_plan.get("current_digest"),
            "file_count": sum(
                1 for path in static_release.rglob("*") if path.is_file()
            ),
            "change": "synced",
        }
        release = {
            "schema_version": 1,
            "app": manifest.app,
            "environment": manifest.environment,
            "kind": manifest.kind,
            "release_id": release_id,
            "previous_release_id": previous_release_id,
            "active_release_id_before": previous_release_id,
            "source_manifest": str(manifest_lock),
            "manifest_path": str(manifest_lock),
            "manifest_hash": hashlib.sha256(manifest_bytes).hexdigest(),
            "rendered_bundle_hash": confirmed.rendered_bundle_hash,
            "candidate_digest": "sha256:" + confirmed.candidate_digest,
            "commit_sha": deploy_metadata.commit_sha,
            "build_time": deploy_metadata.build_time,
            "git_sha": deploy_metadata.commit_sha,
            "images": [],
            "image_digests": [],
            "runtime_env": {
                "OPHELIA_RELEASE_ID": release_id,
                "OPHELIA_COMMIT_SHA": deploy_metadata.commit_sha or "",
                "OPHELIA_BUILD_TIME": deploy_metadata.build_time or "",
            },
            "static_assets": static_assets,
            "generated_files": [
                str(path) for path in sorted(confirmed.generated_files)
            ],
            "support_files": [],
            "bundle_path": str(bundle_root),
            "runtime_path": str(app_root),
            "deployed_at": receipt.completed_at,
            "deployed_by": "ophelia-kernel",
            "source": "kernel-journal",
            "applied": True,
            "verified": True if externally_verified else None,
            "apply": {
                "status": "applied",
                "ok": True,
                "applied": True,
                "phase": "kernel_commit",
                "phases": [],
                "operation_id": operation.operation_id,
                "receipt_id": receipt.receipt_id,
            },
            "verification": verification,
            "active": True,
            "activated_at": receipt.completed_at,
            "kernel": {
                "authority": "operations.db",
                "operation_id": operation.operation_id,
                "receipt_id": receipt.receipt_id,
                "revision_id": receipt.desired_revision_id,
                "revision_digest": receipt.desired_revision_digest,
                "active_generation": active.generation,
            },
        }
        release_bytes = (
            json.dumps(release, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        if len(release_bytes) > 65536:
            raise StaticBackendError(
                "Compatibility projection exceeds its bounded record size."
            )
        releases_root = app_root / "releases"
        releases_root.mkdir(parents=True, exist_ok=True)
        _atomic_projection_write(
            app_root / "manifest.lock.json", manifest_bytes, runtime_root
        )
        _atomic_projection_write(
            releases_root / f"{release_id}.json", release_bytes, runtime_root
        )
        _atomic_projection_write(
            app_root / "release.json", release_bytes, runtime_root
        )
        _atomic_projection_write(
            app_root / "active_release.json", release_bytes, runtime_root
        )
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _read_projection(path: Path) -> dict:
    if path.is_symlink():
        raise StaticBackendError("Compatibility projection must not be a symlink.")
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_size > 65536:
        raise StaticBackendError("Compatibility projection is malformed.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StaticBackendError("Compatibility projection is malformed.") from exc
    if not isinstance(payload, dict):
        raise StaticBackendError("Compatibility projection is malformed.")
    return payload


def _atomic_projection_write(
    path: Path, content: bytes, trusted_root: Path
) -> None:
    if _path_has_symlink_parent(path, trusted_root) or path.is_symlink():
        raise StaticBackendError("Compatibility projection target is unsafe.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(
        temp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory_fd = os.open(
            path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _static_predecessor_state(
    manifest: Manifest, runtime_root: Path
) -> tuple[str, str | None, str | None]:
    current = runtime_root / "static" / manifest.app / "current"
    caddy = runtime_root / "caddy" / "sites.d" / f"{manifest.app}.caddy"
    if _path_has_symlink_parent(
        current, runtime_root
    ) or _path_has_symlink_parent(caddy, runtime_root):
        return "unsafe", None, None
    current_present = current.exists() or current.is_symlink()
    caddy_present = caddy.exists() or caddy.is_symlink()
    if not current_present and not caddy_present:
        return "empty", None, None
    if (
        not current.is_symlink()
        or caddy.is_symlink()
        or not caddy.is_file()
    ):
        return "unsafe", None, None
    target = current.readlink()
    if (
        target.is_absolute()
        or len(target.parts) != 2
        or target.parts[0] != "releases"
        or target.parts[1] in {"", ".", ".."}
        or "\\" in target.parts[1]
    ):
        return "unsafe", None, None
    record = (
        runtime_root
        / "apps"
        / manifest.app
        / "static-revisions"
        / f"{target.parts[1]}.json"
    )
    if _path_has_symlink_parent(record, runtime_root):
        return "unsafe", None, None
    if record.is_symlink():
        return "unsafe", None, None
    if not record.exists():
        return "legacy", None, None
    if not record.is_file():
        return "unsafe", None, None
    try:
        if record.stat().st_size > 65536:
            return "unsafe", None, None
        payload = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unsafe", None, None
    operation_id = (
        payload.get("operation_id") if isinstance(payload, dict) else None
    )
    revision_digest = (
        payload.get("revision_digest") if isinstance(payload, dict) else None
    )
    if not isinstance(operation_id, str) or not isinstance(revision_digest, str):
        return "unsafe", None, None
    try:
        require_entity_id(operation_id, "operation", "live_operation_id")
        require_digest(revision_digest, "live_revision_digest")
    except ValueError:
        return "unsafe", None, None
    return "managed", operation_id, revision_digest


def _journal_scope_has_managed_state(
    manifest: Manifest, runtime_root: Path
) -> bool:
    database = runtime_root / "host-state" / "operations.db"
    if not database.exists() and not database.is_symlink():
        return False
    if (
        _path_has_symlink_parent(database, runtime_root)
        or database.is_symlink()
        or not database.is_file()
    ):
        raise sqlite3.DatabaseError("operation journal has an unsafe type")
    connection = sqlite3.connect(
        f"{database.resolve(strict=True).as_uri()}?mode=ro",
        uri=True,
    )
    try:
        active = connection.execute(
            """
            SELECT 1
            FROM active_revisions
            WHERE app = ? AND environment = ?
            LIMIT 1
            """,
            (manifest.app, manifest.environment or "unknown"),
        ).fetchone()
        nonterminal = connection.execute(
            """
            SELECT 1
            FROM operations AS o
            LEFT JOIN terminal_receipts AS r
              ON r.operation_id = o.operation_id
            WHERE o.app = ? AND o.environment = ?
              AND r.operation_id IS NULL
            LIMIT 1
            """,
            (manifest.app, manifest.environment or "unknown"),
        ).fetchone()
    finally:
        connection.close()
    return active is not None or nonterminal is not None


def _path_has_symlink_parent(path: Path, trusted_root: Path) -> bool:
    try:
        relative = path.relative_to(trusted_root)
    except ValueError:
        return True
    if trusted_root.is_symlink():
        return True
    current = trusted_root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _require_reconciled_predecessor(
    journal: SQLiteOperationJournal,
    bundle: LegacyExecutionBundle,
    predecessor_state: str,
    live_operation_id: str | None,
    live_revision_digest: str | None,
) -> None:
    active = journal.active_revision(
        bundle.plan.host_id,
        bundle.plan.app,
        bundle.plan.environment,
    )
    if predecessor_state == "empty":
        if active is not None:
            raise StaticBackendError(
                "Operation journal identifies an active revision but live static state is empty."
            )
        return
    if (
        predecessor_state != "managed"
        or live_operation_id is None
        or live_revision_digest is None
    ):
        raise StaticBackendError(
            "Static predecessor is outside the canonical revision layout."
        )
    if (
        active is not None
        and active.operation_id == live_operation_id
        and active.revision_digest == live_revision_digest
    ):
        return
    if _journal_has_exact_recoverable_execution(
        journal,
        bundle.execution_input.input_digest,
        live_operation_id,
        live_revision_digest,
        bundle.plan.host_id,
        bundle.plan.app,
        bundle.plan.environment,
    ):
        return
    raise StaticBackendError(
        "Live static revision does not reconcile with active or recoverable journal state."
    )


def _journal_has_exact_recoverable_execution(
    journal: SQLiteOperationJournal,
    input_digest: str,
    operation_id: str,
    revision_digest: str,
    host_id: str,
    app: str,
    environment: str,
) -> bool:
    try:
        if journal.receipt(operation_id) is not None:
            return False
        execution_input = journal.load_execution_input(operation_id)
    except (KeyError, OSError, RuntimeError, ValueError, sqlite3.Error):
        return False
    return (
        execution_input.input_digest == input_digest
        and execution_input.revision.content_digest() == revision_digest
        and execution_input.plan.host_id == host_id
        and execution_input.plan.app == app
        and execution_input.plan.environment == environment
    )


def _verification_result(
    payload: Dict[str, object], revision: Revision
) -> VerificationResult:
    result_values = payload.get("results")
    results = result_values if isinstance(result_values, list) else []
    checks = []
    for index, item in enumerate(results, start=1):
        if not isinstance(item, dict):
            continue
        observed_digest = canonical_digest(
            {
                "ok": item.get("ok"),
                "type": item.get("type"),
                "phase": item.get("phase"),
                "status_code": item.get("status_code"),
                "returncode": item.get("returncode"),
                "error_kind": item.get("error_kind"),
            }
        )
        checks.append(
            VerificationCheck(
                name=f"external_{index}",
                status=(
                    CheckStatus.PASSED
                    if item.get("ok") is True
                    else CheckStatus.FAILED
                ),
                observed_digest=observed_digest,
            )
        )
    passed = payload.get("ok") is True
    if not checks:
        checks.append(
            VerificationCheck(
                name="external",
                status=CheckStatus.PASSED if passed else CheckStatus.FAILED,
                observed_digest=canonical_digest(
                    {
                        "ok": payload.get("ok"),
                        "status": payload.get("status"),
                        "phase": payload.get("phase"),
                        "attempt": payload.get("attempt"),
                        "attempts": payload.get("attempts"),
                        "failure_mode": payload.get("failure_mode"),
                    }
                ),
            )
        )
    observed_state_digest = canonical_digest(
        {
            "ok": passed,
            "check_digests": tuple(check.observed_digest for check in checks),
        }
    )
    return VerificationResult(
        status=(
            VerificationStatus.PASSED
            if passed
            else VerificationStatus.FAILED
        ),
        observed_revision_digest=(
            revision.content_digest() if passed else None
        ),
        observed_state_digest=observed_state_digest,
        observed_at=datetime.now(timezone.utc).isoformat(),
        checks=tuple(checks),
    )
