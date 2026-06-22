"""GitHub provider contracts and local observations.

This module is intentionally contract-first. It validates provider configuration,
describes GitHub App operations without reading private keys, and compares
desired GitHub repo state with local observation files when present. No function
here calls GitHub over the network.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .manifest import Manifest
from .operation_schema import SCHEMA_VERSION, issue
from .redaction import deep_redact

GITHUB_PROVIDER_STATUS_KIND = "ophelia.github_provider_status"

GITHUB_PROVIDERS = {"auto", "gh", "github-app", "github_app"}
DEFAULT_GITHUB_ENVIRONMENTS = ["staging", "production"]
DEFAULT_RELEASE_LABELS = ["release:patch", "release:minor", "release:major"]
DEFAULT_STATUS_CHECKS = ["validate", "tests"]
DEFAULT_DEPLOY_SECRETS = [
    "OPHELIA_DEPLOY_HOST",
    "OPHELIA_DEPLOY_PORT",
    "OPHELIA_DEPLOY_USER",
    "OPHELIA_DEPLOY_KEY",
]
DEFAULT_REGISTRY_SECRETS = ["GHCR_TOKEN"]
DEFAULT_WORKFLOWS = [
    ".github/workflows/ophelia-staging.yml",
    ".github/workflows/ophelia-release.yml",
]


def github_provider_status(
    *,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    ophelia_root: Path = REPO_ROOT,
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    runtime_root = Path(runtime_root)
    config, resolved_path, warnings = _load_integrations_config(runtime_root, ophelia_root, config_path)
    github_config = config.get("github") if isinstance(config.get("github"), dict) else {}
    preferred = _normal_provider(str(github_config.get("preferred_provider") or "gh"))
    fallback = _normal_provider(str(github_config.get("fallback_provider") or "gh"))
    app_config = github_config.get("app") if isinstance(github_config.get("app"), dict) else {}
    gh_config = github_config.get("gh") if isinstance(github_config.get("gh"), dict) else {}
    repositories = _repo_bindings(github_config.get("repositories"))

    app_enabled = bool(app_config.get("enabled"))
    app_id_env = _str_or_none(app_config.get("app_id_env"))
    private_key_env = _str_or_none(app_config.get("private_key_env"))
    installation_id = _str_or_none(app_config.get("installation_id")) or _str_or_none(app_config.get("installation_id_env"))
    webhook_secret_env = _str_or_none(app_config.get("webhook_secret_env"))
    app_missing: List[str] = []
    if app_enabled:
        if not app_id_env:
            app_missing.append("app_id_env")
        elif app_id_env not in os.environ:
            app_missing.append(app_id_env)
        if not private_key_env:
            app_missing.append("private_key_env")
        elif private_key_env not in os.environ:
            app_missing.append(private_key_env)
        if not installation_id:
            app_missing.append("installation_id")
    app_usable = app_enabled and not app_missing

    gh_enabled = bool(gh_config.get("enabled", True))
    gh_path = shutil.which("gh")
    gh_usable = gh_enabled and gh_path is not None

    selected = preferred
    selection_reason = "configured preferred provider"
    if selected == "github_app" and not app_usable:
        selected = fallback if fallback in {"gh", "github_app"} else "gh"
        selection_reason = "github_app unavailable; using fallback provider"
    if selected == "gh" and not gh_usable and app_usable:
        selected = "github_app"
        selection_reason = "gh unavailable; using configured GitHub App provider"

    blockers: List[Dict[str, str]] = []
    if selected == "gh" and not gh_usable:
        warnings.append(issue("github_gh_provider_unavailable", "`gh` provider is selected but gh is not available."))
    if selected == "github_app" and not app_usable:
        warnings.append(
            issue(
                "github_app_provider_unavailable",
                "GitHub App provider is selected but required env refs or installation metadata are missing.",
            )
        )

    status = "blocked" if blockers else "warn" if warnings or app_missing or not gh_usable else "ok"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": GITHUB_PROVIDER_STATUS_KIND,
        "operation": "providers.github.status",
        "status": status,
        "config_path": str(resolved_path) if resolved_path is not None else None,
        "configured": resolved_path is not None,
        "selected_provider": selected,
        "selection_reason": selection_reason,
        "preferred_provider": preferred,
        "fallback_provider": fallback,
        "providers": [
            {
                "name": "github_app",
                "configured": app_enabled,
                "usable": app_usable,
                "missing": app_missing,
                "installation_id_present": bool(installation_id),
                "app_id_env": app_id_env,
                "app_id_present": app_id_env in os.environ if app_id_env else False,
                "private_key_env": private_key_env,
                "private_key_present": private_key_env in os.environ if private_key_env else False,
                "webhook_secret_env": webhook_secret_env,
                "webhook_secret_present": webhook_secret_env in os.environ if webhook_secret_env else None,
                "permissions": dict(app_config.get("permissions") or {}),
                "repositories": repositories,
                "value_redacted": True,
            },
            {
                "name": "gh",
                "configured": gh_enabled,
                "usable": gh_usable,
                "path": gh_path,
                "value_redacted": True,
            },
        ],
        "blockers": blockers,
        "warnings": warnings,
        "values_redacted": True,
        "summary": f"GitHub provider status: selected={selected}, status={status}.",
    }
    return deep_redact(
        payload,
        safe_keys={
            "values_redacted",
            "value_redacted",
            "app_id_env",
            "app_id_present",
            "private_key_env",
            "private_key_present",
            "webhook_secret_env",
            "webhook_secret_present",
        },
        propagate=True,
    )


def resolve_github_provider(
    requested: str,
    *,
    runtime_root: Path,
    ophelia_root: Path = REPO_ROOT,
    config_path: Optional[Path] = None,
) -> Tuple[str, Dict[str, Any]]:
    requested_normal = _normal_provider(requested or "auto")
    status = github_provider_status(runtime_root=runtime_root, ophelia_root=ophelia_root, config_path=config_path)
    if requested_normal == "auto":
        return str(status.get("selected_provider") or "gh"), status
    selected = requested_normal
    blockers = status.setdefault("blockers", [])
    if selected == "github_app":
        app_provider = _provider_entry(status, "github_app")
        if not app_provider.get("usable"):
            blockers.append(
                issue(
                    "github_app_provider_unavailable",
                    "Requested GitHub App provider is not usable from the current local configuration.",
                )
            )
    if selected == "gh":
        gh_provider = _provider_entry(status, "gh")
        if not gh_provider.get("usable"):
            blockers.append(issue("github_gh_provider_unavailable", "Requested gh provider is not usable."))
    status["selected_provider"] = selected
    status["status"] = "blocked" if blockers else status.get("status", "ok")
    return selected, status


def github_app_operations(repo: str, branch_policy: Dict[str, Any]) -> List[Dict[str, Any]]:
    owner, _, name = repo.partition("/")
    operations: List[Dict[str, Any]] = [
        {
            "id": "create_repo",
            "provider": "github_app",
            "description": f"Create the private GitHub repository {repo}.",
            "api": {
                "method": "POST",
                "path": f"/orgs/{owner}/repos",
                "body_shape": {"name": name, "private": True},
            },
            "executed": False,
        },
        {
            "id": "create_staging_environment",
            "provider": "github_app",
            "description": "Create the staging deployment environment.",
            "api": {"method": "PUT", "path": f"/repos/{repo}/environments/staging"},
            "executed": False,
        },
        {
            "id": "create_production_environment",
            "provider": "github_app",
            "description": "Create the production deployment environment.",
            "api": {"method": "PUT", "path": f"/repos/{repo}/environments/production"},
            "executed": False,
        },
    ]
    for branch, policy in branch_policy.items():
        operations.append(
            {
                "id": f"protect_{branch}",
                "provider": "github_app",
                "description": f"Protect the {branch} branch (reviews + status checks).",
                "api": {
                    "method": "PUT",
                    "path": f"/repos/{repo}/branches/{branch}/protection",
                    "body_shape": {
                        "required_status_checks": {
                            "strict": True,
                            "contexts": list(policy.get("required_status_checks") or []),
                        },
                        "enforce_admins": True,
                        "required_pull_request_reviews": {
                            "required_approving_review_count": int(policy.get("required_reviews") or 1),
                        },
                        "restrictions": None,
                    },
                },
                "requires_existing_branch": True,
                "executed": False,
            }
        )
    return operations


def github_drift_snapshot(manifest: Manifest, runtime_root: Path) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
    runtime_root = Path(runtime_root)
    desired = desired_github_state(manifest, runtime_root)
    observed, observation_path = _github_observation(manifest.app, runtime_root)
    if observed is None:
        finding = _finding(
            "github_settings_not_observed",
            "No local GitHub observation is available for this manifest.",
            "info",
            "github",
            manifest.app,
            [f"ship app github plan --app {manifest.app} --template {desired['template']} --repo {desired['repo']} --json"],
            [],
        )
        return (
            _snapshot(
                "github_desired_settings_vs_observed_settings",
                "not_observed",
                "github",
                "GitHub repository settings are not locally observed.",
                desired=desired,
                observed={"available": False, "observation_path": str(observation_path)},
            ),
            [finding],
        )

    findings = _compare_github_state(manifest, desired, observed)
    status = "clean" if not findings else "drift"
    snapshot = _snapshot(
        "github_desired_settings_vs_observed_settings",
        status,
        "github",
        "GitHub local observation compared with desired release model.",
        desired=desired,
        observed=_bounded_observed_github(observed, observation_path),
    )
    return snapshot, findings


def desired_github_state(manifest: Manifest, runtime_root: Path) -> Dict[str, object]:
    provider_status = github_provider_status(runtime_root=runtime_root)
    repo = _repo_for_app(manifest.app, provider_status) or f"OWNER/{manifest.app}"
    template = _template_for_manifest(manifest)
    required_secrets = list(DEFAULT_DEPLOY_SECRETS)
    if template != "static-site":
        required_secrets.extend(DEFAULT_REGISTRY_SECRETS)
    return {
        "app": manifest.app,
        "repo": repo,
        "template": template,
        "provider": provider_status.get("selected_provider"),
        "branches": {
            "next": {"environment": "staging", "required_status_checks": list(DEFAULT_STATUS_CHECKS)},
            "master": {
                "environment": "production",
                "required_status_checks": list(DEFAULT_STATUS_CHECKS),
                "release_label_required": True,
            },
        },
        "environments": list(DEFAULT_GITHUB_ENVIRONMENTS),
        "release_labels": list(DEFAULT_RELEASE_LABELS),
        "workflows": list(DEFAULT_WORKFLOWS),
        "required_secrets": required_secrets,
    }


def _compare_github_state(
    manifest: Manifest,
    desired: Dict[str, object],
    observed: Dict[str, Any],
) -> List[Dict[str, object]]:
    findings: List[Dict[str, object]] = []
    observed_envs = observed.get("environments") if isinstance(observed.get("environments"), dict) else {}
    observed_labels = set(_string_list(observed.get("labels")))
    observed_workflows = set(_string_list(observed.get("workflows")))
    observed_branches = observed.get("branch_protection") if isinstance(observed.get("branch_protection"), dict) else {}

    for env in _string_list(desired.get("environments")):
        env_observed = observed_envs.get(env) if isinstance(observed_envs.get(env), dict) else None
        if env_observed is None:
            findings.append(
                _finding(
                    "github_environment_missing",
                    f"GitHub environment `{env}` was not observed.",
                    "high",
                    "github",
                    f"{manifest.app}:{env}",
                    [f"ship app github plan --app {manifest.app} --template {desired['template']} --phase environments --json"],
                    [],
                )
            )
            continue
        secrets = set(_secret_names(env_observed.get("secrets")))
        for secret in _string_list(desired.get("required_secrets")):
            if secret not in secrets:
                findings.append(
                    _finding(
                        "github_environment_secret_missing",
                        f"GitHub environment `{env}` is missing required secret `{secret}`.",
                        "high",
                        "github",
                        secret,
                        [f"ship secrets providers {manifest.app} --environment {env} --json"],
                        [],
                    )
                )

    branches = desired.get("branches") if isinstance(desired.get("branches"), dict) else {}
    for branch, branch_desired in branches.items():
        branch_observed = observed_branches.get(branch) if isinstance(observed_branches.get(branch), dict) else None
        if branch_observed is None or not branch_observed.get("protected"):
            findings.append(
                _finding(
                    "github_branch_protection_missing",
                    f"GitHub branch `{branch}` protection was not observed.",
                    "medium",
                    "github",
                    str(branch),
                    [f"ship app github plan --app {manifest.app} --template {desired['template']} --phase protection --json"],
                    [],
                )
            )
            continue
        observed_checks = set(_string_list(branch_observed.get("required_status_checks")))
        for check in _string_list(branch_desired.get("required_status_checks") if isinstance(branch_desired, dict) else []):
            if check not in observed_checks:
                findings.append(
                    _finding(
                        "github_status_check_missing",
                        f"GitHub branch `{branch}` is missing required status check `{check}`.",
                        "medium",
                        "github",
                        check,
                        [f"ship app github plan --app {manifest.app} --template {desired['template']} --phase protection --json"],
                        [],
                    )
                )

    for workflow in _string_list(desired.get("workflows")):
        if workflow not in observed_workflows:
            findings.append(
                _finding(
                    "github_workflow_missing",
                    f"GitHub workflow `{workflow}` was not observed.",
                    "medium",
                    "github",
                    workflow,
                    [f"ship app github plan --app {manifest.app} --template {desired['template']} --json"],
                    [],
                )
            )
    for label in _string_list(desired.get("release_labels")):
        if label not in observed_labels:
            findings.append(
                _finding(
                    "github_release_label_missing",
                    f"GitHub release label `{label}` was not observed.",
                    "low",
                    "github",
                    label,
                    [f"ship app github plan --app {manifest.app} --template {desired['template']} --json"],
                    [],
                )
            )
    return findings


def _load_integrations_config(
    runtime_root: Path,
    ophelia_root: Path,
    config_path: Optional[Path],
) -> Tuple[Dict[str, Any], Optional[Path], List[Dict[str, str]]]:
    warnings: List[Dict[str, str]] = []
    candidates = [Path(config_path)] if config_path is not None else [
        runtime_root / "integrations" / "ophelia-integrations.yml",
        runtime_root / "integrations" / "github.yml",
        ophelia_root / "config" / "ophelia-integrations.yml",
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            return _read_mapping(candidate), candidate, warnings
        except (OSError, ValueError) as exc:
            warnings.append(issue("github_provider_config_unreadable", f"Could not read GitHub provider config: {type(exc).__name__}.", str(candidate)))
            return {}, candidate, warnings
    warnings.append(issue("github_provider_config_missing", "No GitHub provider config discovered."))
    return {}, None, warnings


def _read_mapping(path: Path) -> Dict[str, Any]:
    text = path.read_text()
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised when PyYAML absent
            raise ValueError("PyYAML is required for YAML integration config") from exc
        payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise ValueError("integration config must be an object")
    return payload


def _github_observation(app: str, runtime_root: Path) -> Tuple[Optional[Dict[str, Any]], Path]:
    root = runtime_root / "github" / "observations"
    candidates = [root / f"{app}.json", root / f"{app}.github.json"]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            payload = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            return None, candidate
        return payload if isinstance(payload, dict) else None, candidate
    return None, candidates[0]


def _bounded_observed_github(observed: Dict[str, Any], observation_path: Path) -> Dict[str, object]:
    environments = observed.get("environments") if isinstance(observed.get("environments"), dict) else {}
    return {
        "available": True,
        "observation_path": str(observation_path),
        "repo": observed.get("repo") if isinstance(observed.get("repo"), str) else None,
        "environments": sorted(str(name) for name in environments),
        "branch_protection": sorted(str(name) for name in (observed.get("branch_protection") or {})),
        "labels": _string_list(observed.get("labels")),
        "workflows": _string_list(observed.get("workflows")),
        "values_redacted": True,
    }


def _repo_for_app(app: str, provider_status: Dict[str, Any]) -> Optional[str]:
    for provider in provider_status.get("providers", []) if isinstance(provider_status.get("providers"), list) else []:
        if not isinstance(provider, dict):
            continue
        for binding in provider.get("repositories", []) if isinstance(provider.get("repositories"), list) else []:
            if isinstance(binding, dict) and binding.get("app") == app and isinstance(binding.get("repo"), str):
                return str(binding["repo"])
    return None


def _repo_bindings(value: Any) -> List[Dict[str, str]]:
    bindings: List[Dict[str, str]] = []
    if not isinstance(value, list):
        return bindings
    for item in value:
        if not isinstance(item, dict):
            continue
        app = item.get("app")
        repo = item.get("repo")
        if isinstance(app, str) and isinstance(repo, str):
            bindings.append({"app": app, "repo": repo, "provider": str(item.get("provider") or "")})
    return bindings


def _normal_provider(value: str) -> str:
    normalized = value.replace("-", "_")
    return normalized if normalized in {"auto", "gh", "github_app"} else "gh"


def _provider_entry(status: Dict[str, Any], name: str) -> Dict[str, Any]:
    for provider in status.get("providers", []) if isinstance(status.get("providers"), list) else []:
        if isinstance(provider, dict) and provider.get("name") == name:
            return provider
    return {}


def _template_for_manifest(manifest: Manifest) -> str:
    kind = str(getattr(manifest, "kind", "") or "")
    if kind == "static":
        return "static-site"
    if getattr(manifest, "addons", None) and getattr(manifest.addons, "postgres", False):
        return "web-postgres"
    if getattr(manifest, "addons", None) and getattr(manifest.addons, "redis", False):
        return "web-redis"
    return "docker-web"


def _secret_names(value: Any) -> List[str]:
    names: List[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict) and isinstance(item.get("name"), str):
                names.append(str(item["name"]))
    return sorted(set(names))


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item) for item in value if isinstance(item, str)})


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _snapshot(
    name: str,
    status: str,
    owner: str,
    summary: str,
    *,
    desired: Optional[Dict[str, object]] = None,
    observed: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    return {
        "name": name,
        "status": status,
        "owner": owner,
        "summary": summary,
        "desired": desired or {},
        "observed": observed or {},
    }


def _finding(
    code: str,
    message: str,
    severity: str,
    owner: str,
    path: Optional[str],
    remediation_commands: List[str],
    plan_candidates: List[Dict[str, object]],
) -> Dict[str, object]:
    return {
        "code": code,
        "message": message,
        "severity": severity,
        "owner": owner,
        "path": path,
        "remediation_commands": remediation_commands,
        "plan_candidates": plan_candidates,
    }
