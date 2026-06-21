"""Shared finding and remediation shapes.

``Finding`` and ``Remediation`` give readiness, provider validation, secrets
audit, conflicts, and policy a single typed vocabulary for "what is wrong, why
it matters, and the exact Ophelia command to fix it".

Backwards compatibility: many reports already emit ``issue()`` dicts shaped
``{code, message, path?}`` (see :func:`ophelia.operation_schema.issue`).
``Finding.to_dict()`` is a strict superset of that shape, so existing consumers
keep working while new consumers can read ``severity``, ``area``, and
``remediation``. :func:`finding_from_issue` adapts legacy issue dicts upward.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

SEVERITIES = ("blocker", "warning", "info")


@dataclass(frozen=True)
class Remediation:
    """A concrete, typed way to resolve a finding.

    ``commands`` must be typed Ophelia commands (usually ``plan`` forms), never
    raw shell. Mutating remediations should prefer a ``plan`` command and set
    ``requires_human_approval`` so agents do not auto-apply.
    """

    summary: str
    commands: List[str] = field(default_factory=list)
    docs: List[str] = field(default_factory=list)
    manifest_patch_hint: Optional[Dict[str, Any]] = None
    requires_human_approval: bool = False

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "summary": self.summary,
            "commands": list(self.commands),
            "docs": list(self.docs),
            "requires_human_approval": self.requires_human_approval,
        }
        if self.manifest_patch_hint is not None:
            payload["manifest_patch_hint"] = self.manifest_patch_hint
        return payload


@dataclass(frozen=True)
class Finding:
    """A single readiness/validation finding.

    ``to_dict()`` preserves the legacy ``{code, message, path}`` keys so it can
    be dropped into existing ``blockers``/``warnings`` lists, and adds
    ``severity``, ``area``, and optional ``remediation``.
    """

    code: str
    severity: str
    area: str
    message: str
    path: Optional[str] = None
    remediation: Optional[Remediation] = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}, got {self.severity!r}")

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "area": self.area,
        }
        if self.path is not None:
            payload["path"] = self.path
        if self.remediation is not None:
            payload["remediation"] = self.remediation.to_dict()
        return payload


def finding_from_issue(
    issue: Mapping[str, Any],
    *,
    severity: str = "blocker",
    area: str = "general",
    remediation: Optional[Remediation] = None,
) -> Finding:
    """Lift a legacy ``issue()`` dict (`{code, message, path?}`) into a Finding."""
    path = issue.get("path")
    return Finding(
        code=str(issue.get("code", "")),
        severity=severity,
        area=area,
        message=str(issue.get("message", "")),
        path=str(path) if path else None,
        remediation=remediation,
    )


def attach_remediation(issue: Mapping[str, Any], remediation: Optional[Remediation]) -> Dict[str, Any]:
    """Return a copy of an issue dict with ``remediation`` attached additively.

    Keeps every existing key so this is safe to apply to issues already present
    in a report's ``blockers``/``warnings`` list.
    """
    enriched: Dict[str, Any] = dict(issue)
    if remediation is not None:
        enriched["remediation"] = remediation.to_dict()
    return enriched
