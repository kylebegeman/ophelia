from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .config import REPO_ROOT

SKIP_PARTS = {".git", ".venv", "_worktrees", "build", "dist"}
INLINE_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
URL_SCHEMES = ("http://", "https://", "mailto:")


def check_markdown_docs(root: Path = REPO_ROOT) -> List[str]:
    failures: List[str] = []
    for path in _markdown_files(root):
        content = path.read_text()
        if content.count("```") % 2 != 0:
            failures.append(f"{path}: unmatched fenced code block")
        if "VPS_SSH_KEY=" in content or "GHCR_TOKEN=" in content:
            failures.append(f"{path}: possible secret assignment in docs")
        for line_number, target in _local_markdown_link_targets(content):
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                failures.append(f"{path}:{line_number}: broken local markdown link: {target}")
    return failures


def _markdown_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.glob("**/*.md")):
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        yield path


def _local_markdown_link_targets(content: str) -> Iterable[Tuple[int, str]]:
    for line_number, line in enumerate(content.splitlines(), start=1):
        for match in INLINE_LINK_RE.finditer(line):
            target = _normalize_link_target(match.group(1))
            if target is not None:
                yield line_number, target


def _normalize_link_target(raw: str) -> Optional[str]:
    target = raw.strip()
    if not target or target.startswith("#") or target.startswith(URL_SCHEMES):
        return None
    if "://" in target:
        return None
    target = target.split("#", 1)[0].strip()
    if not target:
        return None
    if (target.startswith("<") and target.endswith(">")):
        target = target[1:-1].strip()
    return target.replace("%20", " ")


def main() -> int:
    failures = check_markdown_docs()

    if failures:
        for failure in failures:
            print(failure)
        return 1

    print("Markdown docs check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
