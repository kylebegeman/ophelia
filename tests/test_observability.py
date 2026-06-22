from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia import observability as obs
from ophelia.lumen_adapter import dashboard_data
from ophelia.manifest import ManifestError, load_manifest


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _manifest_text(*, health_url: str | None = None, with_metrics: bool = False, with_secret_env: bool = False) -> str:
    lines = [
        "version: 1",
        "app: demo-service",
        "environment: production",
        "kind: service",
        "image: ghcr.io/example/demo-service:latest",
        "services:",
        "  web:",
        "    port: 3000",
        "routes:",
        "  - domain: demo-service.example.test",
        "    service: web",
        "data:",
        "  backups:",
        "    required: true",
    ]
    obs_lines: list[str] = []
    if health_url is not None:
        obs_lines += ["  health:", f"    url: {health_url}"]
    if with_metrics:
        obs_lines += ["  metrics:", "    url: http://web:3000/metrics", "    format: prometheus", "    auth: bearer_env"]
    if obs_lines:
        lines.append("observability:")
        lines.extend(obs_lines)
    if with_secret_env:
        lines += ["env:", "  API_TOKEN: super-secret-value", "  DATABASE_URL: postgres://user:secret@db/app"]
    return "\n".join(lines) + "\n"


def _seed_runtime(root: Path, *, with_backup: bool, with_failed_receipt: bool) -> Path:
    runtime_root = root / "runtime"
    app_root = runtime_root / "apps" / "demo-service"
    (app_root / "receipts").mkdir(parents=True)
    if with_backup:
        backup_root = runtime_root / "backups" / "apps" / "demo-service" / "20260620T120000Z-fixture"
        backup_root.mkdir(parents=True)
        (backup_root / "backup-manifest.json").write_text(
            json.dumps(
                {
                    "backup_id": "20260620T120000Z-fixture",
                    "app": "demo-service",
                    "created_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=10)),
                    "coverage": {"app_env": True},
                }
            )
            + "\n"
        )
    if with_failed_receipt:
        (app_root / "receipts" / "deploy-failed.json").write_text(
            json.dumps(
                {
                    "kind": "ophelia.receipt",
                    "operation": "app.deploy.apply",
                    "operation_id": "deploy-failed-1",
                    "app": "demo-service",
                    "environment": "production",
                    "status": "failed",
                    "started_at": _iso(datetime.now(timezone.utc)),
                }
            )
            + "\n"
        )
    return runtime_root


class ValidateHealthUrlTests(unittest.TestCase):
    def test_accepts_clean_https(self) -> None:
        self.assertIsNone(obs.validate_health_url("https://h/health"))

    def test_rejects_credentials(self) -> None:
        self.assertIsNotNone(obs.validate_health_url("https://user:pass@h/health"))

    def test_rejects_query(self) -> None:
        self.assertIsNotNone(obs.validate_health_url("https://h/health?x=1"))

    def test_rejects_fragment(self) -> None:
        self.assertIsNotNone(obs.validate_health_url("https://h/health#f"))

    def test_rejects_non_http_scheme(self) -> None:
        self.assertIsNotNone(obs.validate_health_url("ftp://h/health"))
        self.assertIsNotNone(obs.validate_health_url("file:///etc/passwd"))


class ManifestObservabilityTests(unittest.TestCase):
    def test_credentialed_health_url_rejected_at_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.ophelia.yml"
            path.write_text(_manifest_text(health_url="https://user:pass@web:3000/health"))
            with self.assertRaises(ManifestError):
                load_manifest(path)

    def test_query_string_health_url_rejected_at_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.ophelia.yml"
            path.write_text(_manifest_text(health_url="https://web:3000/health?token=abc"))
            with self.assertRaises(ManifestError):
                load_manifest(path)

    def test_clean_health_url_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.ophelia.yml"
            path.write_text(_manifest_text(health_url="https://web:3000/health"))
            manifest = load_manifest(path)
            self.assertIsNotNone(manifest.observability.health)
            self.assertEqual("https://web:3000/health", manifest.observability.health.url)

    def test_invalid_metrics_format_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.ophelia.yml"
            text = _manifest_text() + "observability:\n  metrics:\n    url: http://web:3000/m\n    format: graphite\n"
            path.write_text(text)
            with self.assertRaises(ManifestError):
                load_manifest(path)

    def test_manifest_without_observability_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.ophelia.yml"
            path.write_text(_manifest_text())
            manifest = load_manifest(path)
            self.assertIsNone(manifest.observability.health)
            self.assertIsNone(manifest.observability.metrics)


class ObservabilityStatusTests(unittest.TestCase):
    def test_default_makes_no_network_call_and_surfaces_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest_text(health_url="https://web:3000/health"))
            runtime_root = _seed_runtime(root, with_backup=True, with_failed_receipt=True)

            # Guard: any attempt to open a socket would raise; the default path
            # must not touch the network.
            import urllib.request as urlreq

            original = urlreq.urlopen

            def _boom(*args, **kwargs):  # pragma: no cover - must not be called
                raise AssertionError("observability_status made a network call by default")

            urlreq.urlopen = _boom
            try:
                report = obs.observability_status(
                    "demo-service",
                    environment="production",
                    runtime_root=runtime_root,
                    manifest_path=manifest_path,
                )
            finally:
                urlreq.urlopen = original

            # Parseable JSON round-trip.
            json.loads(json.dumps(report))
            self.assertEqual("ophelia.observability_status", report["kind"])
            self.assertFalse(report["probed_http"])
            self.assertIsNone(report["health"]["probe"])
            self.assertEqual("fresh", report["backups"]["freshness"])
            self.assertEqual(1, report["receipts_failures"]["count"])
            self.assertTrue(report["health"]["configured"])

    def test_probe_http_times_out_cleanly_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "app.ophelia.yml"
            # Guaranteed-unreachable address: port 1 on loopback.
            manifest_path.write_text(_manifest_text(health_url="http://127.0.0.1:1/health"))
            runtime_root = _seed_runtime(root, with_backup=False, with_failed_receipt=False)

            report = obs.observability_status(
                "demo-service",
                environment="production",
                runtime_root=runtime_root,
                manifest_path=manifest_path,
                probe_http=True,
                http_timeout=0.25,
            )

            self.assertTrue(report["probed_http"])
            self.assertIsNotNone(report["health"]["probe"])
            self.assertFalse(report["health"]["probe"]["ok"])
            codes = {w.get("code") for w in report["warnings"]}
            self.assertIn("observability_health_probe_failed", codes)
            self.assertEqual("warning", report["status"])

    def test_probe_http_monkeypatched_error_records_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest_text(health_url="https://web:3000/health"))
            runtime_root = _seed_runtime(root, with_backup=False, with_failed_receipt=False)

            import urllib.request as urlreq

            original = urlreq.urlopen

            def _timeout(*args, **kwargs):
                raise TimeoutError("timed out")

            urlreq.urlopen = _timeout
            try:
                report = obs.observability_status(
                    "demo-service",
                    environment="production",
                    runtime_root=runtime_root,
                    manifest_path=manifest_path,
                    probe_http=True,
                    http_timeout=1.0,
                )
            finally:
                urlreq.urlopen = original

            self.assertEqual("timeout", report["health"]["probe"]["reason"])

    def test_missing_metrics_endpoint_is_warning_not_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "app.ophelia.yml"
            # Declare a metrics block with format none and no URL.
            text = _manifest_text() + "observability:\n  metrics:\n    format: none\n"
            manifest_path.write_text(text)
            runtime_root = _seed_runtime(root, with_backup=False, with_failed_receipt=False)

            report = obs.observability_status(
                "demo-service",
                environment="production",
                runtime_root=runtime_root,
                manifest_path=manifest_path,
            )
            codes = {w.get("code") for w in report["warnings"]}
            self.assertIn("observability_metrics_endpoint_missing", codes)
            self.assertEqual([], report["blockers"])

    def test_no_secret_leak_in_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(
                _manifest_text(health_url="https://web:3000/health", with_secret_env=True)
            )
            runtime_root = _seed_runtime(root, with_backup=False, with_failed_receipt=False)

            report = obs.observability_status(
                "demo-service",
                environment="production",
                runtime_root=runtime_root,
                manifest_path=manifest_path,
            )
            text = json.dumps(report)
            self.assertNotIn("super-secret-value", text)
            self.assertNotIn("postgres://user:secret", text)


class ObservabilityPlanExportTests(unittest.TestCase):
    def test_plan_lists_monitors_and_no_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(
                _manifest_text(health_url="https://web:3000/health", with_metrics=True)
            )
            runtime_root = _seed_runtime(root, with_backup=False, with_failed_receipt=False)

            report = obs.observability_plan(
                "demo-service",
                environment="production",
                runtime_root=runtime_root,
                manifest_path=manifest_path,
            )
            self.assertEqual("ophelia.observability_plan", report["kind"])
            self.assertTrue(report["read_only"])
            types = {m["type"] for m in report["monitors"]}
            self.assertIn("http_health", types)
            self.assertIn("metrics", types)

    def test_export_snapshot_is_compact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest_text(health_url="https://web:3000/health"))
            runtime_root = _seed_runtime(root, with_backup=True, with_failed_receipt=True)

            report = obs.observability_export(
                "demo-service",
                environment="production",
                runtime_root=runtime_root,
                manifest_path=manifest_path,
            )
            self.assertEqual("ophelia.observability_export", report["kind"])
            snapshot = report["snapshot"]
            self.assertEqual(1, snapshot["receipt_failures"])
            self.assertEqual("fresh", snapshot["backup_freshness"])
            self.assertTrue(snapshot["health_configured"])
            # Snapshot is scalar-only: no nested dicts/lists.
            for value in snapshot.values():
                self.assertNotIsInstance(value, (dict, list))


class DashboardObservabilityTests(unittest.TestCase):
    def test_dashboard_includes_observability_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifests_dir = root / "manifests"
            manifests_dir.mkdir()
            (manifests_dir / "app.ophelia.yml").write_text(
                _manifest_text(health_url="https://web:3000/health", with_secret_env=True)
            )
            runtime_root = _seed_runtime(root, with_backup=True, with_failed_receipt=True)

            report = dashboard_data(runtime_root=runtime_root, manifests_dir=manifests_dir)
            apps = report.get("apps", [])
            self.assertTrue(apps)
            row = next(entry for entry in apps if entry["app"] == "demo-service")
            self.assertIsNotNone(row["observability"])
            self.assertTrue(row["observability"]["health_configured"])
            self.assertEqual("fresh", row["observability"]["backup_freshness"])
            # No secret leak in the whole dashboard payload.
            text = json.dumps(report)
            self.assertNotIn("super-secret-value", text)
            self.assertNotIn("postgres://user:secret", text)

    def test_dashboard_includes_traffic_and_top_level_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifests_dir = root / "manifests"
            manifests_dir.mkdir()
            (manifests_dir / "app.ophelia.yml").write_text(_manifest_text(health_url="https://web:3000/health"))
            runtime_root = _seed_runtime(root, with_backup=True, with_failed_receipt=False)

            report = dashboard_data(runtime_root=runtime_root, manifests_dir=manifests_dir)

            row = next(entry for entry in report["apps"] if entry["app"] == "demo-service")
            self.assertIsNotNone(row["traffic_status"])
            self.assertEqual("ok", row["traffic_status"]["status"])
            self.assertIsInstance(report["traffic_status"], dict)
            self.assertIsInstance(report["observability"], dict)
            self.assertEqual(1, report["traffic_status"]["app_count"])
            self.assertEqual(1, report["observability"]["app_count"])


class ObservabilityScheduleTests(unittest.TestCase):
    def test_schedule_run_writes_latest_and_timestamped_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifests_dir = root / "manifests"
            manifests_dir.mkdir()
            (manifests_dir / "app.ophelia.yml").write_text(_manifest_text(health_url="https://web:3000/health"))
            runtime_root = _seed_runtime(root, with_backup=True, with_failed_receipt=False)

            report = obs.observability_schedule_run(
                runtime_root=runtime_root,
                manifests_dir=manifests_dir,
            )

            self.assertEqual("ophelia.observability_schedule_run", report["kind"])
            self.assertEqual("succeeded", report["status"])
            self.assertEqual(1, report["totals"]["app_count"])
            self.assertEqual("demo-service", report["apps"][0]["app"])
            latest = runtime_root / "observability" / "latest.json"
            self.assertTrue(latest.exists())
            run_artifacts = list((runtime_root / "observability" / "runs").glob("*.json"))
            self.assertEqual(1, len(run_artifacts))
            json.loads(latest.read_text())

    def test_schedule_run_does_not_probe_http_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifests_dir = root / "manifests"
            manifests_dir.mkdir()
            (manifests_dir / "app.ophelia.yml").write_text(_manifest_text(health_url="https://web:3000/health"))
            runtime_root = _seed_runtime(root, with_backup=True, with_failed_receipt=False)

            import urllib.request as urlreq

            original = urlreq.urlopen

            def _boom(*args, **kwargs):  # pragma: no cover - must not be called
                raise AssertionError("schedule run made a network call by default")

            urlreq.urlopen = _boom
            try:
                report = obs.observability_schedule_run(
                    runtime_root=runtime_root,
                    manifests_dir=manifests_dir,
                )
            finally:
                urlreq.urlopen = original

            self.assertFalse(report["probed_http"])


if __name__ == "__main__":
    unittest.main()
