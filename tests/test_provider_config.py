from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.provider_config import (
    explain_provider_config,
    validate_provider_config,
    validate_ttl,
)


def _write(temp_dir: str, name: str, payload: dict) -> Path:
    path = Path(temp_dir) / name
    path.write_text(json.dumps(payload))
    return path


class ProviderConfigTests(unittest.TestCase):
    def test_valid_file_dns_provider_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _write(temp_dir, "file.json", {"dns": {"file": {"record_file": "records.json"}}})
            report = validate_provider_config(config)
        self.assertEqual(report["kind"], "ophelia.provider_config.validation")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["blockers"], [])
        self.assertTrue(report["values_redacted"])

    def test_valid_cloudflare_with_api_token_env_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _write(
                temp_dir,
                "cf.json",
                {"dns": {"cloudflare": {"zone_id": "z", "api_token_env": "CF_TOKEN", "ttl": 300}}},
            )
            report = validate_provider_config(config)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["blockers"], [])
        provider = report["providers"][0]
        self.assertEqual(provider["type"], "cloudflare")
        self.assertEqual(provider["status"], "ok")

    def test_raw_api_token_value_is_blocked_and_never_echoed(self) -> None:
        secret = "raw-token-leak-do-not-print"
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _write(
                temp_dir,
                "bad.json",
                {"dns": {"cloudflare": {"zone_id": "z", "api_token": secret}}},
            )
            report = validate_provider_config(config)
        self.assertEqual(report["status"], "blocked")
        codes = {item["code"] for item in report["blockers"]}
        self.assertIn("provider_token_must_be_env_ref", codes)
        self.assertNotIn(secret, json.dumps(report))

    def test_raw_token_key_variants_are_blocked(self) -> None:
        for key in ("token", "secret", "password", "private_key", "API_TOKEN"):
            with self.subTest(key=key):
                with tempfile.TemporaryDirectory() as temp_dir:
                    config = _write(
                        temp_dir,
                        "bad.json",
                        {"dns": {"cloudflare": {"zone_id": "z", "api_token_env": "CF", key: "VALUE_LEAK"}}},
                    )
                    report = validate_provider_config(config)
                codes = {item["code"] for item in report["blockers"]}
                self.assertIn("provider_token_must_be_env_ref", codes)
                self.assertNotIn("VALUE_LEAK", json.dumps(report))

    def test_invalid_ttl_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _write(
                temp_dir,
                "ttl.json",
                {"dns": {"cloudflare": {"zone_id": "z", "api_token_env": "CF_TOKEN", "ttl": 5}}},
            )
            report = validate_provider_config(config)
        self.assertEqual(report["status"], "blocked")
        codes = {item["code"] for item in report["blockers"]}
        self.assertIn("cloudflare_ttl_invalid", codes)

    def test_unknown_provider_type_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _write(temp_dir, "unknown.json", {"dns": {"type": "route53", "zone_id": "z"}})
            report = validate_provider_config(config)
        self.assertEqual(report["status"], "blocked")
        codes = {item["code"] for item in report["blockers"]}
        self.assertIn("provider_type_unknown", codes)

    def test_missing_config_file_is_clear_error(self) -> None:
        report = validate_provider_config(Path("/tmp/ophelia-nonexistent-provider-config.json"))
        self.assertEqual(report["kind"], "ophelia.error")
        codes = {item["code"] for item in report["blockers"]}
        self.assertIn("provider_config_missing", codes)

    def test_not_json_is_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad.json"
            path.write_text("this is not json {")
            report = validate_provider_config(path)
        self.assertEqual(report["kind"], "ophelia.error")
        codes = {item["code"] for item in report["blockers"]}
        self.assertIn("provider_config_invalid_json", codes)

    def test_explain_reports_env_refs_by_name_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _write(
                temp_dir,
                "cf.json",
                {"dns": {"cloudflare": {"zone_id": "z", "api_token_env": "CF_TOKEN"}}},
            )
            report = explain_provider_config(config)
        self.assertEqual(report["kind"], "ophelia.provider_config.explanation")
        provider = report["providers"][0]
        self.assertEqual(provider["env_refs"], ["CF_TOKEN"])
        self.assertTrue(report["values_redacted"])

    def test_validate_ttl_accepts_documented_range(self) -> None:
        self.assertEqual(validate_ttl(1), (1, True))
        self.assertEqual(validate_ttl(300), (300, True))
        self.assertEqual(validate_ttl(86400), (86400, True))
        self.assertEqual(validate_ttl(None), (None, True))
        self.assertFalse(validate_ttl(5)[1])
        self.assertFalse(validate_ttl(99999)[1])
        self.assertFalse(validate_ttl("abc")[1])
        self.assertFalse(validate_ttl(True)[1])


if __name__ == "__main__":
    unittest.main()
