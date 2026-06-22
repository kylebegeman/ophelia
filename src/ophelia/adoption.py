from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DEFAULT_RUNTIME_ROOT
from .manifest import Manifest, ManifestError, load_manifest
from .operation_schema import artifact, issue, plan_envelope
from .portability import pack_validation_report


ADOPTION_ARTIFACTS = (
    ("ophelia/runbook.md", "runbook", "App-specific operator runbook."),
    ("ophelia/agent.md", "agent-notes", "App-specific agent guidance."),
    ("ophelia/checks/data-verify.sh", "data-check", "Read-only data verification script."),
    ("ophelia/hooks/pre-export.sh", "hook", "Pre-export hook."),
    ("ophelia/hooks/freeze.sh", "hook", "Freeze hook."),
    ("ophelia/hooks/unfreeze.sh", "hook", "Unfreeze hook."),
    ("ophelia/hooks/post-import.sh", "hook", "Post-import hook."),
)


def adoption_plan(
    app: str,
    environment: Optional[str],
    repo_path: Path,
    manifest_path: Optional[Path] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> Dict[str, Any]:
    """Plan an app repository's adoption into the Ophelia contract.

    This is intentionally read-only. It validates the repository-facing contract
    and emits next Ophelia commands without collecting live values, probing
    production, or mutating the app repository.
    """
    repo_root = repo_path.expanduser()
    resolved_manifest_path = _manifest_path(repo_root, manifest_path)
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    checks: List[Dict[str, Any]] = []
    artifacts = _repo_artifacts(repo_root, resolved_manifest_path)
    manifest: Optional[Manifest] = None
    pack_validation: Optional[Dict[str, Any]] = None

    repo_exists = repo_root.exists() and repo_root.is_dir()
    checks.append(
        {
            "name": "repo_path",
            "ok": repo_exists,
            "message": str(repo_root),
        }
    )
    if not repo_exists:
        blockers.append(issue("adoption_repo_missing", f"Repository path does not exist or is not a directory: {repo_root}", str(repo_root)))

    if not resolved_manifest_path.exists():
        blockers.append(
            issue(
                "adoption_manifest_missing",
                f"Missing Ophelia manifest: {resolved_manifest_path}",
                str(resolved_manifest_path),
            )
        )
        checks.append({"name": "manifest_present", "ok": False, "message": str(resolved_manifest_path)})
    else:
        checks.append({"name": "manifest_present", "ok": True, "message": str(resolved_manifest_path)})
        try:
            manifest = load_manifest(resolved_manifest_path)
        except ManifestError as exc:
            blockers.append(
                issue(
                    "adoption_manifest_invalid",
                    f"Manifest is not a valid Ophelia manifest: {exc}",
                    str(resolved_manifest_path),
                )
            )
            checks.append({"name": "manifest_parse", "ok": False, "message": str(exc)})
        else:
            checks.append({"name": "manifest_parse", "ok": True, "message": "Manifest parsed with Ophelia validation."})
            if manifest.app != app:
                blockers.append(
                    issue(
                        "adoption_app_mismatch",
                        f"Manifest app `{manifest.app}` does not match requested app `{app}`.",
                        str(resolved_manifest_path),
                    )
                )
            if environment and manifest.environment and manifest.environment != environment:
                warnings.append(
                    issue(
                        "adoption_environment_mismatch",
                        f"Manifest environment `{manifest.environment}` differs from requested environment `{environment}`.",
                        str(resolved_manifest_path),
                    )
                )
            if environment and manifest.environment is None:
                warnings.append(
                    issue(
                        "adoption_environment_unset",
                        f"Manifest has no environment; adoption was requested for `{environment}`.",
                        str(resolved_manifest_path),
                    )
                )
            pack_validation = pack_validation_report(
                manifest,
                resolved_manifest_path,
                manifest_dir=resolved_manifest_path.parent,
            )
            checks.append(
                {
                    "name": "pack_validation",
                    "ok": bool(pack_validation.get("ok")),
                    "message": str(pack_validation.get("summary")),
                }
            )
            for item in _issue_list(pack_validation.get("errors")):
                blockers.append(
                    issue(
                        "adoption_pack_validation_blocked",
                        str(item.get("message") or item),
                        str(item.get("path") or resolved_manifest_path),
                    )
                )
            for item in _issue_list(pack_validation.get("warnings")):
                warnings.append(
                    issue(
                        "adoption_pack_validation_warning",
                        str(item.get("message") or item),
                        str(item.get("path") or resolved_manifest_path),
                    )
                )

    missing_artifacts = [item for item in artifacts if item.get("required") and not item.get("present")]
    if missing_artifacts:
        warnings.append(
            issue(
                "adoption_artifacts_missing",
                f"{len(missing_artifacts)} recommended Ophelia adoption artifact(s) are missing.",
                str(repo_root / "ophelia"),
            )
        )
    checks.append(
        {
            "name": "repo_adoption_artifacts",
            "ok": not missing_artifacts,
            "message": f"{len(artifacts) - len(missing_artifacts)}/{len(artifacts)} artifact(s) present.",
        }
    )

    resolved_environment = _resolved_environment(environment, manifest)
    next_commands = _next_commands(app, resolved_environment, repo_root, resolved_manifest_path, runtime_root)
    gates = _adoption_gates(
        manifest_present=resolved_manifest_path.exists(),
        manifest_valid=manifest is not None,
        pack_ok=bool(pack_validation and pack_validation.get("ok")),
        artifacts_complete=not missing_artifacts,
    )
    summary = f"Adoption plan for {app} {resolved_environment}: {len(blockers)} blocker(s), {len(warnings)} warning(s)."
    return plan_envelope(
        "app.adoption.plan",
        app,
        resolved_environment,
        summary,
        blockers=blockers,
        warnings=warnings,
        checks=checks,
        artifacts=[artifact(str(Path(str(item["path"]))), str(item["kind"]), str(item.get("description") or ""), present=bool(item.get("present"))) for item in artifacts],
        confirmation_required=False,
        confirmation_token=None,
        exact_apply_input=None,
        risk="low",
        read_only=True,
        mutates_state=False,
        repo_path=str(repo_root),
        manifest_path=str(resolved_manifest_path),
        required_artifacts=artifacts,
        pack_validation=pack_validation,
        adoption_gates=gates,
        next_commands=next_commands,
        secrets_redacted=True,
        live_values_required=False,
        live_values_collected=False,
    )


def _manifest_path(repo_root: Path, manifest_path: Optional[Path]) -> Path:
    if manifest_path is None:
        return repo_root / ".ophelia.yml"
    expanded = manifest_path.expanduser()
    if expanded.is_absolute():
        return expanded
    return repo_root / expanded


def _repo_artifacts(repo_root: Path, manifest_path: Path) -> List[Dict[str, Any]]:
    manifest_present = manifest_path.exists()
    artifacts: List[Dict[str, Any]] = [
        {
            "path": str(manifest_path),
            "relative_path": str(manifest_path.relative_to(repo_root)) if _is_relative_to(manifest_path, repo_root) else str(manifest_path),
            "kind": "manifest",
            "description": "Ophelia app/runtime contract.",
            "required": True,
            "present": manifest_present,
        }
    ]
    for relative, kind, description in ADOPTION_ARTIFACTS:
        path = repo_root / relative
        artifacts.append(
            {
                "path": str(path),
                "relative_path": relative,
                "kind": kind,
                "description": description,
                "required": True,
                "present": path.exists(),
            }
        )
    return artifacts


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolved_environment(environment: Optional[str], manifest: Optional[Manifest]) -> str:
    if environment:
        return environment
    if manifest and manifest.environment:
        return manifest.environment
    return "unknown"


def _next_commands(
    app: str,
    environment: str,
    repo_root: Path,
    manifest_path: Path,
    runtime_root: Path,
) -> List[Dict[str, Any]]:
    env_arg = "" if environment == "unknown" else f" --environment {environment}"
    manifest_for_command = _display_path(manifest_path)
    repo_for_command = _display_path(repo_root)
    runtime_for_command = _display_path(runtime_root)
    return [
        {
            "id": "preview-pack-scaffold",
            "phase": "repo-contract",
            "command": f"ship pack init --app {app}{env_arg} --directory {repo_for_command} --json",
            "mutates_state": False,
            "description": "Preview the standard Ophelia artifact scaffold for this repo.",
        },
        {
            "id": "validate-pack",
            "phase": "repo-contract",
            "command": f"ship pack validate {manifest_for_command} --json",
            "mutates_state": False,
            "description": "Validate the app manifest and pack contract.",
        },
        {
            "id": "explain-pack",
            "phase": "repo-contract",
            "command": f"ship pack explain {manifest_for_command} --json",
            "mutates_state": False,
            "description": "Inspect the data, host, route, verification, and movement contract.",
        },
        {
            "id": "readiness-later",
            "phase": "runtime-readiness",
            "command": f"ship app readiness {app}{env_arg} --manifest {manifest_for_command} --runtime-root {runtime_for_command} --json",
            "mutates_state": False,
            "description": "Run after truthful runtime state exists for the app.",
        },
        {
            "id": "runbook-later",
            "phase": "operator-handoff",
            "command": f"ship app runbook {app}{env_arg} --manifest {manifest_for_command} --runtime-root {runtime_for_command}",
            "mutates_state": False,
            "description": "Generate an operator runbook from readiness evidence.",
        },
    ]


def _display_path(path: Path) -> str:
    return str(path)


def _adoption_gates(
    *,
    manifest_present: bool,
    manifest_valid: bool,
    pack_ok: bool,
    artifacts_complete: bool,
) -> List[Dict[str, Any]]:
    return [
        {
            "id": "manifest_contract",
            "status": "passed" if manifest_present and manifest_valid else "blocked",
            "required_now": True,
            "description": "A valid `.ophelia.yml` declares the app/runtime contract.",
        },
        {
            "id": "repo_artifacts",
            "status": "passed" if artifacts_complete else "warning",
            "required_now": False,
            "description": "Recommended repo-local Ophelia runbook, agent notes, hooks, and checks are present.",
        },
        {
            "id": "pack_validation",
            "status": "passed" if pack_ok else "blocked",
            "required_now": True,
            "description": "The manifest satisfies portable pack validation.",
        },
        {
            "id": "runtime_readiness_later",
            "status": "not_started",
            "required_now": False,
            "description": "Run after the app has truthful Ophelia runtime state.",
        },
        {
            "id": "live_hydration_later",
            "status": "not_started",
            "required_now": False,
            "description": "Collect non-secret live evidence only during an approved migration or deployment phase.",
        },
        {
            "id": "production_deployment_later",
            "status": "not_started",
            "required_now": False,
            "description": "Deploy only after provider access, approvals, backups, probes, receipts, and rollback notes are ready.",
        },
    ]


def _issue_list(value: object) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
