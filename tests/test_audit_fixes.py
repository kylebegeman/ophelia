"""Regression tests for the audit-pass security/correctness fixes."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.addons import _load_state, _sql_literal
from ophelia.provider_config import explain_provider_config, validate_provider_config
from ophelia.secrets_audit import secrets_audit


def _write(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


class ProviderRawSecretDetectionTests(unittest.TestCase):
    def test_extended_secret_key_names_are_blocked(self) -> None:
        # access_token / api_key / client_secret literals must all be blocked,
        # not just the original api_token/token/secret/password/private_key set.
        for key in ("access_token", "api_key", "client_secret", "secret_key"):
            with tempfile.TemporaryDirectory() as temp_dir:
                cfg = _write(
                    Path(temp_dir) / "p.json",
                    json.dumps({"dns": {"cloudflare": {"zone_id": "z", key: "RAWVALUE123"}}}),
                )
                report = validate_provider_config(cfg)
                self.assertEqual("blocked", report["status"], key)
                blob = json.dumps(report)
                self.assertNotIn("RAWVALUE123", blob, key)
                self.assertIn("provider_token_must_be_env_ref", blob, key)

    def test_env_reference_keys_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _write(
                Path(temp_dir) / "p.json",
                json.dumps({"dns": {"cloudflare": {"zone_id": "z", "api_token_env": "CF_TOKEN"}}}),
            )
            report = validate_provider_config(cfg)
            self.assertNotEqual("blocked", report["status"])
            self.assertNotIn("provider_token_must_be_env_ref", json.dumps(report))

    def test_explain_does_not_echo_nested_secret_shaped_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _write(
                Path(temp_dir) / "p.json",
                json.dumps(
                    {
                        "dns": {
                            "cloudflare": {
                                "zone_id": "z",
                                "api_token_env": "CF_TOKEN",
                                "extra": {"dsn": "postgres://u:NESTEDLEAK@h/db"},
                            }
                        }
                    }
                ),
            )
            report = explain_provider_config(cfg)
            self.assertNotIn("NESTEDLEAK", json.dumps(report))


class SecretsAuditPlaceholderTests(unittest.TestCase):
    def test_placeholder_required_key_counts_as_missing(self) -> None:
        manifest = (
            "version: 1\napp: ph\nenvironment: production\nkind: service\n"
            "image: ghcr.io/example/ph:latest\nservices:\n  web:\n    port: 3000\n"
            "routes:\n  - domain: ph.example.com\n    service: web\naddons:\n  postgres: true\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mp = _write(root / "ph.ophelia.yml", manifest)
            app_env = root / "runtime" / "apps" / "ph"
            app_env.mkdir(parents=True)
            # DATABASE_URL (implied by postgres) is present but still a placeholder.
            (app_env / "env").write_text("DATABASE_URL=replace-me\n")
            report = secrets_audit(mp, environment="production", runtime_root=root / "runtime")
        present_check = next(c for c in report["checks"] if c["name"] == "required_secrets_present")
        # The placeholder does not satisfy the requirement, so the check agrees
        # with the (blocked/warn) status instead of falsely reporting ok.
        self.assertFalse(present_check["ok"])


class AddonStateRobustnessTests(unittest.TestCase):
    def test_corrupt_addon_state_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "addons.json"
            state_path.write_text("{")

            self.assertEqual({}, _load_state(state_path))

    def test_sql_literal_escapes_quotes(self) -> None:
        self.assertEqual("'a''b'", _sql_literal("a'b"))


if __name__ == "__main__":
    unittest.main()
