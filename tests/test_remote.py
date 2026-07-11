from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.manifest import Manifest
from ophelia.remote import (
    _build_remote_stage_script,
    _ssh_command,
    _sync_bundle,
    _sync_plan_candidate,
    stage_remote_bundle,
)
from ophelia.runtime import DeployMetadata


class RemoteTests(unittest.TestCase):
    def test_remote_apply_script_forwards_confirmation_and_verification_options(self) -> None:
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
            confirm="remote-token",
            verify=True,
            verify_attempts=3,
            verify_interval=2,
            verify_timeout=4,
            verify_failure_mode="warn",
            deploy_metadata=DeployMetadata(
                release_id="release-123",
                commit_sha="abc123",
                build_time="2026-06-25T12:00:00Z",
            ),
        )

        self.assertIn("--apply --confirm remote-token --verify", script)
        self.assertIn("--verify-attempts 3", script)
        self.assertIn("--verify-interval 2", script)
        self.assertIn("--verify-timeout 4", script)
        self.assertIn("--verify-failure-mode warn", script)
        self.assertIn("--release-id release-123", script)
        self.assertIn("--commit-sha abc123", script)
        self.assertIn("--build-time 2026-06-25T12:00:00Z", script)

    def test_remote_plan_script_uses_remote_app_root_and_json_flag(self) -> None:
        manifest = _manifest(environment="production")

        script = _build_remote_stage_script(
            manifest=manifest,
            manifest_path=Path("remote-app.ophelia.yml"),
            remote_runtime_root="~/ophelia-runtime",
            remote_ophelia_root="~/ophelia",
            apply=False,
            plan=True,
            json_output=True,
            plan_operation_id="deploy-plan.remote-app.fixture",
        )

        self.assertIn(
            'APP_ROOT=$HOME/ophelia-runtime/staging/deploy-plan.remote-app.fixture/candidate',
            script,
        )
        self.assertIn('deploy "$APP_ROOT/manifest.lock.json"', script)
        self.assertIn("--runtime-root \"$REMOTE_RUNTIME_ROOT\"", script)
        self.assertIn("--ophelia-root \"$REMOTE_OPHELIA_ROOT\"", script)
        self.assertIn("--plan --json", script)
        self.assertIn("--plan-operation-id deploy-plan.remote-app.fixture", script)
        self.assertNotIn("--apply", script)

    def test_remote_stage_script_preserves_stage_metadata_and_caddy_sync(self) -> None:
        manifest = _manifest(environment="staging")

        script = _build_remote_stage_script(
            manifest=manifest,
            manifest_path=Path("remote-app.ophelia.yml"),
            remote_runtime_root="~/ophelia-runtime",
            remote_ophelia_root="~/ophelia",
            apply=False,
        )

        self.assertIn('cat > "$APP_ROOT/release.json"', script)
        self.assertIn('"mode": "staged"', script)
        self.assertIn('CADDY_TARGET="$REMOTE_RUNTIME_ROOT/caddy/sites.d/remote-app.caddy"', script)
        self.assertIn('cp "$APP_ROOT/caddy/remote-app.caddy" "$CADDY_TARGET"', script)

    def test_remote_plan_sync_targets_candidate_without_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle_root = Path(temp_dir) / "remote-app"
            bundle_root.mkdir()

            with patch("ophelia.remote._run") as run:
                _sync_plan_candidate(
                    bundle_root,
                    "deploy@example.com",
                    None,
                    "~/ophelia-runtime",
                    "deploy-plan.remote-app.fixture",
                )

            command = run.call_args.args[0]
            self.assertNotIn("--delete", command)
            destination = command[-1]
            self.assertIn("/staging/deploy-plan.remote-app.fixture/candidate/", destination)
            self.assertNotIn("/apps/", destination)

    def test_remote_plan_never_calls_live_bundle_sync(self) -> None:
        manifest = _manifest(environment="production")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "remote-app.ophelia.yml"
            manifest_path.write_text(_manifest_text(environment="production"))
            completed = unittest.mock.MagicMock(stdout='{"planned": true}')
            with (
                patch("ophelia.remote._run", return_value=completed) as run,
                patch("ophelia.remote._sync_bundle", side_effect=AssertionError("live rsync")),
            ):
                output = stage_remote_bundle(
                    manifest,
                    manifest_path,
                    "deploy@example.com",
                    None,
                    "~/ophelia-runtime",
                    "~/ophelia",
                    apply=False,
                    plan=True,
                    json_output=True,
                )

            self.assertEqual('{"planned": true}', output)
            commands = [call.args[0] for call in run.call_args_list]
            rsync = next(command for command in commands if command[0] == "rsync")
            self.assertNotIn("--delete", rsync)
            self.assertIn("/staging/", rsync[-1])
            self.assertNotIn("/apps/", rsync[-1])
            remote_script = commands[-1][-1]
            self.assertIn("/staging/", remote_script)

    def test_remote_bundle_sync_preserves_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle_root = Path(temp_dir) / "remote-app"
            bundle_root.mkdir()

            with patch("ophelia.remote._run") as run:
                _sync_bundle(bundle_root, "deploy@example.com", None, "~/ophelia-runtime", "remote-app")

            command = run.call_args.args[0]
            self.assertNotIn("-e", command)
            self.assertIn("env", _excluded_paths(command))
            self.assertIn("release.json", _excluded_paths(command))
            self.assertIn("active_release.json", _excluded_paths(command))
            self.assertIn("releases/", _excluded_paths(command))
            self.assertIn("release-bundles/", _excluded_paths(command))
            self.assertIn("addons.json", _excluded_paths(command))
            self.assertIn("restore-previews/", _excluded_paths(command))

    def test_remote_bundle_sync_uses_explicit_ssh_port_when_provided(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle_root = Path(temp_dir) / "remote-app"
            bundle_root.mkdir()

            with patch("ophelia.remote._run") as run:
                _sync_bundle(bundle_root, "deploy@example.com", 22022, "~/ophelia-runtime", "remote-app")

            command = run.call_args.args[0]
            self.assertIn("-e", command)
            self.assertIn("ssh -p 22022", command)

    def test_ssh_command_uses_ssh_config_when_port_is_omitted(self) -> None:
        command = _ssh_command("deploy@example.com", None, "echo ok")

        self.assertEqual("ssh", command[0])
        self.assertNotIn("-p", command)
        self.assertIn("deploy@example.com", command)

    def test_ssh_command_uses_explicit_port_when_provided(self) -> None:
        command = _ssh_command("deploy@example.com", 22022, "echo ok")

        self.assertEqual(["ssh", "-p", "22022"], command[:3])
        self.assertIn("deploy@example.com", command)

    def test_host_plan_runs_remote_plan_not_local_plan(self) -> None:
        from ophelia.commands import deploy

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "remote-app.ophelia.yml"
            manifest_path.write_text(_manifest_text(environment="production"))
            args = _deploy_args(manifest_path, root, host="deploy@example.com", plan=True, apply=False, json=True)

            with patch("ophelia.commands.deploy.stage_remote_bundle", return_value='{"remote": true}') as stage:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = deploy.run(args)

            self.assertEqual(0, result)
            self.assertEqual('{"remote": true}\n', stdout.getvalue())
            kwargs = stage.call_args.kwargs
            self.assertFalse(kwargs["apply"])
            self.assertTrue(kwargs["plan"])
            self.assertTrue(kwargs["json_output"])

    def test_host_production_apply_without_confirm_prints_remote_plan(self) -> None:
        from ophelia.commands import deploy

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "remote-app.ophelia.yml"
            manifest_path.write_text(_manifest_text(environment="production"))
            args = _deploy_args(manifest_path, root, host="deploy@example.com", apply=True, confirm=None)

            with patch("ophelia.commands.deploy.stage_remote_bundle", return_value="REMOTE PLAN") as stage:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = deploy.run(args)

            self.assertEqual(1, result)
            self.assertIn("REMOTE PLAN", stdout.getvalue())
            self.assertIn("remote plan above", stdout.getvalue())
            kwargs = stage.call_args.kwargs
            self.assertFalse(kwargs["apply"])
            self.assertTrue(kwargs["plan"])

    def test_host_production_apply_forwards_remote_confirmation_token(self) -> None:
        from ophelia.commands import deploy

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "remote-app.ophelia.yml"
            manifest_path.write_text(_manifest_text(environment="production"))
            args = _deploy_args(manifest_path, root, host="deploy@example.com", apply=True, confirm="remote-token")

            with patch("ophelia.commands.deploy.stage_remote_bundle", return_value="applied") as stage:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = deploy.run(args)

            self.assertEqual(0, result)
            self.assertIn("Applied bundle for remote-app on deploy@example.com", stdout.getvalue())
            kwargs = stage.call_args.kwargs
            self.assertTrue(kwargs["apply"])
            self.assertFalse(kwargs.get("plan", False))
            self.assertEqual("remote-token", kwargs["confirm"])


def _excluded_paths(command: list[str]) -> set[str]:
    return {command[index + 1] for index, item in enumerate(command[:-1]) if item == "--exclude"}


def _manifest(environment: str) -> Manifest:
    return Manifest(
        version=1,
        app="remote-app",
        kind="static",
        environment=environment,
        profile=None,
        image=None,
        services={},
        routes=[],
    )


def _manifest_text(environment: str) -> str:
    return f"""
version: 1
app: remote-app
environment: {environment}
kind: static
static_root: /tmp/remote-app-static
routes:
  - domain: remote-app.example.com
""".strip() + "\n"


def _deploy_args(
    manifest_path: Path,
    root: Path,
    *,
    host: str | None = None,
    plan: bool = False,
    apply: bool = False,
    json: bool = False,
    confirm: str | None = None,
) -> Namespace:
    return Namespace(
        manifest=manifest_path,
        runtime_root=root / "local-runtime",
        host=host,
        ssh_port=None,
        remote_runtime_root="~/ophelia-runtime",
        remote_ophelia_root="~/ophelia",
        apply=apply,
        plan=plan,
        json=json,
        artifacts_dir=None,
        confirm=confirm,
        verify=False,
        verify_attempts=None,
        verify_interval=None,
        verify_timeout=None,
        verify_failure_mode=None,
        ophelia_root=root,
        release_id=None,
        commit_sha=None,
        build_time=None,
    )


if __name__ == "__main__":
    unittest.main()
