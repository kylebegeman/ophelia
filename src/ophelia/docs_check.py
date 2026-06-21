from __future__ import annotations

import sys
from pathlib import Path

from .config import REPO_ROOT


def main() -> int:
    failures = []
    for path in sorted(REPO_ROOT.glob("**/*.md")):
        if ".venv" in path.parts or "_worktrees" in path.parts:
            continue
        content = path.read_text()
        if content.count("```") % 2 != 0:
            failures.append(f"{path}: unmatched fenced code block")
        if "VPS_SSH_KEY=" in content or "GHCR_TOKEN=" in content:
            failures.append(f"{path}: possible secret assignment in docs")

    if failures:
        for failure in failures:
            print(failure)
        return 1

    print("Markdown docs check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
