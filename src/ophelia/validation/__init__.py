"""Pure canonical validation primitives for future manifest adapters.

This package is intentionally independent from the public v1 manifest parser.
It exposes immutable canonical values and structured failures that adapters can
translate without parsing exception text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class ValidationIssue:
    """Stable, audit-friendly description of a rejected boundary value."""

    code: str
    field: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "field": self.field, "message": self.message}


class CanonicalValidationError(ValueError):
    """Raised when a value cannot be represented canonically and safely."""

    def __init__(self, issue: ValidationIssue) -> None:
        self.issue = issue
        super().__init__(issue.message)

    @property
    def code(self) -> str:
        return self.issue.code

    @property
    def field(self) -> str:
        return self.issue.field

    def to_dict(self) -> Dict[str, str]:
        return self.issue.to_dict()


def _reject(code: str, field: str, message: str) -> None:
    raise CanonicalValidationError(ValidationIssue(code, field, message))


from .artifacts import (
    ArchiveInspection,
    ArchiveInspectionError,
    ArchiveLimits,
    ArchiveMember,
    inspect_archive,
)
from .domains import IDNA_PROFILE, CanonicalDomain, parse_domain
from .identifiers import Environment, Identifier, parse_environment, parse_identifier
from .paths import (
    CanonicalHostPath,
    CanonicalSourcePath,
    HostPathCapability,
    SourceRoot,
    host_path_capability,
    resolve_host_path,
    resolve_source_path,
)
from .routes import CanonicalHTTPPath, HTTPPathKind, parse_http_path

__all__ = [
    "ArchiveInspection",
    "ArchiveInspectionError",
    "ArchiveLimits",
    "ArchiveMember",
    "CanonicalDomain",
    "CanonicalHostPath",
    "CanonicalHTTPPath",
    "CanonicalSourcePath",
    "CanonicalValidationError",
    "Environment",
    "HTTPPathKind",
    "HostPathCapability",
    "IDNA_PROFILE",
    "Identifier",
    "SourceRoot",
    "ValidationIssue",
    "host_path_capability",
    "inspect_archive",
    "parse_domain",
    "parse_environment",
    "parse_http_path",
    "parse_identifier",
    "resolve_host_path",
    "resolve_source_path",
]
