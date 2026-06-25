from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ophelia.docs_check import check_markdown_docs


class DocsCheckTests(unittest.TestCase):
    def test_accepts_valid_local_and_external_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "README.md", "\n".join(
                [
                    "[Guide](docs/guide.md)",
                    "[Section](#local-anchor)",
                    "[External](https://example.com/docs)",
                    "[Mail](mailto:security@example.com)",
                    "![Image links are ignored](missing-image.png)",
                    "",
                ]
            ))
            _write(root / "docs" / "guide.md", "# Guide\n")

            self.assertEqual([], check_markdown_docs(root))

    def test_reports_missing_local_markdown_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "README.md", "[Missing](docs/missing.md)\n")

            failures = check_markdown_docs(root)

        self.assertEqual(1, len(failures))
        self.assertIn("broken local markdown link: docs/missing.md", failures[0])

    def test_skips_build_and_virtualenv_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "README.md", "[Guide](docs/guide.md)\n")
            _write(root / "docs" / "guide.md", "# Guide\n")
            _write(root / "build" / "generated.md", "[Missing](missing.md)\n")
            _write(root / ".venv" / "generated.md", "[Missing](missing.md)\n")

            self.assertEqual([], check_markdown_docs(root))

    def test_reports_fence_and_secret_assignment_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "README.md", "```bash\nVPS_SSH_KEY=value\n")

            failures = check_markdown_docs(root)

        self.assertEqual(2, len(failures))
        self.assertTrue(any("unmatched fenced code block" in failure for failure in failures))
        self.assertTrue(any("possible secret assignment" in failure for failure in failures))


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
