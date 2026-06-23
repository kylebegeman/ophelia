"""Read-only traffic provider config validation and explanation.

Operator UIs and operators point ``ship app traffic`` at a JSON provider config to
drive opt-in DNS/Caddy writes. This module validates that config *before* any
mutation is planned, and explains in plain language what each declared provider
*would* do, without ever touching a VPS, the network, or a secret value.

Two invariants this module exists to enforce:

1. **Secrets stay as env-var references, never literals.** Cloudflare must use
   ``api_token_env`` (the *name* of an environment variable), never an inline
   ``api_token``/``token``/``secret``/``password``/``private_key`` value. A raw
   token in the config is a blocker, and the offending value is never echoed.
2. **TTL is validated from a single source.** :func:`validate_ttl` is the one
   place the accepted Cloudflare TTL range lives; ``portability.py`` imports it
   so the CLI plan path and this validator can never drift.

The validator and explainer mirror :func:`ophelia.operation_schema.report_envelope`
structure (``schema_version``, ``kind``, ``status``, ``blockers``, ``warnings``,
``checks``) but carry their own ``kind`` strings. Every output is routed through
:func:`ophelia.redaction.deep_redact` before return so no nested value can leak.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import DEFAULT_RUNTIME_ROOT
from .operation_schema import SCHEMA_VERSION, error_envelope, issue
from .redaction import deep_redact, is_sensitive_key

# Provider groups and the types we understand within each.
KNOWN_PROVIDER_TYPES: Dict[str, Tuple[str, ...]] = {
    "dns": ("file", "cloudflare"),
    "caddy": ("file",),
}

# Example secret-bearing key names shown in the remediation message. The actual
# detection uses :func:`is_sensitive_key` (so api_key/access_token/client_secret/
# etc. are all caught), while any ``*_env`` reference is permitted.
RAW_SECRET_KEYS = ("api_token", "token", "secret", "password", "private_key", "api_key")


def _is_raw_secret_key(key: str) -> bool:
    """True for a key that should carry an ``*_env`` reference, not a literal.

    Reuses the central sensitivity definition so the set stays in sync with
    :data:`ophelia.redaction.SENSITIVE_KEY_PATTERNS`; ``*_env`` names are the
    accepted reference form and are never treated as raw secrets.
    """
    lowered = key.lower()
    return is_sensitive_key(key) and not lowered.endswith("_env")

VALIDATION_KIND = "ophelia.provider_config.validation"
EXPLANATION_KIND = "ophelia.provider_config.explanation"


def validate_ttl(value: object) -> Tuple[Optional[int], bool]:
    """Validate a DNS TTL value against the accepted Cloudflare range.

    Single source of truth for TTL validation. ``portability.py`` imports this
    so the traffic plan path and provider-config validation cannot drift.

    Accepts ``1`` (Cloudflare "automatic") or ``30``..``86400`` seconds, matching
    the range the traffic plan path has always enforced. Returns
    ``(parsed_or_None, ok)``. ``None``/empty parses to ``(None, True)`` so an
    omitted TTL is not treated as invalid; callers decide whether absence is
    acceptable. ``bool`` values and non-integers are rejected.
    """
    if value in (None, ""):
        return None, True
    if isinstance(value, bool):
        return None, False
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None, False
    if parsed == 1 or 30 <= parsed <= 86400:
        return parsed, True
    return parsed, False


def _load_config(config_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Load and JSON-parse the config; return ``(config, error_payload)``."""
    if not config_path.exists():
        return None, error_envelope(
            f"Provider config not found: {config_path}",
            "provider_config_missing",
            config_path=str(config_path),
        )
    try:
        raw = config_path.read_text()
    except OSError as exc:
        return None, error_envelope(
            f"Provider config could not be read: {exc}",
            "provider_config_unreadable",
            config_path=str(config_path),
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, error_envelope(
            f"Provider config is not valid JSON: {exc}",
            "provider_config_invalid_json",
            config_path=str(config_path),
        )
    if not isinstance(parsed, dict):
        return None, error_envelope(
            "Provider config must be a JSON object.",
            "provider_config_not_object",
            config_path=str(config_path),
        )
    return parsed, None


def _provider_section(config: Dict[str, Any], group: str, provider_name: str) -> Optional[Dict[str, Any]]:
    """Resolve a provider section, mirroring ``_traffic_provider_config_section``.

    Supports nested (``config["providers"]["dns"]["cloudflare"]``), flat
    (``config["dns"]["cloudflare"]``), and group-as-provider
    (``config["dns"]`` carrying ``provider``/``type``) shapes. Returns ``None``
    when the provider is not declared so callers can skip it.
    """
    providers = config.get("providers")
    if isinstance(providers, dict):
        grouped = providers.get(group)
        if isinstance(grouped, dict):
            nested = grouped.get(provider_name)
            if isinstance(nested, dict):
                return nested
    grouped = config.get(group)
    if isinstance(grouped, dict):
        nested = grouped.get(provider_name)
        if isinstance(nested, dict):
            return nested
        if grouped.get("type") == provider_name or grouped.get("provider") == provider_name:
            return grouped
    return None


def _declared_providers(config: Dict[str, Any]) -> List[Tuple[str, str, Dict[str, Any]]]:
    """Return declared ``(group, type, section)`` tuples plus unknown types.

    Unknown provider types are surfaced as ``(group, declared_type, section)``
    so the validator can flag them; the caller checks membership in
    :data:`KNOWN_PROVIDER_TYPES`.
    """
    found: List[Tuple[str, str, Dict[str, Any]]] = []
    seen: set[Tuple[str, str]] = set()

    def consider(group: str, provider_type: str, section: Dict[str, Any]) -> None:
        key = (group, provider_type)
        if key in seen:
            return
        seen.add(key)
        found.append((group, provider_type, section))

    for group, known in KNOWN_PROVIDER_TYPES.items():
        # Known types in nested/flat shapes.
        for provider_type in known:
            section = _provider_section(config, group, provider_type)
            if isinstance(section, dict):
                consider(group, provider_type, section)
        # Group-as-provider: a group dict carrying an explicit type/provider.
        grouped = config.get(group)
        if isinstance(grouped, dict):
            declared_type = grouped.get("type") or grouped.get("provider")
            if isinstance(declared_type, str) and declared_type:
                consider(group, declared_type, grouped)
            # Unknown nested keys that look like provider objects.
            for nested_name, nested in grouped.items():
                if isinstance(nested, dict) and nested_name not in known and nested_name not in {"type", "provider"}:
                    # Only treat as a provider declaration if it carries config-like keys.
                    if any(isinstance(k, str) for k in nested):
                        consider(group, nested_name, nested)
        providers = config.get("providers")
        if isinstance(providers, dict):
            nested_group = providers.get(group)
            if isinstance(nested_group, dict):
                for nested_name, nested in nested_group.items():
                    if isinstance(nested, dict) and nested_name not in known:
                        consider(group, nested_name, nested)
    return found


def _raw_secret_findings(group: str, provider_type: str, index: int, section: Dict[str, Any]) -> List[Dict[str, str]]:
    """Block any literal secret value carried directly on a provider object."""
    findings: List[Dict[str, str]] = []
    for key in section:
        if not isinstance(key, str):
            continue
        if _is_raw_secret_key(key):
            # We block on presence of the literal key regardless of its value
            # (an empty literal is still the wrong shape: it should be an *_env
            # ref). The value itself is never read or echoed.
            findings.append(
                issue(
                    "provider_token_must_be_env_ref",
                    (
                        f"`{group}.{provider_type}.{key}` must not carry a literal secret. "
                        f"Use an `*_env` environment variable reference (e.g. `api_token_env`)."
                    ),
                    f"providers[{index}].{key}",
                )
            )
    return findings


def _validate_file_dns(group: str, provider_type: str, index: int, section: Dict[str, Any]) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    record_file = section.get("record_file")
    if not isinstance(record_file, str) or not record_file.strip():
        blockers.append(
            issue(
                "dns_file_record_file_missing",
                "`dns.file.record_file` is required.",
                f"providers[{index}].record_file",
            )
        )
    return blockers, warnings


def _validate_cloudflare_dns(group: str, provider_type: str, index: int, section: Dict[str, Any]) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    zone_id = section.get("zone_id")
    if not isinstance(zone_id, str) or not zone_id.strip():
        blockers.append(
            issue("cloudflare_zone_id_missing", "`dns.cloudflare.zone_id` is required.", f"providers[{index}].zone_id")
        )
    api_token_env = section.get("api_token_env")
    if not isinstance(api_token_env, str) or not api_token_env.strip():
        blockers.append(
            issue(
                "cloudflare_api_token_env_missing",
                "`dns.cloudflare.api_token_env` is required and must name an environment variable.",
                f"providers[{index}].api_token_env",
            )
        )
    base_url = section.get("base_url")
    if isinstance(base_url, str) and base_url.strip() and not base_url.startswith("https://"):
        blockers.append(
            issue("cloudflare_base_url_invalid", "`dns.cloudflare.base_url` must start with https://.", f"providers[{index}].base_url")
        )
    ttl_parsed, ttl_ok = validate_ttl(section.get("ttl"))
    if not ttl_ok:
        blockers.append(
            issue(
                "cloudflare_ttl_invalid",
                "`dns.cloudflare.ttl` must be 1 (automatic) or between 30 and 86400 seconds.",
                f"providers[{index}].ttl",
            )
        )
    return blockers, warnings


def _validate_file_caddy(group: str, provider_type: str, index: int, section: Dict[str, Any], runtime_root: Path) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    sites_dir = section.get("sites_dir")
    if not isinstance(sites_dir, str) or not sites_dir.strip():
        blockers.append(
            issue("caddy_file_sites_dir_missing", "`caddy.file.sites_dir` is required.", f"providers[{index}].sites_dir")
        )
    timeout = section.get("timeout")
    if timeout not in (None, "") and not _is_positive_int(timeout):
        blockers.append(
            issue("caddy_file_timeout_invalid", "`caddy.file.timeout` must be a positive integer.", f"providers[{index}].timeout")
        )
    reload_requested = bool(section.get("reload"))
    validate_requested = bool(section.get("validate")) or reload_requested
    # The sites_dir-must-be-under-runtime-root constraint protects `caddy validate`
    # / `reload`, which operate against `<runtime_root>/caddy/sites.d`. A plain
    # local file write (allow_mutation without validate/reload) does not run those
    # commands, so it is not constrained here. This mirrors the apply path in
    # ``portability._plan_file_caddy_provider`` so the validator and the executor
    # cannot disagree about when the constraint applies.
    if validate_requested and isinstance(sites_dir, str) and sites_dir.strip():
        configured_root = section.get("runtime_root")
        root = Path(str(configured_root)).expanduser() if isinstance(configured_root, str) and configured_root.strip() else runtime_root
        expected = (root / "caddy" / "sites.d").expanduser().resolve(strict=False)
        actual = Path(sites_dir).expanduser().resolve(strict=False)
        if actual != expected:
            blockers.append(
                issue(
                    "caddy_file_sites_dir_outside_runtime_root",
                    "`caddy.file.sites_dir` must be `<runtime_root>/caddy/sites.d` when reload/mutation is requested.",
                    f"providers[{index}].sites_dir",
                )
            )
    return blockers, warnings


def _is_positive_int(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return int(str(value)) > 0
    except (TypeError, ValueError):
        return False


def _aggregate_status(blockers: List[Dict[str, str]], warnings: List[Dict[str, str]]) -> str:
    if blockers:
        return "blocked"
    if warnings:
        return "warn"
    return "ok"


def validate_provider_config(config_path: Path) -> Dict[str, Any]:
    """Validate a traffic provider config. Never echoes a secret value."""
    config_path = Path(config_path)
    config, error = _load_config(config_path)
    if error is not None:
        error["config_path"] = str(config_path)
        return deep_redact(error)

    assert config is not None
    declared = _declared_providers(config)
    providers_out: List[Dict[str, Any]] = []
    all_blockers: List[Dict[str, str]] = []
    all_warnings: List[Dict[str, str]] = []

    if not declared:
        all_warnings.append(
            issue("no_providers_declared", "No DNS or Caddy providers are declared in this config.")
        )

    for index, (group, provider_type, section) in enumerate(declared):
        blockers: List[Dict[str, str]] = []
        warnings: List[Dict[str, str]] = []

        known = KNOWN_PROVIDER_TYPES.get(group, ())
        if provider_type not in known:
            blockers.append(
                issue(
                    "provider_type_unknown",
                    f"Unknown {group} provider type `{provider_type}`. Known: {', '.join(known) or 'none'}.",
                    f"providers[{index}].type",
                )
            )
        else:
            blockers.extend(_raw_secret_findings(group, provider_type, index, section))
            if group == "dns" and provider_type == "file":
                b, w = _validate_file_dns(group, provider_type, index, section)
            elif group == "dns" and provider_type == "cloudflare":
                b, w = _validate_cloudflare_dns(group, provider_type, index, section)
            elif group == "caddy" and provider_type == "file":
                b, w = _validate_file_caddy(group, provider_type, index, section, DEFAULT_RUNTIME_ROOT)
            else:
                b, w = [], []
            blockers.extend(b)
            warnings.extend(w)

        providers_out.append(
            {
                "name": provider_type,
                "type": provider_type,
                "group": group,
                "status": _aggregate_status(blockers, warnings),
                "blockers": blockers,
                "warnings": warnings,
            }
        )
        all_blockers.extend(blockers)
        all_warnings.extend(warnings)

    status = _aggregate_status(all_blockers, all_warnings)
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": VALIDATION_KIND,
        "status": status,
        "config_path": str(config_path),
        "summary": (
            f"{len(providers_out)} provider(s) validated: "
            f"{len(all_blockers)} blocker(s), {len(all_warnings)} warning(s)."
        ),
        "providers": providers_out,
        "blockers": all_blockers,
        "warnings": all_warnings,
        "checks": [
            {
                "name": "provider_config_loaded",
                "ok": True,
                "message": str(config_path),
            }
        ],
        "values_redacted": True,
    }
    return deep_redact(payload)


def explain_provider_config(config_path: Path) -> Dict[str, Any]:
    """Explain, read-only, what each declared provider WOULD do. No secrets."""
    config_path = Path(config_path)
    config, error = _load_config(config_path)
    if error is not None:
        error["config_path"] = str(config_path)
        return deep_redact(error)

    assert config is not None
    declared = _declared_providers(config)
    providers_out: List[Dict[str, Any]] = []

    for group, provider_type, section in declared:
        explanation: Dict[str, Any] = {
            "name": provider_type,
            "type": provider_type,
            "group": group,
            "known": provider_type in KNOWN_PROVIDER_TYPES.get(group, ()),
        }
        if group == "dns" and provider_type == "file":
            record_file = section.get("record_file")
            explanation.update(
                {
                    "would": "Write DNS records into a local records file (no network).",
                    "record_file": record_file if isinstance(record_file, str) else None,
                    "allow_mutation": bool(section.get("allow_mutation")),
                    "env_refs": [],
                }
            )
        elif group == "dns" and provider_type == "cloudflare":
            api_token_env = section.get("api_token_env")
            explanation.update(
                {
                    "would": "Call the Cloudflare DNS API to upsert records for routed domains.",
                    "zone_id": section.get("zone_id") if isinstance(section.get("zone_id"), str) else None,
                    "base_url": section.get("base_url") if isinstance(section.get("base_url"), str) else "https://api.cloudflare.com/client/v4",
                    "proxied": bool(section.get("proxied")),
                    "allow_create": bool(section.get("allow_create")),
                    "allow_mutation": bool(section.get("allow_mutation")),
                    "env_refs": [api_token_env] if isinstance(api_token_env, str) and api_token_env.strip() else [],
                    "ttl": validate_ttl(section.get("ttl"))[0],
                }
            )
        elif group == "caddy" and provider_type == "file":
            sites_dir = section.get("sites_dir")
            explanation.update(
                {
                    "would": "Write a Caddy site file and optionally validate/reload Caddy locally.",
                    "sites_dir": sites_dir if isinstance(sites_dir, str) else None,
                    "reload": bool(section.get("reload")),
                    "allow_reload": bool(section.get("allow_reload")),
                    "allow_mutation": bool(section.get("allow_mutation")),
                    "env_refs": [],
                }
            )
        else:
            explanation.update(
                {
                    "would": "Unknown provider type; Ophelia would refuse to execute it.",
                    "env_refs": [],
                }
            )
        providers_out.append(explanation)

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": EXPLANATION_KIND,
        "status": "ok",
        "config_path": str(config_path),
        "summary": f"{len(providers_out)} provider(s) explained. Read-only; no mutation, no secrets.",
        "providers": providers_out,
        "blockers": [],
        "warnings": [],
        "values_redacted": True,
    }
    return deep_redact(payload)
