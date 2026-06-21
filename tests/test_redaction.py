from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.redaction import (
    REDACTED,
    deep_redact,
    is_sensitive_key,
    looks_like_secret_value,
    redact_command_string,
    redact_mapping,
    redact_value,
    redacted_cloudflare_record,
    redacted_compose_text,
)


class RedactionKeyTests(unittest.TestCase):
    def test_sensitive_keys_detected_case_insensitively(self) -> None:
        for key in ["DATABASE_URL", "api_token", "Stripe_Secret", "REDIS_URL", "X_PASSWORD", "PRIVATE_KEY"]:
            self.assertTrue(is_sensitive_key(key), key)

    def test_benign_keys_not_sensitive(self) -> None:
        for key in ["PORT", "OPHELIA_APP", "hostname", "region"]:
            self.assertFalse(is_sensitive_key(key), key)

    def test_non_string_key_is_not_sensitive(self) -> None:
        self.assertFalse(is_sensitive_key(None))
        self.assertFalse(is_sensitive_key(42))


class RedactionValueTests(unittest.TestCase):
    def test_connection_strings_look_like_secrets(self) -> None:
        self.assertTrue(looks_like_secret_value("postgres://user:pw@host/db"))
        self.assertTrue(looks_like_secret_value("redis://:pw@host:6379/0"))
        self.assertTrue(looks_like_secret_value("https://user:token@example.com/path"))

    def test_plain_values_do_not_look_like_secrets(self) -> None:
        self.assertFalse(looks_like_secret_value("https://example.com/health"))
        self.assertFalse(looks_like_secret_value("production"))
        self.assertFalse(looks_like_secret_value(""))
        self.assertFalse(looks_like_secret_value(8080))

    def test_redact_value_preserves_none_and_empty(self) -> None:
        self.assertIsNone(redact_value(None))
        self.assertEqual("", redact_value(""))
        self.assertEqual(REDACTED, redact_value("super-secret"))

    def test_command_string_masks_secret_literals(self) -> None:
        command = "pg_restore --password hunter2 --api-token sk-live-secret DATABASE_URL=postgres://u:p@db/app"
        redacted = redact_command_string(command)
        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("sk-live-secret", redacted)
        self.assertNotIn("postgres://u:p@db/app", redacted)
        self.assertIn("--password", redacted)
        self.assertIn("--api-token", redacted)

    def test_deep_redact_masks_command_keys_and_can_propagate_sensitive_containers(self) -> None:
        payload = {
            "verify_command": "check --token sk-live-secret",
            "credentials": {"username": "alice", "region": "us-east-1"},
        }
        shallow = deep_redact(payload)
        propagated = deep_redact(payload, propagate=True)
        self.assertNotIn("sk-live-secret", str(shallow))
        self.assertEqual("alice", shallow["credentials"]["username"])
        self.assertEqual(REDACTED, propagated["credentials"]["username"])
        self.assertEqual(REDACTED, propagated["credentials"]["region"])


class RedactMappingTests(unittest.TestCase):
    def test_masks_by_key_name_and_value_shape(self) -> None:
        result = redact_mapping(
            {
                "DATABASE_URL": "postgres://u:p@h/db",
                "PORT": "8080",
                "dsn": "postgres://u:p@h/db",
                "region": "eu",
            }
        )
        self.assertEqual(REDACTED, result["DATABASE_URL"])
        self.assertEqual(REDACTED, result["dsn"])  # masked by value shape
        self.assertEqual("8080", result["PORT"])
        self.assertEqual("eu", result["region"])

    def test_safe_keys_passed_through(self) -> None:
        result = redact_mapping({"API_KEY": "abc"}, safe_keys=["API_KEY"])
        self.assertEqual("abc", result["API_KEY"])

    def test_known_secret_string_never_appears(self) -> None:
        secret = "sk-super-secret-value"
        result = redact_mapping({"STRIPE_SECRET": secret, "note": "ok"})
        self.assertNotIn(secret, str(result))


class CompatRedactorsTests(unittest.TestCase):
    def test_cloudflare_record_whitelist(self) -> None:
        record = {
            "id": "abc",
            "type": "A",
            "name": "x.example.com",
            "content": "1.2.3.4",
            "ttl": 60,
            "api_token": "leak-me",
            "meta": {"x": 1},
        }
        redacted = redacted_cloudflare_record(record)
        self.assertEqual({"id", "type", "name", "content", "ttl"}, set(redacted))
        self.assertNotIn("leak-me", str(redacted))

    def test_compose_text_masks_indented_env_values(self) -> None:
        content = (
            "services:\n"
            "  web:\n"
            "    environment:\n"
            "      DATABASE_URL: postgres://u:p@h/db\n"
            "      PORT: 8080\n"
        )
        redacted = redacted_compose_text(content)
        self.assertNotIn("postgres://u:p@h/db", redacted)
        self.assertIn('DATABASE_URL: "<redacted>"', redacted)
        self.assertIn("PORT: 8080", redacted)  # safe structural key preserved


if __name__ == "__main__":
    unittest.main()
