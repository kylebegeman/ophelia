"""Canonical identifiers and deployment environments."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from . import _reject


_IDENTIFIER = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class Identifier:
    value: str

    def __str__(self) -> str:
        return self.value


class Environment(str, Enum):
    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"


def parse_identifier(value: Any, *, field: str = "identifier") -> Identifier:
    """Parse a 1-63 character lowercase DNS-style identifier."""

    if (
        not isinstance(value, str)
        or len(value) > 63
        or _IDENTIFIER.fullmatch(value) is None
    ):
        _reject(
            "invalid_identifier",
            field,
            "%s must be 1-63 lowercase alphanumeric or hyphen characters, "
            "starting and ending with an alphanumeric character." % field,
        )
    return Identifier(value)


def parse_environment(
    value: Any, *, field: str = "environment"
) -> Optional[Environment]:
    """Preserve the optional v1 dev/staging/production environment contract."""

    if value is None:
        return None
    if not isinstance(value, str):
        _reject(
            "invalid_environment",
            field,
            "%s must be one of dev, staging, or production." % field,
        )
    try:
        return Environment(value)
    except ValueError:
        _reject(
            "invalid_environment",
            field,
            "%s must be one of dev, staging, or production." % field,
        )
    raise AssertionError("unreachable")
