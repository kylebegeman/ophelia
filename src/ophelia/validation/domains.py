"""Canonical, injection-safe DNS domain names.

The implementation deliberately uses Python's stdlib IDNA 2003 codec with a
strict NFC-lowercase decode/re-encode round trip. The codec's transitional
mappings are not accepted when they change the caller's Unicode label.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, List

from . import _reject


IDNA_PROFILE = "stdlib-idna-2003-strict-nfc-lowercase-round-trip"
_ASCII_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_UNICODE_DOTS = str.maketrans({"。": ".", "．": ".", "｡": "."})


@dataclass(frozen=True)
class CanonicalDomain:
    """An ASCII DNS name with wildcard intent represented separately."""

    ascii_name: str
    wildcard: bool = False

    @property
    def value(self) -> str:
        return ("*." if self.wildcard else "") + self.ascii_name

    def __str__(self) -> str:
        return self.value


def _canonical_label(label: str, field: str) -> str:
    normalized = unicodedata.normalize("NFC", label).lower()
    try:
        normalized.encode("ascii")
        is_ascii = True
    except UnicodeEncodeError:
        is_ascii = False

    try:
        ascii_label = normalized.encode("idna").decode("ascii").lower()
        if ascii_label.startswith("xn--") or not is_ascii:
            decoded = ascii_label.encode("ascii").decode("idna")
            decoded_normalized = unicodedata.normalize("NFC", decoded).lower()
            recoded = decoded_normalized.encode("idna").decode("ascii").lower()
            lossy_unicode = not is_ascii and decoded_normalized != normalized
            if recoded != ascii_label or lossy_unicode:
                _reject(
                    "idna_round_trip_failed",
                    field,
                    "%s contains a lossy or non-canonical IDNA label." % field,
                )
    except UnicodeError:
        _reject("invalid_idna_domain", field, "%s is not a valid IDNA domain." % field)

    if _ASCII_LABEL.fullmatch(ascii_label) is None:
        _reject("invalid_domain_label", field, "%s contains an invalid DNS label." % field)
    return ascii_label


def parse_domain(
    value: Any, *, allow_wildcard: bool = False, field: str = "domain"
) -> CanonicalDomain:
    """Return a canonical ASCII domain under the declared IDNA profile."""

    if not isinstance(value, str) or not value:
        _reject("invalid_domain", field, "%s must be a non-empty domain name." % field)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _reject("domain_control_character", field, "%s contains a control character." % field)
    if any(character.isspace() for character in value):
        _reject("domain_whitespace", field, "%s may not contain whitespace." % field)
    if ":" in value:
        _reject("domain_port_not_allowed", field, "%s may not include a port." % field)

    normalized_value = value.translate(_UNICODE_DOTS)
    if normalized_value.endswith("."):
        _reject("domain_trailing_dot", field, "%s may not end with a dot." % field)

    wildcard = normalized_value.startswith("*.")
    name = normalized_value[2:] if wildcard else normalized_value
    if "*" in name or ("*" in normalized_value and not wildcard):
        _reject("invalid_domain_wildcard", field, "%s has an invalid wildcard." % field)
    if wildcard and not allow_wildcard:
        _reject("domain_wildcard_not_allowed", field, "%s may not be a wildcard." % field)

    input_labels = name.split(".")
    if len(input_labels) < 2:
        _reject("domain_requires_suffix", field, "%s must contain at least two DNS labels." % field)
    if any(not label for label in input_labels):
        _reject("invalid_domain_label", field, "%s contains an invalid DNS label." % field)

    labels: List[str] = [_canonical_label(label, field) for label in input_labels]
    ascii_name = ".".join(labels)
    if len(ascii_name) > 253:
        _reject("invalid_domain_length", field, "%s exceeds DNS length limits." % field)

    return CanonicalDomain(ascii_name=ascii_name, wildcard=wildcard)
