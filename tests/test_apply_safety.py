from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class ApplySafetyTests(unittest.TestCase):
    def test_journaled_static_gate_excludes_unmodeled_and_legacy_state(self) -> None:
        from ophelia.execution.legacy_static_runner import supports_journaled_static
        from ophelia.manifest import load_manifest

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "static.ophelia.yml"
            manifest_path.write_text(
                _static_manifest(Path("static"), environment="production")
            )
            manifest = load_manifest(manifest_path)
            self.assertTrue(
                supports_journaled_static(manifest, runtime_root)
            )

            on_demand_path = root / "on-demand.ophelia.yml"
            on_demand_path.write_text(
                _static_manifest(Path("static"), environment="production")
                + "edge:\n"
                + "  on_demand_tls:\n"
                + "    ask: https://control.example.com/allow\n"
            )
            self.assertFalse(
                supports_journaled_static(
                    load_manifest(on_demand_path), runtime_root
                )
            )

            legacy_release = (
                runtime_root
                / "static"
                / manifest.app
                / "releases"
                / "legacy-release"
            )
            legacy_release.mkdir(parents=True)
            current = legacy_release.parents[1] / "current"
            current.symlink_to(
                Path("releases") / "legacy-release",
                target_is_directory=True,
            )
            caddy = (
                runtime_root
                / "caddy"
                / "sites.d"
                / f"{manifest.app}.caddy"
            )
            caddy.parent.mkdir(parents=True)
            caddy.write_text("legacy\n")
            self.assertFalse(
                supports_journaled_static(manifest, runtime_root)
            )

            record = (
                runtime_root
                / "apps"
                / manifest.app
                / "static-revisions"
                / "legacy-release.json"
            )
            record.parent.mkdir(parents=True)
            record.write_text("{}\n")
            self.assertFalse(
                supports_journaled_static(manifest, runtime_root)
            )

            global_caddy = (
                runtime_root
                / "caddy"
                / "global.d"
                / "ophelia-on-demand-tls.caddy"
            )
            global_caddy.parent.mkdir(parents=True)
            global_caddy.write_text("on_demand_tls {}\n")
            self.assertFalse(
                supports_journaled_static(manifest, runtime_root)
            )

    def test_production_apply_requires_confirmation_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "static.ophelia.yml"
            static_root = root / "static"
            static_root.mkdir()
            manifest_path.write_text(
                _static_manifest(Path("static"), environment="production")
            )
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
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"inspect\" ]; then echo true; fi\n"
                "exit 0\n"
            )
            fake_docker.chmod(0o755)
            caddy_root = root / "platform" / "shared" / "caddy"
            caddy_root.mkdir(parents=True)
            (caddy_root / "Caddyfile").write_text(
                "import /runtime/caddy/sites.d/*.caddy\n"
            )
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

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
                env=environment,
            )
            failure_detail = ""
            database_path = runtime_root / "host-state" / "operations.db"
            if applied.returncode and database_path.exists():
                diagnostic = sqlite3.connect(database_path)
                try:
                    row = diagnostic.execute(
                        "SELECT payload_json FROM terminal_receipts"
                    ).fetchone()
                    failure_detail = "" if row is None else row[0]
                finally:
                    diagnostic.close()
            self.assertEqual(
                0,
                applied.returncode,
                applied.stdout + applied.stderr + failure_detail,
            )
            self.assertIn("Apply result:", applied.stdout)
            self.assertIn("Kernel receipt:", applied.stdout)
            current = runtime_root / "static" / "safe-static" / "current"
            self.assertTrue(current.is_symlink())
            connection = sqlite3.connect(
                runtime_root / "host-state" / "operations.db"
            )
            try:
                self.assertEqual(
                    "succeeded",
                    connection.execute(
                        "SELECT outcome FROM terminal_receipts"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    "safe-static",
                    connection.execute(
                        "SELECT app FROM active_revisions"
                    ).fetchone()[0],
                )
            finally:
                connection.close()

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

    def test_apply_rejects_missing_required_env_keys_before_docker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "static.ophelia.yml"
            static_root = root / "static"
            static_root.mkdir()
            manifest_path.write_text(_static_manifest(static_root, environment="staging", required_env=True))
            runtime_root = root / "runtime"
            app_root = runtime_root / "apps" / "safe-static"
            app_root.mkdir(parents=True)
            (app_root / "env").write_text("OPHELIA_APP=safe-static\n")
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
            self.assertIn("missing required env keys", result.stdout)
            self.assertIn("API_TOKEN", result.stdout)
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
            reload_commands = [call.args[0] for call in caddy_calls if call.args[0][-3:-1] == ["sh", "-ec"]]
            self.assertTrue(reload_commands)
            reload_command = reload_commands[-1]
            self.assertIn("sh", reload_command)
            self.assertIn("-ec", reload_command)
            reload_script = reload_command[-1]
            self.assertIn("caddy adapt --config /etc/caddy/Caddyfile", reload_script)
            self.assertIn("--envfile /etc/caddy/env", reload_script)
            self.assertIn('caddy reload --config "$tmp"', reload_script)
            command_windows = [reload_command[index : index + 4] for index in range(len(reload_command))]
            self.assertNotIn(["caddy", "reload", "--config", "/etc/caddy/Caddyfile"], command_windows)

    def test_apply_uses_envfile_reload_when_caddy_env_changes(self) -> None:
        from ophelia.caddy_manager import CADDY_ENVFILE_RELOAD_SCRIPT
        from ophelia.manifest import load_manifest
        from ophelia.runtime import apply_local_bundle

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "static.ophelia.yml"
            static_root = root / "static"
            runtime_root = root / "custom-runtime"
            ophelia_root = root / "ophelia"
            static_root.mkdir()
            (static_root / "index.html").write_text("<h1>Static</h1>\n")
            (ophelia_root / "platform" / "shared").mkdir(parents=True)
            (ophelia_root / "platform" / "shared" / "compose.yml").write_text("services: {}\n")
            manifest_path.write_text(
                f"""
version: 1
app: env-static
environment: staging
kind: static
static_root: {static_root}
env:
  EDGE_TOKEN: secret
routes:
  - domain: env-static.example.com
edge:
  on_demand_tls:
    ask: http://control.example.com/allow?token={{$EDGE_TOKEN}}
""".strip()
                + "\n"
            )
            manifest = load_manifest(manifest_path)

            def fake_run(command, **kwargs):
                if "ps" in command:
                    return subprocess.CompletedProcess(command, 0, stdout="caddy\n", stderr="")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch("ophelia.runtime._run", side_effect=fake_run) as run:
                apply_local_bundle(manifest, manifest_path, runtime_root, ophelia_root)

            caddy_calls = [call.args[0] for call in run.call_args_list if "docker" in call.args[0] and "compose" in call.args[0]]

        self.assertTrue(caddy_calls)
        self.assertFalse(any("--force-recreate" in command for command in caddy_calls))
        reload_commands = [command for command in caddy_calls if command[-3:-1] == ["sh", "-ec"]]
        self.assertTrue(reload_commands)
        self.assertEqual(CADDY_ENVFILE_RELOAD_SCRIPT, reload_commands[-1][-1])

    def test_apply_prepares_missing_shared_external_networks(self) -> None:
        from ophelia.manifest import load_manifest
        from ophelia.runtime import ensure_compose_networks

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "service.ophelia.yml"
            manifest_path.write_text(
                """
version: 1
app: networked-app
kind: service
image: ghcr.io/example/networked-app:latest
services:
  web:
    port: 3000
routes:
  - domain: networked.example.com
    service: web
""".strip()
                + "\n"
            )
            manifest = load_manifest(manifest_path)

            with mock.patch("ophelia.runtime._run", return_value=None) as run, mock.patch(
                "ophelia.runtime._run_apply_phase"
            ) as apply_phase:
                ensure_compose_networks(manifest)

            inspect_calls = [call.args[0] for call in run.call_args_list]
            create_calls = [call.args[0] for call in apply_phase.call_args_list]
            self.assertIn(["docker", "network", "inspect", "ophelia-edge"], inspect_calls)
            self.assertIn(["docker", "network", "inspect", "ophelia-internal"], inspect_calls)
            self.assertIn(["docker", "network", "create", "ophelia-edge"], create_calls)
            self.assertIn(["docker", "network", "create", "ophelia-internal"], create_calls)

    def test_apply_prepares_shared_internal_network_for_per_app_postgres(self) -> None:
        from ophelia.manifest import load_manifest
        from ophelia.runtime import ensure_compose_networks

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "service.ophelia.yml"
            manifest_path.write_text(
                """
version: 1
app: networked-postgres-app
environment: staging
kind: service
image: ghcr.io/example/networked-postgres-app:latest
networking:
  internal: per-app
addons:
  postgres: true
services:
  web:
    port: 3000
routes:
  - domain: networked-postgres.example.com
    service: web
""".strip()
                + "\n"
            )
            manifest = load_manifest(manifest_path)

            with mock.patch("ophelia.runtime._run", return_value=None) as run, mock.patch(
                "ophelia.runtime._run_apply_phase"
            ) as apply_phase:
                ensure_compose_networks(manifest)

            inspect_calls = [call.args[0] for call in run.call_args_list]
            create_calls = [call.args[0] for call in apply_phase.call_args_list]
            self.assertIn(["docker", "network", "inspect", "ophelia-edge"], inspect_calls)
            self.assertIn(["docker", "network", "inspect", "ophelia-internal"], inspect_calls)
            self.assertIn(["docker", "network", "create", "ophelia-edge"], create_calls)
            self.assertIn(["docker", "network", "create", "ophelia-internal"], create_calls)

    def test_apply_uses_local_image_when_private_pull_fails_but_image_exists(self) -> None:
        from ophelia.manifest import load_manifest
        from ophelia.runtime import apply_local_bundle

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "service.ophelia.yml"
            manifest_path.write_text(_service_manifest())
            manifest = load_manifest(manifest_path)
            commands: list[list[str]] = []

            def fake_run(command, capture_output=False, allow_failure=False, env=None):
                commands.append(command)
                if command[:2] == ["docker", "compose"] and command[-1] == "pull":
                    raise subprocess.CalledProcessError(
                        1,
                        command,
                        stderr="error from registry: denied",
                    )
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch("ophelia.runtime._run", side_effect=fake_run):
                app_root = apply_local_bundle(manifest, manifest_path, runtime_root, root)

            release = json.loads((app_root / "release.json").read_text())
            self.assertTrue(release["applied"])
            self.assertEqual("applied", release["apply"]["status"])
            self.assertIn(
                ["docker", "image", "inspect", "ghcr.io/example/private@sha256:aaaaaaaa"],
                commands,
            )
            self.assertTrue(any(command[:2] == ["docker", "compose"] and "up" in command for command in commands))

    def test_on_demand_tls_global_config_is_host_consolidated(self) -> None:
        from ophelia.manifest import load_manifest
        from ophelia.runtime import apply_local_bundle

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "edge.ophelia.yml"
            manifest_path.write_text(_tunnel_on_demand_manifest("edge-app", "http://control.example.com/allow?token={$EDGE_TOKEN}"))

            apply_local_bundle(load_manifest(manifest_path), manifest_path, runtime_root, root)

            host_global = runtime_root / "caddy" / "global.d" / "ophelia-on-demand-tls.caddy"
            self.assertTrue(host_global.exists())
            self.assertIn("on_demand_tls", host_global.read_text())
            self.assertIn("http://control.example.com/allow?token={$EDGE_TOKEN}", host_global.read_text())
            self.assertFalse((runtime_root / "caddy" / "global.d" / "edge-app.caddy").exists())

    def test_on_demand_tls_conflicting_ask_endpoints_fail_before_activation(self) -> None:
        from ophelia.manifest import load_manifest
        from ophelia.runtime import ApplyPhaseError, apply_local_bundle

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            first_path = root / "first.ophelia.yml"
            second_path = root / "second.ophelia.yml"
            first_path.write_text(_tunnel_on_demand_manifest("edge-one", "http://control-one.example.com/allow"))
            second_path.write_text(_tunnel_on_demand_manifest("edge-two", "http://control-two.example.com/allow"))

            apply_local_bundle(load_manifest(first_path), first_path, runtime_root, root)
            with self.assertRaises(ApplyPhaseError) as failure:
                apply_local_bundle(load_manifest(second_path), second_path, runtime_root, root)

            self.assertEqual("caddy_global_sync", failure.exception.phase)
            self.assertFalse((runtime_root / "apps" / "edge-two" / "active_release.json").exists())


def _static_manifest(
    static_root: Path,
    environment: str,
    placeholder: bool = False,
    required_env: bool = False,
) -> str:
    env = "env:\n  API_TOKEN: replace-me\n" if placeholder else ""
    required = "required_env:\n  - API_TOKEN\n" if required_env else ""
    return f"""
version: 1
app: safe-static
environment: {environment}
kind: static
static_root: {static_root}
{required}
{env}
routes:
  - domain: safe-static.example.com
""".strip() + "\n"


def _tunnel_on_demand_manifest(app: str, ask: str) -> str:
    return f"""
version: 1
app: {app}
kind: tunnel
env:
  EDGE_TOKEN: secret
routes:
  - domain: {app}.example.com
    upstream: host.docker.internal:3000
edge:
  on_demand_tls:
    ask: {ask}
  catch_all:
    upstream: host.docker.internal:3000
""".strip() + "\n"


def _service_manifest() -> str:
    return """
version: 1
app: private-service
environment: staging
kind: service
image: ghcr.io/example/private@sha256:aaaaaaaa
services:
  app:
    port: 3000
routes:
  - domain: private-service.example.com
    service: app
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
