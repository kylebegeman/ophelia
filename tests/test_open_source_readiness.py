from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ophelia.command_catalog import command_registry  # noqa: E402
from ophelia.main import main  # noqa: E402
from ophelia.open_source_readiness import OPEN_SOURCE_AUDIT_KIND, open_source_audit_report  # noqa: E402


class OpenSourceReadinessTests(unittest.TestCase):
    def test_minimal_public_safe_repo_is_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "LICENSE", "Apache License placeholder\n")
            _write(root / "SECURITY.md", "Report security issues through private disclosure.\n")
            _write(root / "CONTRIBUTING.md", "Use signed-off commits.\n")
            _write(root / "CODE_OF_CONDUCT.md", "Community standards.\n")
            _write(root / "SUPPORT.md", "Community support policy.\n")
            _write(root / "README.md", "Fixture deploys use demo.example.com.\n")

            report = open_source_audit_report(root=root)

        self.assertEqual(OPEN_SOURCE_AUDIT_KIND, report["kind"])
        self.assertEqual("open_source.audit", report["operation"])
        self.assertEqual("ok", report["status"])
        self.assertEqual("go", report["go_no_go"])
        self.assertEqual([], report["blockers"])
        self.assertEqual([], report["warnings"])
        self.assertTrue(report["read_only"])
        self.assertFalse(report["mutates_state"])

    def test_private_public_surface_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "README.md", "Deploy from /Users/kyle to ops.begam.in\n")
            _write(root / ".github" / "workflows" / "deploy-platform.yml", "name: deploy\n")
            _write(root / "docs" / "scratchpad" / "note.md", "quark staging notes\n")

            report = open_source_audit_report(root=root)

        codes = {finding["code"] for finding in report["blockers"]}
        self.assertEqual("blocked", report["status"])
        self.assertEqual("no_go", report["go_no_go"])
        self.assertIn("missing_license", codes)
        self.assertIn("private_deploy_workflow", codes)
        self.assertIn("tracked_scratchpad_doc", codes)
        self.assertIn("private_dns_or_host", codes)
        self.assertIn("personal_local_path", codes)
        self.assertGreaterEqual(report["warning_count"], 1)

    def test_cli_json_and_catalog_descriptor_are_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "README.md", "Deploy from /home/kyle\n")
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(["open-source", "audit", "--root", str(root), "--allow-blocked", "--json"])

        payload = json.loads(buffer.getvalue())
        descriptor = next(descriptor for descriptor in command_registry() if descriptor.operation == "open_source.audit")

        self.assertEqual(0, exit_code)
        self.assertEqual(OPEN_SOURCE_AUDIT_KIND, payload["kind"])
        self.assertEqual("blocked", payload["status"])
        self.assertEqual("ship open-source audit", descriptor.command)
        self.assertFalse(descriptor.mutates_state)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
