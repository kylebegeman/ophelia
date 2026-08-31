from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.execution.staging import (
    StagingError,
    confirmation_token,
    consume_confirmed_staging,
    find_confirmed_staging,
)
from ophelia.manifest import ManifestError, load_manifest
from ophelia.planning import deploy_plan
from ophelia.runtime import ApplyPhaseError, DeployMetadata, apply_local_bundle


class ConfirmedApplyTests(unittest.TestCase):
    def test_apply_materializes_exact_reviewed_candidate_and_removes_stale_support(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            public = root / "public"
            env_dir = root / "env"
            public.mkdir()
            env_dir.mkdir()
            (public / "index.html").write_text("old live\n")
            (env_dir / "old.env").write_text("OLD=true\n")
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_static_manifest("staging", "env/old.env"))
            apply_local_bundle(
                load_manifest(manifest_path),
                manifest_path,
                runtime_root,
                root,
            )

            (public / "index.html").write_text("reviewed candidate\n")
            (env_dir / "new.env").write_text("NEW=reviewed\n")
            manifest_path.write_text(_static_manifest("production", "env/new.env"))
            metadata = DeployMetadata(
                release_id="reviewed-release",
                commit_sha="abc123",
                build_time="2026-07-11T09:00:00+00:00",
            )
            plan = deploy_plan(
                load_manifest(manifest_path),
                manifest_path,
                runtime_root,
                deploy_metadata=metadata,
            )
            confirmed = find_confirmed_staging(
                runtime_root,
                "exact-candidate",
                str(plan["confirmation_token"]),
            )
            reviewed_caddy = (confirmed.staging.candidate / "caddy" / "exact-candidate.caddy").read_bytes()

            (public / "index.html").write_text("changed after review\n")
            (env_dir / "new.env").write_text("NEW=changed-after-review\n")
            confirmed_manifest = load_manifest(confirmed.manifest_path)
            locked = DeployMetadata(**confirmed.binding["deploy_metadata"], locked=True)

            app_root = apply_local_bundle(
                confirmed_manifest,
                confirmed.manifest_path,
                runtime_root,
                root,
                locked,
                candidate_root=confirmed.staging.candidate,
                candidate_generated_files=list(confirmed.generated_files),
                expected_candidate_digest=confirmed.candidate_digest,
                expected_bundle_hash=confirmed.rendered_bundle_hash,
                expected_baseline_digest=confirmed.baseline_digest,
            )
            consume_confirmed_staging(confirmed)

            self.assertEqual(reviewed_caddy, (app_root / "caddy" / "exact-candidate.caddy").read_bytes())
            self.assertEqual("NEW=reviewed\n", (app_root / "env.d" / "01-new.env").read_text())
            self.assertFalse((app_root / "env.d" / "01-old.env").exists())
            current = runtime_root / "static" / "exact-candidate" / "current"
            self.assertEqual("reviewed candidate\n", (current / "index.html").read_text())
            with self.assertRaisesRegex(StagingError, "exactly one valid"):
                find_confirmed_staging(
                    runtime_root,
                    "exact-candidate",
                    str(plan["confirmation_token"]),
                )

    def test_invalid_candidate_env_fails_before_creating_live_app(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = _write_plan_manifest(root, app="preflight-candidate", required_env=True)
            plan = deploy_plan(load_manifest(manifest_path), manifest_path, runtime_root)
            confirmed = find_confirmed_staging(
                runtime_root,
                "preflight-candidate",
                str(plan["confirmation_token"]),
            )
            locked = DeployMetadata(**confirmed.binding["deploy_metadata"], locked=True)

            with self.assertRaisesRegex(ApplyPhaseError, "placeholder env values"):
                apply_local_bundle(
                    load_manifest(confirmed.manifest_path),
                    confirmed.manifest_path,
                    runtime_root,
                    root,
                    locked,
                    candidate_root=confirmed.staging.candidate,
                    candidate_generated_files=list(confirmed.generated_files),
                    expected_candidate_digest=confirmed.candidate_digest,
                    expected_bundle_hash=confirmed.rendered_bundle_hash,
                    expected_baseline_digest=confirmed.baseline_digest,
                )

            self.assertFalse((runtime_root / "apps" / "preflight-candidate").exists())

    def test_live_state_change_invalidates_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = _write_plan_manifest(root, app="stale-plan")
            plan = deploy_plan(load_manifest(manifest_path), manifest_path, runtime_root)
            app_root = runtime_root / "apps" / "stale-plan"
            app_root.mkdir(parents=True)
            (app_root / "release.json").write_text('{"release_id":"newer"}\n')

            with self.assertRaisesRegex(StagingError, "changed after"):
                find_confirmed_staging(
                    runtime_root,
                    "stale-plan",
                    str(plan["confirmation_token"]),
                )

    def test_operator_can_supply_runtime_env_after_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = _write_plan_manifest(root, app="planned-env", required_env=True)
            plan = deploy_plan(load_manifest(manifest_path), manifest_path, runtime_root)
            app_root = runtime_root / "apps" / "planned-env"
            app_root.mkdir(parents=True)
            (app_root / "env").write_text("OPHELIA_APP=planned-env\nAPI_TOKEN=ready\n")
            confirmed = find_confirmed_staging(
                runtime_root,
                "planned-env",
                str(plan["confirmation_token"]),
            )
            locked = DeployMetadata(**confirmed.binding["deploy_metadata"], locked=True)

            apply_local_bundle(
                load_manifest(confirmed.manifest_path),
                confirmed.manifest_path,
                runtime_root,
                root,
                locked,
                candidate_root=confirmed.staging.candidate,
                candidate_generated_files=list(confirmed.generated_files),
                expected_candidate_digest=confirmed.candidate_digest,
                expected_bundle_hash=confirmed.rendered_bundle_hash,
                expected_baseline_digest=confirmed.baseline_digest,
            )

            self.assertEqual(
                "OPHELIA_APP=planned-env\nAPI_TOKEN=ready\n",
                (app_root / "env").read_text(),
            )

    def test_expired_confirmation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = _write_plan_manifest(root, app="expired-plan")
            plan = deploy_plan(load_manifest(manifest_path), manifest_path, runtime_root)
            binding_path = (
                runtime_root / "staging" / str(plan["operation_id"]) / "plan-binding.json"
            )
            binding = json.loads(binding_path.read_text())
            payload = binding["confirmation_payload"]
            payload["confirmation_created_at"] = "2020-01-01T00:00:00+00:00"
            payload["confirmation_expires_at"] = "2020-01-01T00:01:00+00:00"
            expired_token = confirmation_token(payload)
            binding["confirmation_token"] = expired_token
            binding_path.write_text(json.dumps(binding, indent=2, sort_keys=True) + "\n")

            with self.assertRaisesRegex(StagingError, "expired"):
                find_confirmed_staging(runtime_root, "expired-plan", expired_token)

    def test_candidate_mode_change_invalidates_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = _write_plan_manifest(root, app="mode-plan")
            plan = deploy_plan(load_manifest(manifest_path), manifest_path, runtime_root)
            candidate_manifest = (
                runtime_root
                / "staging"
                / str(plan["operation_id"])
                / "candidate"
                / "manifest.lock.json"
            )
            candidate_manifest.chmod(0o644)

            with self.assertRaisesRegex(StagingError, "permissions|mode"):
                find_confirmed_staging(
                    runtime_root,
                    "mode-plan",
                    str(plan["confirmation_token"]),
                )

    def test_production_plan_with_missing_required_support_has_no_token_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            public = root / "public"
            public.mkdir()
            (public / "index.html").write_text("ready\n")
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_static_manifest("production", "env/missing.env"))

            with self.assertRaisesRegex((ManifestError, StagingError), "missing source|Unable to stage"):
                deploy_plan(load_manifest(manifest_path), manifest_path, runtime_root)

            bindings = list((runtime_root / "staging").glob("*/plan-binding.json"))
            self.assertEqual([], bindings)


def _write_plan_manifest(root: Path, *, app: str, required_env: bool = False) -> Path:
    public = root / "public"
    public.mkdir()
    (public / "index.html").write_text("reviewed\n")
    required = "\nrequired_env:\n  - API_TOKEN" if required_env else ""
    manifest_path = root / "app.ophelia.yml"
    manifest_path.write_text(
        f"""
version: 1
app: {app}
environment: production
kind: static
static_root: public{required}
routes:
  - domain: {app}.example.com
""".strip()
        + "\n"
    )
    return manifest_path


def _static_manifest(environment: str, env_file: str) -> str:
    return f"""
version: 1
app: exact-candidate
environment: {environment}
kind: static
static_root: public
env_files:
  - {env_file}
routes:
  - domain: exact-candidate.example.com
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
