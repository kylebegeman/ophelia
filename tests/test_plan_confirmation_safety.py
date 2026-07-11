from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

from ophelia.commands import deploy as deploy_command
from ophelia.execution.staging import (
    OperationStaging,
    StagingError,
    find_confirmed_staging,
)
from ophelia.manifest import load_manifest
from ophelia.planning import deploy_plan
from ophelia.remote import RemoteError, stage_remote_bundle
from ophelia.runtime import DeployMetadata


METADATA = DeployMetadata(
    release_id="release-reviewed",
    commit_sha="abc123",
    build_time="2026-07-11T09:00:00+00:00",
)


class PlanConfirmationSafetyTests(unittest.TestCase):
    def _manifest(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        path = root / "app.ophelia.yml"
        path.write_text(_manifest_text())
        return path, load_manifest(path)

    def _plan(self, root: Path):
        manifest_path, manifest = self._manifest(root)
        runtime_root = root / "runtime"
        plan = deploy_plan(
            manifest,
            manifest_path,
            runtime_root,
            deploy_metadata=METADATA,
        )
        return manifest_path, manifest, runtime_root, plan

    def test_evidence_and_candidate_mutation_invalidate_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, manifest, runtime_root, evidence_plan = self._plan(root)
            evidence_path = next(
                Path(item["path"])
                for item in evidence_plan["artifacts"]
                if item["name"] == "plan-evidence"
            )
            evidence_path.write_text(evidence_path.read_text() + "tampered\n")
            with self.assertRaisesRegex(StagingError, "evidence digest"):
                find_confirmed_staging(
                    runtime_root,
                    manifest.app,
                    evidence_plan["confirmation_token"],
                )

            second_root = root / "second"
            _, second_manifest, second_runtime, candidate_plan = self._plan(second_root)
            candidate = Path(candidate_plan["staging"]["candidate"])
            compose = candidate / "compose.yml"
            compose.write_text(compose.read_text() + "# tampered\n")
            with self.assertRaisesRegex(StagingError, "candidate digest"):
                find_confirmed_staging(
                    second_runtime,
                    second_manifest.app,
                    candidate_plan["confirmation_token"],
                )

    def test_plan_binds_metadata_into_candidate_and_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path, manifest = self._manifest(root)
            runtime_root = root / "runtime"
            first = deploy_plan(
                manifest,
                manifest_path,
                runtime_root,
                deploy_metadata=METADATA,
            )
            second = deploy_plan(
                manifest,
                manifest_path,
                runtime_root,
                deploy_metadata=DeployMetadata(
                    release_id="release-other",
                    commit_sha=METADATA.commit_sha,
                    build_time=METADATA.build_time,
                ),
            )

            self.assertNotEqual(first["candidate_digest"], second["candidate_digest"])
            self.assertNotEqual(first["confirmation_token"], second["confirmation_token"])
            candidate = Path(first["staging"]["candidate"])
            self.assertIn("release-reviewed", (candidate / "compose.yml").read_text())
            self.assertIn("release-reviewed", (candidate / "deploy-metadata.json").read_text())

            with self.assertRaisesRegex(StagingError, "contains controls"):
                deploy_plan(
                    manifest,
                    manifest_path,
                    root / "rejected-runtime",
                    deploy_metadata=DeployMetadata(
                        release_id="release\nINJECTED=value",
                        commit_sha="abc123",
                        build_time=METADATA.build_time,
                    ),
                )
            self.assertFalse((root / "rejected-runtime").exists())

    def test_local_command_requires_exact_binding_before_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path, manifest, runtime_root, plan = self._plan(root)

            def forbidden(*_args, **_kwargs):
                raise AssertionError("runtime/provider mutation occurred before confirmation")

            for token in (None, "wrong-token"):
                with self.subTest(token=token), patch(
                    "ophelia.runtime.ensure_addons", side_effect=forbidden
                ), patch(
                    "ophelia.caddy_manager.reload_caddy", side_effect=forbidden
                ), patch(
                    "ophelia.portability._apply_traffic_provider_mutations",
                    side_effect=forbidden,
                ), patch(
                    "ophelia.runtime.activate_release", side_effect=forbidden
                ), patch(
                    "ophelia.runtime._write_active_release", side_effect=forbidden
                ), patch(
                    "ophelia.commands.deploy.apply_local_bundle",
                    side_effect=forbidden,
                ) as apply:
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(
                            1,
                            deploy_command.run(
                                _deploy_args(manifest_path, runtime_root, confirm=token)
                            ),
                        )
                    apply.assert_not_called()

            with patch(
                "ophelia.commands.deploy.apply_local_bundle",
                return_value=runtime_root / "apps" / manifest.app,
            ) as apply, patch(
                "ophelia.commands.deploy.current_release_id",
                return_value="release-reviewed",
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = deploy_command.run(
                        _deploy_args(
                            manifest_path,
                            runtime_root,
                            confirm=plan["confirmation_token"],
                        )
                    )

            self.assertEqual(0, result)
            apply.assert_called_once()
            kwargs = apply.call_args.kwargs
            self.assertEqual(
                Path(plan["staging"]["candidate"]) / "manifest.lock.json",
                kwargs["manifest_path"],
            )
            self.assertTrue(kwargs["deploy_metadata"].locked)

    def test_valid_token_rejects_conflicting_cli_metadata_before_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path, _, runtime_root, plan = self._plan(root)
            args = _deploy_args(
                manifest_path,
                runtime_root,
                confirm=plan["confirmation_token"],
            )
            args.release_id = "different-release"
            with patch("ophelia.commands.deploy.apply_local_bundle") as apply:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = deploy_command.run(args)
            self.assertEqual(1, result)
            self.assertIn("does not match --release-id", stdout.getvalue())
            apply.assert_not_called()

    def test_runtime_policy_blocks_the_same_local_apply_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path, _ = self._manifest(root)
            runtime_root = root / "runtime"
            policy_dir = runtime_root / "policy"
            policy_dir.mkdir(parents=True)
            (policy_dir / "ophelia-policy.yml").write_text(
                """
version: 1
rules:
  - id: require-pinned-image
    operation: deploy.apply
    environment: production
    require:
      image_digest_pinned: true
    severity: blocker
""".strip()
                + "\n"
            )
            stdout = io.StringIO()
            with patch("ophelia.commands.deploy.apply_local_bundle") as apply:
                with contextlib.redirect_stdout(stdout):
                    result = deploy_command.run(
                        _deploy_args(manifest_path, runtime_root, confirm=None)
                    )

            self.assertEqual(1, result)
            self.assertIn("blocked by policy", stdout.getvalue())
            self.assertIn("policy_require_pinned_image", stdout.getvalue())
            apply.assert_not_called()

    def test_staging_rejects_symlinked_ancestors_and_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside"
            outside.mkdir()

            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "staging").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(StagingError):
                OperationStaging.create(runtime, "operation-one")
            self.assertFalse((outside / "operation-one").exists())

            runtime_two = root / "runtime-two"
            staging = runtime_two / "staging"
            staging.mkdir(parents=True)
            outside_operation = outside / "operation-two"
            (outside_operation / "candidate").mkdir(parents=True)
            (staging / "operation-two").symlink_to(
                outside_operation,
                target_is_directory=True,
            )
            with self.assertRaises(StagingError):
                OperationStaging.open_uploaded(runtime_two, "operation-two")

            runtime_three = root / "runtime-three"
            operation = runtime_three / "staging" / "operation-three"
            operation.mkdir(parents=True)
            outside_candidate = outside / "candidate"
            outside_candidate.mkdir()
            (operation / "candidate").symlink_to(
                outside_candidate,
                target_is_directory=True,
            )
            with self.assertRaises(StagingError):
                OperationStaging.open_uploaded(runtime_three, "operation-three")

    def test_remote_production_apply_performs_no_rsync_before_remote_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path, manifest = self._manifest(root)
            completed = MagicMock(stdout="applied")
            with patch("ophelia.remote._run", return_value=completed) as run, patch(
                "ophelia.remote._sync_bundle",
                side_effect=AssertionError("live rsync"),
            ) as live_sync, patch(
                "ophelia.remote._sync_plan_candidate",
                side_effect=AssertionError("candidate upload"),
            ) as candidate_sync:
                result = stage_remote_bundle(
                    manifest,
                    manifest_path,
                    "deploy@example.com",
                    None,
                    "~/ophelia-runtime",
                    "~/ophelia",
                    apply=True,
                    confirm="stale-or-invalid-token",
                    deploy_metadata=METADATA,
                )

            self.assertEqual("applied", result)
            live_sync.assert_not_called()
            candidate_sync.assert_not_called()
            self.assertEqual(1, run.call_count)
            command = run.call_args.args[0]
            self.assertEqual("ssh", command[0])
            self.assertIn("@confirmed:plan-safety", command[-1])
            self.assertNotIn("rsync", command)

    def test_remote_roots_reject_shell_metacharacters_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path, manifest = self._manifest(root)
            for unsafe in (
                "~/runtime;touch-pwned",
                "~/runtime with-space",
                "~/runtime$(touch-pwned)",
                "~/runtime`touch-pwned`",
                "relative/runtime",
            ):
                with self.subTest(unsafe=unsafe), patch("ophelia.remote._run") as run:
                    with self.assertRaises(RemoteError):
                        stage_remote_bundle(
                            manifest,
                            manifest_path,
                            "deploy@example.com",
                            None,
                            unsafe,
                            "~/ophelia",
                            apply=False,
                            plan=True,
                        )
                    run.assert_not_called()


def _deploy_args(
    manifest_path: Path,
    runtime_root: Path,
    *,
    confirm: str | None,
) -> Namespace:
    return Namespace(
        manifest=manifest_path,
        runtime_root=runtime_root,
        host=None,
        ssh_port=None,
        remote_runtime_root="~/ophelia-runtime",
        remote_ophelia_root="~/ophelia",
        apply=True,
        plan=False,
        json=False,
        artifacts_dir=None,
        confirm=confirm,
        verify=False,
        verify_attempts=None,
        verify_interval=None,
        verify_timeout=None,
        verify_failure_mode=None,
        ophelia_root=manifest_path.parent,
        release_id=METADATA.release_id,
        commit_sha=METADATA.commit_sha,
        build_time=METADATA.build_time,
    )


def _manifest_text() -> str:
    return """
version: 1
app: plan-safety
environment: production
kind: service
image: ghcr.io/example/plan-safety:latest
services:
  web:
    port: 3000
routes:
  - domain: plan-safety.example.com
    service: web
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
