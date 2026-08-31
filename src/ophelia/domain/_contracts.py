"""Shared primitives for Ophelia's internal kernel contracts.

These contracts are deliberately not wired into the public CLI schemas yet.
They provide deterministic, secret-safe values for the P0 executor and its
adapters while the v1 dictionary envelopes remain compatible.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar, Dict, Mapping, Optional, Tuple, Type

from ..redaction import deep_redact


KERNEL_CONTRACT_SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_ENTITY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*_[A-Za-z0-9][A-Za-z0-9._-]{0,126}$")
_OPERATION_PATTERN = re.compile(
    r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*)+$"
)
_FORBIDDEN_MAPPING_KEYS = {
    "nonce",
    "approval_nonce",
    "raw_nonce",
    "raw_approval_nonce",
    "secret_value",
    "secret_values",
    "stdout",
    "stderr",
    "output",
    "command_output",
    "subprocess_output",
}


class ContractValidationError(ValueError):
    """Raised before an invalid value can cross the kernel boundary."""


class Contract:
    """Mixin for immutable, schema-versioned internal contract values."""

    schema_version: ClassVar[int] = KERNEL_CONTRACT_SCHEMA_VERSION
    kind: ClassVar[str] = "ophelia.kernel.contract"

    def to_dict(self) -> Dict[str, Any]:
        payload = _serialize(self)
        if not isinstance(payload, dict):  # pragma: no cover - defensive
            raise TypeError("Contract serialization must produce a mapping.")
        return payload

    def canonical_json(self) -> str:
        return canonical_json(self)

    def digest(self) -> str:
        return canonical_digest(self)


def _serialize(value: Any) -> Any:
    if isinstance(value, Contract):
        result: Dict[str, Any] = {
            "schema_version": value.schema_version,
            "kind": value.kind,
        }
        if not is_dataclass(value):  # pragma: no cover - all current contracts are dataclasses
            raise TypeError("Contract values must be dataclasses.")
        for item in fields(value):
            if item.metadata.get("serialize", True):
                result[item.name] = _serialize(getattr(value, item.name))
        return _secret_safe(result)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            item.name: _serialize(getattr(value, item.name))
            for item in fields(value)
            if item.metadata.get("serialize", True)
        }
    if isinstance(value, Mapping):
        result = {
            str(key): _serialize(item)
            for key, item in value.items()
            if str(key).lower() not in _FORBIDDEN_MAPPING_KEYS
        }
        return _secret_safe(result)
    if isinstance(value, (tuple, list)):
        return [_serialize(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("Unsupported contract value: %s" % type(value).__name__)


def _secret_safe(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Redact secret material while retaining validated digest metadata."""

    redacted = deep_redact(payload, propagate=True)
    restored = _restore_digest_metadata(payload, redacted)
    if not isinstance(restored, dict):  # pragma: no cover - payload is always a mapping
        raise TypeError("Secret-safe contract serialization must produce a mapping.")
    return restored


def _restore_digest_metadata(original: Any, redacted: Any) -> Any:
    if isinstance(original, Mapping) and isinstance(redacted, dict):
        for key, value in original.items():
            serialized_key = str(key)
            if _is_safe_digest_metadata(serialized_key, value):
                redacted[serialized_key] = value
            elif serialized_key in redacted:
                redacted[serialized_key] = _restore_digest_metadata(
                    value,
                    redacted[serialized_key],
                )
        return redacted
    if isinstance(original, list) and isinstance(redacted, list):
        return [
            _restore_digest_metadata(source, target)
            for source, target in zip(original, redacted)
        ]
    return redacted


def _is_safe_digest_metadata(key: str, value: Any) -> bool:
    lowered = key.lower()
    if lowered.endswith("_digest"):
        return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None
    if lowered.endswith("_digests"):
        return (
            isinstance(value, list)
            and all(
                isinstance(item, str) and _SHA256_PATTERN.fullmatch(item) is not None
                for item in value
            )
        )
    return False


def canonical_json(value: Any) -> str:
    """Return the one canonical JSON representation used for kernel digests."""

    return json.dumps(
        _serialize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def canonical_digest(value: Any) -> str:
    encoded = canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def digest_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def require_digest(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise ContractValidationError("%s must be a sha256:<64 lowercase hex> digest." % field_name)


def require_digests(values: Tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple):
        raise ContractValidationError("%s must be an immutable tuple." % field_name)
    for value in values:
        require_digest(value, field_name)
    if values != tuple(sorted(set(values))):
        raise ContractValidationError("%s must be sorted and contain no duplicates." % field_name)


def require_enum(value: Any, enum_type: Type[Enum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise ContractValidationError(
            "%s must be a %s value, not a raw string or another enum."
            % (field_name, enum_type.__name__)
        )


def require_slug(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > 63
        or not _SLUG_PATTERN.fullmatch(value)
    ):
        raise ContractValidationError(
            "%s must be a 1-63 character lowercase Ophelia slug." % field_name
        )


def require_entity_id(value: str, prefix: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith(prefix + "_")
        or not _ENTITY_ID_PATTERN.fullmatch(value)
    ):
        raise ContractValidationError("%s must be an Ophelia %s identifier." % (field_name, prefix))


def require_operation(value: str) -> None:
    if not isinstance(value, str) or len(value) > 127 or not _OPERATION_PATTERN.fullmatch(value):
        raise ContractValidationError("operation must be a dotted lowercase operation name.")


def require_text(value: str, field_name: str, maximum: int = 2048) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ContractValidationError("%s must be a non-empty string of at most %d characters." % (field_name, maximum))
    if any(ord(character) < 32 and character not in {"\n", "\t"} for character in value):
        raise ContractValidationError("%s must not contain control characters." % field_name)


def require_utc(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ContractValidationError("%s must be a UTC timestamp." % field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractValidationError("%s must be an ISO-8601 timestamp." % field_name) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ContractValidationError("%s must include a UTC offset." % field_name)


def parse_utc(value: str) -> datetime:
    require_utc(value, "timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def optional_entity_id(value: Optional[str], prefix: str, field_name: str) -> None:
    if value is not None:
        require_entity_id(value, prefix, field_name)
