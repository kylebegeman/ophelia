from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.manifest import load_manifest
from ophelia.receipt_index import receipt_timeline
from ophelia.runtime import deploy_bundle
from ophelia.state_db import (
    STATE_SCHEMA_VERSION,
    query_receipts,
    rebuild_state,
    state_summary,
    state_db_path,
    state_status,
)


# Secret values that must never appear in the index. They are written into the
# runtime env file and the manifest lock (root env + service env) so the test
# proves the redaction path before anything is stored.
ROOT_SECRET = "super-secret-root-token-ABC123"
SERVICE_SECRET = "service-db-password-SECRET999"
NESTED_SECRET = "nested-credential-value-777"


def _manifest_yaml() -> str:
    return (
        """
version: 1
app: dragon-writer
environment: production
kind: service
image: ghcr.io/example/dragon-writer:latest
env:
  API_TOKEN: %s
services:
  web:
    port: 3000
    env:
      DATABASE_URL: postgres://user:%s@db/dragon
routes:
  - domain: dragonwriter.example.com
    service: web
verify:
  - name: health
    url: https://dragonwriter.example.com/health
"""
        % (ROOT_SECRET, SERVICE_SECRET)
    ).strip() + "\n"


def _build_runtime_root(base: Path) -> Path:
    runtime_root = base / "runtime"
    manifest_path = base / "dragon-writer.ophelia.yml"
    manifest_path.write_text(_manifest_yaml())
    manifest = load_manifest(manifest_path)
    app_root = deploy_bundle(manifest, manifest_path, runtime_root)

    # Operator env file with raw secrets (mirrors test_portability fixtures).
    (app_root / "env").write_text(
        f"API_TOKEN={ROOT_SECRET}\nDATABASE_URL=postgres://user:{SERVICE_SECRET}@db/dragon\n"
    )

    receipts_root = app_root / "receipts"
    receipts_root.mkdir(parents=True, exist_ok=True)
    (receipts_root / "export-create.json").write_text(
        json.dumps(
            {
                "operation": "app.export.create",
                "operation_id": "app.export.create.dragon-writer.production.fixture",
                "status": "succeeded",
                "app": "dragon-writer",
                "environment": "production",
                "started_at": "2026-06-20T10:00:00Z",
                "completed_at": "2026-06-20T10:01:00Z",
                "inputs_redacted": True,
                "credentials": {"username": NESTED_SECRET, "region": "us-east-1"},
                "artifacts": [{"name": "bundle", "kind": "ophelia.artifact", "path": "bundle.tar"}],
                "checks": [{"name": "export_ok", "ok": True, "message": "exported"}],
                "rollback": {"available": True},
            }
        )
        + "\n"
    )
    (receipts_root / "deploy-apply.json").write_text(
        json.dumps(
            {
                "operation": "deploy.apply",
                "operation_id": "deploy.apply.dragon-writer.production.fixture",
                "status": "succeeded",
                "app": "dragon-writer",
                "environment": "production",
                "started_at": "2026-06-21T10:00:00Z",
                "inputs_redacted": True,
            }
        )
        + "\n"
    )
    (receipts_root / "traffic-apply.json").write_text(
        json.dumps(
            {
                "operation": "app.traffic.apply",
                "operation_id": "app.traffic.apply.dragon-writer.production.fixture",
                "status": "succeeded",
                "app": "dragon-writer",
                "environment": "production",
                "started_at": "2026-06-21T11:00:00Z",
                "completed_at": "2026-06-21T11:01:00Z",
                "provider_config": str(runtime_root / "traffic-providers.json"),
                "provider_config_validation": {"status": "ok", "providers": [{"name": "file", "type": "file"}]},
                "provider_mutations": [
                    {
                        "provider": "dns.file",
                        "status": "succeeded",
                        "path": str(runtime_root / "traffic" / "dns-records.json"),
                        "file_written": True,
                    }
                ],
                "provider_mutation_performed": True,
                "inputs_redacted": True,
            }
        )
        + "\n"
    )
    (receipts_root / "github-apply.json").write_text(
        json.dumps(
            {
                "operation": "app.github.provision.apply",
                "operation_id": "app.github.provision.apply.dragon-writer.production.fixture",
                "status": "succeeded",
                "app": "dragon-writer",
                "environment": "production",
                "started_at": "2026-06-21T12:00:00Z",
                "completed_at": "2026-06-21T12:01:00Z",
                "repository": "example/dragon-writer",
                "inputs_redacted": True,
            }
        )
        + "\n"
    )

    observability_root = runtime_root / "observability"
    runs_root = observability_root / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    observability_run = {
        "operation": "observability.schedule.run",
        "operation_id": "observability.schedule.run.fixture",
        "status": "succeeded",
        "completed_at": "2026-06-21T13:00:00Z",
        "apps": [
            {
                "app": "dragon-writer",
                "environment": "production",
                "status": "ok",
                "snapshot": {"health_configured": True},
            }
        ],
        "secrets_redacted": True,
    }
    (runs_root / "observability.schedule.run.fixture.json").write_text(json.dumps(observability_run) + "\n")
    (observability_root / "latest.json").write_text(json.dumps(observability_run) + "\n")

    backup_root = runtime_root / "backups" / "apps" / "dragon-writer" / "20260620T120000Z-fixture"
    backup_root.mkdir(parents=True)
    (backup_root / "backup-manifest.json").write_text(
        json.dumps(
            {
                "backup_id": "20260620T120000Z-fixture",
                "app": "dragon-writer",
                "created_at": "2026-06-20T12:00:00Z",
                "coverage": {"release_metadata": True, "app_env": True},
                "database": {"postgres": True, "mode": "metadata-only"},
            }
        )
        + "\n"
    )

    return runtime_root


class StateDbTests(unittest.TestCase):
    def test_rebuild_populates_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = _build_runtime_root(Path(temp_dir))
            report = rebuild_state(runtime_root, Path(temp_dir) / "manifests")

        self.assertEqual("ok", report["status"])
        self.assertEqual(STATE_SCHEMA_VERSION, report["schema_version_db"])
        self.assertIsNotNone(report["refreshed_at"])
        self.assertEqual("current", report["freshness"]["status"])
        counts = report["counts"]
        self.assertGreater(counts["apps"], 0)
        self.assertGreater(counts["manifests"], 0)
        self.assertGreater(counts["receipts"], 0)
        self.assertGreater(counts["backups"], 0)
        self.assertGreater(counts["routes"], 0)
        self.assertGreater(counts["observability_runs"], 0)
        self.assertGreater(counts["observability_app_snapshots"], 0)
        self.assertGreater(counts["traffic_state"], 0)
        self.assertGreater(counts["provider_snapshots"], 0)
        self.assertGreater(counts["github_provisioning"], 0)

    def test_rebuild_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = _build_runtime_root(Path(temp_dir))
            manifests_dir = Path(temp_dir) / "manifests"
            first = rebuild_state(runtime_root, manifests_dir)
            # A second rebuild over the same files must not raise duplicate-key
            # errors and must produce identical counts.
            second = rebuild_state(runtime_root, manifests_dir)

        self.assertEqual("ok", first["status"])
        self.assertEqual("ok", second["status"])
        self.assertEqual(first["counts"], second["counts"])

    def test_corrupt_receipt_becomes_warning_and_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = _build_runtime_root(Path(temp_dir))
            corrupt = runtime_root / "apps" / "dragon-writer" / "receipts" / "corrupt.json"
            corrupt.write_text("{ not valid json ")

            report = rebuild_state(runtime_root, Path(temp_dir) / "manifests")

        # The rebuild does not crash, and the corrupt receipt surfaces as a
        # warning rather than an exception.
        self.assertEqual("ok", report["status"])
        warning_codes = {warning["code"] for warning in report["warnings"]}
        self.assertIn("receipt_unreadable", warning_codes)

    def test_query_receipts_matches_receipt_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = _build_runtime_root(Path(temp_dir))
            rebuild_state(runtime_root, Path(temp_dir) / "manifests")

            query = query_receipts(runtime_root)
            timeline = receipt_timeline(runtime_root)

            query_ids = [item["receipt_id"] for item in query["receipts"]]
            timeline_ids = [item["receipt_id"] for item in timeline["receipts"]]
            self.assertEqual(timeline_ids, query_ids)

            # Same ordering and content under a filter, too.
            query_filtered = query_receipts(runtime_root, operation="deploy.apply")
            timeline_filtered = receipt_timeline(runtime_root, operation="deploy.apply")
            self.assertEqual(
                [item["receipt_id"] for item in timeline_filtered["receipts"]],
                [item["receipt_id"] for item in query_filtered["receipts"]],
            )

    def test_query_receipts_can_resolve_ref_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = _build_runtime_root(Path(temp_dir))
            rebuild_state(runtime_root, Path(temp_dir) / "manifests")

            report = query_receipts(runtime_root, ref="latest:deploy.apply")

        self.assertEqual("ok", report["status"])
        self.assertEqual(1, len(report["receipts"]))
        self.assertEqual("deploy.apply.dragon-writer.production.fixture", report["receipts"][0]["receipt_id"])
        self.assertEqual("latest", report["resolved_ref"]["strategy"])

    def test_query_receipts_without_index_reports_needs_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            report = query_receipts(runtime_root)

        self.assertTrue(report["needs_rebuild"])
        self.assertEqual("missing", report["status"])
        self.assertIn("state refresh", report["summary"])
        self.assertEqual([], report["receipts"])

    def test_no_secret_value_is_stored_in_the_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = _build_runtime_root(Path(temp_dir))
            rebuild_state(runtime_root, Path(temp_dir) / "manifests")
            db_path = state_db_path(runtime_root)

            # 1. Raw bytes of the SQLite file contain no secret value.
            raw_bytes = db_path.read_bytes()
            self.assertNotIn(ROOT_SECRET.encode("utf-8"), raw_bytes)
            self.assertNotIn(SERVICE_SECRET.encode("utf-8"), raw_bytes)
            self.assertNotIn(NESTED_SECRET.encode("utf-8"), raw_bytes)

            # 2. Every stored payload_json column is secret-free.
            connection = sqlite3.connect(str(db_path))
            try:
                tables = [
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                ]
                for table in tables:
                    columns = [info[1] for info in connection.execute(f"PRAGMA table_info({table})").fetchall()]
                    if "payload_json" not in columns:
                        continue
                    for (payload_json,) in connection.execute(f"SELECT payload_json FROM {table}").fetchall():
                        if not payload_json:
                            continue
                        self.assertNotIn(ROOT_SECRET, payload_json)
                        self.assertNotIn(SERVICE_SECRET, payload_json)
                        self.assertNotIn(NESTED_SECRET, payload_json)
            finally:
                connection.close()

    def test_state_status_after_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = _build_runtime_root(Path(temp_dir))
            rebuild_state(runtime_root, Path(temp_dir) / "manifests")
            status = state_status(runtime_root)

        self.assertTrue(status["available"])
        self.assertTrue(status["exists"])
        self.assertFalse(status["needs_rebuild"])
        self.assertFalse(status["needs_refresh"])
        self.assertEqual("current", status["freshness"]["status"])
        self.assertEqual(STATE_SCHEMA_VERSION, status["schema_version_code"])
        self.assertEqual(STATE_SCHEMA_VERSION, status["schema_version_db"])
        self.assertIn("receipts", status["counts"])
        self.assertIn("refreshed_at", status)

    def test_state_status_reports_stale_index_without_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = _build_runtime_root(Path(temp_dir))
            rebuild_state(runtime_root, Path(temp_dir) / "manifests")
            stale = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            connection = sqlite3.connect(str(state_db_path(runtime_root)))
            try:
                connection.execute(
                    "UPDATE state_metadata SET value = ? WHERE key = 'refreshed_at'",
                    (stale,),
                )
                connection.commit()
            finally:
                connection.close()

            status = state_status(runtime_root)

        self.assertFalse(status["needs_rebuild"])
        self.assertTrue(status["needs_refresh"])
        self.assertEqual("stale", status["freshness"]["status"])
        self.assertIn("state refresh", status["summary"])

    def test_state_summary_reads_app_aggregates_from_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = _build_runtime_root(Path(temp_dir))
            rebuild_state(runtime_root, Path(temp_dir) / "manifests")

            summary = state_summary(runtime_root)

        self.assertEqual("ophelia.state_summary", summary["kind"])
        self.assertEqual("current", summary["freshness"]["status"])
        apps = {item["app"]: item for item in summary["apps"]}
        self.assertIn("dragon-writer", apps)
        self.assertGreater(apps["dragon-writer"]["route_count"], 0)
        self.assertGreater(apps["dragon-writer"]["receipt_count"], 0)
        self.assertGreater(apps["dragon-writer"]["backup_count"], 0)
        self.assertGreater(apps["dragon-writer"]["observability_snapshot_count"], 0)
        self.assertGreater(apps["dragon-writer"]["traffic_state_count"], 0)
        self.assertGreater(apps["dragon-writer"]["provider_snapshot_count"], 0)
        self.assertGreater(apps["dragon-writer"]["github_provisioning_count"], 0)

    def test_state_status_without_index_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            status = state_status(runtime_root)

        self.assertFalse(status["exists"])
        self.assertTrue(status["needs_rebuild"])
        self.assertTrue(status["needs_refresh"])
        self.assertIsNone(status["schema_version_db"])
        # Reading status must not create the index.
        self.assertFalse(state_db_path(runtime_root).exists())


if __name__ == "__main__":
    unittest.main()
