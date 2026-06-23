"""Centralized redaction helpers.

Every Ophelia surface that emits JSON (plans, receipts, reports, the state DB,
operator-console payloads, and diff artifacts) must route secret-bearing values through
this module. Redaction is decided by key name and by value shape, never by the
caller guessing. The existing whitelist redactors that used to live in
``portability.py`` (`redacted_cloudflare_record`, `redacted_compose_text`) now
live here so there is a single source of truth.

Rules:
- Never return a raw secret. Replace it with ``REDACTED``.
- Preserve useful metadata: a redacted mapping keeps its keys so callers can
  still report "present vs missing" without exposing values.
- Empty string / ``None`` are passed through unchanged so "present but empty"
  stays observable.
"""

from __future__ import annotations

import shlex
from typing import Any, Dict, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

REDACTED = "<redacted>"

# Substrings (case-insensitive) that mark a key as carrying a secret value.
SENSITIVE_KEY_PATTERNS = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PRIVATE_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "API_KEY",
    "CREDENTIAL",
)

# Compose env keys that are structural, not secret, and safe to surface.
COMPOSE_SAFE_KEYS = ("OPHELIA_APP", "OPHELIA_SERVICE", "PORT")

# Cloudflare DNS record fields that carry no credentials and are safe to show.
_CLOUDFLARE_SAFE_FIELDS = (
    "id",
    "type",
    "name",
    "content",
    "ttl",
    "proxied",
    "comment",
    "created_on",
    "modified_on",
)

# Connection-string schemes whose values are always treated as secrets.
_SECRET_VALUE_PREFIXES = (
    "postgres://",
    "postgresql://",
    "redis://",
    "rediss://",
    "mysql://",
    "mongodb://",
    "amqp://",
    "amqps://",
)

_COMMAND_STRING_KEYS = {
    "command",
    "commands",
    "exact_command",
    "recommended_command",
}

_SENSITIVE_COMMAND_FLAGS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api-key",
    "apikey",
    "private-key",
    "private_key",
    "database-url",
    "database_url",
    "redis-url",
    "redis_url",
)


def is_sensitive_key(name: object) -> bool:
    """True when a key name looks like it holds a secret value."""
    if not isinstance(name, str):
        return False
    upper = name.upper()
    return any(pattern in upper for pattern in SENSITIVE_KEY_PATTERNS)


def looks_like_secret_value(value: object) -> bool:
    """True when a value's shape looks like a credential regardless of its key.

    Catches connection strings and any URL that embeds ``user:pass@host`` so a
    benignly named key (``url``, ``dsn``) cannot leak credentials.
    """
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not candidate:
        return False
    lowered = candidate.lower()
    if any(lowered.startswith(prefix) for prefix in _SECRET_VALUE_PREFIXES):
        return True
    if "://" in candidate:
        authority = candidate.split("://", 1)[1].split("/", 1)[0]
        if "@" in authority and ":" in authority.split("@", 1)[0]:
            return True
    return False


def looks_like_command_key(name: object) -> bool:
    """True when a key conventionally carries a runnable command string."""
    if not isinstance(name, str):
        return False
    lowered = name.lower()
    return lowered in _COMMAND_STRING_KEYS or lowered.endswith("_command") or lowered.endswith("_commands")


def redact_command_string(command: str, marker: str = REDACTED) -> str:
    """Mask secret-shaped command arguments while preserving useful argv shape.

    Manifest restore/verify hooks are intentionally free-form strings, so a
    caller cannot know whether they contain literal credentials. This scrubber is
    conservative: it masks values after sensitive option names, ``KEY=value``
    assignments whose key is sensitive, and any token whose value shape already
    looks like a credential (for example a URL with ``user:pass@host``).
    """
    if not isinstance(command, str) or not command:
        return command
    try:
        parts = shlex.split(command)
    except ValueError:
        # Malformed shell syntax is still displayable; fall back to whitespace
        # splitting so obvious credentials are still removed.
        parts = command.split()

    redacted = []
    redact_next = False
    for part in parts:
        if redact_next:
            redacted.append(marker)
            redact_next = False
            continue

        option, separator, option_value = part.partition("=")
        option_name = option.lstrip("-").lower()
        if separator and any(flag in option_name for flag in _SENSITIVE_COMMAND_FLAGS):
            redacted.append(f"{option}={marker}")
            continue
        if separator and (is_sensitive_key(option) or looks_like_secret_value(option_value)):
            redacted.append(f"{option}={marker}")
            continue
        if part.startswith("-") and any(flag in option_name for flag in _SENSITIVE_COMMAND_FLAGS):
            redacted.append(part)
            redact_next = True
            continue
        if looks_like_secret_value(part):
            redacted.append(str(marker))
            continue
        redacted.append(part)

    if redact_next:
        redacted.append(marker)
    return " ".join(shlex.quote(part) for part in redacted)


def redact_url(url: str, marker: str = REDACTED) -> str:
    """Mask URL credentials, query strings, and fragments for emitted metadata."""
    if not isinstance(url, str) or not url:
        return url
    try:
        parsed = urlsplit(url)
    except ValueError:
        return marker
    if not parsed.scheme or not parsed.netloc:
        return url

    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return marker
    host = hostname or parsed.netloc.rsplit("@", 1)[-1]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None:
        host = f"{host}:{port}"
    if parsed.username is not None or parsed.password is not None:
        host = f"{marker}@{host}"

    query = marker if parsed.query else ""
    fragment = marker if parsed.fragment else ""
    return urlunsplit((parsed.scheme, host, parsed.path, query, fragment))


def redact_value(value: object, marker: str = REDACTED) -> object:
    """Replace a real value with ``marker``; keep ``None`` and empty string.

    Keeping empty/``None`` lets callers still distinguish "set but empty" from
    "has a value" without ever revealing the value.
    """
    if value is None:
        return None
    if isinstance(value, str) and value == "":
        return ""
    return marker


def redact_mapping(
    mapping: Mapping[str, Any],
    *,
    marker: str = REDACTED,
    safe_keys: Iterable[str] = (),
    redact_by_value: bool = True,
) -> Dict[str, Any]:
    """Return a shallow copy of ``mapping`` with top-level secret values masked.

    A value is masked when its key matches :data:`SENSITIVE_KEY_PATTERNS` or,
    when ``redact_by_value`` is set, when the value's shape looks like a
    credential. Keys listed in ``safe_keys`` are always passed through. This is
    intentionally shallow; use :func:`deep_redact` when nested data may hide a
    secret-shaped value.
    """
    safe = set(safe_keys)
    result: Dict[str, Any] = {}
    for key, value in mapping.items():
        if key in safe:
            result[key] = value
        elif is_sensitive_key(key) or (redact_by_value and looks_like_secret_value(value)):
            result[key] = redact_value(value, marker)
        else:
            result[key] = value
    return result


def deep_redact(
    obj: Any,
    *,
    marker: str = REDACTED,
    safe_keys: Iterable[str] = (),
    propagate: bool = False,
) -> Any:
    """Recursively mask secret-bearing scalars in nested dicts/lists.

    A scalar is masked when its immediately-enclosing key is sensitive
    (:func:`is_sensitive_key`) or its own value shape looks like a credential
    (:func:`looks_like_secret_value`), recursing through dicts and lists at any
    depth. Structure (keys, list order, and non-secret scalars such as booleans,
    ports, and plain strings) is preserved so the result stays useful as metadata.
    Use this on payloads whose nested shape is not fully known (manifest locks,
    backup manifests, provider configs) before they reach a report, the state DB,
    or an API response.

    By default sensitivity is decided per-key, not propagated into a whole
    subtree under a sensitive key. Data-bearing call sites can opt into
    ``propagate=True`` so a sensitive-named container masks all nested scalars
    without applying that aggressive behavior to metadata/schema descriptors.
    """
    safe = set(safe_keys)

    def _walk(value: Any, key_is_sensitive: bool, key_name: object = None) -> Any:
        if isinstance(value, dict):
            return {
                key: _walk(
                    item,
                    ((key not in safe) and is_sensitive_key(key)) or (propagate and key_is_sensitive),
                    key,
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [_walk(item, key_is_sensitive, key_name) for item in value]
        if key_is_sensitive:
            return redact_value(value, marker)
        if isinstance(value, str) and looks_like_command_key(key_name):
            return redact_command_string(value, marker)
        if looks_like_secret_value(value):
            return redact_value(value, marker)
        return value

    return _walk(obj, False)


def redacted_cloudflare_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Whitelist a Cloudflare DNS record down to its non-credential fields."""
    return {key: record.get(key) for key in _CLOUDFLARE_SAFE_FIELDS if key in record}


def redacted_compose_text(content: str, marker: str = REDACTED) -> str:
    """Mask service-level env values in rendered Compose text.

    Only indented ``      KEY: value`` lines are touched, mirroring how Compose
    nests ``environment`` entries; structural keys in :data:`COMPOSE_SAFE_KEYS`
    are preserved.
    """
    safe_keys = set(COMPOSE_SAFE_KEYS)
    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if ":" in stripped:
            key, _value = stripped.split(":", 1)
            if key and key.replace("_", "").isalnum() and key not in safe_keys and line.startswith("      "):
                indent = line[: len(line) - len(line.lstrip())]
                lines.append(f'{indent}{key}: "{marker}"')
                continue
        lines.append(line)
    return "\n".join(lines) + "\n"
