from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.manifest import load_manifest
from ophelia.portability import (
    app_readiness_report,
    backup_status_report,
    cutover_apply,
    cutover_plan,
    env_shape_diff_report,
    export_create,
    export_plan,
    import_apply,
    import_plan,
    isolation_plan,
    pack_init_report,
    pack_validation_report,
    receipt_list_report,
    receipt_show_report,
    restore_drill_apply,
    restore_drill_plan,
)
from ophelia.runtime import deploy_bundle


class PortabilityTests(unittest.TestCase):
    def test_pack_validation_warns_for_noncritical_inferred_addon_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "app.ophelia.yml"
            manifest_path.write_text(_addon_manifest(portability=None))
            manifest = load_manifest(manifest_path)

            report = pack_validation_report(manifest, manifest_path)

        self.assertTrue(report["ok"])
        warning_codes = {item["code"] for item in report["warnings"]}
        self.assertIn("postgres_data_contract_inferred", warning_codes)

    def test_pack_validation_blocks_critical_inferred_addon_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "app.ophelia.yml"
            manifest_path.write_text(_addon_manifest(portability="critical"))
            manifest = load_manifest(manifest_path)

            report = pack_validation_report(manifest, manifest_path)

        self.assertFalse(report["ok"])
        error_codes = {item["code"] for item in report["errors"]}
        self.assertIn("postgres_data_contract_inferred", error_codes)

    def test_export_plan_is_read_only_and_redacts_env_shape(self) -> None:
        with self._skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = root / "dragonwriter.ophelia.yml"
                manifest_path.write_text(_portable_manifest())
                manifest = load_manifest(manifest_path)
                deploy_bundle(manifest, manifest_path, runtime_root)
                (runtime_root / "apps" / "dragon-writer" / "env").write_text(
                    "OPHELIA_APP=dragon-writer\nDATABASE_URL=super-secret\n"
                )

                plan = export_plan(
                    "dragon-writer",
                    environment="production",
                    runtime_root=runtime_root,
                    manifest_path=manifest_path,
                    ophelia_root=root,
                )

        self.assertEqual("app.export.plan", plan["receipt_type"])
        self.assertTrue(plan["read_only"])
        self.assertTrue(plan["can_create"])
        self.assertEqual([], plan["blockers"])
        self.assertIn("postgres", plan["data_dependencies"])
        self.assertIn("export-plan.json", plan["artifact_paths"]["export_plan_receipt"])
        self.assertNotIn("super-secret", json.dumps(plan))

    def test_export_create_requires_token_and_writes_redacted_runtime_bundle(self) -> None:
        with self._skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = root / "dragonwriter.ophelia.yml"
                uploads_root = root / "uploads"
                uploads_root.mkdir()
                (uploads_root / "draft.txt").write_text("private draft text\n")
                manifest_path.write_text(_portable_manifest_with_env_secret())
                manifest = load_manifest(manifest_path)
                app_root = deploy_bundle(manifest, manifest_path, runtime_root)
                (app_root / "env").write_text("DATABASE_URL=postgres://user:secret@example/db\nAPI_TOKEN=super-secret\n")
                plan = export_plan(
                    "dragon-writer",
                    environment="production",
                    runtime_root=runtime_root,
                    manifest_path=manifest_path,
                    ophelia_root=root,
                )

                blocked = export_create(
                    "dragon-writer",
                    environment="production",
                    runtime_root=runtime_root,
                    manifest_path=manifest_path,
                    confirm="wrong",
                    ophelia_root=root,
                )
                receipt = export_create(
                    "dragon-writer",
                    environment="production",
                    runtime_root=runtime_root,
                    manifest_path=manifest_path,
                    confirm=str(plan["confirmation_token"]),
                    ophelia_root=root,
                )
                bundle_path = Path(str(receipt["bundle_path"]))
                bundle_tar_path = Path(str(receipt["bundle_tar_path"]))
                imported = import_plan(bundle_path, runtime_root=runtime_root, ophelia_root=root)
                imported_from_tar = import_plan(bundle_tar_path, runtime_root=runtime_root, ophelia_root=root)
                listed = receipt_list_report(runtime_root, app="dragon-writer")
                bundle_manifest_exists = (bundle_path / "manifest.json").exists()
                bundle_tar_exists = bundle_tar_path.exists()
                source_host_exists = (bundle_path / "source-host.json").exists()
                checksums_exists = (bundle_path / "checksums.sha256").exists()
                export_plan_receipt_exists = (bundle_path / "receipts" / "export-plan.json").exists()
                export_create_receipt_exists = (bundle_path / "receipts" / "export-create.json").exists()
                runtime_lock_exists = (bundle_path / "runtime" / "manifest.lock.json").exists()
                runtime_env_exists = (bundle_path / "runtime" / "env").exists()
                volume_archive_exists = (bundle_path / "data" / "volumes" / "uploads.tar").exists()
                bundle_text = "\n".join(path.read_text(errors="ignore") for path in bundle_path.rglob("*") if path.is_file())

        self.assertEqual("blocked", blocked["status"])
        self.assertEqual("succeeded", receipt["status"])
        self.assertTrue(bundle_manifest_exists)
        self.assertTrue(bundle_tar_exists)
        self.assertTrue(source_host_exists)
        self.assertTrue(checksums_exists)
        self.assertTrue(export_plan_receipt_exists)
        self.assertTrue(export_create_receipt_exists)
        self.assertTrue(runtime_lock_exists)
        self.assertFalse(runtime_env_exists)
        self.assertTrue(volume_archive_exists)
        self.assertNotIn("super-secret", bundle_text)
        self.assertNotIn("postgres://user:secret", bundle_text)
        self.assertIn("<redacted>", bundle_text)
        self.assertEqual("dragon-writer", imported["app"])
        self.assertEqual("dragon-writer", imported_from_tar["app"])
        self.assertIn("data/volumes/uploads.tar", {item["path"] for item in imported["source_artifacts"]["data_archives"]})
        self.assertIn("app.export.create", {item["operation"] for item in listed["receipts"]})

    def test_export_create_archives_static_root(self) -> None:
        with self._skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                static_root = root / "site"
                static_root.mkdir()
                (static_root / "index.html").write_text("<h1>Static</h1>\n")
                manifest_path = root / "static.ophelia.yml"
                manifest_path.write_text(_static_manifest(static_root))
                manifest = load_manifest(manifest_path)
                deploy_bundle(manifest, manifest_path, runtime_root)
                plan = export_plan("static-portable", "staging", runtime_root, manifest_path, root)
                receipt = export_create(
                    "static-portable",
                    "staging",
                    runtime_root,
                    manifest_path,
                    str(plan["confirmation_token"]),
                    root,
                )
                bundle_path = Path(str(receipt["bundle_path"]))
                static_archive_exists = (bundle_path / "data" / "static-assets" / "static-portable.tar").exists()
                imported = import_plan(Path(str(receipt["bundle_tar_path"])), runtime_root=runtime_root, ophelia_root=root)

        self.assertEqual("succeeded", receipt["status"])
        self.assertTrue(static_archive_exists)
        self.assertIn("data/static-assets/static-portable.tar", {item["path"] for item in imported["source_artifacts"]["data_archives"]})

    def test_export_create_writes_optional_zstd_archive_when_available(self) -> None:
        with self._skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                static_root = root / "site"
                static_root.mkdir()
                (static_root / "index.html").write_text("<h1>Static</h1>\n")
                manifest_path = root / "static.ophelia.yml"
                manifest_path.write_text(_static_manifest(static_root))
                manifest = load_manifest(manifest_path)
                deploy_bundle(manifest, manifest_path, runtime_root)
                plan = export_plan("static-portable", "staging", runtime_root, manifest_path, root)

                def fake_run(args, **kwargs):
                    if args and args[0] == "git":
                        return subprocess.CompletedProcess(args=args, returncode=0, stdout="fixture-sha\n", stderr="")
                    self.assertEqual("/usr/bin/zstd", args[0])
                    Path(args[args.index("-o") + 1]).write_bytes(b"compressed fixture\n")
                    return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

                with mock.patch("ophelia.portability.shutil.which", return_value="/usr/bin/zstd"), mock.patch(
                    "ophelia.portability.subprocess.run",
                    side_effect=fake_run,
                ):
                    receipt = export_create(
                        "static-portable",
                        "staging",
                        runtime_root,
                        manifest_path,
                        str(plan["confirmation_token"]),
                        root,
                    )
                archive_path = Path(str(receipt["bundle_archive_path"]))
                archive_exists = archive_path.exists()

        self.assertTrue(archive_exists)
        self.assertEqual("created", receipt["compressed_archive"]["status"])
        self.assertTrue(str(archive_path).endswith(".tar.zst"))

    def test_export_create_can_run_opt_in_postgres_dump_without_leaking_url(self) -> None:
        with self._skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                uploads_root = root / "uploads"
                uploads_root.mkdir()
                manifest_path = root / "dragonwriter.ophelia.yml"
                manifest_path.write_text(_portable_manifest_with_env_secret())
                manifest = load_manifest(manifest_path)
                app_root = deploy_bundle(manifest, manifest_path, runtime_root)
                database_url = "postgres://writer:secret-password@db.example.test:5432/dragon_writer?sslmode=require"
                (app_root / "env").write_text(f"DATABASE_URL={database_url}\nAPI_TOKEN=super-secret\n")
                plan_without_postgres = export_plan(
                    "dragon-writer",
                    "production",
                    runtime_root,
                    manifest_path,
                    root,
                )
                plan = export_plan(
                    "dragon-writer",
                    "production",
                    runtime_root,
                    manifest_path,
                    root,
                    include_postgres=True,
                )

                def fake_run(args, **kwargs):
                    if args and str(args[0]).endswith("pg_dump"):
                        self.assertNotIn(database_url, args)
                        self.assertEqual("secret-password", kwargs["env"]["PGPASSWORD"])
                        Path(args[args.index("--file") + 1]).write_text("dump bytes\n")
                        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
                    return subprocess.CompletedProcess(args=args, returncode=0, stdout="fixture-sha\n", stderr="")

                blocked = export_create(
                    "dragon-writer",
                    "production",
                    runtime_root,
                    manifest_path,
                    str(plan_without_postgres["confirmation_token"]),
                    root,
                    include_postgres=True,
                )
                with mock.patch("ophelia.portability.shutil.which", return_value="/usr/bin/pg_dump"), mock.patch(
                    "ophelia.portability.subprocess.run",
                    side_effect=fake_run,
                ):
                    receipt = export_create(
                        "dragon-writer",
                        "production",
                        runtime_root,
                        manifest_path,
                        str(plan["confirmation_token"]),
                        root,
                        include_postgres=True,
                    )
                imported = import_plan(Path(str(receipt["bundle_tar_path"])), runtime_root=runtime_root, ophelia_root=root)

        self.assertEqual("blocked", blocked["status"])
        self.assertEqual("succeeded", receipt["status"])
        self.assertIn("data/postgres/dragon_writer.dump", {item["path"] for item in imported["source_artifacts"]["postgres_dumps"]})
        receipt_text = json.dumps(receipt)
        self.assertNotIn(database_url, receipt_text)
        self.assertNotIn("secret-password", receipt_text)

    def test_import_plan_reads_export_metadata_json(self) -> None:
        with self._skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                metadata_path = root / "export-plan.json"
                metadata_path.write_text(
                    json.dumps(
                        {
                            "receipt_type": "app.export.plan",
                            "app": "dragon-writer",
                            "environment": "production",
                            "domains": ["dragonwriter.begam.in"],
                            "routes": [{"domain": "dragonwriter.begam.in", "service": "web"}],
                            "data_dependencies": {
                                "postgres": {
                                    "mode": "shared-postgres-database",
                                    "import": {"command": "pg_restore"},
                                },
                                "volumes": [
                                    {
                                        "name": "uploads",
                                        "mount": "/app/uploads",
                                        "class": "critical",
                                    }
                                ],
                            },
                            "env_shape": [{"key": "DATABASE_URL", "secret_value_redacted": True}],
                            "verification_checks": [
                                {
                                    "name": "health",
                                    "url": "https://dragonwriter.begam.in/health",
                                    "expect_status": 200,
                                }
                            ],
                        }
                    )
                    + "\n"
                )

                plan = import_plan(metadata_path, runtime_root=root / "runtime", ophelia_root=root)

        self.assertEqual("app.import.plan", plan["receipt_type"])
        self.assertEqual("dragon-writer", plan["app"])
        self.assertEqual("production", plan["environment"])
        self.assertEqual([], plan["blockers"])
        self.assertTrue(plan["apply_supported"])
        self.assertIn("postgres.import", {item["name"] for item in plan["data_restore_commands"]})

    def test_import_apply_creates_rehearsal_preview_only(self) -> None:
        with self._skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                metadata_path = root / "export-plan.json"
                metadata_path.write_text(
                    json.dumps(
                        {
                            "receipt_type": "app.export.plan",
                            "app": "dragon-writer",
                            "environment": "production",
                            "domains": ["dragonwriter.begam.in"],
                            "routes": [{"domain": "dragonwriter.begam.in", "service": "web"}],
                            "data_dependencies": {
                                "postgres": {
                                    "mode": "shared-postgres-database",
                                    "import": {"command": "pg_restore"},
                                }
                            },
                            "env_shape": [{"key": "DATABASE_URL", "secret_value_redacted": True}],
                            "verification_checks": [{"name": "health", "url": "https://dragonwriter.begam.in/health"}],
                        }
                    )
                    + "\n"
                )
                plan = import_plan(metadata_path, runtime_root=runtime_root, ophelia_root=root)
                blocked = import_apply(metadata_path, runtime_root=runtime_root, confirm="wrong", ophelia_root=root)
                receipt = import_apply(
                    metadata_path,
                    runtime_root=runtime_root,
                    confirm=str(plan["confirmation_token"]),
                    ophelia_root=root,
                )
                preview_path = Path(str(receipt["preview_path"]))
                listed = receipt_list_report(runtime_root, app="dragon-writer")
                preview_files = {path.name for path in preview_path.iterdir()}

        self.assertTrue(plan["apply_supported"])
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual("succeeded", receipt["status"])
        self.assertIn("import-plan.json", preview_files)
        self.assertIn("source-artifacts.json", preview_files)
        self.assertFalse((runtime_root / "apps" / "dragon-writer" / "manifest.lock.json").exists())
        self.assertIn("app.import.apply", {item["operation"] for item in listed["receipts"]})

    def test_restore_drill_apply_writes_isolated_receipt(self) -> None:
        with self._skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                static_root = root / "site"
                static_root.mkdir()
                (static_root / "index.html").write_text("<h1>Static</h1>\n")
                manifest_path = root / "static.ophelia.yml"
                manifest_path.write_text(_static_manifest(static_root))
                manifest = load_manifest(manifest_path)
                app_root = deploy_bundle(manifest, manifest_path, runtime_root)
                (app_root / "active_release.json").write_text((app_root / "release.json").read_text())
                export = export_plan("static-portable", "staging", runtime_root, manifest_path, root)
                export_receipt = export_create("static-portable", "staging", runtime_root, manifest_path, str(export["confirmation_token"]), root)
                source = Path(str(export_receipt["bundle_tar_path"]))
                plan = restore_drill_plan("static-portable", "staging", runtime_root, manifest_path, source)
                blocked = restore_drill_apply("static-portable", "staging", runtime_root, manifest_path, source, "wrong")
                receipt = restore_drill_apply(
                    "static-portable",
                    "staging",
                    runtime_root,
                    manifest_path,
                    source,
                    str(plan["confirmation_token"]),
                )
                drill_path = Path(str(receipt["drill_path"]))
                drill_plan_exists = (drill_path / "restore-drill-plan.json").exists()
                artifact_checks_exists = (drill_path / "artifact-checks.json").exists()

        self.assertEqual([], plan["blockers"])
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual("succeeded", receipt["status"])
        self.assertTrue(drill_plan_exists)
        self.assertTrue(artifact_checks_exists)

    def test_cutover_apply_writes_checkpoint_without_route_mutation(self) -> None:
        with self._skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                static_root = root / "site"
                static_root.mkdir()
                (static_root / "index.html").write_text("<h1>Static</h1>\n")
                manifest_path = root / "static.ophelia.yml"
                manifest_path.write_text(_static_manifest(static_root))
                manifest = load_manifest(manifest_path)
                app_root = deploy_bundle(manifest, manifest_path, runtime_root)
                (app_root / "active_release.json").write_text((app_root / "release.json").read_text())
                plan = cutover_plan("static-portable", "source-host", "target-host", "staging", runtime_root, manifest_path)
                blocked = cutover_apply("static-portable", "source-host", "target-host", "staging", runtime_root, manifest_path, "wrong")
                receipt = cutover_apply(
                    "static-portable",
                    "source-host",
                    "target-host",
                    "staging",
                    runtime_root,
                    manifest_path,
                    str(plan["confirmation_token"]),
                )
                cutover_path = Path(str(receipt["cutover_path"]))
                cutover_plan_exists = (cutover_path / "cutover-plan.json").exists()

        self.assertEqual([], plan["blockers"])
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual("succeeded", receipt["status"])
        self.assertFalse(receipt["route_mutation_performed"])
        self.assertTrue(cutover_plan_exists)

    def test_isolation_plan_reflects_per_app_networking_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "isolated.ophelia.yml"
            manifest_path.write_text(
                _portable_manifest().replace(
                    "host_requirements:\n  min_disk_free: 1b\n",
                    "host_requirements:\n  min_disk_free: 1b\nnetworking:\n  internal: per-app\n",
                )
            )

            plan = isolation_plan(
                "dragon-writer",
                "production",
                runtime_root=root / "runtime",
                manifest_path=manifest_path,
            )

        self.assertEqual("per-app", plan["mode"])
        self.assertEqual(["ophelia-edge", "dragon-writer-production-internal"], plan["target_networks"])
        self.assertEqual([], plan["warnings"])

    def test_env_shape_diff_redacts_values_and_reports_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "dragonwriter.ophelia.yml"
            manifest_path.write_text(_portable_manifest())
            manifest = load_manifest(manifest_path)
            deploy_bundle(manifest, manifest_path, runtime_root)
            (runtime_root / "apps" / "dragon-writer" / "env").write_text(
                "DATABASE_URL=postgres://user:password@example/db\nEXTRA_SECRET=secret-value\n"
            )

            report = env_shape_diff_report("dragon-writer", "production", runtime_root, manifest_path)

        statuses = {item["key"]: item["status"] for item in report["entries"]}
        self.assertEqual("present", statuses["DATABASE_URL"])
        self.assertEqual("extra", statuses["EXTRA_SECRET"])
        self.assertTrue(report["values_redacted"])
        self.assertNotIn("postgres://user:password", json.dumps(report))
        self.assertIn("schema_version", report)

    def test_backup_status_reports_fresh_metadata_without_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "dragonwriter.ophelia.yml"
            manifest_path.write_text(_portable_manifest())
            backup_root = runtime_root / "backups" / "apps" / "dragon-writer" / "20260620T120000Z-fixture"
            backup_root.mkdir(parents=True)
            (backup_root / "backup-manifest.json").write_text(
                json.dumps(
                    {
                        "backup_id": "20260620T120000Z-fixture",
                        "app": "dragon-writer",
                        "created_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=10)),
                        "coverage": {"release_metadata": True, "app_env": True},
                        "database": {"postgres": True, "mode": "metadata-only"},
                    }
                )
                + "\n"
            )

            report = backup_status_report("dragon-writer", "production", runtime_root, manifest_path)

        self.assertEqual("fresh", report["freshness"]["status"])
        self.assertEqual([], report["blockers"])
        self.assertEqual("metadata-only", report["validation"]["status"])
        self.assertEqual("20260620T120000Z-fixture", report["latest_backup"]["backup_id"])

    def test_readiness_aggregates_score_without_printing_env_values(self) -> None:
        with self._skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = root / "dragonwriter.ophelia.yml"
                manifest_path.write_text(_portable_manifest())
                manifest = load_manifest(manifest_path)
                app_root = deploy_bundle(manifest, manifest_path, runtime_root)
                release = json.loads((app_root / "release.json").read_text())
                (app_root / "active_release.json").write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
                _write_filled_env(app_root)
                _write_backup(runtime_root, "dragon-writer")
                drill_root = app_root / "restore-drills"
                drill_root.mkdir()
                (drill_root / "successful-drill.json").write_text(
                    json.dumps(
                        {
                            "operation": "app.restore-drill.apply",
                            "operation_id": "app.restore-drill.apply.dragon-writer.production.fixture",
                            "status": "succeeded",
                            "app": "dragon-writer",
                            "environment": "production",
                        }
                    )
                    + "\n"
                )

                report = app_readiness_report("dragon-writer", "production", runtime_root, manifest_path)

        self.assertEqual([], report["blockers"])
        self.assertIn(report["readiness_level"], {"ready", "warning"})
        self.assertGreaterEqual(report["portability_score"]["score"], 80)
        self.assertNotIn("secret-value", json.dumps(report))

    def test_receipt_browser_lists_and_shows_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            receipt_root = runtime_root / "apps" / "dragon-writer" / "receipts"
            receipt_root.mkdir(parents=True)
            receipt_path = receipt_root / "export-create.json"
            receipt_path.write_text(
                json.dumps(
                    {
                        "operation": "app.export.create",
                        "operation_id": "app.export.create.dragon-writer.production.fixture",
                        "status": "succeeded",
                        "app": "dragon-writer",
                        "environment": "production",
                        "inputs_redacted": True,
                    }
                )
                + "\n"
            )

            listed = receipt_list_report(runtime_root, app="dragon-writer", environment="production")
            shown = receipt_show_report("app.export.create.dragon-writer.production.fixture", runtime_root)

        self.assertEqual(1, len(listed["receipts"]))
        self.assertEqual("app.export.create", listed["receipts"][0]["operation"])
        self.assertEqual("succeeded", shown["receipt"]["status"])

    def test_pack_init_preview_is_read_only_and_write_refuses_partial_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            preview = pack_init_report("dragon-writer", "production", True, True, False, True, root)
            self.assertTrue(preview["dry_run"])
            self.assertFalse((root / "ophelia" / "runbook.md").exists())

            existing = root / "ophelia" / "runbook.md"
            existing.parent.mkdir()
            existing.write_text("existing\n")
            blocked = pack_init_report("dragon-writer", "production", True, True, False, True, root, write=True)

        self.assertTrue(blocked["blockers"])
        self.assertFalse((root / "ophelia" / "agent.md").exists())

    def test_pack_and_app_plan_commands_emit_json(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        env = {**os.environ, "OPHELIA_SKIP_DOCKER_STATUS": "1"}
        commands = [
            ["pack", "validate", "examples/dragonwriter.ophelia.yml", "--json"],
            ["pack", "explain", "examples/dragonwriter.ophelia.yml", "--json"],
            [
                "app",
                "export",
                "plan",
                "dragon-writer",
                "--environment",
                "production",
                "--manifest",
                "examples/dragonwriter.ophelia.yml",
                "--runtime-root",
                str(Path(tempfile.gettempdir()) / "ophelia-portability-empty-runtime"),
                "--json",
            ],
            [
                "env",
                "diff",
                "dragon-writer",
                "--environment",
                "production",
                "--manifest",
                "examples/dragonwriter.ophelia.yml",
                "--runtime-root",
                str(Path(tempfile.gettempdir()) / "ophelia-portability-empty-runtime"),
                "--json",
            ],
            [
                "backup",
                "status",
                "dragon-writer",
                "--environment",
                "production",
                "--manifest",
                "examples/dragonwriter.ophelia.yml",
                "--runtime-root",
                str(Path(tempfile.gettempdir()) / "ophelia-portability-empty-runtime"),
                "--json",
            ],
            [
                "app",
                "readiness",
                "dragon-writer",
                "--environment",
                "production",
                "--manifest",
                "examples/dragonwriter.ophelia.yml",
                "--runtime-root",
                str(Path(tempfile.gettempdir()) / "ophelia-portability-empty-runtime"),
                "--json",
            ],
            ["receipts", "list", "--runtime-root", str(Path(tempfile.gettempdir()) / "ophelia-portability-empty-runtime"), "--json"],
            ["pack", "init", "--app", "dragon-writer", "--environment", "production", "--critical", "--postgres", "--uploads", "--json"],
        ]

        for command in commands:
            result = subprocess.run(
                [str(repo / "cli" / "ship"), *command],
                text=True,
                capture_output=True,
                env=env,
                cwd=repo,
            )
            self.assertIn(result.returncode, {0, 1})
            json.loads(result.stdout)

    def _skip_docker_status(self):
        return _EnvPatch("OPHELIA_SKIP_DOCKER_STATUS", "1")


class _EnvPatch:
    def __init__(self, key: str, value: str):
        self.key = key
        self.value = value
        self.previous = None

    def __enter__(self):
        self.previous = os.environ.get(self.key)
        os.environ[self.key] = self.value

    def __exit__(self, exc_type, exc, tb):
        if self.previous is None:
            os.environ.pop(self.key, None)
        else:
            os.environ[self.key] = self.previous


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_filled_env(app_root: Path) -> None:
    lines = []
    for raw_line in (app_root / "env.example").read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _value = line.split("=", 1)
        lines.append(f"{key}=filled")
    if "DATABASE_URL=filled" not in lines:
        lines.append("DATABASE_URL=filled")
    (app_root / "env").write_text("\n".join(lines) + "\n")


def _write_backup(runtime_root: Path, app: str) -> None:
    backup_root = runtime_root / "backups" / "apps" / app / "20260620T120000Z-readiness"
    backup_root.mkdir(parents=True)
    (backup_root / "backup-manifest.json").write_text(
        json.dumps(
            {
                "backup_id": "20260620T120000Z-readiness",
                "app": app,
                "created_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=5)),
                "coverage": {"release_metadata": True, "app_env": True, "postgres_metadata": True},
                "database": {"postgres": True, "mode": "metadata-only"},
                "warnings": [],
            }
        )
        + "\n"
    )


def _addon_manifest(portability: str | None) -> str:
    pack = ""
    if portability is not None:
        pack = f"""
pack:
  portability: {portability}
"""
    return f"""
version: 1
app: addon-portable
environment: production
kind: service
image: ghcr.io/example/addon-portable:latest
{pack}
services:
  web:
    port: 3000
routes:
  - domain: addon-portable.example.com
    service: web
addons:
  postgres: true
  redis: false
verify:
  - name: health
    url: https://addon-portable.example.com/health
""".strip() + "\n"


def _portable_manifest() -> str:
    return """
version: 1
app: dragon-writer
environment: production
kind: service
image: ghcr.io/example/dragon-writer@sha256:aaaaaaaa
pack:
  portability: critical
  owner: personal
host_requirements:
  min_disk_free: 1b
services:
  web:
    port: 3000
routes:
  - domain: dragonwriter.begam.in
    service: web
data:
  postgres:
    mode: shared-postgres-database
    database: dragon_writer
    export:
      format: custom
      command: pg_dump
    import:
      command: pg_restore
    verify:
      command: ophelia/checks/data-verify.sh
  volumes:
    - name: uploads
      mount: /app/uploads
      source: uploads
      class: critical
      export: tar-zstd
      import: tar-zstd
  backups:
    required: true
    restore_drill_required: true
    offsite_required: true
verify:
  - name: health
    url: https://dragonwriter.begam.in/health
""".strip() + "\n"


def _static_manifest(static_root: Path) -> str:
    return f"""
version: 1
app: static-portable
environment: staging
kind: static
static_root: {static_root}
routes:
  - domain: static-portable.example.com
pack:
  portability: static
  owner: personal
""".strip() + "\n"


def _portable_manifest_with_env_secret() -> str:
    return """
version: 1
app: dragon-writer
environment: production
kind: service
image: ghcr.io/example/dragon-writer@sha256:aaaaaaaa
env:
  API_TOKEN: super-secret
pack:
  portability: critical
  owner: personal
host_requirements:
  min_disk_free: 1b
services:
  web:
    port: 3000
routes:
  - domain: dragonwriter.begam.in
    service: web
data:
  postgres:
    mode: shared-postgres-database
    database: dragon_writer
    export:
      format: custom
      command: pg_dump
    import:
      command: pg_restore
    verify:
      command: ophelia/checks/data-verify.sh
  volumes:
    - name: uploads
      mount: /app/uploads
      source: uploads
      class: critical
      export: tar-zstd
      import: tar-zstd
  backups:
    required: true
    restore_drill_required: true
    offsite_required: true
verify:
  - name: health
    url: https://dragonwriter.begam.in/health
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
