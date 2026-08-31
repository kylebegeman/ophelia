from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ophelia.runtime_secrets import (
    RuntimeSecretError,
    RuntimeSecretResolver,
    parse_runtime_secret_reference,
)


class RuntimeSecretResolverTests(unittest.TestCase):
    def test_resolves_strict_reference_without_shell_interpolation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / "apps" / "lumen-staging" / "env"
            env.parent.mkdir(parents=True)
            env.write_text(
                "LUMEN_OPERATOR_TOKEN='literal-$(must-not-run)'\n"
                'LUMEN_CA_BASE64="Y2VydGlmaWNhdGU="\n',
                encoding="utf-8",
            )
            os.chmod(env, 0o600)
            resolver = RuntimeSecretResolver(root)

            self.assertEqual(
                "literal-$(must-not-run)",
                resolver("secret://lumen-staging/staging/lumen-operator-token"),
            )
            self.assertEqual(
                "Y2VydGlmaWNhdGU=",
                resolver("secret://lumen-staging/staging/lumen-ca-base64"),
            )
            self.assertTrue(
                resolver.available(
                    ["secret://lumen-staging/staging/lumen-operator-token"]
                )
            )

    def test_rejects_unsafe_provider_files_and_missing_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / "secrets" / "demo.production.env"
            env.parent.mkdir(parents=True)
            env.write_text("API_TOKEN=value\n", encoding="utf-8")
            os.chmod(env, 0o644)
            resolver = RuntimeSecretResolver(root)

            with self.assertRaises(RuntimeSecretError):
                resolver("secret://demo/production/api-token")
            os.chmod(env, 0o600)
            with self.assertRaises(RuntimeSecretError):
                resolver("secret://demo/production/missing-token")

    def test_reference_parser_maps_path_to_environment_key(self) -> None:
        self.assertEqual(
            ("demo", "production", "DATABASE_ADMIN_PASSWORD"),
            parse_runtime_secret_reference(
                "secret://demo/production/database/admin-password"
            ),
        )
        for invalid in (
            "https://demo/production/api-token",
            "secret://Demo/production/api-token",
            "secret://demo/production",
            "secret://demo/production/api-token?value=bad",
        ):
            with self.assertRaises(RuntimeSecretError):
                parse_runtime_secret_reference(invalid)


if __name__ == "__main__":
    unittest.main()
