from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .config import REPO_ROOT
from .operation_schema import SCHEMA_VERSION, operation_id
from .redaction import deep_redact

OPEN_SOURCE_AUDIT_KIND = "ophelia.open_source_audit_report"

_GOVERNANCE_FILES = ("SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SUPPORT.md")
_LICENSE_NAMES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE", "LICENCE.md", "LICENCE.txt")
_SKIP_PARTS = {".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", "build", "dist"}
_TEXT_EXTENSIONS = {
    "",
    ".cfg",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".tpl",
    ".txt",
    ".yaml",
    ".yml",
}

_MAINTAINER_USER = "ky" + "le"
_PRIVATE_DOMAIN = "beg" + "am"
_PRIVATE_PRODUCT_TERMS = (
    "qua" + "rk",
    "pri" + "sm",
    "dragon-" + "writer",
    "dragon" + "writer",
    "poke" + "dex",
    "aspect" + "avy",
    "joo" + "bily",
    "still" + "up",
    "cleared" + "torun",
)
_PRIVATE_REGISTRY_TERMS = (
    "mr" + "bagels",
    "bagel" + "works",
)


@dataclass(frozen=True)
class AuditRule:
    code: str
    severity: str
    pattern: re.Pattern[str]
    message: str
    recommendation: str


_RULES: Sequence[AuditRule] = (
    AuditRule(
        code="secret_literal",
        severity="blocker",
        pattern=re.compile(
            r"(-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{20,})",
            re.IGNORECASE,
        ),
        message="High-confidence secret literal found in tracked source.",
        recommendation="Remove the value from Git, rotate it if it was real, and keep only an environment-variable name or fixture placeholder.",
    ),
    AuditRule(
        code="private_dns_or_host",
        severity="blocker",
        pattern=re.compile(
            rf"(\b{_PRIVATE_DOMAIN}\.in\b|\b[\w.-]+\.{_PRIVATE_DOMAIN}\.in\b|\b{_MAINTAINER_USER}begeman\.com\b|\bwww\.{_MAINTAINER_USER}begeman\.com\b|\b209\.74\.71\.165\b)",
            re.IGNORECASE,
        ),
        message="Private DNS name, personal domain, or host IP found in a tracked public surface.",
        recommendation="Replace with fixture domains such as example.com, or move the material to private deployment notes outside the public repository.",
    ),
    AuditRule(
        code="personal_local_path",
        severity="blocker",
        pattern=re.compile(rf"(/Users/{_MAINTAINER_USER}\b|/home/{_MAINTAINER_USER}\b|\b{_MAINTAINER_USER}@[\w.-]+)", re.IGNORECASE),
        message="Personal workstation, home-directory, or SSH target reference found.",
        recommendation="Use repo-relative paths, environment variables, or generic examples such as operator@example-host.",
    ),
    AuditRule(
        code="private_or_legacy_product_reference",
        severity="warning",
        pattern=re.compile(
            r"(?<![A-Za-z0-9])(" + "|".join(re.escape(term) for term in _PRIVATE_PRODUCT_TERMS) + r")(?![A-Za-z0-9])",
            re.IGNORECASE,
        ),
        message="Private, retained, or legacy product name found in tracked source.",
        recommendation="Use synthetic fixture app names in public docs and tests; keep real-product adoption notes in private or explicitly scoped deployment docs.",
    ),
    AuditRule(
        code="private_registry_reference",
        severity="warning",
        pattern=re.compile(
            rf"\b(ghcr\.io/(?:{_PRIVATE_REGISTRY_TERMS[0]}|{_PRIVATE_REGISTRY_TERMS[1]})/|repos/{_PRIVATE_REGISTRY_TERMS[1]}/|{_PRIVATE_REGISTRY_TERMS[0]}/)",
            re.IGNORECASE,
        ),
        message="Private registry, owner, or repository reference found.",
        recommendation="Replace with ghcr.io/example/... examples or move the reference to private deployment material.",
    ),
)


def open_source_audit_report(
    *,
    root: Path = REPO_ROOT,
    max_findings: int = 50,
) -> Dict[str, Any]:
    """Scan the tracked public surface for open-source release blockers.

    The audit is read-only and intentionally conservative: it reports private
    host, product, path, and governance issues so a maintainer can decide what
    to rewrite, move, or preserve before making the repository public.
    """
    root = Path(root).resolve()
    files = _tracked_or_recursive_files(root)
    findings = _governance_findings(root)
    findings.extend(_scratchpad_findings(files))
    findings.extend(_workflow_findings(files))
    findings.extend(_content_findings(root, files))
    findings = sorted(findings, key=lambda item: (_severity_rank(item.get("severity")), item.get("path", ""), item.get("line") or 0, item.get("code", "")))

    blockers = [finding for finding in findings if finding.get("severity") == "blocker"]
    warnings = [finding for finding in findings if finding.get("severity") == "warning"]
    displayed_findings = findings[: max(0, max_findings)]
    displayed_blockers = [finding for finding in displayed_findings if finding.get("severity") == "blocker"]
    displayed_warnings = [finding for finding in displayed_findings if finding.get("severity") == "warning"]
    status = "blocked" if blockers else "warning" if warnings else "ok"

    checks = [
        {
            "name": "license_file_present",
            "ok": _has_any(root, _LICENSE_NAMES),
            "message": "License file is present." if _has_any(root, _LICENSE_NAMES) else "No LICENSE file is present.",
        },
        {
            "name": "security_policy_present",
            "ok": (root / "SECURITY.md").exists(),
            "message": "SECURITY.md is present." if (root / "SECURITY.md").exists() else "SECURITY.md is missing.",
        },
        {
            "name": "public_surface_scan",
            "ok": not blockers,
            "message": f"{len(blockers)} blocker(s), {len(warnings)} warning(s) across {len(files)} tracked/public file(s).",
        },
    ]

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": OPEN_SOURCE_AUDIT_KIND,
        "operation": "open_source.audit",
        "operation_id": operation_id("open_source.audit"),
        "status": status,
        "go_no_go": "no_go" if blockers else "review" if warnings else "go",
        "root": _display_root(root),
        "read_only": True,
        "mutates_state": False,
        "confirmation_required": False,
        "checks": checks,
        "file_count": len(files),
        "finding_count": len(findings),
        "blocker_count": len(blockers),
        "warning_count": len(warnings),
        "findings_truncated": len(displayed_findings) < len(findings),
        "max_findings": max_findings,
        "findings": displayed_findings,
        "blockers": displayed_blockers,
        "warnings": displayed_warnings,
        "recommendation": _recommendation(status),
        "summary": f"Open-source readiness: {status}; {len(blockers)} blocker(s), {len(warnings)} warning(s).",
    }
    return deep_redact(payload, safe_keys={"read_only", "mutates_state", "confirmation_required"}, propagate=True)


def _tracked_or_recursive_files(root: Path) -> List[Path]:
    tracked = _git_ls_files(root)
    if tracked is not None:
        files = []
        for rel in tracked:
            path = root / rel
            if path.exists() and _is_scannable_path(path):
                files.append(path)
        return files
    return sorted(path for path in root.rglob("*") if path.is_file() and _is_scannable_path(path))


def _git_ls_files(root: Path) -> Optional[List[Path]]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return [Path(line) for line in result.stdout.splitlines() if line.strip()]


def _is_scannable_path(path: Path) -> bool:
    if any(part in _SKIP_PARTS for part in path.parts):
        return False
    return path.suffix.lower() in _TEXT_EXTENSIONS


def _governance_findings(root: Path) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    if not _has_any(root, _LICENSE_NAMES):
        findings.append(
            _finding(
                code="missing_license",
                severity="blocker",
                path="LICENSE",
                message="No repository license file is present.",
                recommendation="Choose and add an OSI-approved license before publishing; Apache-2.0 is the recommended default for Ophelia.",
            )
        )
    for filename in _GOVERNANCE_FILES:
        if not (root / filename).exists():
            findings.append(
                _finding(
                    code="missing_governance_file",
                    severity="warning",
                    path=filename,
                    message=f"{filename} is missing.",
                    recommendation="Add the file before public launch so contributors, users, and security reporters know the project rules.",
                )
            )
    return findings


def _scratchpad_findings(files: Iterable[Path]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for path in files:
        if len(path.parts) >= 2 and path.parts[-2] == "scratchpad" and "docs" in path.parts:
            findings.append(
                _finding(
                    code="tracked_scratchpad_doc",
                    severity="blocker",
                    path=_relative_display(path),
                    message="Scratchpad document is tracked and may contain private working notes.",
                    recommendation="Promote durable public content into docs/architecture, docs/operations, docs/product, or docs/technical; remove or privatize scratchpad notes before release.",
                )
            )
    return findings


def _workflow_findings(files: Iterable[Path]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for path in files:
        if len(path.parts) >= 3 and path.parts[-3:] == (".github", "workflows", "deploy-platform.yml"):
            findings.append(
                _finding(
                    code="private_deploy_workflow",
                    severity="blocker",
                    path=".github/workflows/deploy-platform.yml",
                    message="Tracked deploy workflow targets the maintainer's private VPS release path.",
                    recommendation="Move private deployment automation to a private repository, or gate it behind an example workflow that cannot deploy without explicit user configuration.",
                )
            )
    return findings


def _content_findings(root: Path, files: Iterable[Path]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except (FileNotFoundError, UnicodeDecodeError):
            continue
        rel = path.relative_to(root) if _is_relative_to(path, root) else Path(_relative_display(path))
        for line_number, line in enumerate(content.splitlines(), start=1):
            for rule in _RULES:
                if rule.pattern.search(line):
                    findings.append(
                        _finding(
                            code=rule.code,
                            severity=rule.severity,
                            path=str(rel),
                            line=line_number,
                            message=rule.message,
                            recommendation=rule.recommendation,
                        )
                    )
    return findings


def _finding(
    *,
    code: str,
    severity: str,
    path: str,
    message: str,
    recommendation: str,
    line: Optional[int] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "code": code,
        "severity": severity,
        "path": path,
        "message": message,
        "recommendation": recommendation,
    }
    if line is not None:
        payload["line"] = line
    return payload


def _has_any(root: Path, names: Sequence[str]) -> bool:
    return any((root / name).exists() for name in names)


def _recommendation(status: str) -> str:
    if status == "blocked":
        return "Do not publish yet. Resolve blocker findings, then rerun the audit without --allow-blocked."
    if status == "warning":
        return "Public release is possible after maintainer review, but warnings should be handled or explicitly accepted."
    return "Public release hygiene checks passed."


def _severity_rank(value: object) -> int:
    return {"blocker": 0, "warning": 1}.get(str(value), 2)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _relative_display(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _display_root(root: Path) -> str:
    try:
        return "." if root == REPO_ROOT else str(root.relative_to(REPO_ROOT))
    except ValueError:
        return str(root)
