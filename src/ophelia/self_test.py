"""Local install self-test for the Ophelia control plane.

``run_self_test`` runs a battery of read-only checks that confirm the installed
package can do its job: the CLI entrypoint imports, packaged runtime templates
resolve through ``importlib.resources`` (the installed-wheel path, not the
repo-root path), PyYAML is importable, the runtime root resolves, and the
Python version is supported. Docker, git, and the HTTP API are checked only
behind explicit flags and never block.

The result is a JSON-serializable dict. It NEVER contains environment values or
anything that looks like a secret; it reports only structural facts about the
installation. ``package.location`` is the package directory path, which is not a
secret.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import DEFAULT_RUNTIME_ROOT

SCHEMA_VERSION = 1
KIND = "ophelia.self_test"

_TEMPLATE_FILES = (
    ("compose", "app.compose.tpl"),
    ("caddy", "site.caddy.tpl"),
)

# Key docs that should exist in a source checkout. Best-effort only.
_DOC_PATHS = (
    "README.md",
    "docs/job-action-api.md",
)


def run_self_test(
    *,
    check_docker: bool = False,
    check_git: bool = False,
    check_api: bool = False,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> Dict[str, Any]:
    """Run install self-test checks and return a structured result dict."""
    checks: List[Dict[str, Any]] = []
    blockers: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    def record(
        name: str,
        runner: Callable[[], Optional[str]],
        *,
        blocking: bool = True,
    ) -> None:
        """Run one check. ``runner`` returns an optional human message; raising
        marks failure. ``blocking`` controls whether a failure is a blocker or a
        warning."""
        try:
            message = runner()
            checks.append(_check(name, "passed", message))
        except _SkipCheck as exc:
            checks.append(_check(name, "skipped", str(exc)))
        except _WarnCheck as exc:
            checks.append(_check(name, "warning", str(exc)))
            warnings.append({"check": name, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - report any failure verbatim
            message = f"{type(exc).__name__}: {exc}"
            status = "failed" if blocking else "warning"
            checks.append(_check(name, status, message))
            target = blockers if blocking else warnings
            target.append({"check": name, "message": message})

    record("entrypoint", _check_entrypoint)
    record("python_version", _check_python_version)
    record("pyyaml", _check_pyyaml)
    record("templates", _check_templates)
    record("default_config_load", _check_default_config_load)
    record("runtime_root", lambda: _check_runtime_root(runtime_root))
    record("package_version", _check_package_version, blocking=False)
    record("docs_paths", _check_docs_paths, blocking=False)

    if check_docker:
        record("docker", _check_docker, blocking=False)
    if check_git:
        record("git", _check_git, blocking=False)
    if check_api:
        record("api", _check_api, blocking=False)

    if blockers:
        status = "blocked"
    elif warnings:
        status = "warn"
    else:
        status = "ok"

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "status": status,
        "package": _package_info(),
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
    }


class _SkipCheck(Exception):
    """Raised by a check runner to mark itself skipped."""


class _WarnCheck(Exception):
    """Raised by a check runner to mark itself a (non-blocking) warning."""


def _check(name: str, status: str, message: Optional[str]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"name": name, "status": status}
    if message:
        payload["message"] = message
    return payload


def _check_entrypoint() -> str:
    import ophelia.main as main_module

    if not callable(getattr(main_module, "main", None)):
        raise RuntimeError("ophelia.main.main is not callable")
    return "ophelia.main.main is importable and callable"


def _check_python_version() -> str:
    if sys.version_info < (3, 9):
        raise RuntimeError(
            f"Python 3.9+ required, found {sys.version_info.major}.{sys.version_info.minor}"
        )
    return f"Python {sys.version_info.major}.{sys.version_info.minor} is supported"


def _check_pyyaml() -> str:
    importlib.import_module("yaml")
    return "PyYAML is importable"


def _check_templates() -> str:
    from importlib import resources

    base = resources.files("ophelia.resources").joinpath("templates")
    resolved: List[str] = []
    for parts in _TEMPLATE_FILES:
        node = base.joinpath(*parts)
        text = node.read_text(encoding="utf-8")
        if not text:
            raise RuntimeError(f"packaged template {'/'.join(parts)} is empty")
        resolved.append("/".join(parts))
    return "resolved packaged templates via importlib.resources: " + ", ".join(resolved)


def _check_default_config_load() -> str:
    config = importlib.import_module("ophelia.config")
    runtime_root = getattr(config, "DEFAULT_RUNTIME_ROOT", None)
    if runtime_root is None:
        raise RuntimeError("ophelia.config.DEFAULT_RUNTIME_ROOT is not defined")
    Path(runtime_root).expanduser()
    return "ophelia.config imported; DEFAULT_RUNTIME_ROOT resolves"


def _check_runtime_root(runtime_root: Path) -> str:
    resolved = Path(runtime_root).expanduser()
    parent = resolved.parent
    writable = parent.exists() and os.access(parent, os.W_OK)
    note = "parent writable" if writable else "parent not writable or missing"
    # Do not create the directory; only resolve and report.
    return f"runtime root resolves; {note}"


def _check_package_version() -> str:
    from importlib import metadata

    try:
        version = metadata.version("ophelia")
    except metadata.PackageNotFoundError as exc:
        raise _WarnCheck(
            "package metadata not found (editable/PYTHONPATH run); version unknown"
        ) from exc
    return f"ophelia {version}"


def _check_docs_paths() -> str:
    from .config import REPO_ROOT

    repo_root = Path(REPO_ROOT)
    looks_like_checkout = (repo_root / "pyproject.toml").exists() or (repo_root / "README.md").exists()
    if not looks_like_checkout:
        raise _WarnCheck("REPO_ROOT is not a source checkout (installed wheel); skipped doc check")

    missing = [rel for rel in _DOC_PATHS if not (repo_root / rel).exists()]
    if missing:
        raise _WarnCheck("missing docs: " + ", ".join(missing))
    return "key docs present: " + ", ".join(_DOC_PATHS)


def _check_docker() -> str:
    if shutil.which("docker") is None:
        raise _WarnCheck("docker not found on PATH")
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise _WarnCheck(f"docker present but not responsive: {exc}") from exc
    if result.returncode != 0:
        raise _WarnCheck("docker present but daemon not reachable")
    version = result.stdout.strip() or "unknown"
    return f"docker server reachable (version {version})"


def _check_git() -> str:
    if shutil.which("git") is None:
        raise _WarnCheck("git not found on PATH")
    return "git is on PATH"


def _check_api() -> str:
    importlib.import_module("ophelia.api")
    return "ophelia.api is importable"


def _package_info() -> Dict[str, Any]:
    from importlib import metadata

    location: Optional[str] = None
    try:
        spec = importlib.util.find_spec("ophelia")
        if spec is not None and spec.origin:
            location = str(Path(spec.origin).resolve().parent)
    except (ImportError, ValueError):
        location = None

    version: Optional[str] = None
    try:
        version = metadata.version("ophelia")
    except metadata.PackageNotFoundError:
        version = None

    return {"name": "ophelia", "version": version, "location": location}
