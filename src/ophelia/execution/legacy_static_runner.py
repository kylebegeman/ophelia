"""CLI compatibility boundary for the journaled production-static executor."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict

from ..caddy_manager import reload_caddy
from ..domain import (
    CheckStatus,
    OperationRef,
    Revision,
    TerminalReceipt,
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
    canonical_digest,
)
from ..manifest import Manifest
from ..runtime import DeployMetadata
from .executor import JournaledExecutor
from .legacy_adapter import build_legacy_static_execution
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

    global_caddy = (
        runtime_root
        / "caddy"
        / "global.d"
        / "ophelia-on-demand-tls.caddy"
    )
    return (
        manifest.kind == "static"
        and manifest.environment == "production"
        and manifest.static_root is not None
        and not Path(manifest.static_root).is_absolute()
        and manifest.edge.on_demand_tls is None
        and not manifest.addons.postgres
        and not manifest.addons.redis
        and not global_caddy.exists()
        and not global_caddy.is_symlink()
        and _static_predecessor_is_empty_or_managed(manifest, runtime_root)
    )


def execute_confirmed_static(
    *,
    confirmed: ConfirmedStaging,
    manifest: Manifest,
    confirmation_token: str,
    runtime_root: Path,
    ophelia_root: Path,
    deploy_metadata: DeployMetadata,
    caddy_reloader: CaddyReloader = reload_caddy,
    external_verifier: Callable[[], Dict[str, object]] | None = None,
    owner_id: str | None = None,
) -> LegacyStaticExecutionResult:
    """Run one reviewed production-static candidate through the kernel.

    Caddy validation and reload are part of traffic activation. Their raw output
    is never admitted to the operation journal; only bounded result fields are
    reduced into an evidence digest.
    """

    if not supports_journaled_static(manifest, runtime_root):
        raise StaticBackendError(
            "Static deployment requires side effects or predecessor migration "
            "outside the journaled compatibility slice."
        )

    bundle = build_legacy_static_execution(
        confirmed,
        manifest,
        confirmation_token,
    )
    journal = SQLiteOperationJournal.beneath_runtime_root(runtime_root)

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
                "validated": report.get("validated"),
                "reloaded": report.get("reloaded"),
                "returncode": report.get("returncode"),
                "error_codes": error_codes,
            }
        )

    def backend_factory(execution_input, operation, fence):
        active_verifier = (
            None
            if external_verifier is None
            else lambda revision, _handle: _verification_result(
                external_verifier(), revision
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
    return LegacyStaticExecutionResult(
        operation=operation,
        receipt=receipt,
        app_root=runtime_root / "apps" / manifest.app,
        release_id=deploy_metadata.release_id,
    )


def _static_predecessor_is_empty_or_managed(
    manifest: Manifest, runtime_root: Path
) -> bool:
    current = runtime_root / "static" / manifest.app / "current"
    caddy = runtime_root / "caddy" / "sites.d" / f"{manifest.app}.caddy"
    current_present = current.exists() or current.is_symlink()
    caddy_present = caddy.exists() or caddy.is_symlink()
    if not current_present and not caddy_present:
        return True
    if (
        not current.is_symlink()
        or caddy.is_symlink()
        or not caddy.is_file()
    ):
        return False
    target = current.readlink()
    if (
        target.is_absolute()
        or len(target.parts) != 2
        or target.parts[0] != "releases"
        or target.parts[1] in {"", ".", ".."}
        or "\\" in target.parts[1]
    ):
        return False
    record = (
        runtime_root
        / "apps"
        / manifest.app
        / "static-revisions"
        / f"{target.parts[1]}.json"
    )
    if record.is_symlink() or not record.is_file():
        return False
    try:
        payload = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    revision_digest = (
        payload.get("revision_digest") if isinstance(payload, dict) else None
    )
    if not isinstance(revision_digest, str):
        return False

    database = runtime_root / "host-state" / "operations.db"
    if database.is_symlink() or not database.is_file():
        return False
    try:
        connection = sqlite3.connect(
            f"{database.resolve(strict=True).as_uri()}?mode=ro",
            uri=True,
        )
        try:
            rows = connection.execute(
                """
                SELECT revision_digest
                FROM active_revisions
                WHERE app = ? AND environment = ?
                """,
                (manifest.app, manifest.environment or "unknown"),
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return False
    return len(rows) == 1 and rows[0][0] == revision_digest


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
