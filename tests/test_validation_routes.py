from __future__ import annotations

import unittest

from ophelia.validation import (
    CanonicalValidationError,
    HTTPPathKind,
    parse_http_path,
)


class HTTPPathValidationTests(unittest.TestCase):
    def test_accepts_known_fixture_exact_and_prefix_paths(self) -> None:
        exact = parse_http_path("/api/openapi.json")
        prefix = parse_http_path("/api", kind=HTTPPathKind.PREFIX)
        self.assertEqual("/api/openapi.json", exact.value)
        self.assertEqual(HTTPPathKind.EXACT, exact.kind)
        self.assertEqual("/api", prefix.value)
        self.assertEqual(HTTPPathKind.PREFIX, prefix.kind)
        self.assertEqual(
            "/assets/%E2%82%AC",
            parse_http_path("/assets/%e2%82%ac").value,
        )
        self.assertEqual(
            "/assets/~icon-A",
            parse_http_path("/assets/%7eicon-%41").value,
        )

    def test_rejects_route_syntax_and_malformed_escapes(self) -> None:
        for value in (
            "api",
            "/a?query=1",
            "/a#fragment",
            "/a\\b",
            "/a\nb",
            "/a b",
            "/a{env.SECRET}",
            "/café",
            "/percent%",
            "/percent%2",
            "/percent%GG",
        ):
            with self.subTest(value=value), self.assertRaises(CanonicalValidationError):
                parse_http_path(value)

    def test_exact_paths_reject_caddy_matchers_and_placeholders(self) -> None:
        for value in (
            "/admin*",
            "/*",
            "/admin%2A",
            "/{env.OPHELIA_APP}",
            "/%7Benv.OPHELIA_APP%7D",
        ):
            with self.subTest(value=value), self.assertRaises(
                CanonicalValidationError
            ):
                parse_http_path(value)

    def test_rejects_literal_and_encoded_dot_segments(self) -> None:
        for value in ("/./x", "/../x", "/a/.", "/a/..", "/%2e/x", "/%2E%2E/x"):
            with self.subTest(value=value), self.assertRaises(CanonicalValidationError) as raised:
                parse_http_path(value)
            self.assertEqual("http_path_dot_segment", raised.exception.code)

    def test_rejects_ambiguous_delimiters_and_prefixes(self) -> None:
        for value in ("/a//b", "/a%2fb", "/a%5Cb", "/a%3Fb", "/a%23b"):
            with self.subTest(value=value), self.assertRaises(CanonicalValidationError):
                parse_http_path(value)

        for value in ("/", "/api/"):
            with self.subTest(value=value), self.assertRaises(CanonicalValidationError) as raised:
                parse_http_path(value, kind=HTTPPathKind.PREFIX)
            self.assertEqual("ambiguous_http_prefix", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
