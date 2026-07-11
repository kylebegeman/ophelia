"""Canonical filesystem paths with explicit trust boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

from . import _reject


def _resolved(path: Path, field: str) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        _reject("path_resolution_failed", field, "%s could not be resolved." % field)
    raise AssertionError("unreachable")


def _contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _raw_path(value: Any, field: str) -> Path:
    if not isinstance(value, (str, Path)):
        _reject("invalid_path", field, "%s must be a filesystem path." % field)
    raw = str(value)
    if not raw or "\x00" in raw:
        _reject("invalid_path", field, "%s must be a non-empty path without NUL." % field)
    return Path(raw)


@dataclass(frozen=True)
class SourceRoot:
    path: Path

    @classmethod
    def from_path(cls, value: Any, *, field: str = "source_root") -> "SourceRoot":
        path = _raw_path(value, field)
        if not path.is_absolute():
            _reject("source_root_not_absolute", field, "%s must be absolute." % field)
        return cls(_resolved(path, field))


@dataclass(frozen=True)
class CanonicalSourcePath:
    path: Path
    root: SourceRoot


@dataclass(frozen=True)
class HostPathCapability:
    """Explicit authority to use absolute paths beneath resolved allowlist roots."""

    allowed_roots: Tuple[Path, ...]


@dataclass(frozen=True)
class CanonicalHostPath:
    path: Path
    allowed_root: Path


def host_path_capability(
    allowed_roots: Iterable[Any], *, field: str = "allowed_host_roots"
) -> HostPathCapability:
    roots = []
    for value in allowed_roots:
        path = _raw_path(value, field)
        if not path.is_absolute():
            _reject("host_allowlist_root_not_absolute", field, "%s entries must be absolute." % field)
        resolved = _resolved(path, field)
        if resolved not in roots:
            roots.append(resolved)
    if not roots:
        _reject("empty_host_path_allowlist", field, "%s must not be empty." % field)
    return HostPathCapability(tuple(roots))


def resolve_source_path(
    root: SourceRoot,
    value: Any,
    *,
    relative_to: Optional[Any] = None,
    field: str = "source_path",
) -> CanonicalSourcePath:
    """Resolve relative to a trusted base and enforce source-root containment.

    The optional base supports manifest-relative v1 values while root remains
    the repository or source trust boundary. The base must itself be contained.
    """

    if not isinstance(root, SourceRoot):
        _reject("invalid_source_root", field, "%s requires a SourceRoot." % field)
    path = _raw_path(value, field)
    if path.is_absolute():
        _reject(
            "absolute_source_path_requires_capability",
            field,
            "%s must be relative; absolute host paths require HostPathCapability." % field,
        )

    base = root.path
    if relative_to is not None:
        raw_base = _raw_path(relative_to, field + "_base")
        base = _resolved(
            raw_base if raw_base.is_absolute() else root.path / raw_base,
            field + "_base",
        )
        if not _contains(root.path, base):
            _reject(
                "source_path_base_escape",
                field,
                "%s base resolves outside its source root." % field,
            )

    resolved = _resolved(base / path, field)
    if not _contains(root.path, resolved):
        _reject("source_path_escape", field, "%s resolves outside its source root." % field)
    return CanonicalSourcePath(path=resolved, root=root)


def resolve_host_path(
    capability: HostPathCapability, value: Any, *, field: str = "host_path"
) -> CanonicalHostPath:
    """Resolve an absolute host path under an explicitly authorized allowlist."""

    if not isinstance(capability, HostPathCapability):
        _reject("host_path_capability_required", field, "%s requires HostPathCapability." % field)
    path = _raw_path(value, field)
    if not path.is_absolute():
        _reject("host_path_not_absolute", field, "%s must be absolute." % field)
    resolved = _resolved(path, field)
    for root in capability.allowed_roots:
        if _contains(root, resolved):
            return CanonicalHostPath(path=resolved, allowed_root=root)
    _reject("host_path_not_allowed", field, "%s is outside the allowed host roots." % field)
    raise AssertionError("unreachable")
