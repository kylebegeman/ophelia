from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import REPO_ROOT
from .operation_schema import SCHEMA_VERSION, issue
from .redaction import deep_redact, looks_like_secret_value

PLUGIN_MANIFEST_KIND = "ophelia.plugin_manifest"
PLUGIN_VALIDATION_KIND = "ophelia.plugin_validation"
PLUGIN_CATALOG_KIND = "ophelia.plugin_catalog"

DEFAULT_PLUGINS_DIR = REPO_ROOT / "plugins"
PLUGIN_MANIFEST_NAMES = (
    "ophelia-plugin.yml",
    "ophelia-plugin.yaml",
    "ophelia-plugin.json",
)

ALLOWED_CAPABILITY_TYPES = {
    "app_template",
    "workflow_template",
    "policy_pack",
    "provider_adapter",
    "secret_provider",
    "host_inventory_adapter",
    "lumen_surface",
}

ALLOWED_TOP_LEVEL_KEYS = {
    "schema_version",
    "kind",
    "name",
    "version",
    "display_name",
    "description",
    "enabled_by_default",
    "capabilities",
    "commands",
    "lumen_surfaces",
    "compatibility",
    "safety_notes",
    "metadata",
}

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,79}$")
_SECRET_LITERAL_KEY_PARTS = ("token", "password", "private_key", "api_key", "credential")


def plugin_inventory(plugins_dir: Path = DEFAULT_PLUGINS_DIR) -> Dict[str, Any]:
    """Return a read-only inventory of trusted plugin manifests.

    This is metadata discovery only. Ophelia never imports plugin code from this
    surface and never registers plugin commands into the executable command
    catalog. A missing plugin directory is a clean empty catalog.
    """
    plugins_dir = Path(plugins_dir)
    warnings: List[Dict[str, str]] = []
    blockers: List[Dict[str, str]] = []
    plugins: List[Dict[str, Any]] = []

    if not plugins_dir.exists():
        payload = {
            "schema_version": SCHEMA_VERSION,
            "kind": PLUGIN_CATALOG_KIND,
            "status": "ok",
            "plugins_dir": str(plugins_dir),
            "trusted_directory": True,
            "plugins": [],
            "blockers": [],
            "warnings": [],
            "summary": f"No plugin directory found at {plugins_dir}.",
            "values_redacted": True,
        }
        return deep_redact(payload, safe_keys={"values_redacted"}, propagate=True)

    if not plugins_dir.is_dir():
        blockers.append(issue("plugins_dir_not_directory", "Plugin discovery path is not a directory.", "plugins_dir"))
    else:
        for manifest_path in _discover_plugin_manifests(plugins_dir):
            report = validate_plugin_manifest(manifest_path, trusted_root=plugins_dir)
            if report.get("status") == "ok":
                plugin = report.get("plugin")
                if isinstance(plugin, dict):
                    plugins.append(plugin)
            else:
                blockers.extend(
                    issue(
                        str(item.get("code") or "plugin_invalid"),
                        f"{manifest_path}: {item.get('message') or item}",
                        str(item.get("path") or manifest_path),
                    )
                    for item in report.get("blockers", [])
                    if isinstance(item, dict)
                )
            warnings.extend(
                issue(
                    str(item.get("code") or "plugin_warning"),
                    f"{manifest_path}: {item.get('message') or item}",
                    str(item.get("path") or manifest_path),
                )
                for item in report.get("warnings", [])
                if isinstance(item, dict)
            )

    duplicate_names = _duplicates(str(plugin.get("name") or "") for plugin in plugins)
    for name in duplicate_names:
        blockers.append(issue("plugin_name_duplicate", f"Plugin name `{name}` appears more than once.", name))

    status = "blocked" if blockers else "warning" if warnings else "ok"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": PLUGIN_CATALOG_KIND,
        "status": status,
        "plugins_dir": str(plugins_dir),
        "trusted_directory": True,
        "plugins": sorted(plugins, key=lambda plugin: str(plugin.get("name") or "")),
        "blockers": blockers,
        "warnings": warnings,
        "summary": f"{len(plugins)} plugin manifest(s) discovered; {len(blockers)} blocker(s), {len(warnings)} warning(s).",
        "values_redacted": True,
    }
    return deep_redact(payload, safe_keys={"values_redacted"}, propagate=True)


def validate_plugin_manifest(path: Path, trusted_root: Optional[Path] = None) -> Dict[str, Any]:
    path = Path(path)
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    raw: Dict[str, Any] = {}

    if trusted_root is not None and not _is_relative_to(path.resolve(), Path(trusted_root).resolve()):
        blockers.append(issue("plugin_manifest_outside_trusted_root", "Plugin manifest is outside the trusted plugin directory.", str(path)))

    try:
        raw = _read_mapping(path)
    except FileNotFoundError:
        blockers.append(issue("plugin_manifest_missing", "Plugin manifest was not found.", str(path)))
    except (OSError, ValueError) as exc:
        blockers.append(issue("plugin_manifest_unreadable", f"Plugin manifest could not be read: {exc}.", str(path)))

    plugin: Dict[str, Any] = {}
    if raw:
        plugin = _normalize_plugin(raw, path, blockers, warnings)

    status = "blocked" if blockers else "warning" if warnings else "ok"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": PLUGIN_VALIDATION_KIND,
        "status": status,
        "manifest_path": str(path),
        "plugin": plugin if not blockers else None,
        "blockers": blockers,
        "warnings": warnings,
        "summary": f"Plugin manifest {path}: {status}.",
        "values_redacted": True,
    }
    return deep_redact(payload, safe_keys={"values_redacted"}, propagate=True)


def _normalize_plugin(
    raw: Dict[str, Any],
    path: Path,
    blockers: List[Dict[str, str]],
    warnings: List[Dict[str, str]],
) -> Dict[str, Any]:
    unknown_keys = sorted(set(raw) - ALLOWED_TOP_LEVEL_KEYS)
    for key in unknown_keys:
        warnings.append(issue("plugin_unknown_key", f"Unknown top-level plugin key `{key}`.", key))

    schema_version = raw.get("schema_version")
    if schema_version != 1:
        blockers.append(issue("plugin_schema_version_invalid", "`schema_version` must be 1.", "schema_version"))

    kind = raw.get("kind", PLUGIN_MANIFEST_KIND)
    if kind != PLUGIN_MANIFEST_KIND:
        blockers.append(issue("plugin_kind_invalid", f"`kind` must be `{PLUGIN_MANIFEST_KIND}`.", "kind"))

    name = _required_slug(raw.get("name"), "name", blockers)
    version = _required_text(raw.get("version"), "version", blockers)
    display_name = _optional_text(raw.get("display_name"), "display_name", blockers)
    description = _optional_text(raw.get("description"), "description", blockers)
    enabled_by_default = raw.get("enabled_by_default", False)
    if not isinstance(enabled_by_default, bool):
        blockers.append(issue("plugin_enabled_by_default_invalid", "`enabled_by_default` must be boolean.", "enabled_by_default"))
        enabled_by_default = False
    if enabled_by_default:
        blockers.append(issue("plugin_enabled_by_default_forbidden", "Plugins must be disabled by default in this phase.", "enabled_by_default"))

    capabilities = _normalize_capabilities(raw.get("capabilities"), blockers)
    commands = _normalize_commands(raw.get("commands"), name, blockers)
    lumen_surfaces = _normalize_lumen_surfaces(raw.get("lumen_surfaces"), blockers)
    compatibility = _optional_mapping(raw.get("compatibility"), "compatibility", blockers)
    metadata = _optional_mapping(raw.get("metadata"), "metadata", blockers)
    safety_notes = _string_list(raw.get("safety_notes"), "safety_notes", blockers)

    _scan_for_literal_secrets(raw, blockers)

    capability_counts: Dict[str, int] = {}
    for capability in capabilities:
        cap_type = str(capability.get("type") or "")
        capability_counts[cap_type] = capability_counts.get(cap_type, 0) + 1

    return {
        "name": name,
        "version": version,
        "display_name": display_name or name,
        "description": description,
        "enabled": False,
        "enabled_by_default": False,
        "manifest_path": str(path),
        "capabilities": capabilities,
        "capability_counts": capability_counts,
        "commands": commands,
        "lumen_surfaces": lumen_surfaces,
        "compatibility": compatibility,
        "metadata": metadata,
        "safety_notes": safety_notes,
        "values_redacted": True,
    }


def _normalize_capabilities(value: Any, blockers: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        blockers.append(issue("plugin_capabilities_invalid", "`capabilities` must be a list.", "capabilities"))
        return []
    capabilities: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for index, item in enumerate(value):
        path = f"capabilities[{index}]"
        if not isinstance(item, dict):
            blockers.append(issue("plugin_capability_invalid", "Capability entries must be mappings.", path))
            continue
        cap_type = item.get("type")
        cap_id = item.get("id")
        if cap_type not in ALLOWED_CAPABILITY_TYPES:
            blockers.append(issue("plugin_capability_type_invalid", f"Capability type `{cap_type}` is not allowed.", f"{path}.type"))
            continue
        cap_id = _required_slug(cap_id, f"{path}.id", blockers)
        key = (str(cap_type), cap_id)
        if key in seen:
            blockers.append(issue("plugin_capability_duplicate", f"Duplicate capability `{cap_type}:{cap_id}`.", path))
            continue
        seen.add(key)
        normalized = {
            "type": cap_type,
            "id": cap_id,
            "summary": _optional_text(item.get("summary"), f"{path}.summary", blockers),
            "metadata": {
                str(key): value
                for key, value in item.items()
                if key not in {"type", "id", "summary"} and _json_scalar_or_collection(value)
            },
        }
        capabilities.append(normalized)
    return capabilities


def _normalize_commands(value: Any, plugin_name: str, blockers: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        blockers.append(issue("plugin_commands_invalid", "`commands` must be a list.", "commands"))
        return []
    commands: List[Dict[str, Any]] = []
    for index, item in enumerate(value):
        path = f"commands[{index}]"
        if not isinstance(item, dict):
            blockers.append(issue("plugin_command_invalid", "Plugin command descriptors must be mappings.", path))
            continue
        command = _required_text(item.get("command"), f"{path}.command", blockers)
        operation = _required_text(item.get("operation"), f"{path}.operation", blockers)
        if operation and plugin_name and not operation.startswith(f"plugin.{plugin_name}."):
            blockers.append(
                issue(
                    "plugin_command_operation_invalid",
                    f"Plugin command operation `{operation}` must start with `plugin.{plugin_name}.`.",
                    f"{path}.operation",
                )
            )
        args_schema = item.get("args_schema", {"type": "object", "properties": {}, "additionalProperties": False})
        _validate_args_schema(args_schema, f"{path}.args_schema", blockers)
        mutates_state = item.get("mutates_state", False)
        requires_confirmation = item.get("requires_confirmation", False)
        plan_command = item.get("plan_command")
        if not isinstance(mutates_state, bool):
            blockers.append(issue("plugin_command_mutates_state_invalid", "`mutates_state` must be boolean.", f"{path}.mutates_state"))
            mutates_state = False
        if not isinstance(requires_confirmation, bool):
            blockers.append(issue("plugin_command_requires_confirmation_invalid", "`requires_confirmation` must be boolean.", f"{path}.requires_confirmation"))
            requires_confirmation = False
        if mutates_state and (not requires_confirmation or not isinstance(plan_command, str) or not plan_command.strip()):
            blockers.append(
                issue(
                    "plugin_mutation_without_plan",
                    "Mutating plugin commands must declare `requires_confirmation: true` and `plan_command`.",
                    path,
                )
            )
        commands.append(
            {
                "command": command,
                "operation": operation,
                "summary": _optional_text(item.get("summary"), f"{path}.summary", blockers) or "",
                "risk": _risk(item.get("risk"), f"{path}.risk", blockers),
                "mutates_state": bool(mutates_state),
                "requires_confirmation": bool(requires_confirmation),
                "plan_command": plan_command if isinstance(plan_command, str) and plan_command.strip() else None,
                "apply_command": item.get("apply_command") if isinstance(item.get("apply_command"), str) else None,
                "json_kind": item.get("json_kind") if isinstance(item.get("json_kind"), str) else "ophelia.plugin.report",
                "args_schema": args_schema if isinstance(args_schema, dict) else {},
                "examples": _string_list(item.get("examples"), f"{path}.examples", blockers),
            }
        )
    return commands


def _normalize_lumen_surfaces(value: Any, blockers: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        blockers.append(issue("plugin_lumen_surfaces_invalid", "`lumen_surfaces` must be a list.", "lumen_surfaces"))
        return []
    surfaces: List[Dict[str, Any]] = []
    for index, item in enumerate(value):
        path = f"lumen_surfaces[{index}]"
        if not isinstance(item, dict):
            blockers.append(issue("plugin_lumen_surface_invalid", "Lumen surface entries must be mappings.", path))
            continue
        surfaces.append(
            {
                "id": _required_slug(item.get("id"), f"{path}.id", blockers),
                "title": _optional_text(item.get("title"), f"{path}.title", blockers),
                "kind": _optional_text(item.get("kind"), f"{path}.kind", blockers) or "panel",
                "summary": _optional_text(item.get("summary"), f"{path}.summary", blockers),
                "read_only": item.get("read_only", True) is not False,
            }
        )
    return surfaces


def _validate_args_schema(value: Any, path: str, blockers: List[Dict[str, str]]) -> None:
    if not isinstance(value, dict):
        blockers.append(issue("plugin_args_schema_invalid", "Plugin `args_schema` must be a mapping.", path))
        return
    if value.get("type") != "object":
        blockers.append(issue("plugin_args_schema_type_invalid", "Plugin `args_schema.type` must be `object`.", f"{path}.type"))
    properties = value.get("properties", {})
    if not isinstance(properties, dict):
        blockers.append(issue("plugin_args_schema_properties_invalid", "`args_schema.properties` must be a mapping.", f"{path}.properties"))
    required = value.get("required", [])
    if required is not None and not (isinstance(required, list) and all(isinstance(item, str) for item in required)):
        blockers.append(issue("plugin_args_schema_required_invalid", "`args_schema.required` must be a list of strings.", f"{path}.required"))
    if value.get("additionalProperties") is not False:
        blockers.append(
            issue(
                "plugin_args_schema_unbounded",
                "Plugin `args_schema.additionalProperties` must be false.",
                f"{path}.additionalProperties",
            )
        )


def _discover_plugin_manifests(root: Path) -> List[Path]:
    manifests: List[Path] = []
    for name in PLUGIN_MANIFEST_NAMES:
        direct = root / name
        if direct.exists():
            manifests.append(direct)
    for child in sorted(root.iterdir()) if root.exists() and root.is_dir() else []:
        if not child.is_dir():
            continue
        for name in PLUGIN_MANIFEST_NAMES:
            candidate = child / name
            if candidate.exists():
                manifests.append(candidate)
                break
    return sorted(set(manifests))


def _read_mapping(path: Path) -> Dict[str, Any]:
    text = path.read_text()
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise ValueError("PyYAML is required for YAML plugin manifests") from exc
        payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise ValueError("plugin manifest root must be an object")
    return payload


def _required_slug(value: Any, path: str, blockers: List[Dict[str, str]]) -> str:
    if not isinstance(value, str) or not _SLUG_RE.fullmatch(value):
        blockers.append(issue("plugin_slug_invalid", f"`{path}` must be a lowercase slug.", path))
        return ""
    return value


def _required_text(value: Any, path: str, blockers: List[Dict[str, str]]) -> str:
    if not isinstance(value, str) or not value.strip():
        blockers.append(issue("plugin_text_invalid", f"`{path}` must be a non-empty string.", path))
        return ""
    return value.strip()


def _optional_text(value: Any, path: str, blockers: List[Dict[str, str]]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        blockers.append(issue("plugin_text_invalid", f"`{path}` must be a non-empty string.", path))
        return None
    return value.strip()


def _optional_mapping(value: Any, path: str, blockers: List[Dict[str, str]]) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        blockers.append(issue("plugin_mapping_invalid", f"`{path}` must be a mapping.", path))
        return {}
    if not _json_scalar_or_collection(value):
        blockers.append(issue("plugin_mapping_non_json", f"`{path}` must contain JSON-compatible values.", path))
        return {}
    return dict(value)


def _string_list(value: Any, path: str, blockers: List[Dict[str, str]]) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        blockers.append(issue("plugin_string_list_invalid", f"`{path}` must be a list of non-empty strings.", path))
        return []
    return [item.strip() for item in value]


def _risk(value: Any, path: str, blockers: List[Dict[str, str]]) -> str:
    if value is None:
        return "low"
    if value not in {"low", "medium", "high"}:
        blockers.append(issue("plugin_risk_invalid", f"`{path}` must be one of low, medium, or high.", path))
        return "low"
    return str(value)


def _json_scalar_or_collection(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_json_scalar_or_collection(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_scalar_or_collection(item) for key, item in value.items())
    return False


def _scan_for_literal_secrets(value: Any, blockers: List[Dict[str, str]], path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            lowered = str(key).lower()
            if any(part in lowered for part in _SECRET_LITERAL_KEY_PARTS) and item not in (None, "", [], {}):
                blockers.append(issue("plugin_literal_secret_forbidden", "Plugin manifests may reference secret names only, never literal secret values.", child_path))
            _scan_for_literal_secrets(item, blockers, child_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_for_literal_secrets(item, blockers, f"{path}[{index}]")
    elif looks_like_secret_value(value):
        blockers.append(issue("plugin_secret_shaped_value_forbidden", "Plugin manifest contains a secret-shaped value.", path))


def _duplicates(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

