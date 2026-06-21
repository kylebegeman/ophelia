"""Names-only secrets audit for an app.

Composes a single, redaction-safe view of *which* env keys an app needs, where
each requirement comes from, and whether the key is present in the runtime env
(and optionally the process environment). It reports **names and presence
booleans only**, never a value. This is the surface Phase 4 readiness imports so
the "are required secrets in place?" question has one implementation.

Reuse, not reimplementation:
- :func:`ophelia.portability.env_shape_diff_report` does the manifest-vs-runtime
  env scanning (status ``missing``/``placeholder``/``present``/``extra``).
- :func:`ophelia.portability._desired_env_entries` enumerates the desired keys
  and their sources/required-by.
- Optional provider env refs are pulled by NAME from a discoverable provider
  config via :func:`ophelia.provider_config.explain_provider_config`.

Mirrors :func:`ophelia.operation_schema.report_envelope` structure with a
dedicated ``kind`` of ``ophelia.secrets_audit``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .config import DEFAULT_RUNTIME_ROOT
from .manifest import ManifestError, load_manifest
from .operation_schema import SCHEMA_VERSION, issue, operation_id
from .portability import env_shape_diff_report
from .provider_config import explain_provider_config

SECRETS_AUDIT_KIND = "ophelia.secrets_audit"


def _is_app_id(manifest_or_app: Union[str, Path]) -> bool:
    """Heuristic: a path-like or manifest-suffixed token is a manifest path."""
    text = str(manifest_or_app)
    if Path(text).exists():
        return False
    if text.endswith(".ophelia.yml") or text.endswith(".yaml") or text.endswith(".yml") or text.endswith(".json"):
        return False
    if "/" in text or "\\" in text:
        return False
    return True


def _resolve(
    manifest_or_app: Union[str, Path],
    environment: Optional[str],
) -> "tuple[Optional[str], Optional[Path]]":
    """Return ``(app_id, manifest_path)`` for a ``<manifest-or-app>`` argument.

    When the argument is a manifest path we load it to learn the app id; when it
    is an app id we let :func:`env_shape_diff_report` (via ``resolve_app_manifest``)
    do the lookup against the default manifest search dirs and runtime locks.
    """
    text = str(manifest_or_app)
    if _is_app_id(manifest_or_app):
        return text, None
    manifest_path = Path(text)
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError:
        return None, manifest_path
    return manifest.app, manifest_path


def _discover_provider_config(
    app: Optional[str],
    manifest_path: Optional[Path],
    runtime_root: Path,
) -> Optional[Path]:
    """Best-effort: find a provider config next to the manifest or under runtime.

    Returns the first existing candidate or ``None``. Provider configs are
    optional, so absence is never an error.
    """
    candidates: List[Path] = []
    if manifest_path is not None:
        parent = manifest_path.parent
        candidates.append(parent / "providers.json")
        candidates.append(parent / f"{manifest_path.stem.split('.')[0]}.providers.json")
    if app:
        candidates.append(runtime_root / "apps" / app / "providers.json")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _provider_env_refs(provider_config_path: Optional[Path]) -> List[Dict[str, str]]:
    """Return provider env-var *names* declared in a provider config (no values)."""
    if provider_config_path is None:
        return []
    explanation = explain_provider_config(provider_config_path)
    refs: List[Dict[str, str]] = []
    seen: set[str] = set()
    for provider in explanation.get("providers", []) if isinstance(explanation, dict) else []:
        if not isinstance(provider, dict):
            continue
        provider_name = str(provider.get("name") or provider.get("type") or "provider")
        for ref in provider.get("env_refs", []) or []:
            if isinstance(ref, str) and ref and ref not in seen:
                seen.add(ref)
                refs.append({"name": ref, "source": f"provider:{provider_name}"})
    return refs


def secrets_audit(
    manifest_or_app: Union[str, Path],
    environment: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    include_process_env: bool = False,
) -> Dict[str, Any]:
    """Names-only audit of an app's env/secret requirements and presence.

    Never returns a value. Each key carries ``value_redacted: True`` and only
    presence booleans. Missing *required* keys are blockers; extra runtime keys
    are warnings (names only).
    """
    runtime_root = Path(runtime_root)
    app, manifest_path = _resolve(manifest_or_app, environment)

    diff = env_shape_diff_report(
        app or str(manifest_or_app),
        environment=environment,
        runtime_root=runtime_root,
        manifest_path=manifest_path,
    )
    resolved_app = diff.get("app") or app
    resolved_env = diff.get("environment") or environment or "unknown"

    blockers: List[Dict[str, str]] = list(diff.get("blockers", []) or [])
    warnings: List[Dict[str, str]] = list(diff.get("warnings", []) or [])

    # diff entries already carry sources/required_by derived from
    # _desired_env_entries, so they are reused directly below.
    keys: List[Dict[str, Any]] = []
    process_env = os.environ if include_process_env else {}
    for entry in diff.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("key"))
        required = bool(entry.get("required"))
        runtime_present = bool(entry.get("runtime_present"))
        status = str(entry.get("status"))
        sources = entry.get("sources") if isinstance(entry.get("sources"), list) else []
        source = sources[0] if sources else ("runtime.env" if status == "extra" else "manifest")
        present = runtime_present
        if include_process_env and not present:
            present = name in process_env
        keys.append(
            {
                "name": name,
                "required": required,
                "source": str(source),
                "sources": [str(item) for item in sources],
                "status": status,
                "runtime_present": runtime_present,
                "present": present,
                "process_env_present": (name in process_env) if include_process_env else None,
                "example_present": "env.example" in [str(item) for item in sources],
                "value_redacted": True,
            }
        )
        if entry.get("status") == "extra":
            warnings.append(
                issue("extra_runtime_env_key", f"Runtime env key `{name}` is not declared by the manifest.", name)
            )

    # Provider env refs (optional). Add names not already covered.
    provider_config_path = _discover_provider_config(resolved_app, manifest_path, runtime_root)
    existing_names = {item["name"] for item in keys}
    for ref in _provider_env_refs(provider_config_path):
        name = ref["name"]
        if name in existing_names:
            continue
        existing_names.add(name)
        present = (name in process_env) if include_process_env else False
        keys.append(
            {
                "name": name,
                "required": True,
                "source": ref["source"],
                "sources": [ref["source"]],
                "status": "present" if present else "missing",
                "runtime_present": False,
                "present": present,
                "process_env_present": (name in process_env) if include_process_env else None,
                "example_present": False,
                "value_redacted": True,
            }
        )

    keys.sort(key=lambda item: item["name"])

    missing_required = [item for item in keys if item["required"] and not item["present"]]
    extra_keys = [item for item in keys if item["status"] == "extra"]

    # The manifest env diff already blocks on missing manifest-required keys.
    # Provider env refs are appended after the diff, so a missing *required*
    # provider ref must add its own blocker; otherwise `status` could read "ok"
    # while `required_secrets_present` reports a missing key.
    blocker_paths = {blocker.get("path") for blocker in blockers if blocker.get("path")}
    for item in missing_required:
        if str(item.get("source", "")).startswith("provider:") and item["name"] not in blocker_paths:
            blockers.append(
                issue(
                    "required_provider_secret_missing",
                    f"Required provider secret `{item['name']}` is not present.",
                    item["name"],
                )
            )
            blocker_paths.add(item["name"])

    if blockers:
        status = "blocked"
    elif warnings or extra_keys:
        status = "warn"
    else:
        status = "ok"

    checks = [
        {
            "name": "required_secrets_present",
            "ok": not missing_required,
            "message": f"{len(missing_required)} required key(s) missing.",
        },
        {
            "name": "provider_config_discovered",
            "ok": provider_config_path is not None,
            "message": str(provider_config_path) if provider_config_path is not None else "none",
        },
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": SECRETS_AUDIT_KIND,
        "operation": "secrets.audit",
        "operation_id": operation_id("secrets.audit", resolved_app, resolved_env),
        "status": status,
        "app": resolved_app,
        "environment": resolved_env,
        "summary": (
            f"Secrets audit for {resolved_app}: {len(keys)} key(s), "
            f"{len(missing_required)} missing required, {len(extra_keys)} extra. Names only, values redacted."
        ),
        "keys": keys,
        "provider_config_path": str(provider_config_path) if provider_config_path is not None else None,
        "include_process_env": include_process_env,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
        "values_redacted": True,
    }
