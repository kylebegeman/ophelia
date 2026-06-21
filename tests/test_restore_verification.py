from __future__ import annotations

import hashlib
import json
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia import restore_verification as rv
from ophelia.portability import app_readiness_report
from ophelia.restore_verification import (
    backup_verify_apply,
    backup_verify_plan,
    restore_drills_list,
    restore_drills_show,
)


CANARY = "postgres://user:CANARYSECRETVALUE@db.example.com:5432/app"


class RestoreVerificationTests(unittest.TestCase):
    # ------------------------------------------------------------------ #
    # plan is read-only
    # ------------------------------------------------------------------ #
    def test_plan_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "dragonwriter.ophelia.yml"
            manifest_path.write_text(_critical_manifest())
            _write_backup(runtime_root, "dragon-writer", with_checksums=True)

            before = _snapshot(runtime_root)
            plan = backup_verify_plan("dragon-writer", "production", runtime_root, manifest_path=manifest_path)
            after = _snapshot(runtime_root)

        self.assertEqual(before, after)
        self.assertTrue(plan["read_only"])
        self.assertTrue(plan["confirmation_required"])
        self.assertIsNotNone(plan["confirmation_token"])
        self.assertNotEqual("<redacted>", plan["confirmation_token"])
        self.assertEqual("ophelia.plan", plan["kind"])
        self.assertIn("rehearsals", plan["rehearsal_target"])
        self.assertIn("cleanup", plan["cleanup_note"].lower())

    def test_plan_blocks_when_no_backup_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "dragonwriter.ophelia.yml"
            manifest_path.write_text(_critical_manifest())

            plan = backup_verify_plan("dragon-writer", "production", runtime_root, manifest_path=manifest_path)

        self.assertIsNone(plan["confirmation_token"])
        codes = {item["code"] for item in plan["blockers"]}
        self.assertIn("backup_missing", codes)

    # ------------------------------------------------------------------ #
    # apply is token-gated, isolated, and never deletes
    # ------------------------------------------------------------------ #
    def test_apply_requires_matching_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "dragonwriter.ophelia.yml"
            manifest_path.write_text(_critical_manifest())
            _write_backup(runtime_root, "dragon-writer", with_checksums=True)
            plan = backup_verify_plan("dragon-writer", "production", runtime_root, manifest_path=manifest_path)

            drills_dir = runtime_root / "apps" / "dragon-writer" / "restore-drills"

            # Empty token => error, no work.
            empty = backup_verify_apply("dragon-writer", "production", runtime_root, confirm="", manifest_path=manifest_path)
            self.assertEqual("ophelia.error", empty["kind"])
            self.assertEqual("confirmation_token_missing", empty["blockers"][0]["code"])
            self.assertFalse(drills_dir.exists())

            # Wrong token => error, no work.
            wrong = backup_verify_apply(
                "dragon-writer", "production", runtime_root, confirm="not-the-token", manifest_path=manifest_path
            )
            self.assertEqual("ophelia.error", wrong["kind"])
            self.assertEqual("confirmation_token_mismatch", wrong["blockers"][0]["code"])
            self.assertFalse(drills_dir.exists())

            # Correct token => receipt written under restore-drills.
            receipt = backup_verify_apply(
                "dragon-writer",
                "production",
                runtime_root,
                confirm=str(plan["confirmation_token"]),
                manifest_path=manifest_path,
            )

            self.assertEqual("ophelia.receipt", receipt["kind"])
            self.assertEqual("succeeded", receipt["status"])
            self.assertEqual(VERIFY := "backup.verify.apply", receipt["operation"])
            self.assertFalse(receipt["production_data_modified"])
            self.assertFalse(receipt["deleted_anything"])
            self.assertEqual("No source runtime state was changed.", receipt["rollback"]["note"])

            written = sorted(drills_dir.glob("*.json"))
            self.assertEqual(1, len(written))

            # Nothing was written into the production app bundle proper (only the
            # restore-drills + rehearsals areas), and nothing deleted: the source
            # backup tree is intact.
            app_dir = runtime_root / "apps" / "dragon-writer"
            self.assertFalse((app_dir / "env").exists())
            self.assertFalse((app_dir / "compose.yml").exists())
            self.assertTrue((runtime_root / "rehearsals" / "dragon-writer").exists())
            self.assertTrue(
                (runtime_root / "backups" / "apps" / "dragon-writer").exists()
            )
            self.assertTrue(_backup_dir(runtime_root, "dragon-writer").joinpath("backup-manifest.json").exists())

    def test_apply_refuses_target_resolving_into_production_app_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "dragonwriter.ophelia.yml"
            manifest_path.write_text(_critical_manifest())
            _write_backup(runtime_root, "dragon-writer", with_checksums=True)

            # Force the plan to advertise a rehearsal target inside the production
            # app dir; the apply must refuse and write nothing.
            unsafe_target = str(runtime_root / "apps" / "dragon-writer")

            def _unsafe(runtime, app, backup_id):  # noqa: ANN001
                return unsafe_target

            with mock.patch.object(rv, "_rehearsal_target", _unsafe):
                plan = backup_verify_plan("dragon-writer", "production", runtime_root, manifest_path=manifest_path)
                receipt = backup_verify_apply(
                    "dragon-writer",
                    "production",
                    runtime_root,
                    confirm=str(plan["confirmation_token"]),
                    manifest_path=manifest_path,
                )

        self.assertEqual("ophelia.error", receipt["kind"])
        self.assertEqual("unsafe_rehearsal_target", receipt["blockers"][0]["code"])
        # Nothing written: no restore-drills receipt, no production app env file.
        self.assertFalse((runtime_root / "apps" / "dragon-writer" / "restore-drills").exists())
        self.assertFalse((runtime_root / "apps" / "dragon-writer" / "env").exists())

    # ------------------------------------------------------------------ #
    # missing-checksum severity depends on criticality
    # ------------------------------------------------------------------ #
    def test_missing_checksum_is_blocker_for_critical_app(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "dragonwriter.ophelia.yml"
            manifest_path.write_text(_critical_manifest())
            _write_backup(runtime_root, "dragon-writer", with_checksums=False)
            plan = backup_verify_plan("dragon-writer", "production", runtime_root, manifest_path=manifest_path)

            receipt = backup_verify_apply(
                "dragon-writer",
                "production",
                runtime_root,
                confirm=str(plan["confirmation_token"]),
                manifest_path=manifest_path,
            )

        self.assertTrue(plan["missing_checksum_is_blocking"])
        self.assertEqual("failed", receipt["status"])
        codes = {item["code"] for item in receipt["blockers"]}
        self.assertIn("checksum_missing", codes)
        checksum_check = next(c for c in receipt["checks"] if c["name"] == "checksum_match")
        self.assertFalse(checksum_check["ok"])

    def test_missing_checksum_is_warning_for_noncritical_app(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "standard.ophelia.yml"
            manifest_path.write_text(_standard_manifest())
            _write_backup(runtime_root, "standard-app", with_checksums=False)
            plan = backup_verify_plan("standard-app", "production", runtime_root, manifest_path=manifest_path)

            receipt = backup_verify_apply(
                "standard-app",
                "production",
                runtime_root,
                confirm=str(plan["confirmation_token"]),
                manifest_path=manifest_path,
            )

        self.assertFalse(plan["missing_checksum_is_blocking"])
        # No checksum blocker; verification still succeeds (warning recorded).
        codes = {item["code"] for item in receipt["blockers"]}
        self.assertNotIn("checksum_missing", codes)
        warn_codes = {item["code"] for item in receipt["warnings"]}
        self.assertIn("checksum_missing", warn_codes)
        self.assertEqual("succeeded", receipt["status"])

    # ------------------------------------------------------------------ #
    # readiness consumes the latest successful verification receipt
    # ------------------------------------------------------------------ #
    def test_readiness_consumes_latest_successful_verification_receipt(self) -> None:
        with mock.patch("ophelia.portability.host_inventory", return_value={}):
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = root / "dragonwriter.ophelia.yml"
                manifest_path.write_text(_critical_manifest())
                app_root = runtime_root / "apps" / "dragon-writer"
                app_root.mkdir(parents=True)
                _write_release(app_root)
                _write_filled_env(app_root)
                _write_backup(runtime_root, "dragon-writer", with_checksums=True)
                # No restore-drill receipt; only a successful verification receipt.
                _write_verification_receipt(runtime_root, "dragon-writer", "production", status="succeeded")

                report = app_readiness_report("dragon-writer", "production", runtime_root, manifest_path)

        # The verification receipt satisfies the restore_drill factor: no
        # restore_drill_missing blocker even though there is no drill receipt.
        blocker_codes = {item["code"] for item in report["blockers"]}
        self.assertNotIn("restore_drill_missing", blocker_codes)
        restore_check = next(c for c in report["checks"] if c["name"] == "restore_drill")
        self.assertTrue(restore_check["ok"])
        backup_verification = report["source_reports"]["backup_verification"]
        self.assertEqual("succeeded", backup_verification["status"])
        self.assertIsNotNone(backup_verification["verify_id"])
        factor = next(f for f in report["portability_score"]["factors"] if f["name"] == "restore_drill")
        self.assertTrue(factor["ok"])

    def test_readiness_without_verification_keeps_restore_drill_blocker(self) -> None:
        # Additive guard: an app with neither a drill nor a verification receipt
        # still gets the restore_drill_missing blocker (unchanged behavior).
        with mock.patch("ophelia.portability.host_inventory", return_value={}):
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = root / "dragonwriter.ophelia.yml"
                manifest_path.write_text(_critical_manifest())
                app_root = runtime_root / "apps" / "dragon-writer"
                app_root.mkdir(parents=True)
                _write_release(app_root)
                _write_filled_env(app_root)
                _write_backup(runtime_root, "dragon-writer", with_checksums=True)

                report = app_readiness_report("dragon-writer", "production", runtime_root, manifest_path)

        blocker_codes = {item["code"] for item in report["blockers"]}
        self.assertIn("restore_drill_missing", blocker_codes)
        self.assertEqual("missing", report["source_reports"]["backup_verification"]["status"])

    # ------------------------------------------------------------------ #
    # listing / show + no secret leakage
    # ------------------------------------------------------------------ #
    def test_list_and_show_surface_receipts_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "dragonwriter.ophelia.yml"
            manifest_path.write_text(_critical_manifest())
            _write_backup(runtime_root, "dragon-writer", with_checksums=True)
            plan = backup_verify_plan("dragon-writer", "production", runtime_root, manifest_path=manifest_path)
            receipt = backup_verify_apply(
                "dragon-writer",
                "production",
                runtime_root,
                confirm=str(plan["confirmation_token"]),
                manifest_path=manifest_path,
            )
            verify_id = receipt["verify_id"]

            listing = restore_drills_list("dragon-writer", "production", runtime_root)
            shown = restore_drills_show(verify_id, runtime_root)

        self.assertEqual("ophelia.restore_drills", listing["kind"])
        kinds = {item["kind"] for item in listing["drills"]}
        self.assertIn("backup-verification", kinds)
        self.assertEqual("ophelia.restore_drill", shown["kind"])
        self.assertEqual(verify_id, shown["drill_id"])
        self.assertEqual("succeeded", shown["drill"]["status"])

    def test_no_secret_canary_appears_in_any_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "dragonwriter.ophelia.yml"
            manifest_path.write_text(_critical_manifest())
            _write_backup(runtime_root, "dragon-writer", with_checksums=True, canary=True)

            plan = backup_verify_plan("dragon-writer", "production", runtime_root, manifest_path=manifest_path)
            receipt = backup_verify_apply(
                "dragon-writer",
                "production",
                runtime_root,
                confirm=str(plan["confirmation_token"]),
                manifest_path=manifest_path,
            )
            verify_id = receipt["verify_id"]
            listing = restore_drills_list("dragon-writer", "production", runtime_root)
            shown = restore_drills_show(verify_id, runtime_root)

        for payload in (plan, receipt, listing, shown):
            self.assertNotIn("CANARYSECRETVALUE", json.dumps(payload))

    def test_show_missing_receipt_is_blocker_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            shown = restore_drills_show("does-not-exist", runtime_root)
        self.assertEqual("restore_drill_not_found", shown["blockers"][0]["code"])

    def test_list_skips_malformed_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            drills_dir = runtime_root / "apps" / "dragon-writer" / "restore-drills"
            drills_dir.mkdir(parents=True)
            (drills_dir / "broken.json").write_text("{not valid json")

            listing = restore_drills_list("dragon-writer", "production", runtime_root)

        self.assertEqual("ophelia.restore_drills", listing["kind"])
        self.assertEqual([], listing["drills"])


# --------------------------------------------------------------------------- #
# fixtures (mirror tests/test_portability.py)
# --------------------------------------------------------------------------- #


def _snapshot(root: Path) -> set:
    if not root.exists():
        return set()
    return {str(path.relative_to(root)) for path in root.rglob("*")}


def _backup_dir(runtime_root: Path, app: str) -> Path:
    return runtime_root / "backups" / "apps" / app / "20260620T120000Z-fixture"


def _write_backup(runtime_root: Path, app: str, *, with_checksums: bool, canary: bool = True) -> None:
    backup_root = _backup_dir(runtime_root, app)
    (backup_root / "runtime").mkdir(parents=True)
    env_value = CANARY if canary else "plain-value"
    (backup_root / "runtime" / "env").write_text(f"DATABASE_URL={env_value}\nOPHELIA_APP={app}\n")
    with tarfile.open(backup_root / "data.tar", "w") as handle:
        handle.add(backup_root / "runtime" / "env", arcname="env")
    (backup_root / "backup-manifest.json").write_text(
        json.dumps(
            {
                "backup_id": "20260620T120000Z-fixture",
                "app": app,
                "created_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=10)),
                "coverage": {"app_env": True, "release_metadata": True},
                "database": {"postgres": True, "mode": "metadata-only"},
            }
        )
        + "\n"
    )
    if with_checksums:
        lines = []
        for relative in ("runtime/env", "data.tar", "backup-manifest.json"):
            target = backup_root / relative
            lines.append(f"{hashlib.sha256(target.read_bytes()).hexdigest()}  {relative}")
        (backup_root / "checksums.sha256").write_text("\n".join(lines) + "\n")


def _write_release(app_root: Path) -> None:
    release = {"release_id": "rel-fixture", "image": "ghcr.io/example/dragon-writer@sha256:aaaaaaaa"}
    (app_root / "release.json").write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
    (app_root / "active_release.json").write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")


def _write_filled_env(app_root: Path) -> None:
    (app_root / "env").write_text("OPHELIA_APP=dragon-writer\n")


def _write_verification_receipt(runtime_root: Path, app: str, environment: str, *, status: str) -> None:
    drills_dir = runtime_root / "apps" / app / "restore-drills"
    drills_dir.mkdir(parents=True, exist_ok=True)
    (drills_dir / "verify-fixture.json").write_text(
        json.dumps(
            {
                "kind": "ophelia.receipt",
                "operation": "backup.verify.apply",
                "operation_id": "backup.verify.apply.dragon-writer.production.fixture",
                "verify_id": "verify-20260621T120000000000Z-fixture",
                "status": status,
                "app": app,
                "environment": environment,
                "backup_id": "20260620T120000Z-fixture",
                "completed_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=1)),
            }
        )
        + "\n"
    )


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _critical_manifest() -> str:
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
  backups:
    required: true
    restore_drill_required: true
    offsite_required: true
verify:
  - name: health
    url: https://dragonwriter.begam.in/health
""".strip() + "\n"


def _standard_manifest() -> str:
    return """
version: 1
app: standard-app
environment: production
kind: service
image: ghcr.io/example/standard-app@sha256:bbbbbbbb
pack:
  portability: standard
  owner: personal
host_requirements:
  min_disk_free: 1b
services:
  web:
    port: 3000
routes:
  - domain: standard-app.example.com
    service: web
data:
  backups:
    required: true
verify:
  - name: health
    url: https://standard-app.example.com/health
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
