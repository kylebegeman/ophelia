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
from ophelia.hardening import PRODUCTION_HARDENING_KIND, production_hardening_report  # noqa: E402
from ophelia.main import main  # noqa: E402


FIXTURE_ROOT = REPO_ROOT / "fixtures" / "app-suite"
MANIFESTS_DIR = FIXTURE_ROOT / "manifests"
RUNTIME_ROOT = FIXTURE_ROOT / "runtime"
HOST_CONFIG = FIXTURE_ROOT / "host-inventory.yml"
PROVIDER_CONFIG = FIXTURE_ROOT / "integrations.yml"
PLUGINS_DIR = FIXTURE_ROOT / "plugins"


class ProductionHardeningTests(unittest.TestCase):
    def test_fixture_report_is_read_only_review_and_redacted(self) -> None:
        with _skip_docker_status():
            report = production_hardening_report(
                runtime_root=RUNTIME_ROOT,
                manifests_dir=MANIFESTS_DIR,
                ophelia_root=REPO_ROOT,
                host_config=HOST_CONFIG,
                provider_config=PROVIDER_CONFIG,
                plugins_dir=PLUGINS_DIR,
                include_fixture_suite=True,
                allow_blocked_live_readiness=True,
            )

        serialized = json.dumps(report, sort_keys=True)
        checks = {check["name"]: check for check in report["checks"]}

        self.assertEqual(PRODUCTION_HARDENING_KIND, report["kind"])
        self.assertEqual("production.hardening", report["operation"])
        self.assertEqual("warning", report["status"])
        self.assertEqual("review", report["go_no_go"])
        self.assertTrue(report["read_only"])
        self.assertFalse(report["mutates_state"])
        self.assertFalse(report["confirmation_required"])
        self.assertFalse(report["safety"]["confirmation_tokens_accepted"])
        self.assertEqual([], report["blockers"])
        self.assertEqual(["live_readiness_blocked"], [item["code"] for item in report["warnings"]])
        self.assertTrue(checks["mutating_command_descriptors_guarded"]["ok"])
        self.assertTrue(checks["fixture_suite_expected_blocked_state"]["ok"])
        self.assertEqual(0, report["reports"]["catalog_safety"]["unguarded_count"])
        self.assertEqual(["fixture-incomplete-app"], report["reports"]["fixture_suite"]["blocked_apps"])
        self.assertEqual(8, report["reports"]["fixture_suite"]["console"]["app_count"])
        self.assertEqual(1, report["reports"]["plugins"]["plugin_count"])
        for forbidden in (
            "fixture-token-value",
            "fixture-postgres-token-value",
            "fixture-db-password",
            "fixture-redis-password",
            "fixture-storage-token-value",
            "fixture-provider-token-value",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_cli_emits_json_and_catalog_route_are_registered(self) -> None:
        buffer = io.StringIO()
        with _skip_docker_status(), contextlib.redirect_stdout(buffer):
            exit_code = main(
                [
                    "hardening",
                    "production-readiness",
                    "--runtime-root",
                    str(RUNTIME_ROOT),
                    "--manifests-dir",
                    str(MANIFESTS_DIR),
                    "--host-config",
                    str(HOST_CONFIG),
                    "--provider-config",
                    str(PROVIDER_CONFIG),
                    "--plugins-dir",
                    str(PLUGINS_DIR),
                    "--include-fixture-suite",
                    "--allow-blocked-live-readiness",
                    "--json",
                ]
            )

        payload = json.loads(buffer.getvalue())
        operations = {descriptor.operation for descriptor in command_registry()}
        descriptor = next(descriptor for descriptor in command_registry() if descriptor.operation == "production.hardening")

        self.assertEqual(0, exit_code)
        self.assertEqual(PRODUCTION_HARDENING_KIND, payload["kind"])
        self.assertEqual("warning", payload["status"])
        self.assertIn("production.hardening", operations)
        self.assertTrue(any("fixtures/app-suite/runtime" in example for example in descriptor.examples))
        self.assertIn("/hardening/production-readiness", HTTP_ROUTE_PATTERNS)


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
