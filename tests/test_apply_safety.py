from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class ApplySafetyTests(unittest.TestCase):
    def test_production_apply_requires_confirmation_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "static.ophelia.yml"
            static_root = root / "static"
            static_root.mkdir()
            manifest_path.write_text(_static_manifest(static_root, environment="production"))
            runtime_root = root / "runtime"
            repo = Path(__file__).resolve().parents[1]

            missing = subprocess.run(
                [
                    str(repo / "cli" / "ship"),
                    "deploy",
                    str(manifest_path),
                    "--runtime-root",
                    str(runtime_root),
                    "--apply",
                ],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(0, missing.returncode)
            self.assertIn("requires confirmation token", missing.stdout)

            plan = subprocess.run(
                [
                    str(repo / "cli" / "ship"),
                    "deploy",
                    str(manifest_path),
                    "--runtime-root",
                    str(runtime_root),
                    "--plan",
                    "--json",
                ],
                text=True,
                capture_output=True,
                check=True,
            )
            token = json.loads(plan.stdout)["confirmation_token"]

            applied = subprocess.run(
                [
                    str(repo / "cli" / "ship"),
                    "deploy",
                    str(manifest_path),
                    "--runtime-root",
                    str(runtime_root),
                    "--ophelia-root",
                    str(root),
                    "--apply",
                    "--confirm",
                    token,
                ],
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertIn("Apply result:", applied.stdout)
            release = json.loads((runtime_root / "apps" / "safe-static" / "release.json").read_text())
            self.assertTrue(release["applied"])
            self.assertIsNone(release["verified"])
            self.assertEqual("applied", release["apply"]["status"])

    def test_apply_rejects_placeholder_env_values_before_docker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "static.ophelia.yml"
            static_root = root / "static"
            static_root.mkdir()
            manifest_path.write_text(_static_manifest(static_root, environment="staging", placeholder=True))
            runtime_root = root / "runtime"
            repo = Path(__file__).resolve().parents[1]

            result = subprocess.run(
                [
                    str(repo / "cli" / "ship"),
                    "deploy",
                    str(manifest_path),
                    "--runtime-root",
                    str(runtime_root),
                    "--ophelia-root",
                    str(root),
                    "--apply",
                ],
                text=True,
                capture_output=True,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("placeholder env values", result.stdout)
            release = json.loads((runtime_root / "apps" / "safe-static" / "release.json").read_text())
            self.assertFalse(release["applied"])
            self.assertEqual("failed", release["apply"]["status"])
            self.assertEqual("env_validation", release["apply"]["phase"])

    def test_shared_caddy_reload_uses_selected_runtime_root(self) -> None:
        from ophelia.manifest import load_manifest
        from ophelia.runtime import apply_local_bundle

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "static.ophelia.yml"
            static_root = root / "static"
            runtime_root = root / "custom-runtime"
            ophelia_root = root / "ophelia"
            static_root.mkdir()
            (ophelia_root / "platform" / "shared").mkdir(parents=True)
            (ophelia_root / "platform" / "shared" / "compose.yml").write_text("services: {}\n")
            manifest_path.write_text(_static_manifest(static_root, environment="staging"))
            manifest = load_manifest(manifest_path)

            def fake_run(command, **kwargs):
                if "ps" in command:
                    return subprocess.CompletedProcess(command, 0, stdout="caddy\n", stderr="")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch("ophelia.runtime._run", side_effect=fake_run) as run:
                apply_local_bundle(manifest, manifest_path, runtime_root, ophelia_root)

            caddy_calls = [call for call in run.call_args_list if "docker" in call.args[0] and "compose" in call.args[0]]
            self.assertTrue(caddy_calls)
            for call in caddy_calls:
                self.assertEqual(str(runtime_root), call.kwargs["env"]["OPHELIA_RUNTIME_ROOT"])


def _static_manifest(static_root: Path, environment: str, placeholder: bool = False) -> str:
    env = "env:\n  API_TOKEN: replace-me\n" if placeholder else ""
    return f"""
version: 1
app: safe-static
environment: {environment}
kind: static
static_root: {static_root}
{env}
routes:
  - domain: safe-static.example.com
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
