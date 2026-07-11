from __future__ import annotations

import unittest

from ophelia.validation import CanonicalValidationError, parse_domain


class DomainValidationTests(unittest.TestCase):
    def test_accepts_fixture_domains_and_canonicalizes_idna(self) -> None:
        self.assertEqual("postgres.fixture.invalid", parse_domain("postgres.fixture.invalid").value)
        self.assertEqual("xn--bcher-kva.example", parse_domain("BÜCHER.example").value)

    def test_wildcard_intent_is_explicit(self) -> None:
        with self.assertRaises(CanonicalValidationError) as raised:
            parse_domain("*.example.com")
        self.assertEqual("domain_wildcard_not_allowed", raised.exception.code)

        domain = parse_domain("*.BÜCHER.example", allow_wildcard=True)
        self.assertTrue(domain.wildcard)
        self.assertEqual("*.xn--bcher-kva.example", domain.value)

    def test_rejects_ports_whitespace_controls_and_injection(self) -> None:
        cases = {
            "example.com:443": "domain_port_not_allowed",
            "example .com": "domain_whitespace",
            "example.com\n": "domain_control_character",
            "example.com {": "domain_whitespace",
            "example.com{env.X}": "invalid_domain_label",
            "example.com;respond": "invalid_domain_label",
        }
        for value, code in cases.items():
            with self.subTest(value=value), self.assertRaises(CanonicalValidationError) as raised:
                parse_domain(value, field="routes[0].domain")
            self.assertEqual(code, raised.exception.code)
            self.assertEqual("routes[0].domain", raised.exception.field)

    def test_rejects_invalid_labels_and_dns_lengths(self) -> None:
        for value in (
            "localhost",
            ".example.com",
            "bad-.example.com",
            "under_score.example.com",
            ("a" * 64) + ".example.com",
            ".".join(["a" * 63] * 4),
            "example.com.",
            "foo.*.example.com",
        ):
            with self.subTest(value=value), self.assertRaises(CanonicalValidationError):
                parse_domain(value)


if __name__ == "__main__":
    unittest.main()
