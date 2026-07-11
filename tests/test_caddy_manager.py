from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.caddy_manager import CADDY_ENVFILE_RELOAD_SCRIPT, reload_caddy, validate_caddy  # noqa: E402


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
                if command[:2] == ["docker", "inspect"]:
                    return _inspect_response(
                        command,
                        runtime_root,
                        ophelia_root,
                        {"shared-caddy-1"},
                    )
                if command[:2] == ["docker", "exec"]:
                    return SimpleNamespace(returncode=0, stdout="reloaded\n", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch("ophelia.caddy_manager.subprocess.run", side_effect=fake_run) as run:
                report = reload_caddy(runtime_root=runtime_root, ophelia_root=ophelia_root)

        self.assertTrue(report["ok"])
        self.assertTrue(report["validated"])
        self.assertTrue(report["reloaded"])
        self.assertTrue(report["container_verified"])
        self.assertEqual("shared-caddy-1", report["container"])
        self.assertEqual(_container_id("shared-caddy-1"), report["container_id"])
        self.assertEqual("ophelia.edge.reload", report["kind"])
        self.assertEqual(0, report["returncode"])
        pinned_id = _container_id("shared-caddy-1")
        self.assertIn(["docker", "exec", pinned_id, "sh", "-ec"], [call.args[0][:5] for call in run.call_args_list])
        self.assertEqual(["docker", "exec", pinned_id, "sh", "-ec", CADDY_ENVFILE_RELOAD_SCRIPT], report["command"])
        self.assertIn("caddy adapt --config /etc/caddy/Caddyfile", report["command"][-1])
        self.assertIn("--envfile /etc/caddy/env", report["command"][-1])
        self.assertIn("managed static root expanded without OPHELIA_STATIC_ROOT", report["command"][-1])
        self.assertIn('caddy reload --config "$tmp"', report["command"][-1])

    def test_reload_caddy_fails_when_static_root_collapses(self) -> None:
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
                if command[:2] == ["docker", "inspect"]:
                    return _inspect_response(
                        command,
                        runtime_root,
                        ophelia_root,
                        {"shared-caddy-1"},
                    )
                if command[:2] == ["docker", "exec"]:
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="Refusing Caddy reload: managed static root expanded without OPHELIA_STATIC_ROOT.\n",
                    )
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch("ophelia.caddy_manager.subprocess.run", side_effect=fake_run):
                report = reload_caddy(runtime_root=runtime_root, ophelia_root=ophelia_root)

        self.assertFalse(report["ok"])
        self.assertFalse(report["reloaded"])
        self.assertEqual("caddy_reload_failed", report["errors"][0]["code"])
        self.assertIn("managed static root expanded without OPHELIA_STATIC_ROOT", report["stderr"])

    def test_reload_caddy_report_does_not_include_envfile_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ophelia_root = root / "ophelia"
            caddy_dir = ophelia_root / "platform" / "shared" / "caddy"
            caddy_dir.mkdir(parents=True)
            (caddy_dir / "Caddyfile").write_text("{}\n")
            runtime_root = root / "runtime"
            env_file = runtime_root / "caddy" / "env"
            env_file.parent.mkdir(parents=True)
            env_file.write_text("OPHELIA_STATIC_ROOT=/srv/private-static\nAPI_TOKEN=super-secret-token\n")

            def fake_run(command, **kwargs):
                if command[:2] == ["docker", "run"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if command[:2] == ["docker", "inspect"]:
                    return _inspect_response(
                        command,
                        runtime_root,
                        ophelia_root,
                        {"shared-caddy-1"},
                    )
                if command[:2] == ["docker", "exec"]:
                    return SimpleNamespace(returncode=0, stdout="reloaded\n", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch("ophelia.caddy_manager.subprocess.run", side_effect=fake_run):
                report = reload_caddy(runtime_root=runtime_root, ophelia_root=ophelia_root)

        serialized = json.dumps(report, sort_keys=True)
        self.assertTrue(report["ok"])
        self.assertNotIn("super-secret-token", serialized)
        self.assertNotIn("/srv/private-static", serialized)

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
                if command[:2] == ["docker", "inspect"]:
                    return _inspect_response(
                        command,
                        runtime_root,
                        ophelia_root,
                        {"quark-reverse-proxy-caddy-1"},
                    )
                if command[:2] == ["docker", "exec"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch("ophelia.caddy_manager.subprocess.run", side_effect=fake_run):
                report = reload_caddy(runtime_root=runtime_root, ophelia_root=ophelia_root)

        self.assertTrue(report["ok"])
        self.assertEqual("quark-reverse-proxy-caddy-1", report["container"])
        warning_codes = {item["code"] for item in report["warnings"]}
        self.assertIn("legacy_caddy_container_name", warning_codes)

    def test_reload_caddy_rejects_unrelated_discovered_container(self) -> None:
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
                if command[:2] == ["docker", "inspect"]:
                    if command[-1] == "unrelated-caddy" and command[3] == "{{json .}}":
                        payload = _inspect_payload(
                            "unrelated-caddy",
                            root / "different-runtime",
                            ophelia_root,
                        )
                        return SimpleNamespace(
                            returncode=0, stdout=payload, stderr=""
                        )
                    return SimpleNamespace(
                        returncode=1, stdout="", stderr="not found"
                    )
                if command[:2] == ["docker", "ps"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout="unrelated-caddy\n",
                        stderr="",
                    )
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch(
                "ophelia.caddy_manager.subprocess.run", side_effect=fake_run
            ) as run:
                report = reload_caddy(
                    runtime_root=runtime_root, ophelia_root=ophelia_root
                )

        self.assertFalse(report["ok"])
        self.assertFalse(report["container_verified"])
        self.assertIsNone(report["container"])
        self.assertFalse(
            any(call.args[0][:2] == ["docker", "exec"] for call in run.call_args_list)
        )

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


def _inspect_response(command, runtime_root, ophelia_root, running):
    name = command[-1]
    if name not in running:
        return SimpleNamespace(returncode=1, stdout="", stderr="not found")
    if command[3] == "{{json .}}":
        return SimpleNamespace(
            returncode=0,
            stdout=_inspect_payload(name, runtime_root, ophelia_root),
            stderr="",
        )
    raise AssertionError(f"unexpected inspect command: {command}")


def _mount_payload(runtime_root, ophelia_root):
    mounts = [
        {
            "Source": str(
                ophelia_root / "platform" / "shared" / "caddy" / "Caddyfile"
            ),
            "Destination": "/etc/caddy/Caddyfile",
        },
        {
            "Source": str(runtime_root / "caddy" / "env"),
            "Destination": "/etc/caddy/env",
        },
        {
            "Source": str(runtime_root / "caddy" / "global.d"),
            "Destination": "/etc/caddy/global.d",
        },
        {
            "Source": str(runtime_root / "caddy" / "sites.d"),
            "Destination": "/etc/caddy/sites.d",
        },
    ]
    return json.dumps(mounts)


def _inspect_payload(name, runtime_root, ophelia_root):
    return json.dumps(
        {
            "Id": _container_id(name),
            "State": {"Running": True},
            "Mounts": json.loads(_mount_payload(runtime_root, ophelia_root)),
        }
    )


def _container_id(name):
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
