from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ophelia.api_routes import HTTP_ROUTE_PATTERNS  # noqa: E402
from ophelia.command_catalog import command_registry  # noqa: E402
from ophelia.live_hydration import LIVE_HYDRATION_KIND, live_hydration_report  # noqa: E402
from ophelia.main import main  # noqa: E402


FIXTURE_PROFILES = REPO_ROOT / "fixtures" / "app-suite" / "live-drills.yml"
LOCAL_PROFILES = REPO_ROOT / "config" / "ophelia-live-drills.yml"


class LiveHydrationTests(unittest.TestCase):
    def test_fixture_postgres_hydration_reports_only_drift_step(self) -> None:
        report = live_hydration_report(profile="fixture-postgres-focused", profiles_path=FIXTURE_PROFILES)
        serialized = json.dumps(report, sort_keys=True)

        self.assertEqual(LIVE_HYDRATION_KIND, report["kind"])
        self.assertEqual("warning", report["status"])
        self.assertTrue(report["read_only"])
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["mutates_state"])
        self.assertEqual("fixture-postgres-api", report["app"])
        self.assertEqual("ok", report["sections"]["env"]["status"])
        self.assertEqual("ok", report["sections"]["secrets"]["status"])
        self.assertEqual("ok", report["sections"]["release"]["status"])
        self.assertEqual("ready", report["sections"]["host"]["status"])
        self.assertEqual(["refresh_or_review_runtime_drift", "rerun_live_profile"], [step["code"] for step in report["hydration_steps"]])
        self.assertIn("ship drift", report["hydration_steps"][0]["target"])
        self.assertIn("fixture-postgres-api.ophelia.yml", report["hydration_steps"][0]["target"])
        self.assertNotIn("fixture-postgres-token-value", serialized)
        self.assertNotIn("fixture-db-password", serialized)

    def test_local_quark_staging_hydration_is_blocked_and_actionable(self) -> None:
        report = live_hydration_report(profile="quark-ops-staging-file-baseline", profiles_path=LOCAL_PROFILES)
        step_codes = {step["code"] for step in report["hydration_steps"]}

        self.assertEqual("blocked", report["status"])
        self.assertEqual("quark-ops-staging", report["app"])
        self.assertEqual("staging", report["environment"])
        self.assertEqual(8, report["sections"]["env"]["required_count"])
        self.assertEqual(8, len(report["sections"]["env"]["missing_required"]))
        self.assertEqual(8, len(report["sections"]["secrets"]["missing_required"]))
        self.assertFalse(report["sections"]["release"]["active_present"])
        self.assertIn("hydrate_runtime_env_shape", step_codes)
        self.assertIn("record_secret_name_observations", step_codes)
        self.assertIn("record_release_metadata", step_codes)
        self.assertIn("complete_host_capability_inventory", step_codes)

    def test_cli_allow_blocked_preserves_blocked_payload_with_zero_exit(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(
                [
                    "live-hydration",
                    "report",
                    "--profile",
                    "quark-ops-staging-file-baseline",
                    "--profiles",
                    str(LOCAL_PROFILES),
                    "--allow-blocked",
                    "--json",
                ]
            )

        payload = json.loads(buffer.getvalue())

        self.assertEqual(0, exit_code)
        self.assertEqual("blocked", payload["status"])
        self.assertEqual("quark-ops-staging", payload["app"])

    def test_cli_catalog_and_api_route(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(
                [
                    "live-hydration",
                    "report",
                    "--profile",
                    "fixture-postgres-focused",
                    "--profiles",
                    str(FIXTURE_PROFILES),
                    "--json",
                ]
            )

        payload = json.loads(buffer.getvalue())
        operations = {descriptor.operation for descriptor in command_registry()}
        descriptor = next(descriptor for descriptor in command_registry() if descriptor.operation == "live_hydration.report")

        self.assertEqual(0, exit_code)
        self.assertEqual(LIVE_HYDRATION_KIND, payload["kind"])
        self.assertIn("live_hydration.report", operations)
        self.assertTrue(any("config/ophelia-live-drills.yml" in example for example in descriptor.examples))
        self.assertIn("/live-hydration/<profile>", HTTP_ROUTE_PATTERNS)

    def test_profile_without_app_blocks(self) -> None:
        report = live_hydration_report(profile="local-file-baseline", profiles_path=LOCAL_PROFILES)
        self.assertEqual("blocked", report["status"])
        codes = {item["code"] for item in report["blockers"]}
        self.assertIn("live_hydration_profile_app_missing", codes)


if __name__ == "__main__":
    unittest.main()
