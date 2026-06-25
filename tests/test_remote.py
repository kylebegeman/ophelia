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
from ophelia.remote import _build_remote_stage_script, _sync_bundle
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
        )

        self.assertIn('deploy "$APP_ROOT/manifest.lock.json"', script)
        self.assertIn("--runtime-root \"$REMOTE_RUNTIME_ROOT\"", script)
        self.assertIn("--ophelia-root \"$REMOTE_OPHELIA_ROOT\"", script)
        self.assertIn("--plan --json", script)
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
        ssh_port=22022,
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
