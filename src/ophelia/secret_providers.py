"""Secret-reference provider reports.

Secret providers validate names, presence booleans, locations, and freshness
metadata only. They never return raw secret values.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .operation_schema import SCHEMA_VERSION, issue, operation_id
from .redaction import deep_redact
from .secrets_audit import secrets_audit

SECRET_PROVIDER_REPORT_KIND = "ophelia.secret_provider_report"
SECRET_PROVIDER_STATUS_KIND = "ophelia.secret_provider_status"


def secret_provider_status(
    *,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    ophelia_root: Path = REPO_ROOT,
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    config, resolved_path, warnings = _load_integrations_config(runtime_root, ophelia_root, config_path)
    secrets_config = config.get("secrets") if isinstance(config.get("secrets"), dict) else {}
    providers = secrets_config.get("providers") if isinstance(secrets_config.get("providers"), dict) else {}
    provider_entries = [
        {
            "name": "local_runtime_env",
            "configured": bool(_provider_config(providers, "local_runtime_env").get("enabled", True)),
            "usable": True,
            "value_redacted": True,
        },
        {
            "name": "github_environments",
            "configured": bool(_provider_config(providers, "github_environments").get("enabled", True)),
            "usable": True,
            "observation_root": str(runtime_root / "github" / "secret-observations"),
            "value_redacted": True,
        },
        {
            "name": "sops_file",
            "configured": bool(_provider_config(providers, "sops").get("enabled", True)),
            "usable": True,
            "search_roots": _sops_search_roots(runtime_root, ophelia_root, providers),
            "value_redacted": True,
        },
    ]
    status = "warn" if warnings else "ok"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": SECRET_PROVIDER_STATUS_KIND,
        "operation": "secrets.providers.status",
        "status": status,
        "config_path": str(resolved_path) if resolved_path is not None else None,
        "configured": resolved_path is not None,
        "providers": provider_entries,
        "warnings": warnings,
        "blockers": [],
        "values_redacted": True,
        "summary": f"Secret provider status: {len(provider_entries)} provider contract(s).",
    }
    return deep_redact(payload, safe_keys={"values_redacted", "value_redacted"}, propagate=True)


def secret_provider_report(
    manifest_or_app: Union[str, Path],
    *,
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    ophelia_root: Path = REPO_ROOT,
    config_path: Optional[Path] = None,
    include_process_env: bool = False,
) -> Dict[str, Any]:
    runtime_root = Path(runtime_root)
    audit = secrets_audit(
        manifest_or_app,
        environment=environment,
        runtime_root=runtime_root,
        include_process_env=include_process_env,
    )
    app = str(audit.get("app") or manifest_or_app)
    resolved_env = str(audit.get("environment") or environment or "unknown")
    status = secret_provider_status(runtime_root=runtime_root, ophelia_root=ophelia_root, config_path=config_path)
    github_observation = _github_secret_observation(app, resolved_env, runtime_root)
    sops_keys, sops_path, sops_freshness = _sops_keys(app, resolved_env, runtime_root, ophelia_root, status)

    keys: List[Dict[str, Any]] = []
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = list(status.get("warnings", [])) if isinstance(status.get("warnings"), list) else []
    for item in audit.get("keys", []) if isinstance(audit.get("keys"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        name = str(item["name"])
        providers = [
            {
                "provider": "local_runtime_env",
                "location": str(runtime_root / "apps" / app / "env"),
                "present": bool(item.get("runtime_present")),
                "freshness": _file_freshness(runtime_root / "apps" / app / "env"),
                "value_redacted": True,
            },
            {
                "provider": "github_environment",
                "location": github_observation.get("location"),
                "present": name in github_observation.get("secret_names", []),
                "freshness": github_observation.get("freshness"),
                "observed": bool(github_observation.get("observed")),
                "value_redacted": True,
            },
            {
                "provider": "sops_file",
                "location": str(sops_path) if sops_path is not None else None,
                "present": name in sops_keys,
                "freshness": sops_freshness,
                "observed": sops_path is not None,
                "value_redacted": True,
            },
        ]
        present_any = any(bool(provider.get("present")) for provider in providers)
        required = bool(item.get("required"))
        if required and not present_any:
            blockers.append(issue("required_secret_provider_missing", f"Required secret `{name}` was not present in any observed provider.", name))
        keys.append(
            {
                "name": name,
                "required": required,
                "source": item.get("source"),
                "status": "present" if present_any else "missing" if required else "optional_missing",
                "present_any": present_any,
                "providers": providers,
                "value_redacted": True,
            }
        )

    report_status = "blocked" if blockers else "warn" if warnings else "ok"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": SECRET_PROVIDER_REPORT_KIND,
        "operation": "secrets.providers",
        "operation_id": operation_id("secrets.providers", app, resolved_env),
        "status": report_status,
        "app": app,
        "environment": resolved_env,
        "keys": sorted(keys, key=lambda entry: entry["name"]),
        "provider_status": status,
        "blockers": blockers,
        "warnings": warnings,
        "checks": [
            {
                "name": "required_secret_refs_present",
                "ok": not blockers,
                "message": f"{len(blockers)} required secret reference(s) missing across observed providers.",
            },
            {
                "name": "values_redacted",
                "ok": True,
                "message": "Only names, booleans, locations, and freshness metadata are emitted.",
            },
        ],
        "values_redacted": True,
        "summary": f"Secret provider report for {app}: {len(keys)} key(s), {len(blockers)} missing required.",
    }
    return deep_redact(payload, safe_keys={"values_redacted", "value_redacted"}, propagate=True)


def _github_secret_observation(app: str, environment: str, runtime_root: Path) -> Dict[str, Any]:
    root = runtime_root / "github" / "secret-observations"
    candidates = [root / f"{app}.{environment}.json", root / f"{app}.json"]
    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"observed": False, "location": str(path), "secret_names": [], "freshness": None}
        secret_names = _secret_names_for_environment(payload, environment)
        return {
            "observed": True,
            "location": str(path),
            "secret_names": secret_names,
            "freshness": _file_freshness(path),
        }
    return {"observed": False, "location": str(candidates[0]), "secret_names": [], "freshness": None}


def _secret_names_for_environment(payload: Any, environment: str) -> List[str]:
    if not isinstance(payload, dict):
        return []
    envs = payload.get("environments") if isinstance(payload.get("environments"), dict) else {}
    env_payload = envs.get(environment) if isinstance(envs.get(environment), dict) else payload
    secrets = env_payload.get("secrets") if isinstance(env_payload, dict) else None
    names: List[str] = []
    if isinstance(secrets, list):
        for item in secrets:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict) and isinstance(item.get("name"), str):
                names.append(str(item["name"]))
    return sorted(set(names))


def _sops_keys(
    app: str,
    environment: str,
    runtime_root: Path,
    ophelia_root: Path,
    provider_status: Dict[str, Any],
) -> Tuple[List[str], Optional[Path], Optional[Dict[str, Any]]]:
    candidates: List[Path] = []
    for provider in provider_status.get("providers", []) if isinstance(provider_status.get("providers"), list) else []:
        if isinstance(provider, dict) and provider.get("name") == "sops_file":
            for root in provider.get("search_roots", []) if isinstance(provider.get("search_roots"), list) else []:
                root_path = Path(str(root))
                candidates.extend(
                    [
                        root_path / f"{app}.{environment}.sops.yaml",
                        root_path / f"{app}.sops.yaml",
                        root_path / f"{app}.{environment}.sops.json",
                        root_path / f"{app}.sops.json",
                    ]
                )
    candidates.extend(
        [
            runtime_root / "secrets" / f"{app}.{environment}.sops.yaml",
            runtime_root / "secrets" / f"{app}.sops.yaml",
            ophelia_root / "secrets" / f"{app}.{environment}.sops.yaml",
            ophelia_root / "secrets" / f"{app}.sops.yaml",
        ]
    )
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if not path.exists():
            continue
        try:
            payload = _read_mapping(path)
        except (OSError, ValueError):
            return [], path, _file_freshness(path)
        return _secret_key_names(payload), path, _file_freshness(path)
    return [], None, None


def _secret_key_names(payload: Any, prefix: str = "") -> List[str]:
    names: List[str] = []
    if not isinstance(payload, dict):
        return names
    for key, value in payload.items():
        if key == "sops":
            continue
        full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            child_names = _secret_key_names(value, full_key)
            names.extend(child_names or [full_key])
        else:
            names.append(full_key)
    return sorted(set(names))


def _load_integrations_config(
    runtime_root: Path,
    ophelia_root: Path,
    config_path: Optional[Path],
) -> tuple[Dict[str, Any], Optional[Path], List[Dict[str, str]]]:
    warnings: List[Dict[str, str]] = []
    candidates = [Path(config_path)] if config_path is not None else [
        runtime_root / "integrations" / "ophelia-integrations.yml",
        runtime_root / "integrations" / "secrets.yml",
        ophelia_root / "config" / "ophelia-integrations.yml",
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            return _read_mapping(candidate), candidate, warnings
        except (OSError, ValueError) as exc:
            warnings.append(issue("secret_provider_config_unreadable", f"Could not read secret provider config: {type(exc).__name__}.", str(candidate)))
            return {}, candidate, warnings
    warnings.append(issue("secret_provider_config_missing", "No secret provider config discovered."))
    return {}, None, warnings


def _read_mapping(path: Path) -> Dict[str, Any]:
    text = path.read_text()
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise ValueError("PyYAML is required for YAML secret provider config") from exc
        payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise ValueError("config must be an object")
    return payload


def _provider_config(providers: Dict[str, Any], name: str) -> Dict[str, Any]:
    value = providers.get(name)
    return value if isinstance(value, dict) else {}


def _sops_search_roots(runtime_root: Path, ophelia_root: Path, providers: Dict[str, Any]) -> List[str]:
    config = _provider_config(providers, "sops")
    roots = config.get("search_roots") if isinstance(config.get("search_roots"), list) else []
    if roots:
        return [str(Path(str(root))) for root in roots]
    return [str(runtime_root / "secrets"), str(ophelia_root / "secrets")]


def _file_freshness(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"mtime": int(stat.st_mtime), "size_bytes": int(stat.st_size)}
