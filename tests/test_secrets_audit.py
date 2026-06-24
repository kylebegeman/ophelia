from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.secrets_audit import secrets_audit


def _manifest(app: str) -> str:
    return f"""
version: 1
app: {app}
environment: production
kind: service
image: ghcr.io/example/{app}:latest
services:
  web:
    port: 3000
    host_port: 3601
routes:
  - domain: {app}.example.com
    service: web
addons:
  postgres: true
env:
  API_KEY: replace-me
verify:
  - name: health
    url: https://{app}.example.com/health
""".strip() + "\n"


class SecretsAuditTests(unittest.TestCase):
    def _setup(self, temp_dir: str, env_lines: str) -> "tuple[Path, Path]":
        root = Path(temp_dir)
        manifest_path = root / "myapp.ophelia.yml"
        manifest_path.write_text(_manifest("myapp"))
        runtime_root = root / "runtime"
        app_env_dir = runtime_root / "apps" / "myapp"
        app_env_dir.mkdir(parents=True)
        (app_env_dir / "env").write_text(env_lines)
        return manifest_path, runtime_root

    def test_missing_required_key_is_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Runtime env omits the required DATABASE_URL (implied by addons.postgres).
            manifest_path, runtime_root = self._setup(temp_dir, "API_KEY=present-value\n")
            report = secrets_audit(manifest_path, environment="production", runtime_root=runtime_root)
        self.assertEqual(report["kind"], "ophelia.secrets_audit")
        self.assertEqual(report["status"], "blocked")
        names = {b.get("path") for b in report["blockers"]}
        self.assertIn("DATABASE_URL", names)

    def test_extra_runtime_key_is_warning_name_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path, runtime_root = self._setup(
                temp_dir,
                "API_KEY=present\nDATABASE_URL=postgres://u:p@h/db\nUNDECLARED_EXTRA=anything\n",
            )
            report = secrets_audit(manifest_path, environment="production", runtime_root=runtime_root)
        warning_codes = {w.get("code") for w in report["warnings"]}
        self.assertIn("extra_runtime_env_key", warning_codes)
        extra = next(item for item in report["keys"] if item["name"] == "UNDECLARED_EXTRA")
        self.assertEqual(extra["status"], "extra")
        self.assertTrue(extra["value_redacted"])

    def test_secret_value_is_never_emitted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path, runtime_root = self._setup(
                temp_dir,
                "API_KEY=present\nDATABASE_URL=postgres://u:FAKESECRET@h/db\n",
            )
            report = secrets_audit(manifest_path, environment="production", runtime_root=runtime_root)
        serialized = json.dumps(report)
        self.assertNotIn("FAKESECRET", serialized)
        for item in report["keys"]:
            self.assertTrue(item["value_redacted"])
            self.assertNotIn("value", item)

    def test_database_url_key_carries_value_redacted_and_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path, runtime_root = self._setup(
                temp_dir,
                "API_KEY=present\nDATABASE_URL=postgres://u:FAKESECRET@h/db\n",
            )
            report = secrets_audit(manifest_path, environment="production", runtime_root=runtime_root)
        database = next(item for item in report["keys"] if item["name"] == "DATABASE_URL")
        self.assertTrue(database["present"])
        self.assertTrue(database["value_redacted"])
        self.assertTrue(database["required"])

    def test_include_process_env_only_sets_booleans(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path, runtime_root = self._setup(temp_dir, "API_KEY=present\n")
            report = secrets_audit(
                manifest_path,
                environment="production",
                runtime_root=runtime_root,
                include_process_env=True,
            )
        for item in report["keys"]:
            self.assertIn(item["process_env_present"], (True, False))
            self.assertTrue(item["value_redacted"])


if __name__ == "__main__":
    unittest.main()
