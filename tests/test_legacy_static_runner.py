from __future__ import annotations

import json
import multiprocessing
import os
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.domain import ReceiptOutcome
from ophelia.execution.legacy_static_runner import (
    execute_confirmed_static,
    static_execution_mode,
    supports_journaled_static,
)
from ophelia.execution.operation_store import SQLiteOperationJournal
from ophelia.execution.staging import find_confirmed_staging
from ophelia.execution.static_backend import StaticBackendError
from ophelia.manifest import load_manifest
from ophelia.planning import deploy_plan
from ophelia.runtime import (
    DeployMetadata,
    current_release_id,
    list_deployments,
    list_releases,
)


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
                    "container_verified": True,
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
            self.assertEqual(
                metadata.release_id,
                current_release_id(runtime_root, manifest.app),
            )
            deployments = list_deployments(runtime_root)
            self.assertEqual(1, len(deployments))
            self.assertEqual(metadata.release_id, deployments[0].active_release_id)
            releases = list_releases(runtime_root, manifest.app)
            self.assertEqual(1, len(releases))
            self.assertTrue(releases[0]["active"])
            self.assertEqual(
                "operations.db", releases[0]["kernel"]["authority"]
            )
            self.assertTrue(
                (runtime_root / "apps" / manifest.app / "manifest.lock.json").is_file()
            )
            projection_bytes = (
                runtime_root / "apps" / manifest.app / "release.json"
            ).read_bytes()
            self.assertNotIn(raw_secret.encode(), projection_bytes)
            self.assertNotIn(token.encode(), projection_bytes)
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
                    "container_verified": True,
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

    def test_process_crash_after_pointer_switch_recovers_through_same_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root, confirmed, manifest, token, metadata = _confirmed(root)

            def reload_success(**_kwargs):
                return {
                    "kind": "ophelia.edge.reload",
                    "ok": True,
                    "container": "shared-caddy-1",
                    "container_verified": True,
                    "validated": True,
                    "reloaded": True,
                    "returncode": 0,
                    "errors": [],
                }

            context = multiprocessing.get_context("spawn")
            child = context.Process(
                target=_crash_after_static_switch,
                args=(str(root), str(runtime_root), token),
            )
            child.start()
            child.join(timeout=20)
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
                self.fail("crash-injection child did not terminate")
            self.assertEqual(91, child.exitcode)
            self.assertTrue(supports_journaled_static(manifest, runtime_root))

            recovered = execute_confirmed_static(
                confirmed=confirmed,
                manifest=manifest,
                confirmation_token=token,
                runtime_root=runtime_root,
                ophelia_root=root,
                deploy_metadata=metadata,
                caddy_reloader=reload_success,
                owner_id="runner-crash-recovery",
            )

            self.assertEqual(ReceiptOutcome.SUCCEEDED, recovered.receipt.outcome)
            journal = SQLiteOperationJournal.beneath_runtime_root(runtime_root)
            active = journal.active_revision(
                recovered.receipt.host_id,
                manifest.app,
                manifest.environment or "unknown",
            )
            self.assertIsNotNone(active)
            self.assertEqual(
                recovered.receipt.desired_revision_digest,
                active.revision_digest,
            )
            journal.integrity_check()

    def test_missing_live_revision_record_with_nonterminal_journal_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root, confirmed, manifest, token, metadata = _confirmed(root)
            context = multiprocessing.get_context("spawn")
            child = context.Process(
                target=_crash_after_static_switch,
                args=(str(root), str(runtime_root), token),
            )
            child.start()
            child.join(timeout=20)
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
                self.fail("crash-injection child did not terminate")
            self.assertEqual(91, child.exitcode)

            record = (
                runtime_root
                / "apps"
                / manifest.app
                / "static-revisions"
                / f"{metadata.release_id}.json"
            )
            self.assertTrue(record.is_file())
            record.unlink()

            self.assertEqual(
                "blocked", static_execution_mode(manifest, runtime_root)
            )
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
                    owner_id="runner-missing-live-record",
                )

            current = runtime_root / "static" / manifest.app / "current"
            self.assertEqual(
                Path("releases") / metadata.release_id,
                current.readlink(),
            )

    def test_recovery_binds_live_operation_and_revision_identity(self) -> None:
        tampered_values = {
            "operation_id": "operation_" + ("f" * 32),
            "revision_digest": "sha256:" + ("f" * 64),
        }
        for field, tampered_value in tampered_values.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                runtime_root, confirmed, manifest, token, metadata = _confirmed(root)
                context = multiprocessing.get_context("spawn")
                child = context.Process(
                    target=_crash_after_static_switch,
                    args=(str(root), str(runtime_root), token),
                )
                child.start()
                child.join(timeout=20)
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
                    self.fail("crash-injection child did not terminate")
                self.assertEqual(91, child.exitcode)

                record = (
                    runtime_root
                    / "apps"
                    / manifest.app
                    / "static-revisions"
                    / f"{metadata.release_id}.json"
                )
                payload = json.loads(record.read_text(encoding="utf-8"))
                payload[field] = tampered_value
                record.unlink()
                record.write_text(
                    json.dumps(payload, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                current = runtime_root / "static" / manifest.app / "current"
                expected_target = Path("releases") / metadata.release_id
                self.assertEqual(expected_target, current.readlink())
                with self.assertRaisesRegex(
                    StaticBackendError, "does not reconcile"
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
                        owner_id="runner-crash-recovery",
                    )

                self.assertEqual(expected_target, current.readlink())
                self.assertTrue(
                    (
                        runtime_root
                        / "static"
                        / manifest.app
                        / "releases"
                        / metadata.release_id
                    ).is_dir()
                )

    def test_incomplete_caddy_success_report_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root, confirmed, manifest, token, metadata = _confirmed(root)

            def reload_without_validation(**_kwargs):
                return {
                    "kind": "ophelia.edge.reload",
                    "ok": True,
                    "container": "shared-caddy-1",
                    "container_verified": True,
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

    def test_managed_live_state_without_authoritative_journal_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root, confirmed, manifest, token, metadata = _confirmed(root)
            release_id = "orphaned-managed-release"
            static_release = (
                runtime_root
                / "static"
                / manifest.app
                / "releases"
                / release_id
            )
            static_release.mkdir(parents=True)
            (static_release / "index.html").write_text("orphaned\n")
            current = static_release.parents[1] / "current"
            current.symlink_to(
                Path("releases") / release_id,
                target_is_directory=True,
            )
            caddy = (
                runtime_root
                / "caddy"
                / "sites.d"
                / f"{manifest.app}.caddy"
            )
            caddy.parent.mkdir(parents=True)
            caddy.write_text("managed caddy\n")
            record = (
                runtime_root
                / "apps"
                / manifest.app
                / "static-revisions"
                / f"{release_id}.json"
            )
            record.parent.mkdir(parents=True)
            record.write_text(
                '{"operation_id":"operation_orphaned-managed",'
                '"revision_digest":"sha256:' + ("a" * 64) + '"}\n'
            )

            self.assertTrue(supports_journaled_static(manifest, runtime_root))
            with self.assertRaisesRegex(
                StaticBackendError, "no authoritative operation journal"
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
                    owner_id="runner-orphaned-managed",
                )

            self.assertFalse(
                (runtime_root / "host-state" / "operations.db").exists()
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
                    "container_verified": True,
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
                blocking_external_verifier=verify_failure,
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
                    "container_verified": True,
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
                blocking_external_verifier=verify_success,
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


def _crash_after_static_switch(
    root_value: str, runtime_value: str, token: str
) -> None:
    root = Path(root_value)
    runtime_root = Path(runtime_value)
    confirmed = find_confirmed_staging(runtime_root, "runner-static", token)
    manifest = load_manifest(confirmed.manifest_path)
    metadata = DeployMetadata(
        **confirmed.binding["deploy_metadata"], locked=True
    )

    def reload_success(**_kwargs):
        return {
            "kind": "ophelia.edge.reload",
            "ok": True,
            "container": "shared-caddy-1",
            "container_verified": True,
            "validated": True,
            "reloaded": True,
            "returncode": 0,
            "errors": [],
        }

    def crash(boundary: str) -> None:
        if boundary == "after_static_switch":
            os._exit(91)

    execute_confirmed_static(
        confirmed=confirmed,
        manifest=manifest,
        confirmation_token=token,
        runtime_root=runtime_root,
        ophelia_root=root,
        deploy_metadata=metadata,
        caddy_reloader=reload_success,
        fault_injector=crash,
        owner_id="runner-crash-recovery",
    )


if __name__ == "__main__":
    unittest.main()
