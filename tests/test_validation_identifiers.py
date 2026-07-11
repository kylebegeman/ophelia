from __future__ import annotations

import dataclasses
import unittest

from ophelia.validation import (
    CanonicalValidationError,
    Environment,
    parse_environment,
    parse_identifier,
)


class IdentifierValidationTests(unittest.TestCase):
    def test_accepts_fixture_and_boundary_identifiers(self) -> None:
        self.assertEqual("fixture-postgres-api", parse_identifier("fixture-postgres-api").value)
        self.assertEqual("a", parse_identifier("a").value)
        self.assertEqual("a" * 63, parse_identifier("a" * 63).value)

    def test_rejects_noncanonical_identifiers_with_structured_diagnostic(self) -> None:
        for value in ("", "-app", "app-", "two--parts", "Upper", "under_score", "a" * 64):
            with self.subTest(value=value):
                with self.assertRaises(CanonicalValidationError) as raised:
                    parse_identifier(value, field="app")
                self.assertEqual(
                    {
                        "code": "invalid_identifier",
                        "field": "app",
                        "message": raised.exception.args[0],
                    },
                    raised.exception.to_dict(),
                )

    def test_identifier_is_immutable(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            parse_identifier("demo-service").value = "changed"  # type: ignore[misc]

    def test_environment_preserves_v1_values_and_optional_behavior(self) -> None:
        self.assertIsNone(parse_environment(None))
        self.assertIs(Environment.DEV, parse_environment("dev"))
        self.assertIs(Environment.STAGING, parse_environment("staging"))
        self.assertIs(Environment.PRODUCTION, parse_environment("production"))
        self.assertEqual("staging", parse_environment("staging"))

        for value in ("qa", "Production", "", 1):
            with self.subTest(value=value), self.assertRaises(CanonicalValidationError) as raised:
                parse_environment(value)
            self.assertEqual("invalid_environment", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
