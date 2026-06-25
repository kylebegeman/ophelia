from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.caddy_manager import reload_caddy, validate_caddy  # noqa: E402


class CaddyManagerTests(unittest.TestCase):
    def test_validate_caddy_uses_configurable_static_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ophelia_root = root / "ophelia"
            caddy_dir = ophelia_root / "platform" / "shared" / "caddy"
            caddy_dir.mkdir(parents=True)
            (caddy_dir / "Caddyfile").write_text("{}\n")
            runtime_root = root / "runtime"
            static_root = root / "public-static"

            with mock.patch("ophelia.caddy_manager.subprocess.run") as run:
                run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
                report = validate_caddy(runtime_root=runtime_root, ophelia_root=ophelia_root, static_root=static_root)
                static_root_exists = static_root.exists()

        command = report["command"]
        self.assertEqual(0, report["returncode"])
        self.assertIn(f"{static_root}:{static_root}:ro", command)
        old_private_mount = "/home/" + "kyle" + "/websites:/home/" + "kyle" + "/websites:ro"
        self.assertNotIn(old_private_mount, command)
        self.assertTrue(static_root_exists)

    def test_reload_caddy_discovers_shared_container_and_returns_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ophelia_root = root / "ophelia"
            caddy_dir = ophelia_root / "platform" / "shared" / "caddy"
            caddy_dir.mkdir(parents=True)
            (caddy_dir / "Caddyfile").write_text("{}\n")
            runtime_root = root / "runtime"

            def fake_run(command, **kwargs):
                if command[:2] == ["docker", "run"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if command[:3] == ["docker", "inspect", "--format"]:
                    return SimpleNamespace(returncode=0, stdout="true\n", stderr="")
                if command[:2] == ["docker", "exec"]:
                    return SimpleNamespace(returncode=0, stdout="reloaded\n", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch("ophelia.caddy_manager.subprocess.run", side_effect=fake_run) as run:
                report = reload_caddy(runtime_root=runtime_root, ophelia_root=ophelia_root)

        self.assertTrue(report["ok"])
        self.assertTrue(report["validated"])
        self.assertTrue(report["reloaded"])
        self.assertEqual("shared-caddy-1", report["container"])
        self.assertEqual("ophelia.edge.reload", report["kind"])
        self.assertEqual(0, report["returncode"])
        self.assertIn(["docker", "exec", "shared-caddy-1", "caddy"], [call.args[0][:4] for call in run.call_args_list])

    def test_reload_caddy_uses_legacy_container_only_as_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ophelia_root = root / "ophelia"
            caddy_dir = ophelia_root / "platform" / "shared" / "caddy"
            caddy_dir.mkdir(parents=True)
            (caddy_dir / "Caddyfile").write_text("{}\n")
            runtime_root = root / "runtime"

            def fake_run(command, **kwargs):
                if command[:2] == ["docker", "run"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if command[:3] == ["docker", "inspect", "--format"]:
                    name = command[-1]
                    if name == "quark-reverse-proxy-caddy-1":
                        return SimpleNamespace(returncode=0, stdout="true\n", stderr="")
                    return SimpleNamespace(returncode=1, stdout="", stderr="not found")
                if command[:2] == ["docker", "exec"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch("ophelia.caddy_manager.subprocess.run", side_effect=fake_run):
                report = reload_caddy(runtime_root=runtime_root, ophelia_root=ophelia_root)

        self.assertTrue(report["ok"])
        self.assertEqual("quark-reverse-proxy-caddy-1", report["container"])
        warning_codes = {item["code"] for item in report["warnings"]}
        self.assertIn("legacy_caddy_container_name", warning_codes)

    def test_reload_caddy_skips_reload_when_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ophelia_root = root / "ophelia"
            caddy_dir = ophelia_root / "platform" / "shared" / "caddy"
            caddy_dir.mkdir(parents=True)
            (caddy_dir / "Caddyfile").write_text("{}\n")
            runtime_root = root / "runtime"

            with mock.patch("ophelia.caddy_manager.subprocess.run") as run:
                run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="bad config")
                report = reload_caddy(runtime_root=runtime_root, ophelia_root=ophelia_root)

        self.assertFalse(report["ok"])
        self.assertFalse(report["reloaded"])
        self.assertEqual("caddy_validation_failed", report["errors"][0]["code"])
        self.assertFalse(any(call.args[0][:2] == ["docker", "exec"] for call in run.call_args_list))


if __name__ == "__main__":
    unittest.main()
