from __future__ import annotations

from pathlib import Path
from typing import List


def external_symlinks(path: Path, allowed_root: Path) -> List[Path]:
    """Return symlinks under ``path`` whose target resolves outside ``allowed_root``."""
    if not path.exists() and not path.is_symlink():
        return []
    if path.is_symlink():
        return [] if symlink_target_within(path, allowed_root) else [path]
    if not path.is_dir():
        return []
    links: List[Path] = []
    for child in path.rglob("*"):
        if child.is_symlink() and not symlink_target_within(child, allowed_root):
            links.append(child)
    return links


def symlink_target_within(path: Path, allowed_root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(allowed_root.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


def assert_no_external_symlinks(path: Path, allowed_root: Path) -> None:
    links = external_symlinks(path, allowed_root)
    if links:
        first = links[0]
        raise ValueError(f"External symlink is not allowed: {first} -> {first.readlink()}")
