"""Version reporting helpers for the Ophelia CLI."""

from __future__ import annotations

import importlib.util
import re
from importlib import metadata
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA_VERSION = 1
KIND = "ophelia.version"


def package_version() -> str:
    info = version_info()
    return str(info.get("version") or "unknown")


def version_info() -> Dict[str, Any]:
    version: Optional[str] = None
    source = "unknown"

    try:
        version = metadata.version("ophelia")
        source = "package_metadata"
    except metadata.PackageNotFoundError:
        version = _pyproject_version()
        source = "pyproject" if version else "unknown"

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "name": "ophelia",
        "version": version,
        "source": source,
        "location": _package_location(),
    }


def _package_location() -> Optional[str]:
    try:
        spec = importlib.util.find_spec("ophelia")
        if spec is not None and spec.origin:
            return str(Path(spec.origin).resolve().parent)
    except (ImportError, ValueError):
        return None
    return None


def _pyproject_version() -> Optional[str]:
    from .config import REPO_ROOT

    path = Path(REPO_ROOT) / "pyproject.toml"
    if not path.exists():
        return None
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', path.read_text(encoding="utf-8"))
    return match.group(1) if match else None
