from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.secret_providers import secret_provider_report, secret_provider_status


class SecretProviderTests(unittest.TestCase):
    def test_secret_provider_status_is_names_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report = secret_provider_status(runtime_root=Path(temp_dir) / "runtime")
        self.assertEqual("ophelia.secret_provider_status", report["kind"])
        names = {provider["name"] for provider in report["providers"]}
        self.assertIn("local_runtime_env", names)
        self.assertIn("github_environments", names)
        self.assertIn("sops_file", names)

    def test_provider_report_combines_runtime_github_and_sops_without_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = root / "myapp.ophelia.yml"
            manifest.write_text(_manifest())
            runtime_root = root / "runtime"
            app_root = runtime_root / "apps" / "myapp"
            app_root.mkdir(parents=True)
            (app_root / "env").write_text("OPHELIA_APP=myapp\nAPI_KEY=runtime-secret-value\n")

            github_obs = runtime_root / "github" / "secret-observations"
            github_obs.mkdir(parents=True)
            (github_obs / "myapp.production.json").write_text(
                json.dumps(
                    {
                        "repo": "example/myapp",
                        "environments": {
                            "production": {
                                "secrets": [
                                    {"name": "DATABASE_URL", "updated_at": "2026-06-21T00:00:00Z"}
                                ]
                            }
                        },
                    }
                )
            )

            sops_dir = runtime_root / "secrets"
            sops_dir.mkdir(parents=True)
            (sops_dir / "myapp.production.sops.yaml").write_text(
                "API_KEY: sops-secret-value\nDATABASE_URL: postgres://user:FAKESECRET@db/app\nsops:\n  mac: encrypted\n"
            )

            report = secret_provider_report(manifest, environment="production", runtime_root=runtime_root)

        self.assertEqual("ok", report["status"])
        keys = {item["name"]: item for item in report["keys"]}
        self.assertTrue(keys["API_KEY"]["present_any"])
        self.assertTrue(keys["DATABASE_URL"]["present_any"])
        database_providers = {provider["provider"]: provider for provider in keys["DATABASE_URL"]["providers"]}
        self.assertTrue(database_providers["github_environment"]["present"])
        self.assertTrue(database_providers["sops_file"]["present"])
        encoded = json.dumps(report)
        self.assertNotIn("runtime-secret-value", encoded)
        self.assertNotIn("sops-secret-value", encoded)
        self.assertNotIn("FAKESECRET", encoded)


def _manifest() -> str:
    return """
version: 1
app: myapp
environment: production
kind: service
image: ghcr.io/example/myapp:latest
addons:
  postgres: true
env:
  API_KEY: replace-me
services:
  web:
    port: 3000
routes:
  - domain: myapp.example.com
    service: web
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
