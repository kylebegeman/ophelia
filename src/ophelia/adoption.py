from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DEFAULT_RUNTIME_ROOT
from .manifest import Manifest, ManifestError, load_manifest
from .operation_schema import artifact, issue, plan_envelope
from .portability import pack_validation_report
from .redaction import deep_redact


ADOPTION_ARTIFACTS = (
    ("ophelia/runbook.md", "runbook", "App-specific operator runbook."),
    ("ophelia/agent.md", "agent-notes", "App-specific agent guidance."),
    ("ophelia/checks/data-verify.sh", "data-check", "Read-only data verification script."),
    ("ophelia/hooks/pre-export.sh", "hook", "Pre-export hook."),
    ("ophelia/hooks/freeze.sh", "hook", "Freeze hook."),
    ("ophelia/hooks/unfreeze.sh", "hook", "Unfreeze hook."),
    ("ophelia/hooks/post-import.sh", "hook", "Post-import hook."),
)
STANDARD_APP_ENDPOINTS = ("/ophelia/health", "/ophelia/release")
STANDARD_PACKAGE_SCRIPTS = ("ophelia:health", "ophelia:data:verify", "ophelia:release")


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
        blockers.append(
            issue(
                "adoption_repo_missing",
                f"Repository path does not exist or is not a directory: {repo_root}",
                str(repo_root),
            )
        )

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
            checks.append(
                {
                    "name": "manifest_parse",
                    "ok": True,
                    "message": "Manifest parsed with Ophelia validation.",
                }
            )
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
    non_executable_artifacts = [
        item
        for item in artifacts
        if item.get("required_executable") and item.get("present") and not item.get("executable")
    ]
    if missing_artifacts:
        warnings.append(
            issue(
                "adoption_artifacts_missing",
                f"{len(missing_artifacts)} recommended Ophelia adoption artifact(s) are missing.",
                str(repo_root / "ophelia"),
            )
        )
    if non_executable_artifacts:
        warnings.append(
            issue(
                "adoption_artifacts_not_executable",
                f"{len(non_executable_artifacts)} Ophelia hook/check script(s) are not executable.",
                str(repo_root / "ophelia"),
            )
        )
    checks.append(
        {
            "name": "repo_adoption_artifacts",
            "ok": not missing_artifacts and not non_executable_artifacts,
            "message": (
                f"{len(artifacts) - len(missing_artifacts)}/{len(artifacts)} artifact(s) present; "
                f"{len(non_executable_artifacts)} script mode warning(s)."
            ),
        }
    )
    app_owned_contract: Dict[str, Any] = {}
    if manifest is not None and manifest.kind in {"service", "multi-service"}:
        app_owned_contract = _app_owned_contract(repo_root, manifest)
        missing_contract = app_owned_contract.get("missing") if isinstance(app_owned_contract.get("missing"), list) else []
        checks.append(
            {
                "name": "app_owned_runtime_contract",
                "ok": not missing_contract,
                "message": f"{len(missing_contract)} missing standard app-owned check(s).",
            }
        )
        if missing_contract:
            warnings.append(
                issue(
                    "adoption_app_owned_checks_missing",
                    "Standard Ophelia app-owned checks are missing: " + ", ".join(str(item) for item in missing_contract) + ".",
                    "verify",
                )
            )

    resolved_environment = _resolved_environment(environment, manifest)
    manifest_exists = resolved_manifest_path.exists()
    next_commands = _next_commands(
        app,
        resolved_environment,
        repo_root,
        resolved_manifest_path,
        runtime_root,
        manifest_exists=manifest_exists,
    )
    gates = _adoption_gates(
        manifest_present=manifest_exists,
        manifest_valid=manifest is not None,
        pack_ok=bool(pack_validation and pack_validation.get("ok")),
        artifacts_complete=not missing_artifacts and not non_executable_artifacts,
    )
    summary = f"Adoption plan for {app} {resolved_environment}: {len(blockers)} blocker(s), {len(warnings)} warning(s)."
    operation_artifacts = [
        artifact(
            str(Path(str(item["path"]))),
            str(item["kind"]),
            str(item.get("description") or ""),
            present=bool(item.get("present")),
        )
        for item in artifacts
    ]
    return plan_envelope(
        "app.adoption.plan",
        app,
        resolved_environment,
        summary,
        blockers=blockers,
        warnings=warnings,
        checks=checks,
        artifacts=operation_artifacts,
        confirmation_required=False,
        confirmation_token=None,
        exact_apply_input=None,
        risk="low",
        read_only=True,
        mutates_state=False,
        repo_path=str(repo_root),
        manifest_path=str(resolved_manifest_path),
        required_artifacts=artifacts,
        pack_validation=deep_redact(pack_validation),
        app_owned_contract=deep_redact(app_owned_contract),
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
            "relative_path": (
                str(manifest_path.relative_to(repo_root))
                if _is_relative_to(manifest_path, repo_root)
                else str(manifest_path)
            ),
            "kind": "manifest",
            "description": "Ophelia app/runtime contract.",
            "required": True,
            "present": manifest_present,
        }
    ]
    for relative, kind, description in ADOPTION_ARTIFACTS:
        path = repo_root / relative
        present = path.exists()
        required_executable = _artifact_requires_executable(path)
        artifacts.append(
            {
                "path": str(path),
                "relative_path": relative,
                "kind": kind,
                "description": description,
                "required": True,
                "present": present,
                "required_executable": required_executable,
                "executable": bool(present and path.stat().st_mode & 0o111) if required_executable else None,
            }
        )
    return artifacts


def _app_owned_contract(repo_root: Path, manifest: Manifest) -> Dict[str, Any]:
    scripts = _package_scripts(repo_root)
    internal_paths = {
        str(check.path)
        for check in manifest.verify
        if check.type == "internal" and check.path
    }
    command_texts = [" ".join(check.command or []) for check in manifest.verify if check.type == "command"]
    manifest_commands = {
        script: any(script in command for command in command_texts)
        for script in STANDARD_PACKAGE_SCRIPTS
    }
    data_verifier_declared = _data_verifier_declared(manifest, command_texts)
    endpoint_presence = {
        path: path in internal_paths
        for path in STANDARD_APP_ENDPOINTS
    }
    script_presence = {
        script: script in scripts
        for script in STANDARD_PACKAGE_SCRIPTS
    }
    runtime_checks = {
        "health": endpoint_presence["/ophelia/health"] or manifest_commands["ophelia:health"],
        "release": endpoint_presence["/ophelia/release"] or manifest_commands["ophelia:release"],
        "data_verify": manifest_commands["ophelia:data:verify"] or data_verifier_declared,
    }
    missing: List[str] = []
    for name, present in runtime_checks.items():
        if not present:
            missing.append(f"manifest runtime check {name}")
    for script, present in script_presence.items():
        if not present:
            missing.append(f"package script {script}")
    return {
        "standard_endpoints": endpoint_presence,
        "standard_scripts": script_presence,
        "manifest_commands": manifest_commands,
        "manifest_data_verifier": data_verifier_declared,
        "runtime_checks": runtime_checks,
        "package_json_present": (repo_root / "package.json").exists(),
        "missing": missing,
    }


def _data_verifier_declared(manifest: Manifest, command_texts: List[str]) -> bool:
    data = getattr(manifest, "data", None)
    postgres = getattr(data, "postgres", None) if data is not None else None
    postgres_verify = getattr(postgres, "verify", None) if postgres is not None else None
    if postgres_verify is not None and getattr(postgres_verify, "command", None):
        return True

    for command in command_texts:
        tokens = shlex.split(command)
        if "ophelia:data:verify" in tokens:
            return True
        if "data-verify" in tokens or "data:verify" in tokens:
            return True
        if any(token.endswith("data-verify.sh") for token in tokens):
            return True
    return False


def _package_scripts(repo_root: Path) -> Dict[str, str]:
    package_json = repo_root / "package.json"
    if not package_json.exists():
        return {}
    try:
        payload = json.loads(package_json.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {str(key): str(value) for key, value in scripts.items() if isinstance(key, str)}


def _artifact_requires_executable(path: Path) -> bool:
    return path.suffix == ".sh" and any(part in {"checks", "hooks"} for part in path.parts)


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
    manifest_exists: bool,
) -> List[Dict[str, Any]]:
    manifest_for_command = _display_path(manifest_path)
    repo_for_command = _display_path(repo_root)
    runtime_for_command = _display_path(runtime_root)
    environment_args = [] if environment == "unknown" else ["--environment", environment]
    commands: List[Dict[str, Any]] = []
    if manifest_exists:
        commands.append(
            _command_payload(
                command_id="preview-pack-scaffold",
                phase="repo-contract",
                description="Preview the standard Ophelia support artifact scaffold for this repo.",
                argv=[
                    "ship",
                    "pack",
                    "init",
                    "--app",
                    app,
                    *environment_args,
                    "--directory",
                    repo_for_command,
                    "--json",
                ],
            )
        )
    else:
        commands.extend(
            [
                _command_payload(
                    command_id="preview-service-bootstrap",
                    phase="repo-contract",
                    description="Preview a service manifest plus standard Ophelia support artifacts.",
                    argv=[
                        "ship",
                        "pack",
                        "init",
                        "--app",
                        app,
                        *environment_args,
                        "--directory",
                        repo_for_command,
                        "--include-manifest",
                        "--kind",
                        "service",
                        "--domain",
                        "<domain>",
                        "--image",
                        "<image>",
                        "--json",
                    ],
                ),
                _command_payload(
                    command_id="preview-static-bootstrap",
                    phase="repo-contract",
                    description="Preview a static manifest plus standard Ophelia support artifacts.",
                    argv=[
                        "ship",
                        "pack",
                        "init",
                        "--app",
                        app,
                        *environment_args,
                        "--directory",
                        repo_for_command,
                        "--include-manifest",
                        "--kind",
                        "static",
                        "--domain",
                        "<domain>",
                        "--static-root",
                        "public",
                        "--json",
                    ],
                ),
            ]
        )
    commands.extend(
        [
            _command_payload(
                command_id="validate-pack",
                phase="repo-contract",
                description="Validate the app manifest and pack contract.",
                argv=["ship", "pack", "validate", manifest_for_command, "--json"],
            ),
            _command_payload(
                command_id="explain-pack",
                phase="repo-contract",
                description="Inspect the data, host, route, verification, and movement contract.",
                argv=["ship", "pack", "explain", manifest_for_command, "--json"],
            ),
            _command_payload(
                command_id="readiness-later",
                phase="runtime-readiness",
                description="Run after truthful runtime state exists for the app.",
                argv=[
                    "ship",
                    "app",
                    "readiness",
                    app,
                    *environment_args,
                    "--manifest",
                    manifest_for_command,
                    "--runtime-root",
                    runtime_for_command,
                    "--json",
                ],
            ),
            _command_payload(
                command_id="runbook-later",
                phase="operator-handoff",
                description="Generate an operator runbook from readiness evidence.",
                argv=[
                    "ship",
                    "app",
                    "runbook",
                    app,
                    *environment_args,
                    "--manifest",
                    manifest_for_command,
                    "--runtime-root",
                    runtime_for_command,
                ],
            ),
        ]
    )
    return commands


def _command_payload(command_id: str, phase: str, description: str, argv: List[str]) -> Dict[str, Any]:
    return {
        "id": command_id,
        "phase": phase,
        "command": shlex.join(argv),
        "argv": argv,
        "mutates_state": False,
        "description": description,
    }


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
