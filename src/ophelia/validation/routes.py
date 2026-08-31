"""Canonical HTTP paths safe for route matching and Caddy rendering."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, List

from . import _reject


_HEX = frozenset("0123456789abcdefABCDEF")
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_FORBIDDEN_RENDER_CHARACTERS = frozenset("\\?#{}*")


class HTTPPathKind(str, Enum):
    EXACT = "exact"
    PREFIX = "prefix"


@dataclass(frozen=True)
class CanonicalHTTPPath:
    value: str
    kind: HTTPPathKind

    def __str__(self) -> str:
        return self.value


def parse_http_path(
    value: Any,
    *,
    kind: HTTPPathKind = HTTPPathKind.EXACT,
    field: str = "path",
) -> CanonicalHTTPPath:
    """Validate and normalize percent escapes in an absolute HTTP path."""

    if not isinstance(kind, HTTPPathKind):
        _reject("invalid_http_path_kind", field, "%s has an invalid path kind." % field)
    if not isinstance(value, str) or not value.startswith("/"):
        _reject("invalid_http_path", field, "%s must start with /." % field)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _reject("http_path_control_character", field, "%s contains a control character." % field)
    if any(ord(character) > 127 for character in value):
        _reject(
            "http_path_non_ascii",
            field,
            "%s must encode non-ASCII bytes with percent escapes." % field,
        )
    if any(character in _FORBIDDEN_RENDER_CHARACTERS for character in value):
        _reject("http_path_forbidden_syntax", field, "%s contains forbidden route syntax." % field)
    if any(character.isspace() for character in value):
        _reject("http_path_whitespace", field, "%s may not contain whitespace." % field)
    if "//" in value:
        _reject("ambiguous_http_path", field, "%s may not contain empty path segments." % field)

    normalized: List[str] = []
    decoded: List[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != "%":
            normalized.append(character)
            decoded.append(character)
            index += 1
            continue
        if index + 2 >= len(value) or value[index + 1] not in _HEX or value[index + 2] not in _HEX:
            _reject("malformed_http_escape", field, "%s contains a malformed percent escape." % field)
        escape = value[index : index + 3]
        byte = int(escape[1:], 16)
        decoded_character = chr(byte)
        if byte < 32 or byte == 127:
            _reject("encoded_http_control", field, "%s contains an encoded control character." % field)
        if decoded_character in "/\\?#{}*":
            _reject("ambiguous_http_escape", field, "%s contains an ambiguous encoded delimiter." % field)
        normalized.append(
            decoded_character
            if decoded_character in _UNRESERVED
            else "%" + escape[1:].upper()
        )
        decoded.append(decoded_character)
        index += 3

    decoded_value = "".join(decoded)
    segments = decoded_value.split("/")[1:]
    if any(segment in {".", ".."} for segment in segments):
        _reject("http_path_dot_segment", field, "%s may not contain dot segments." % field)

    canonical = "".join(normalized)
    if kind is HTTPPathKind.PREFIX and (
        canonical == "/" or canonical.endswith("/")
    ):
        _reject(
            "ambiguous_http_prefix",
            field,
            "%s prefixes may not be / or end with /." % field,
        )

    return CanonicalHTTPPath(value=canonical, kind=kind)
