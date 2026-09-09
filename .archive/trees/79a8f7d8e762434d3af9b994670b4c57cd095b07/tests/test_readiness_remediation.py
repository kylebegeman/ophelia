from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.manifest import load_manifest
from ophelia.portability import (
    READINESS_SCORE_WEIGHTS,
    app_readiness_report,
    pack_validation_report,
)
from ophelia.remediation import remediation_for
from ophelia.runtime import deploy_bundle


class ReadinessRemediationTests(unittest.TestCase):
    def test_critical_app_without_restore_drill_blocks_with_remediation(self) -> None:
        with _skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = root / "demo-service.ophelia.yml"
                manifest_path.write_text(_critical_manifest())
                manifest = load_manifest(manifest_path)
                app_root = deploy_bundle(manifest, manifest_path, runtime_root)
                release = json.loads((app_root / "release.json").read_text())
                (app_root / "active_release.json").write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
                _write_filled_env(app_root)
                _write_backup(runtime_root, "demo-service")
                # Intentionally NO restore-drill receipt.

                report = app_readiness_report("demo-service", "production", runtime_root, manifest_path)

        self.assertEqual("blocked", report["readiness_level"])
        blockers_by_code = {item["code"]: item for item in report["blockers"]}
        self.assertIn("restore_drill_missing", blockers_by_code)
        remediation = blockers_by_code["restore_drill_missing"].get("remediation")
        self.assertIsInstance(remediation, dict)
        self.assertTrue(remediation["requires_human_approval"])
        self.assertTrue(
            any(
                command.startswith("ship app restore-drill plan demo-service --environment production")
                and command.endswith("--json")
                for command in remediation["commands"]
            ),
            remediation["commands"],
        )
        # Original keys preserved.
        self.assertEqual("restore_drill_missing", blockers_by_code["restore_drill_missing"]["code"])
        self.assertIn("message", blockers_by_code["restore_drill_missing"])
        # next_actions surfaces the blocker first with its first command.
        next_codes = [action["code"] for action in report["next_actions"]]
        self.assertIn("restore_drill_missing", next_codes)
        first_blocker_action = next(action for action in report["next_actions"] if action["area"] == "blocker")
        self.assertTrue(first_blocker_action["command"].endswith("--json"))
        # Values stay redacted.
        self.assertNotIn("filled", json.dumps(report["source_reports"]))

    def test_static_app_scores_high_without_db_penalty(self) -> None:
        with _skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                static_root = root / "static-site"
                static_root.mkdir()
                (static_root / "index.html").write_text("<html></html>\n")
                manifest_path = root / "static.ophelia.yml"
                manifest_path.write_text(_static_manifest(static_root))
                manifest = load_manifest(manifest_path)
                app_root = deploy_bundle(manifest, manifest_path, runtime_root)
                release = json.loads((app_root / "release.json").read_text())
                (app_root / "active_release.json").write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
                _write_filled_env(app_root)

                report = app_readiness_report("static-portable", "staging", runtime_root, manifest_path)

        score = report["portability_score"]["score"]
        # A static app has no postgres/redis, no required backups, and no restore
        # drill requirement, so none of those factors should be penalized wrongly.
        self.assertGreaterEqual(score, 85, report["portability_score"]["factors"])
        factors = {factor["name"]: factor for factor in report["portability_score"]["factors"]}
        self.assertTrue(factors["explicit_data_contracts"]["ok"])
        self.assertTrue(factors["backup_fresh"]["ok"])
        self.assertTrue(factors["restore_drill"]["ok"])

    def test_score_details_sum_to_score_and_max_points_total_100(self) -> None:
        with _skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                # Fixture A: critical app, fully ready (drill present).
                crit_path = root / "demo-service.ophelia.yml"
                crit_path.write_text(_critical_manifest())
                crit_manifest = load_manifest(crit_path)
                crit_app_root = deploy_bundle(crit_manifest, crit_path, runtime_root)
                crit_release = json.loads((crit_app_root / "release.json").read_text())
                (crit_app_root / "active_release.json").write_text(json.dumps(crit_release, indent=2, sort_keys=True) + "\n")
                _write_filled_env(crit_app_root)
                _write_backup(runtime_root, "demo-service")
                _write_restore_drill(crit_app_root, "demo-service")

                # Fixture B: static app.
                static_root = root / "static-site"
                static_root.mkdir()
                (static_root / "index.html").write_text("<html></html>\n")
                static_path = root / "static.ophelia.yml"
                static_path.write_text(_static_manifest(static_root))
                static_manifest = load_manifest(static_path)
                static_app_root = deploy_bundle(static_manifest, static_path, runtime_root)
                static_release = json.loads((static_app_root / "release.json").read_text())
                (static_app_root / "active_release.json").write_text(json.dumps(static_release, indent=2, sort_keys=True) + "\n")
                _write_filled_env(static_app_root)

                crit_report = app_readiness_report("demo-service", "production", runtime_root, crit_path)
                static_report = app_readiness_report("static-portable", "staging", runtime_root, static_path)

        for report in (crit_report, static_report):
            details = report["score_details"]
            total_points = sum(int(bucket["points"]) for bucket in details.values())
            total_max = sum(int(bucket["max_points"]) for bucket in details.values())
            self.assertEqual(
                report["portability_score"]["score"],
                total_points,
                f"{report['app']}: category points must sum to score",
            )
            self.assertEqual(100, total_max, f"{report['app']}: max_points must sum to 100")
            self.assertEqual(sum(READINESS_SCORE_WEIGHTS.values()), total_max)
            for bucket in details.values():
                self.assertIn("reason", bucket)

    def test_inferred_data_contracts_lower_score_and_legacy_manifest_loads(self) -> None:
        with _skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                # Addon-only (inferred) contracts: explicit_data_contracts factor fails.
                addon_path = root / "addon.ophelia.yml"
                addon_path.write_text(_addon_manifest())
                addon_manifest = load_manifest(addon_path)
                addon_app_root = deploy_bundle(addon_manifest, addon_path, runtime_root)
                addon_release = json.loads((addon_app_root / "release.json").read_text())
                (addon_app_root / "active_release.json").write_text(json.dumps(addon_release, indent=2, sort_keys=True) + "\n")
                _write_filled_env(addon_app_root)

                # Legacy manifest with NO data: block at all still loads/reports.
                legacy_path = root / "legacy.ophelia.yml"
                legacy_path.write_text(_legacy_no_data_manifest())
                legacy_manifest = load_manifest(legacy_path)
                legacy_app_root = deploy_bundle(legacy_manifest, legacy_path, runtime_root)
                legacy_release = json.loads((legacy_app_root / "release.json").read_text())
                (legacy_app_root / "active_release.json").write_text(json.dumps(legacy_release, indent=2, sort_keys=True) + "\n")
                _write_filled_env(legacy_app_root)

                addon_report = app_readiness_report("addon-portable", "production", runtime_root, addon_path)
                legacy_report = app_readiness_report("legacy-portable", "production", runtime_root, legacy_path)

        addon_factors = {f["name"]: f for f in addon_report["portability_score"]["factors"]}
        self.assertFalse(addon_factors["explicit_data_contracts"]["ok"])
        # Inferred contracts cost the full data weight.
        self.assertEqual(
            READINESS_SCORE_WEIGHTS["explicit_data_contracts"],
            addon_report["score_details"]["data"]["max_points"] - addon_report["score_details"]["data"]["points"],
        )

        # Legacy manifest reports without error and the data factor is not falsely penalized.
        self.assertIn("portability_score", legacy_report)
        legacy_factors = {f["name"]: f for f in legacy_report["portability_score"]["factors"]}
        self.assertTrue(legacy_factors["explicit_data_contracts"]["ok"])
        legacy_details = legacy_report["score_details"]
        self.assertEqual(
            legacy_report["portability_score"]["score"],
            sum(int(bucket["points"]) for bucket in legacy_details.values()),
        )

    def test_source_reports_include_secrets_audit_pointer(self) -> None:
        with _skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = root / "demo-service.ophelia.yml"
                manifest_path.write_text(_critical_manifest())
                manifest = load_manifest(manifest_path)
                app_root = deploy_bundle(manifest, manifest_path, runtime_root)
                _write_filled_env(app_root)
                _write_backup(runtime_root, "demo-service")

                report = app_readiness_report("demo-service", "production", runtime_root, manifest_path)

        sources = report["source_reports"]
        self.assertIn("secrets_audit", sources)
        self.assertIn("status", sources["secrets_audit"])
        self.assertTrue(sources["secrets_audit"]["values_redacted"])
        self.assertIn("env_shape", sources)
        self.assertIn("backup_status", sources)
        self.assertIn("route_conflicts", sources)
        self.assertIn("restore_drill", sources)

    def test_pack_validation_exposes_score_details_rollup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "demo-service.ophelia.yml"
            manifest_path.write_text(_critical_manifest())
            manifest = load_manifest(manifest_path)
            report = pack_validation_report(manifest, manifest_path, manifest_dir=root)

        self.assertIn("score_details", report)
        details = report["score_details"]
        self.assertIn("runtime", details)
        self.assertIn("data", details)
        for bucket in details.values():
            self.assertLessEqual(int(bucket["points"]), int(bucket["max_points"]))
            self.assertIn("reason", bucket)


def _skip_docker_status() -> "_EnvPatch":
    return _EnvPatch("OPHELIA_SKIP_DOCKER_STATUS", "1")


class _EnvPatch:
    def __init__(self, key: str, value: str) -> None:
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


def _write_restore_drill(app_root: Path, app: str) -> None:
    drill_root = app_root / "restore-drills"
    drill_root.mkdir(exist_ok=True)
    (drill_root / "successful-drill.json").write_text(
        json.dumps(
            {
                "operation": "app.restore-drill.apply",
                "operation_id": f"app.restore-drill.apply.{app}.production.fixture",
                "status": "succeeded",
                "app": app,
                "environment": "production",
            }
        )
        + "\n"
    )


def _critical_manifest() -> str:
    return """
version: 1
app: demo-service
environment: production
kind: service
image: ghcr.io/example/demo-service@sha256:aaaaaaaa
pack:
  portability: critical
  owner: personal
host_requirements:
  min_disk_free: 1b
services:
  web:
    port: 3000
routes:
  - domain: demo-service.example.net
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
    url: https://demo-service.example.net/health
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


def _addon_manifest() -> str:
    return """
version: 1
app: addon-portable
environment: production
kind: service
image: ghcr.io/example/addon-portable@sha256:bbbbbbbb
pack:
  portability: standard
  owner: personal
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


def _legacy_no_data_manifest() -> str:
    return """
version: 1
app: legacy-portable
environment: production
kind: service
image: ghcr.io/example/legacy-portable@sha256:cccccccc
pack:
  portability: standard
  owner: personal
services:
  web:
    port: 3000
routes:
  - domain: legacy-portable.example.com
    service: web
verify:
  - name: health
    url: https://legacy-portable.example.com/health
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
