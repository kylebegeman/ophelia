from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ophelia.api_routes import HTTP_ROUTE_PATTERNS  # noqa: E402
from ophelia.command_catalog import command_registry  # noqa: E402
from ophelia.live_drills import (  # noqa: E402
    LIVE_DRILL_PROFILES_KIND,
    LIVE_DRILL_RESULT_KIND,
    LIVE_DRILL_RUNS_KIND,
    list_live_drill_profiles,
    run_all_live_drills,
    run_live_drill,
)
from ophelia.main import main  # noqa: E402


PROFILES = REPO_ROOT / "fixtures" / "app-suite" / "live-drills.yml"


class LiveDrillTests(unittest.TestCase):
    def test_fixture_profiles_list_and_run_expected_states(self) -> None:
        with _skip_docker_status():
            catalog = list_live_drill_profiles(PROFILES)
            suite = run_live_drill("fixture-suite-review", PROFILES)
            focused = run_live_drill("fixture-postgres-focused", PROFILES)

        self.assertEqual(LIVE_DRILL_PROFILES_KIND, catalog["kind"])
        self.assertEqual("ok", catalog["status"])
        self.assertEqual(4, len(catalog["profiles"]))

        self.assertEqual(LIVE_DRILL_RESULT_KIND, suite["kind"])
        self.assertEqual("ok", suite["status"])
        self.assertTrue(suite["read_only"])
        self.assertTrue(suite["dry_run"])
        self.assertFalse(suite["mutates_state"])
        self.assertEqual("blocked", suite["child_statuses"]["live_readiness"])
        self.assertEqual("warning", suite["child_statuses"]["hardening"])
        self.assertEqual(8, suite["reports"]["live_readiness"]["totals"]["app_count"])
        self.assertEqual(1, suite["reports"]["live_readiness"]["totals"]["blocked"])
        self.assertEqual("review", suite["reports"]["hardening"]["go_no_go"])
        self.assertTrue(all(check["ok"] for check in suite["expectation_checks"]))

        self.assertEqual("ok", focused["status"])
        self.assertEqual({"fixture-postgres-api": "warning"}, _check_actual(focused, "app_statuses"))

    def test_run_all_cli_catalog_api_and_redaction(self) -> None:
        buffer = io.StringIO()
        with _skip_docker_status(), contextlib.redirect_stdout(buffer):
            exit_code = main(["live-drills", "run-all", "--profiles", str(PROFILES), "--json"])

        payload = json.loads(buffer.getvalue())
        operations = {descriptor.operation for descriptor in command_registry()}
        descriptor = next(descriptor for descriptor in command_registry() if descriptor.operation == "live_drills.run_all")
        serialized = json.dumps(payload, sort_keys=True)

        self.assertEqual(0, exit_code)
        self.assertEqual(LIVE_DRILL_RUNS_KIND, payload["kind"])
        self.assertEqual("ok", payload["status"])
        self.assertEqual({"profile_count": 4, "ok": 4, "warning": 0, "blocked": 0}, payload["totals"])
        self.assertIn("live_drills.list", operations)
        self.assertIn("live_drills.run", operations)
        self.assertIn("live_drills.run_all", operations)
        self.assertTrue(any("fixtures/app-suite/live-drills.yml" in example for example in descriptor.examples))
        self.assertIn("/live-drills", HTTP_ROUTE_PATTERNS)
        self.assertIn("/live-drills/<profile>", HTTP_ROUTE_PATTERNS)
        self.assertNotIn("fixture-provider-token-value", serialized)
        self.assertNotIn("fixture-db-password", serialized)

    def test_missing_profile_blocks(self) -> None:
        with _skip_docker_status():
            report = run_live_drill("missing-profile", PROFILES)
        self.assertEqual("blocked", report["status"])
        self.assertEqual("live_drill_profile_not_found", report["blockers"][0]["code"])


def _check_actual(report: dict, name: str) -> object:
    for check in report["expectation_checks"]:
        if check["name"] == name:
            return check["actual"]
    raise AssertionError(f"missing check {name}")


@contextlib.contextmanager
def _skip_docker_status():
    previous = os.environ.get("OPHELIA_SKIP_DOCKER_STATUS")
    os.environ["OPHELIA_SKIP_DOCKER_STATUS"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("OPHELIA_SKIP_DOCKER_STATUS", None)
        else:
            os.environ["OPHELIA_SKIP_DOCKER_STATUS"] = previous


if __name__ == "__main__":
    unittest.main()
