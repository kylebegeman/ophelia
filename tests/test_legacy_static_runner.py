from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.domain import ReceiptOutcome
from ophelia.execution.legacy_static_runner import execute_confirmed_static
from ophelia.execution.operation_store import SQLiteOperationJournal
from ophelia.execution.staging import find_confirmed_staging
from ophelia.execution.static_backend import StaticBackendError
from ophelia.manifest import load_manifest
from ophelia.planning import deploy_plan
from ophelia.runtime import DeployMetadata


class LegacyStaticRunnerTests(unittest.TestCase):
    def test_caddy_success_is_digest_only_evidence_in_the_terminal_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root, confirmed, manifest, token, metadata = _confirmed(root)
            raw_secret = "postgres://operator:do-not-store@example.invalid/db"
            calls = []

            def reload_success(**_kwargs):
                calls.append("reload")
                return {
                    "kind": "ophelia.edge.reload",
                    "ok": True,
                    "container": "shared-caddy-1",
                    "validated": True,
                    "reloaded": True,
                    "returncode": 0,
                    "stdout": raw_secret,
                    "stderr": "",
                    "errors": [],
                }

            result = execute_confirmed_static(
                confirmed=confirmed,
                manifest=manifest,
                confirmation_token=token,
                runtime_root=runtime_root,
                ophelia_root=root,
                deploy_metadata=metadata,
                caddy_reloader=reload_success,
                owner_id="runner-success",
            )

            self.assertEqual(ReceiptOutcome.SUCCEEDED, result.receipt.outcome)
            self.assertEqual(["reload"], calls)
            journal = SQLiteOperationJournal.beneath_runtime_root(runtime_root)
            database_bytes = journal.database_path.read_bytes()
            wal_path = Path(str(journal.database_path) + "-wal")
            if wal_path.exists():
                database_bytes += wal_path.read_bytes()
            self.assertNotIn(raw_secret.encode(), database_bytes)
            self.assertNotIn(token.encode(), database_bytes)
            journal.integrity_check()

    def test_terminal_retry_returns_original_receipt_without_reloading(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root, confirmed, manifest, token, metadata = _confirmed(root)
            calls = []

            def reload_success(**_kwargs):
                calls.append("reload")
                return {
                    "kind": "ophelia.edge.reload",
                    "ok": True,
                    "container": "shared-caddy-1",
                    "validated": True,
                    "reloaded": True,
                    "returncode": 0,
                    "errors": [],
                }

            first = execute_confirmed_static(
                confirmed=confirmed,
                manifest=manifest,
                confirmation_token=token,
                runtime_root=runtime_root,
                ophelia_root=root,
                deploy_metadata=metadata,
                caddy_reloader=reload_success,
                owner_id="runner-first",
            )
            retried = execute_confirmed_static(
                confirmed=confirmed,
                manifest=manifest,
                confirmation_token=token,
                runtime_root=runtime_root,
                ophelia_root=root,
                deploy_metadata=metadata,
                caddy_reloader=reload_success,
                owner_id="runner-retry",
            )

            self.assertEqual(ReceiptOutcome.SUCCEEDED, retried.receipt.outcome)
            self.assertEqual(
                first.operation.operation_id,
                retried.operation.operation_id,
            )
            self.assertEqual(first.receipt, retried.receipt)
            self.assertEqual(["reload"], calls)

    def test_incomplete_caddy_success_report_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root, confirmed, manifest, token, metadata = _confirmed(root)

            def reload_without_validation(**_kwargs):
                return {
                    "kind": "ophelia.edge.reload",
                    "ok": True,
                    "container": "shared-caddy-1",
                    "validated": False,
                    "reloaded": True,
                    "returncode": 0,
                    "errors": [],
                }

            result = execute_confirmed_static(
                confirmed=confirmed,
                manifest=manifest,
                confirmation_token=token,
                runtime_root=runtime_root,
                ophelia_root=root,
                deploy_metadata=metadata,
                caddy_reloader=reload_without_validation,
                owner_id="runner-incomplete-caddy",
            )

            self.assertEqual(
                ReceiptOutcome.FAILED_UNCOMPENSATED, result.receipt.outcome
            )
            self.assertFalse(
                (runtime_root / "static" / manifest.app / "current").exists()
            )

    def test_untracked_legacy_live_state_is_rejected_before_journaling(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root, confirmed, manifest, token, metadata = _confirmed(root)
            legacy_release = (
                runtime_root
                / "static"
                / manifest.app
                / "releases"
                / "legacy-release"
            )
            legacy_release.mkdir(parents=True)
            (legacy_release / "index.html").write_text("legacy\n")
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
            caddy.write_text("legacy caddy\n")

            with self.assertRaisesRegex(
                StaticBackendError, "predecessor migration"
            ):
                execute_confirmed_static(
                    confirmed=confirmed,
                    manifest=manifest,
                    confirmation_token=token,
                    runtime_root=runtime_root,
                    ophelia_root=root,
                    deploy_metadata=metadata,
                    caddy_reloader=lambda **_kwargs: self.fail(
                        "Caddy must not be called"
                    ),
                    owner_id="runner-legacy-state",
                )

            self.assertFalse(
                (runtime_root / "host-state" / "operations.db").exists()
            )
            self.assertEqual(
                Path("releases") / "legacy-release", current.readlink()
            )

    def test_caddy_failure_restores_files_and_never_commits_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root, confirmed, manifest, token, metadata = _confirmed(root)
            raw_secret = "redis://:do-not-store@example.invalid/0"

            def reload_failure(**_kwargs):
                return {
                    "kind": "ophelia.edge.reload",
                    "ok": False,
                    "container": None,
                    "validated": False,
                    "reloaded": False,
                    "returncode": 1,
                    "stdout": "",
                    "stderr": raw_secret,
                    "errors": [
                        {
                            "code": "caddy_reload_failed",
                            "message": raw_secret,
                        }
                    ],
                }

            result = execute_confirmed_static(
                confirmed=confirmed,
                manifest=manifest,
                confirmation_token=token,
                runtime_root=runtime_root,
                ophelia_root=root,
                deploy_metadata=metadata,
                caddy_reloader=reload_failure,
                owner_id="runner-failure",
            )

            self.assertEqual(
                ReceiptOutcome.FAILED_UNCOMPENSATED, result.receipt.outcome
            )
            self.assertFalse(
                (runtime_root / "caddy" / "sites.d" / f"{manifest.app}.caddy").exists()
            )
            self.assertFalse(
                (runtime_root / "static" / manifest.app / "current").exists()
            )
            journal = SQLiteOperationJournal.beneath_runtime_root(runtime_root)
            database_bytes = journal.database_path.read_bytes()
            wal_path = Path(str(journal.database_path) + "-wal")
            if wal_path.exists():
                database_bytes += wal_path.read_bytes()
            self.assertNotIn(raw_secret.encode(), database_bytes)
            journal.integrity_check()

    def test_declared_external_failure_compensates_without_raw_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root, confirmed, manifest, token, metadata = _confirmed(root)
            raw_secret = "https://operator:do-not-store@example.invalid/private"

            def reload_success(**_kwargs):
                return {
                    "kind": "ophelia.edge.reload",
                    "ok": True,
                    "container": "shared-caddy-1",
                    "validated": True,
                    "reloaded": True,
                    "returncode": 0,
                    "errors": [],
                }

            def verify_failure():
                return {
                    "ok": False,
                    "status": "failed",
                    "phase": "external_route_verify",
                    "results": [
                        {
                            "name": raw_secret,
                            "type": "http",
                            "url": raw_secret,
                            "ok": False,
                            "status_code": 503,
                            "error": raw_secret,
                            "error_kind": "http_status",
                        }
                    ],
                }

            result = execute_confirmed_static(
                confirmed=confirmed,
                manifest=manifest,
                confirmation_token=token,
                runtime_root=runtime_root,
                ophelia_root=root,
                deploy_metadata=metadata,
                caddy_reloader=reload_success,
                external_verifier=verify_failure,
                owner_id="runner-external-failure",
            )

            self.assertEqual(
                ReceiptOutcome.FAILED_COMPENSATED, result.receipt.outcome
            )
            self.assertFalse(
                (runtime_root / "static" / manifest.app / "current").exists()
            )
            journal = SQLiteOperationJournal.beneath_runtime_root(runtime_root)
            database_bytes = journal.database_path.read_bytes()
            wal_path = Path(str(journal.database_path) + "-wal")
            if wal_path.exists():
                database_bytes += wal_path.read_bytes()
            self.assertNotIn(raw_secret.encode(), database_bytes)
            journal.integrity_check()

    def test_declared_external_success_is_digest_only_terminal_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root, confirmed, manifest, token, metadata = _confirmed(root)
            raw_secret = "https://operator:do-not-store@example.invalid/healthy"

            def reload_success(**_kwargs):
                return {
                    "kind": "ophelia.edge.reload",
                    "ok": True,
                    "container": "shared-caddy-1",
                    "validated": True,
                    "reloaded": True,
                    "returncode": 0,
                    "errors": [],
                }

            def verify_success():
                return {
                    "ok": True,
                    "status": "passed",
                    "phase": "external_route_verify",
                    "results": [
                        {
                            "name": raw_secret,
                            "type": "http",
                            "url": raw_secret,
                            "ok": True,
                            "status_code": 200,
                        }
                    ],
                }

            result = execute_confirmed_static(
                confirmed=confirmed,
                manifest=manifest,
                confirmation_token=token,
                runtime_root=runtime_root,
                ophelia_root=root,
                deploy_metadata=metadata,
                caddy_reloader=reload_success,
                external_verifier=verify_success,
                owner_id="runner-external-success",
            )

            self.assertEqual(ReceiptOutcome.SUCCEEDED, result.receipt.outcome)
            self.assertEqual("passed", result.receipt.verification.status.value)
            self.assertIn(
                "external_1",
                tuple(check.name for check in result.receipt.verification.checks),
            )
            journal = SQLiteOperationJournal.beneath_runtime_root(runtime_root)
            database_bytes = journal.database_path.read_bytes()
            wal_path = Path(str(journal.database_path) + "-wal")
            if wal_path.exists():
                database_bytes += wal_path.read_bytes()
            self.assertNotIn(raw_secret.encode(), database_bytes)
            journal.integrity_check()


def _confirmed(root: Path):
    runtime_root = root / "runtime"
    public = root / "public"
    public.mkdir()
    (public / "index.html").write_text("journaled runner\n")
    manifest_path = root / "app.ophelia.yml"
    manifest_path.write_text(
        """version: 1
app: runner-static
environment: production
kind: static
static_root: public
routes:
  - domain: runner-static.example.com
"""
    )
    requested = DeployMetadata(
        release_id="release-runner",
        commit_sha="abc123",
        build_time="2026-07-11T17:00:00Z",
    )
    plan = deploy_plan(
        load_manifest(manifest_path),
        manifest_path,
        runtime_root,
        deploy_metadata=requested,
    )
    token = str(plan["confirmation_token"])
    confirmed = find_confirmed_staging(runtime_root, "runner-static", token)
    metadata = DeployMetadata(
        **confirmed.binding["deploy_metadata"], locked=True
    )
    return (
        runtime_root,
        confirmed,
        load_manifest(confirmed.manifest_path),
        token,
        metadata,
    )


if __name__ == "__main__":
    unittest.main()
