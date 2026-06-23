"""Derive a JSON Schema (draft 2020-12) for the Ophelia manifest.

The schema is built from the manifest dataclasses in :mod:`ophelia.manifest`
using :func:`dataclasses.fields` and type hints to drive property names and
base types, with an explicit enum side-table for the values the parser
enforces. The goal is *agreement* with the parser: the schema accepts exactly
what ``load_manifest`` accepts and no less. Unknown extension fields are tolerated
the same way the parser tolerates them (silently ignored), so top-level and
nested strict objects use ``additionalProperties: true``.

This module is pure and read-only. It carries no secret defaults and no values
from any environment; it only describes the *shape* of a manifest.
"""

from __future__ import annotations

import dataclasses
import json
import typing
from typing import Any, Dict, List, Optional, Tuple, get_args, get_origin

from . import manifest as manifest_module

SCHEMA_VERSION = 1
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID = "https://ophelia.local/schemas/manifest.v1.json"

# Top-level required fields. Chosen to keep every examples/*.ophelia.yml valid:
# `services` is intentionally omitted because static/tunnel/redirect manifests
# (portfolio, demo-docs-*) do not declare services. The manifest parser still
# enforces kind-specific requirements that JSON Schema cannot express cleanly.
REQUIRED_TOP_LEVEL: List[str] = ["version", "app", "kind", "routes"]


# Enum side-table keyed by a dotted path of dataclass-field names. The dotted
# path mirrors the nesting of the dataclasses (not the YAML key names), so the
# builder can resolve enums while it walks the dataclass graph. Values mirror
# exactly what the parser enforces in ophelia.manifest.
_ENUM_BY_PATH: Dict[str, List[Any]] = {
    "kind": ["service", "multi-service", "static", "tunnel", "redirect"],
    "environment": ["dev", "staging", "production"],
    "profile": ["console"],
    "edge.catch_all.http_redirect_status": [301, 302, 307, 308],
    "pack.portability": ["critical", "standard", "static"],
    "networking.edge": ["shared"],
    "networking.internal": ["shared", "per-app"],
    "edge.tls.mode": ["auto", "internal", "custom"],
    "console.surface": ["console", "root"],
    "verify_policy.failure_mode": ["hard", "warn"],
    "verify.type": ["http", "command"],
    "observability.metrics.format": ["prometheus", "json", "none"],
    "observability.metrics.auth": ["none", "bearer_env", "basic_env"],
}

_STRING_SCHEMA_BY_PATH: Dict[str, Dict[str, Any]] = {
    "verify.url": {
        "type": "string",
        "pattern": r"^https?://(?![^/?#]*@)[^?#]*$",
    },
}

# A few dataclass field names are exported under a different YAML key by the
# parser/exporter (see ophelia.manifest._export_manifest_value).
_FIELD_KEY_ALIASES: Dict[str, str] = {
    "import_config": "import",
    "class_name": "class",
}

# Dotted dataclass-field paths that look required (no dataclass default) but are
# NOT required in YAML, because the parser synthesizes them. ServiceConfig.name
# comes from the services map key, never the service body; DataServiceConfig.mode
# is supplied a default by the parser when omitted. Dropping these keeps the
# schema in agreement with what `load_manifest` actually accepts.
_NOT_REQUIRED_IN_YAML: set = {
    "services.name",
    "data.postgres.mode",
    "data.redis.mode",
}


def manifest_json_schema() -> Dict[str, Any]:
    """Build the manifest JSON Schema as a plain dict."""
    properties, _ = _object_properties(manifest_module.Manifest, prefix="")
    schema: Dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "$id": SCHEMA_ID,
        "title": "Ophelia Manifest",
        "description": (
            "Schema for an Ophelia app manifest (*.ophelia.yml). Derived from the "
            "manifest dataclasses; unknown extension fields are tolerated to match "
            "the parser, which silently ignores unknown keys."
        ),
        "schema_version": SCHEMA_VERSION,
        "type": "object",
        "required": list(REQUIRED_TOP_LEVEL),
        "additionalProperties": True,
        "properties": properties,
        "allOf": [
            {
                "if": {"properties": {"kind": {"const": "redirect"}}},
                "then": {
                    "properties": {
                        "redirect_status": {"enum": [301, 302, 307, 308]},
                    }
                },
            }
        ],
    }
    return schema


def manifest_json_schema_text() -> str:
    """Serialize :func:`manifest_json_schema` deterministically."""
    return json.dumps(manifest_json_schema(), indent=2, sort_keys=True)


def _object_properties(
    dataclass_type: type, prefix: str
) -> Tuple[Dict[str, Any], List[str]]:
    """Return (properties, required-by-no-default) for a dataclass type."""
    properties: Dict[str, Any] = {}
    required: List[str] = []
    hints = typing.get_type_hints(dataclass_type)
    for f in dataclasses.fields(dataclass_type):
        if f.name == "extra":
            continue
        field_path = f"{prefix}.{f.name}" if prefix else f.name
        key = _FIELD_KEY_ALIASES.get(f.name, f.name)
        properties[key] = _schema_for_type(hints.get(f.name, f.type), field_path)
        if _field_is_required(f) and field_path not in _NOT_REQUIRED_IN_YAML:
            required.append(key)
    return properties, required


def _field_is_required(f: "dataclasses.Field") -> bool:
    return (
        f.default is dataclasses.MISSING
        and f.default_factory is dataclasses.MISSING  # type: ignore[attr-defined]
    )


def _schema_for_type(annotation: Any, path: str) -> Dict[str, Any]:
    """Map a (possibly Optional/typed-container) annotation to a schema node."""
    enum = _ENUM_BY_PATH.get(path)

    origin = get_origin(annotation)
    args = get_args(annotation)

    # Optional[X] / Union with NoneType: unwrap to the inner type. The field is
    # already non-required (it has a default), so nullability is implicit.
    if origin is typing.Union:
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) == 1:
            return _schema_for_type(non_none[0], path)
        # Heterogeneous unions (e.g. Any-like): permissive node.
        return _permissive_with_enum(enum)

    if enum is not None:
        return {"enum": list(enum)}

    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is str:
        if path in _STRING_SCHEMA_BY_PATH:
            return dict(_STRING_SCHEMA_BY_PATH[path])
        return {"type": "string"}

    if origin in (list, List):
        item_schema = _schema_for_type(args[0], path) if args else {}
        return {"type": "array", "items": item_schema}

    if origin in (dict, Dict):
        value_schema = _schema_for_type(args[1], path) if len(args) == 2 else {}
        node: Dict[str, Any] = {"type": "object", "additionalProperties": value_schema or True}
        return node

    if dataclasses.is_dataclass(annotation):
        return _dataclass_schema(annotation, path)

    # typing.Any or anything we cannot narrow: accept anything.
    return _permissive_with_enum(enum)


def _dataclass_schema(dataclass_type: type, path: str) -> Dict[str, Any]:
    properties, required = _object_properties(dataclass_type, path)
    # `extra`-bearing dataclasses already capture unknown keys; the others
    # silently ignore them. Either way, the parser never rejects unknown keys,
    # so the schema must not either.
    node: Dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }
    if required:
        node["required"] = required
    return node


def _permissive_with_enum(enum: Optional[List[Any]]) -> Dict[str, Any]:
    if enum is not None:
        return {"enum": list(enum)}
    return {}
