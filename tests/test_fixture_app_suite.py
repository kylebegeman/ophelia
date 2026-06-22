from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ophelia.command_catalog import command_registry  # noqa: E402
from ophelia.live_readiness import LIVE_READINESS_KIND, live_readiness_report  # noqa: E402
from ophelia.manifest import load_manifest  # noqa: E402


FIXTURE_ROOT = REPO_ROOT / "fixtures" / "app-suite"
MANIFESTS_DIR = FIXTURE_ROOT / "manifests"
RUNTIME_ROOT = FIXTURE_ROOT / "runtime"
HOST_CONFIG = FIXTURE_ROOT / "host-inventory.yml"
PROVIDER_CONFIG = FIXTURE_ROOT / "integrations.yml"


class FixtureAppSuiteTests(unittest.TestCase):
    def test_fixture_manifests_load_as_diverse_app_suite(self) -> None:
        manifests = [load_manifest(path) for path in sorted(MANIFESTS_DIR.glob("*.ophelia.yml"))]
        apps = {manifest.app for manifest in manifests}
        kinds = {manifest.kind for manifest in manifests}

        self.assertEqual(
            {
                "fixture-static-site",
                "fixture-http-service",
                "fixture-postgres-api",
                "fixture-redis-worker",
                "fixture-multi-service",
                "fixture-tunnel-app",
                "fixture-stateful-volume",
                "fixture-incomplete-app",
            },
            apps,
        )
        self.assertEqual({"static", "service", "multi-service", "tunnel"}, kinds)

    def test_fixture_live_readiness_is_redacted_read_only_and_mixed(self) -> None:
        before = _file_set(RUNTIME_ROOT)
        previous_skip_docker = os.environ.get("OPHELIA_SKIP_DOCKER_STATUS")
        os.environ["OPHELIA_SKIP_DOCKER_STATUS"] = "preserve-me"
        try:
            report = live_readiness_report(
                runtime_root=RUNTIME_ROOT,
                manifests_dir=MANIFESTS_DIR,
                ophelia_root=REPO_ROOT,
                host_config=HOST_CONFIG,
                provider_config=PROVIDER_CONFIG,
            )
            self.assertEqual("preserve-me", os.environ.get("OPHELIA_SKIP_DOCKER_STATUS"))
        finally:
            if previous_skip_docker is None:
                os.environ.pop("OPHELIA_SKIP_DOCKER_STATUS", None)
            else:
                os.environ["OPHELIA_SKIP_DOCKER_STATUS"] = previous_skip_docker

        after = _file_set(RUNTIME_ROOT)
        by_app = {entry["app"]: entry for entry in report["apps"]}
        serialized = json.dumps(report, sort_keys=True)

        self.assertEqual(before, after)
        self.assertEqual(LIVE_READINESS_KIND, report["kind"])
        self.assertTrue(report["read_only"])
        self.assertTrue(report["dry_run"])
        self.assertEqual("blocked", report["status"])
        self.assertEqual(8, report["totals"]["app_count"])
        self.assertGreaterEqual(report["totals"]["blocked"], 1)
        self.assertGreaterEqual(report["totals"]["warning"], 1)
        self.assertGreaterEqual(report["totals"]["drift"], 1)
        self.assertEqual("blocked", by_app["fixture-incomplete-app"]["status"])
        self.assertIn(by_app["fixture-postgres-api"]["status"], {"ok", "warning"})
        self.assertIn(
            "fixture-edge-a",
            by_app["fixture-postgres-api"]["checks"]["placement"]["recommended_hosts"],
        )

        for forbidden in (
            "fixture-token-value",
            "fixture-postgres-token-value",
            "fixture-db-password",
            "fixture-redis-password",
            "fixture-storage-token-value",
            "fixture-provider-token-value",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_fixture_live_readiness_make_target_command_exits_zero(self) -> None:
        result = subprocess.run(
            [
                str(REPO_ROOT / "cli" / "ship"),
                "live-readiness",
                "run",
                "--runtime-root",
                str(RUNTIME_ROOT),
                "--manifests-dir",
                str(MANIFESTS_DIR),
                "--host-config",
                str(HOST_CONFIG),
                "--provider-config",
                str(PROVIDER_CONFIG),
                "--allow-blocked",
                "--json",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.stderr, "")
        self.assertEqual(0, result.returncode)
        payload = json.loads(result.stdout)
        self.assertEqual(LIVE_READINESS_KIND, payload["kind"])
        self.assertEqual("blocked", payload["status"])

    def test_live_readiness_catalog_includes_fixture_harness_example(self) -> None:
        descriptor = next(
            item for item in command_registry() if item.operation == "live.readiness.run"
        )
        self.assertTrue(
            any("fixtures/app-suite/runtime" in example for example in descriptor.examples)
        )


def _file_set(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}


if __name__ == "__main__":
    unittest.main()

