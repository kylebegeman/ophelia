from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.manifest import Manifest
from ophelia.remote import _build_remote_stage_script, _sync_bundle


class RemoteTests(unittest.TestCase):
    def test_remote_apply_script_forwards_verification_options(self) -> None:
        manifest = Manifest(
            version=1,
            app="remote-app",
            kind="static",
            environment="staging",
            profile=None,
            image=None,
            services={},
            routes=[],
        )

        script = _build_remote_stage_script(
            manifest=manifest,
            manifest_path=Path("remote-app.ophelia.yml"),
            remote_runtime_root="~/ophelia-runtime",
            remote_ophelia_root="~/ophelia",
            apply=True,
            verify=True,
            verify_attempts=3,
            verify_interval=2,
            verify_timeout=4,
            verify_failure_mode="warn",
        )

        self.assertIn("--apply --verify", script)
        self.assertIn("--verify-attempts 3", script)
        self.assertIn("--verify-interval 2", script)
        self.assertIn("--verify-timeout 4", script)
        self.assertIn("--verify-failure-mode warn", script)

    def test_remote_bundle_sync_preserves_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle_root = Path(temp_dir) / "remote-app"
            bundle_root.mkdir()

            with patch("ophelia.remote._run") as run:
                _sync_bundle(bundle_root, "deploy@example.com", 22022, "~/ophelia-runtime", "remote-app")

            command = run.call_args.args[0]
            self.assertIn("env", _excluded_paths(command))
            self.assertIn("release.json", _excluded_paths(command))
            self.assertIn("active_release.json", _excluded_paths(command))
            self.assertIn("releases/", _excluded_paths(command))
            self.assertIn("release-bundles/", _excluded_paths(command))
            self.assertIn("addons.json", _excluded_paths(command))
            self.assertIn("restore-previews/", _excluded_paths(command))


def _excluded_paths(command: list[str]) -> set[str]:
    return {command[index + 1] for index, item in enumerate(command[:-1]) if item == "--exclude"}


if __name__ == "__main__":
    unittest.main()
