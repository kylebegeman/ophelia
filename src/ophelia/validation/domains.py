"""Canonical, injection-safe DNS domain names."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import _reject


_ASCII_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


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


def parse_domain(
    value: Any, *, allow_wildcard: bool = False, field: str = "domain"
) -> CanonicalDomain:
    """Return a lowercase IDNA ASCII domain after strict DNS validation."""

    if not isinstance(value, str) or not value:
        _reject("invalid_domain", field, "%s must be a non-empty domain name." % field)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _reject("domain_control_character", field, "%s contains a control character." % field)
    if any(character.isspace() for character in value):
        _reject("domain_whitespace", field, "%s may not contain whitespace." % field)
    if ":" in value:
        _reject("domain_port_not_allowed", field, "%s may not include a port." % field)
    if value.endswith("."):
        _reject("domain_trailing_dot", field, "%s may not end with a dot." % field)

    wildcard = value.startswith("*.")
    name = value[2:] if wildcard else value
    if "*" in name or ("*" in value and not wildcard):
        _reject("invalid_domain_wildcard", field, "%s has an invalid wildcard." % field)
    if wildcard and not allow_wildcard:
        _reject("domain_wildcard_not_allowed", field, "%s may not be a wildcard." % field)

    try:
        ascii_name = name.encode("idna").decode("ascii").lower()
    except UnicodeError:
        _reject("invalid_idna_domain", field, "%s is not a valid IDNA domain." % field)

    if not ascii_name or len(ascii_name) > 253:
        _reject("invalid_domain_length", field, "%s exceeds DNS length limits." % field)

    labels = ascii_name.split(".")
    if len(labels) < 2:
        _reject("domain_requires_suffix", field, "%s must contain at least two DNS labels." % field)
    if any(_ASCII_LABEL.fullmatch(label) is None for label in labels):
        _reject("invalid_domain_label", field, "%s contains an invalid DNS label." % field)

    return CanonicalDomain(ascii_name=ascii_name, wildcard=wildcard)
