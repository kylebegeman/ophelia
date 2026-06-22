"""Typed remediations for known readiness/validation finding codes.

Readiness aggregates blockers and warnings from several sub-reports
(:func:`ophelia.portability.pack_validation_report`,
:func:`ophelia.portability.env_shape_diff_report`,
:func:`ophelia.portability.backup_status_report`,
:func:`ophelia.conflicts.scan_conflicts`, plus direct readiness checks). Each of
those emits an ``issue()`` dict with a stable ``code``. This module maps those
codes to a concrete :class:`ophelia.findings.Remediation` whose ``commands`` are
typed Ophelia commands (``plan`` forms wherever a mutation is implied), never raw
shell.

The codes here were enumerated from the literal ``code`` strings emitted by those
functions (see ``portability.py`` and ``conflicts.py``); unknown codes return
``None`` so the caller simply leaves the finding un-enriched.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from .findings import Remediation

PACK_SPEC_DOC = "docs/portable-app-pack-spec.md"

# A builder takes the resolved app id and environment (already string-safe) and
# returns the Remediation for its code. Keeping them as builders lets every
# command be parameterized with the concrete app/environment.
_RemediationBuilder = Callable[[str, str], Remediation]


def _readiness_command(app: str, environment: str) -> str:
    return f"ship app readiness {app} --environment {environment} --json"


def _restore_drill_command(app: str, environment: str) -> str:
    return f"ship app restore-drill plan {app} --environment {environment} --json"


def _export_plan_command(app: str, environment: str) -> str:
    return f"ship app export plan {app} --environment {environment} --json"


def _backup_status_command(app: str, environment: str) -> str:
    return f"ship backup status {app} --environment {environment} --json"


def _pack_explain_command(app: str, environment: str) -> str:
    return f"ship pack explain {app}.ophelia.yml --json"


def _pack_validate_command(app: str, environment: str) -> str:
    return f"ship pack validate {app}.ophelia.yml --json"


_REMEDIATIONS: Dict[str, _RemediationBuilder] = {
    # --- direct readiness checks -------------------------------------------------
    "restore_drill_missing": lambda app, environment: Remediation(
        summary="Plan a restore drill from the latest export bundle, then run it to record a receipt.",
        commands=[_restore_drill_command(app, environment), _export_plan_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        requires_human_approval=True,
    ),
    "release_missing": lambda app, environment: Remediation(
        summary="Deploy or record an active release so readiness has release metadata to attest.",
        commands=[_readiness_command(app, environment)],
        docs=[PACK_SPEC_DOC],
    ),
    # --- env / secrets (env_shape_diff_report) -----------------------------------
    "env_key_missing": lambda app, environment: Remediation(
        summary="Populate the missing required env key in the runtime env file; values stay redacted.",
        commands=[_readiness_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "runtime/apps/<app>/env", "action": "set-required-key"},
        requires_human_approval=True,
    ),
    "env_key_placeholder": lambda app, environment: Remediation(
        summary="Replace the placeholder value for the required env key with a real secret; values stay redacted.",
        commands=[_readiness_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "runtime/apps/<app>/env", "action": "replace-placeholder"},
        requires_human_approval=True,
    ),
    # --- backups (backup_status_report) ------------------------------------------
    "backup_not_fresh": lambda app, environment: Remediation(
        summary="Create a fresh export bundle so the required backup is recent, then re-check status.",
        commands=[_export_plan_command(app, environment), _backup_status_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        requires_human_approval=True,
    ),
    "backup_contract_missing": lambda app, environment: Remediation(
        summary="Declare a `data.backups` contract in the manifest so backup freshness can be tracked.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "data.backups", "action": "declare-backup-contract"},
    ),
    # --- route conflicts ---------------------------------------------------------
    "route_conflict": lambda app, environment: Remediation(
        summary="Resolve the conflicting domain/route ownership before moving traffic.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "routes", "action": "resolve-conflict"},
        requires_human_approval=True,
    ),
    "duplicate_domain": lambda app, environment: Remediation(
        summary="Two apps claim the same domain; pick one owner or change the domain in the manifest.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "routes", "action": "resolve-conflict"},
        requires_human_approval=True,
    ),
    "duplicate_route": lambda app, environment: Remediation(
        summary="Two apps claim the same route; pick one owner or change the route in the manifest.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "routes", "action": "resolve-conflict"},
        requires_human_approval=True,
    ),
    "missing_verification_checks": lambda app, environment: Remediation(
        summary="Add at least one health verification check so production readiness can be attested.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "verify", "action": "add-health-check"},
    ),
    # --- pack validation: production / images ------------------------------------
    "production_missing_verification": lambda app, environment: Remediation(
        summary="Add at least one health verification check before shipping to production.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "verify", "action": "add-health-check"},
    ),
    "production_image_without_digest": lambda app, environment: Remediation(
        summary="Pin production images by `@sha256:` digest so the runtime is reproducible.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "image", "action": "pin-by-digest"},
    ),
    "production_writable_bind_without_backup": lambda app, environment: Remediation(
        summary="Declare `data.backups.required` or an explicit data volume for the writable bind mount.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "data.backups", "action": "declare-backup-contract"},
    ),
    "service_missing_image": lambda app, environment: Remediation(
        summary="Declare an image for the service (or a manifest-level default image).",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "services.<service>.image", "action": "set-image"},
    ),
    "hook_path_outside_allowlist": lambda app, environment: Remediation(
        summary="Move the hook script under `ophelia/hooks/` and reference it with a relative path.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "hooks", "action": "use-allowlisted-path"},
    ),
    # --- pack validation: data contracts -----------------------------------------
    "critical_volume_missing_export": lambda app, environment: Remediation(
        summary="Declare export behavior for the critical volume so it can move with the app.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "data.volumes[].export", "action": "declare-export"},
    ),
    "critical_volume_missing_import": lambda app, environment: Remediation(
        summary="Declare import behavior for the critical volume so it can be restored on the target.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "data.volumes[].import", "action": "declare-import"},
    ),
    "app_postgres_missing_volume": lambda app, environment: Remediation(
        summary="Declare an explicit Postgres volume for `data.postgres.mode: app-postgres`.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "data.volumes", "action": "declare-postgres-volume"},
    ),
    "postgres_data_contract_inferred": lambda app, environment: Remediation(
        summary="Make the Postgres data contract explicit with export/import/verify behavior.",
        commands=[_pack_explain_command(app, environment), _pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "data.postgres", "action": "declare-explicit-contract"},
    ),
    "redis_data_contract_inferred": lambda app, environment: Remediation(
        summary="Make the Redis data contract explicit with export/import/verify behavior.",
        commands=[_pack_explain_command(app, environment), _pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "data.redis", "action": "declare-explicit-contract"},
    ),
    "critical_postgres_missing_export": lambda app, environment: Remediation(
        summary="Declare export behavior for the critical Postgres data contract.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "data.postgres.export", "action": "declare-export"},
    ),
    "critical_postgres_missing_import": lambda app, environment: Remediation(
        summary="Declare import behavior for the critical Postgres data contract.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "data.postgres.import", "action": "declare-import"},
    ),
    "critical_redis_missing_export": lambda app, environment: Remediation(
        summary="Declare export behavior for the critical Redis data contract.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "data.redis.export", "action": "declare-export"},
    ),
    "critical_redis_missing_import": lambda app, environment: Remediation(
        summary="Declare import behavior for the critical Redis data contract.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "data.redis.import", "action": "declare-import"},
    ),
    # --- pack validation warnings ------------------------------------------------
    "restore_drill_not_required": lambda app, environment: Remediation(
        summary="Set `data.backups.restore_drill_required: true` so restore drills are tracked.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "data.backups.restore_drill_required", "action": "set-true"},
    ),
    "offsite_backup_not_required": lambda app, environment: Remediation(
        summary="Set `data.backups.offsite_required: true` so offsite coverage is tracked.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "data.backups.offsite_required", "action": "set-true"},
    ),
    "critical_app_shared_postgres": lambda app, environment: Remediation(
        summary="Rehearse dump/restore/verify for the shared Postgres database before cutover.",
        commands=[_restore_drill_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        requires_human_approval=True,
    ),
    "durable_redis_shared_logical_db": lambda app, environment: Remediation(
        summary="Move durable Redis state to an app-owned Redis instead of a shared logical DB.",
        commands=[_pack_explain_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "data.redis.mode", "action": "use-app-owned-redis"},
    ),
    "critical_postgres_missing_verify": lambda app, environment: Remediation(
        summary="Declare verification behavior for the critical Postgres data contract.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "data.postgres.verify", "action": "declare-verify"},
    ),
    "critical_redis_missing_verify": lambda app, environment: Remediation(
        summary="Declare verification behavior for the critical Redis data contract.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "data.redis.verify", "action": "declare-verify"},
    ),
    # --- extra runtime env key (secrets audit / env diff) ------------------------
    "extra_runtime_env_key": lambda app, environment: Remediation(
        summary="Declare the runtime env key in the manifest or remove it if it is not needed.",
        commands=[_pack_validate_command(app, environment)],
        docs=[PACK_SPEC_DOC],
        manifest_patch_hint={"path": "env", "action": "declare-or-remove-key"},
    ),
}


def remediation_for(
    code: str,
    *,
    app: Optional[str] = None,
    environment: Optional[str] = None,
) -> Optional[Remediation]:
    """Return a typed :class:`Remediation` for a known finding ``code`` or ``None``.

    ``app``/``environment`` parameterize the typed commands; when absent they fall
    back to placeholder tokens so the command is still copy-pasteable.
    """
    builder = _REMEDIATIONS.get(code)
    if builder is None:
        return None
    resolved_app = app or "<app>"
    resolved_environment = environment or "<environment>"
    return builder(resolved_app, resolved_environment)
