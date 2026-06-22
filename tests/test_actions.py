from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.actions import ActionError, action_catalog, run_job, validate_action_inputs
from ophelia.api import serve


class ActionTests(unittest.TestCase):
    def test_action_catalog_contains_expected_actions(self) -> None:
        ids = {item["id"] for item in action_catalog()}
        self.assertIn("deploy.apply", ids)
        self.assertIn("runtime.status", ids)
        self.assertIn("restore.apply", ids)
        self.assertIn("release.show", ids)
        self.assertIn("pack.validate", ids)
        self.assertIn("env.diff", ids)
        self.assertIn("backup.status", ids)
        self.assertIn("app.readiness", ids)
        self.assertIn("app.export.plan", ids)
        self.assertIn("app.export.create", ids)
        self.assertIn("app.import.apply", ids)
        self.assertIn("app.restore-drill.apply", ids)
        self.assertIn("app.cutover.apply", ids)
        self.assertIn("app.traffic.plan", ids)
        self.assertIn("app.traffic.apply", ids)
        self.assertIn("app.traffic.rollback.plan", ids)
        self.assertIn("app.traffic.rollback.apply", ids)
        self.assertIn("receipts.list", ids)

    def test_action_catalog_rejects_invalid_config_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "actions.json"
            config_path.write_text("{")
            previous = os.environ.get("OPHELIA_ACTION_CONFIG")
            os.environ["OPHELIA_ACTION_CONFIG"] = str(config_path)
            try:
                with self.assertRaisesRegex(ValueError, "Action config JSON is invalid"):
                    action_catalog()
            finally:
                if previous is None:
                    os.environ.pop("OPHELIA_ACTION_CONFIG", None)
                else:
                    os.environ["OPHELIA_ACTION_CONFIG"] = previous

    def test_invalid_action_inputs_are_rejected(self) -> None:
        with self.assertRaises(ActionError):
            validate_action_inputs("unknown.action", {})
        with self.assertRaises(ActionError):
            validate_action_inputs("manifest.validate", {})
        with self.assertRaises(ActionError):
            validate_action_inputs("manifest.validate", {"manifest_path": "x", "extra": "nope"})
        with self.assertRaises(ActionError):
            run_job("", {}, Path(tempfile.gettempdir()) / "ophelia-empty-action-test")
        with self.assertRaises(ActionError):
            run_job("manifest.validate", [], Path(tempfile.gettempdir()) / "ophelia-bad-input-test")  # type: ignore[arg-type]

    def test_mutating_dry_run_returns_waiting_job_with_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "app.ophelia.yml"
            runtime_root = root / "runtime"
            manifest_path.write_text(_production_static_manifest(root / "static"))
            (root / "static").mkdir()

            result = run_job(
                "deploy.apply",
                {"manifest_path": str(manifest_path), "runtime_root": str(runtime_root), "dry_run": True},
                runtime_root,
                requested_by="test",
                source="unit",
            )

            self.assertEqual("waiting_for_confirmation", result.job["state"])
            payload = result.job["result"]["payload"]
            self.assertTrue(payload["required_confirmation_token"])
            self.assertTrue(payload["confirmation_expires_at"])
            self.assertFalse(payload["exact_apply_input"]["dry_run"])
            self.assertTrue((runtime_root / "jobs" / f"{result.job['job_id']}.events.ndjson").exists())
            token_path = runtime_root / "confirmations" / f"{payload['required_confirmation_token']}.json"
            self.assertTrue(token_path.exists())

    def test_apply_without_or_wrong_confirm_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "app.ophelia.yml"
            runtime_root = root / "runtime"
            manifest_path.write_text(_production_static_manifest(root / "static"))
            (root / "static").mkdir()

            result = run_job(
                "deploy.apply",
                {
                    "manifest_path": str(manifest_path),
                    "runtime_root": str(runtime_root),
                    "dry_run": False,
                    "confirm_token": "wrong",
                },
                runtime_root,
            )

            self.assertEqual("failed", result.job["state"])
            self.assertIn("confirmation token", result.job["error"])

    def test_mutating_apply_requires_prior_dry_run_token_and_consumes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            (runtime_root / "apps" / "backup-app").mkdir(parents=True)
            (runtime_root / "apps" / "backup-app" / "env").write_text("SECRET=replace-me\n")

            dry_run = run_job(
                "backup.create",
                {"app": "backup-app", "runtime_root": str(runtime_root), "dry_run": True},
                runtime_root,
            )
            token = dry_run.job["result"]["payload"]["required_confirmation_token"]
            apply = run_job(
                "backup.create",
                {
                    "app": "backup-app",
                    "runtime_root": str(runtime_root),
                    "dry_run": False,
                    "confirm_token": token,
                },
                runtime_root,
            )
            self.assertEqual("succeeded", apply.job["state"])
            token_record = json.loads((runtime_root / "confirmations" / f"{token}.json").read_text())
            self.assertEqual("consumed", token_record["state"])

    def test_idempotency_and_locking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_production_static_manifest(root / "static"))
            (root / "static").mkdir()

            first = run_job(
                "deploy.apply",
                {"manifest_path": str(manifest_path), "runtime_root": str(runtime_root), "dry_run": True},
                runtime_root,
                idempotency_key="same-key",
            )
            second = run_job(
                "deploy.apply",
                {"manifest_path": str(manifest_path), "runtime_root": str(runtime_root), "dry_run": True},
                runtime_root,
                idempotency_key="same-key",
            )
            self.assertTrue(second.existing)
            self.assertEqual(first.job["job_id"], second.job["job_id"])

            lock = runtime_root / "locks" / "safe-static-production.lock"
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text("held")
            locked = run_job(
                "deploy.apply",
                {"manifest_path": str(manifest_path), "runtime_root": str(runtime_root), "dry_run": True},
                runtime_root,
            )
            self.assertEqual("failed", locked.job["state"])
            self.assertIn("locked", locked.job["error"])

    def test_corrupt_idempotency_index_fails_before_job_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            jobs_root = runtime_root / "jobs"
            jobs_root.mkdir(parents=True)
            (jobs_root / "idempotency.json").write_text("{")

            with self.assertRaisesRegex(ActionError, "Idempotency index is unreadable"):
                run_job(
                    "manifest.validate",
                    {"manifest_path": str(Path(temp_dir) / "missing.ophelia.yml")},
                    runtime_root,
                    idempotency_key="same-key",
                )

            self.assertEqual(["idempotency.json"], [path.name for path in jobs_root.iterdir()])

    def test_same_input_jobs_get_distinct_ids_without_idempotency_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_production_static_manifest(root / "static"))
            (root / "static").mkdir()
            inputs = {"manifest_path": str(manifest_path), "runtime_root": str(runtime_root), "dry_run": True}

            first = run_job("deploy.apply", inputs, runtime_root)
            second = run_job("deploy.apply", inputs, runtime_root)

            self.assertNotEqual(first.job["job_id"], second.job["job_id"])

    def test_api_rejects_non_local_bind_host(self) -> None:
        with self.assertRaises(ValueError):
            serve("0.0.0.0", 8765)

    def test_expired_confirmation_token_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            config_path = root / "actions.json"
            config_path.write_text(
                json.dumps(
                    {
                        "enabled_actions": ["*"],
                        "disabled_actions": [],
                        "confirmation_token_ttl_seconds": -1,
                        "callbacks_enabled": False,
                        "production_apply_enabled": False,
                    }
                )
            )
            (runtime_root / "apps" / "backup-app").mkdir(parents=True)
            previous = os.environ.get("OPHELIA_ACTION_CONFIG")
            os.environ["OPHELIA_ACTION_CONFIG"] = str(config_path)
            try:
                dry_run = run_job(
                    "backup.create",
                    {"app": "backup-app", "runtime_root": str(runtime_root), "dry_run": True},
                    runtime_root,
                )
                token = dry_run.job["result"]["payload"]["required_confirmation_token"]
                apply = run_job(
                    "backup.create",
                    {
                        "app": "backup-app",
                        "runtime_root": str(runtime_root),
                        "dry_run": False,
                        "confirm_token": token,
                    },
                    runtime_root,
                )
                self.assertEqual("failed", apply.job["state"])
                self.assertIn("expired", apply.job["error"])
            finally:
                if previous is None:
                    os.environ.pop("OPHELIA_ACTION_CONFIG", None)
                else:
                    os.environ["OPHELIA_ACTION_CONFIG"] = previous

    def test_completion_callback_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "app.ophelia.yml"
            runtime_root = root / "runtime"
            manifest_path.write_text(_production_static_manifest(root / "static"))
            (root / "static").mkdir()

            result = run_job(
                "manifest.validate",
                {
                    "manifest_path": str(manifest_path),
                    "runtime_root": str(runtime_root),
                    "completion_callback_url": "http://127.0.0.1:9/callback",
                },
                runtime_root,
            )

            self.assertEqual("succeeded", result.job["state"])
            self.assertIn("callbacks are disabled", result.job["warnings"][0])

    def test_completion_callback_rejects_secret_shaped_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "app.ophelia.yml"
            runtime_root = root / "runtime"
            config_path = root / "actions.json"
            manifest_path.write_text(_production_static_manifest(root / "static"))
            (root / "static").mkdir()
            config_path.write_text(json.dumps({"callbacks_enabled": True}))
            previous = os.environ.get("OPHELIA_ACTION_CONFIG")
            os.environ["OPHELIA_ACTION_CONFIG"] = str(config_path)
            try:
                result = run_job(
                    "manifest.validate",
                    {
                        "manifest_path": str(manifest_path),
                        "runtime_root": str(runtime_root),
                        "completion_callback_url": "https://user:secret@example.com/cb?token=abc#frag",
                    },
                    runtime_root,
                )
            finally:
                if previous is None:
                    os.environ.pop("OPHELIA_ACTION_CONFIG", None)
                else:
                    os.environ["OPHELIA_ACTION_CONFIG"] = previous

        blob = json.dumps(result.job)
        self.assertEqual("failed", result.job["state"])
        self.assertIn("must not contain credentials", blob)
        self.assertNotIn("secret", blob)
        self.assertNotIn("token=abc", blob)

    def test_completion_callback_requires_allowlisted_host(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "app.ophelia.yml"
            runtime_root = root / "runtime"
            config_path = root / "actions.json"
            manifest_path.write_text(_production_static_manifest(root / "static"))
            (root / "static").mkdir()
            config_path.write_text(json.dumps({"callbacks_enabled": True}))
            previous = os.environ.get("OPHELIA_ACTION_CONFIG")
            os.environ["OPHELIA_ACTION_CONFIG"] = str(config_path)
            try:
                with mock.patch("ophelia.actions.request.urlopen") as urlopen:
                    result = run_job(
                        "manifest.validate",
                        {
                            "manifest_path": str(manifest_path),
                            "runtime_root": str(runtime_root),
                            "completion_callback_url": "http://127.0.0.1:9/callback",
                        },
                        runtime_root,
                    )
            finally:
                if previous is None:
                    os.environ.pop("OPHELIA_ACTION_CONFIG", None)
                else:
                    os.environ["OPHELIA_ACTION_CONFIG"] = previous

        self.assertEqual("succeeded", result.job["state"])
        self.assertIn("not allowlisted", result.job["warnings"][0])
        self.assertFalse(urlopen.called)

    def test_completion_callback_allowed_host_attempts_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "app.ophelia.yml"
            runtime_root = root / "runtime"
            config_path = root / "actions.json"
            manifest_path.write_text(_production_static_manifest(root / "static"))
            (root / "static").mkdir()
            config_path.write_text(json.dumps({"callbacks_enabled": True, "callback_allowed_hosts": ["127.0.0.1:9"]}))
            previous = os.environ.get("OPHELIA_ACTION_CONFIG")
            os.environ["OPHELIA_ACTION_CONFIG"] = str(config_path)
            try:
                with mock.patch("ophelia.actions.request.urlopen", side_effect=OSError("boom")) as urlopen:
                    result = run_job(
                        "manifest.validate",
                        {
                            "manifest_path": str(manifest_path),
                            "runtime_root": str(runtime_root),
                            "completion_callback_url": "http://127.0.0.1:9/callback",
                        },
                        runtime_root,
                    )
            finally:
                if previous is None:
                    os.environ.pop("OPHELIA_ACTION_CONFIG", None)
                else:
                    os.environ["OPHELIA_ACTION_CONFIG"] = previous

        self.assertEqual("succeeded", result.job["state"])
        self.assertIn("Completion callback failed for http://127.0.0.1:9/callback", json.dumps(result.job))
        self.assertTrue(urlopen.called)

    def test_pack_init_preview_action_does_not_write_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"

            result = run_job(
                "pack.init.preview",
                {
                    "app": "demo-service",
                    "environment": "production",
                    "critical": True,
                    "postgres": True,
                    "uploads": True,
                    "directory": str(root / "app"),
                    "runtime_root": str(runtime_root),
                },
                runtime_root,
            )

            self.assertEqual("succeeded", result.job["state"])
            payload = result.job["result"]["payload"]
            self.assertTrue(payload["dry_run"])
            self.assertFalse((root / "app" / "ophelia" / "runbook.md").exists())

    def test_export_create_action_is_dry_run_first(self) -> None:
        from ophelia.manifest import load_manifest
        from ophelia.runtime import deploy_bundle

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "app.ophelia.yml"
            runtime_root = root / "runtime"
            manifest_path.write_text(_production_static_manifest(root / "static"))
            (root / "static").mkdir()
            deploy_bundle(load_manifest(manifest_path), manifest_path, runtime_root)
            previous = os.environ.get("OPHELIA_SKIP_DOCKER_STATUS")
            os.environ["OPHELIA_SKIP_DOCKER_STATUS"] = "1"
            try:
                dry_run = run_job(
                    "app.export.create",
                    {
                        "app": "safe-static",
                        "environment": "production",
                        "manifest_path": str(manifest_path),
                        "runtime_root": str(runtime_root),
                        "dry_run": True,
                    },
                    runtime_root,
                )
                token = dry_run.job["result"]["payload"]["required_confirmation_token"]
                applied = run_job(
                    "app.export.create",
                    {
                        "app": "safe-static",
                        "environment": "production",
                        "manifest_path": str(manifest_path),
                        "runtime_root": str(runtime_root),
                        "dry_run": False,
                        "confirm_token": token,
                    },
                    runtime_root,
                )
                receipt = applied.job["result"]["payload"]
                bundle_exists = Path(receipt["bundle_path"]).exists()
            finally:
                if previous is None:
                    os.environ.pop("OPHELIA_SKIP_DOCKER_STATUS", None)
                else:
                    os.environ["OPHELIA_SKIP_DOCKER_STATUS"] = previous

        self.assertEqual("waiting_for_confirmation", dry_run.job["state"])
        self.assertEqual("succeeded", applied.job["state"])
        self.assertEqual("succeeded", receipt["status"])
        self.assertTrue(bundle_exists)


def _production_static_manifest(static_root: Path) -> str:
    return f"""
version: 1
app: safe-static
environment: production
kind: static
static_root: {static_root}
routes:
  - domain: safe-static.example.com
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
