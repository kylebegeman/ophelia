from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# First import must be the adapter, on its own, to prove there is no circular
# import: importing ophelia.lumen_adapter before any ophelia.commands.* module
# must succeed standalone.
import ophelia.lumen_adapter as lumen_adapter  # noqa: E402  (ordering is the assertion)

import json  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402

from ophelia import command_catalog  # noqa: E402
from ophelia.api_routes import HTTP_ROUTE_PATTERNS  # noqa: E402
from ophelia.manifest import load_manifest  # noqa: E402
from ophelia.runtime import deploy_bundle  # noqa: E402


class LumenImportTests(unittest.TestCase):
    def test_adapter_imports_standalone_without_circular_import(self) -> None:
        # The module object bound at the top of the file already proves the
        # import succeeded with no other ophelia.commands import preceding it.
        self.assertTrue(hasattr(lumen_adapter, "capabilities"))
        self.assertTrue(hasattr(lumen_adapter, "dashboard_data"))
        self.assertTrue(hasattr(lumen_adapter, "action_descriptors"))

    def test_adapter_import_pulls_in_no_command_modules_in_a_fresh_interpreter(self) -> None:
        # Rigorous, order-independent proof of no cycle: a fresh interpreter
        # whose ONLY ophelia import is ophelia.lumen_adapter must succeed and
        # must not pull in any ophelia.commands.* module (which would indicate a
        # top-level commands import or an import cycle resolved via commands).
        import subprocess

        src_dir = str(Path(__file__).resolve().parents[1] / "src")
        code = (
            "import sys\n"
            "import ophelia.lumen_adapter\n"
            "leaked = sorted(n for n in sys.modules if n.startswith('ophelia.commands.'))\n"
            "print('OK' if not leaked else 'LEAKED:' + ','.join(leaked))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            env={"PYTHONPATH": src_dir, "PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("OK", result.stdout.strip(), result.stdout)


class LumenContractTests(unittest.TestCase):
    def test_capabilities_is_json_serializable_with_stable_keys(self) -> None:
        report = lumen_adapter.capabilities()
        round_tripped = json.loads(json.dumps(report))
        self.assertEqual(lumen_adapter.CAPABILITIES_KIND, round_tripped["kind"])
        self.assertEqual(1, round_tripped["schema_version"])
        self.assertIn("commands", round_tripped)
        self.assertIn("count", round_tripped["commands"])
        self.assertIn("catalog", round_tripped["commands"])
        self.assertEqual(round_tripped["commands"]["count"], len(round_tripped["commands"]["catalog"]))
        self.assertIsInstance(round_tripped["http_endpoints"], list)
        self.assertEqual(HTTP_ROUTE_PATTERNS, round_tripped["http_endpoints"])
        for surface in (
            "catalog",
            "schema",
            "self_test",
            "providers",
            "secrets_audit",
            "readiness",
            "receipts_timeline",
            "state_db",
            "state_service",
            "policy",
            "workflows",
            "plugins",
            "console",
        ):
            self.assertIn(surface, round_tripped["surfaces"])

    def test_action_descriptors_reuse_shared_catalog(self) -> None:
        report = lumen_adapter.action_descriptors()
        round_tripped = json.loads(json.dumps(report))
        self.assertEqual(lumen_adapter.ACTION_DESCRIPTORS_KIND, round_tripped["kind"])
        self.assertEqual(1, round_tripped["schema_version"])
        # Sourced from the shared registry, not a copy: equal to catalog().
        self.assertEqual(command_catalog.catalog(), round_tripped["descriptors"])

    def test_dashboard_data_is_json_serializable_with_stable_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = lumen_adapter.dashboard_data(
                runtime_root=Path(tmp) / "runtime",
                manifests_dir=Path(tmp) / "manifests",
            )
        round_tripped = json.loads(json.dumps(report))
        self.assertEqual(lumen_adapter.DASHBOARD_KIND, round_tripped["kind"])
        self.assertEqual(1, round_tripped["schema_version"])
        self.assertIn("apps", round_tripped)
        self.assertIn("warnings", round_tripped)
        self.assertIn("totals", round_tripped)
        self.assertIn("state_service", round_tripped)
        self.assertIsInstance(round_tripped["traffic_status"], dict)
        self.assertIsInstance(round_tripped["observability"], dict)
        self.assertIsInstance(round_tripped["state_service"], dict)
        self.assertEqual(0, round_tripped["traffic_status"]["app_count"])
        self.assertEqual(0, round_tripped["observability"]["app_count"])

    def test_console_data_covers_fixture_suite_without_leaking_values(self) -> None:
        fixture_root = Path(__file__).resolve().parents[1] / "fixtures" / "app-suite"
        report = lumen_adapter.console_data(
            runtime_root=fixture_root / "runtime",
            manifests_dir=fixture_root / "manifests",
            plugins_dir=fixture_root / "plugins",
        )
        round_tripped = json.loads(json.dumps(report))
        self.assertEqual(lumen_adapter.CONSOLE_KIND, round_tripped["kind"])
        self.assertTrue(round_tripped["read_only"])
        self.assertFalse(round_tripped["mutates_state"])
        self.assertEqual(8, len(round_tripped["apps"]))
        readiness_card = next(card for card in round_tripped["overview"]["cards"] if card["id"] == "readiness")
        self.assertEqual(1, readiness_card["value"]["blocked"])
        self.assertEqual(7, readiness_card["value"]["warning"])
        self.assertTrue(round_tripped["approval_queue"])
        self.assertTrue(round_tripped["quick_actions"])
        self.assertEqual(1, round_tripped["plugins"]["plugin_count"])
        self.assertIn("overview", {item["id"] for item in round_tripped["navigation"]})
        serialized = json.dumps(round_tripped)
        self.assertNotIn("fixture-provider-token-value", serialized)
        self.assertNotIn("fixture-db-password", serialized)


class LumenDashboardSafetyTests(unittest.TestCase):
    def test_dashboard_data_never_leaks_secret_env_value(self) -> None:
        with _skip_docker_status():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runtime_root = root / "runtime"
                manifests_dir = root / "manifests"
                manifests_dir.mkdir()
                manifest_path = manifests_dir / "leakapp.ophelia.yml"
                manifest_path.write_text(_leak_manifest())
                manifest = load_manifest(manifest_path)
                app_root = deploy_bundle(manifest, manifest_path, runtime_root)
                # Env file carries a real-looking secret connection string. The
                # dashboard reads through env-shape/readiness, so it touches this
                # path; the value must never reach the JSON output.
                (app_root / "env").write_text(
                    "DATABASE_URL=postgres://u:LUMENLEAK@h/db\nAPP_ENV=production\n"
                )

                # Resolution searches Path.cwd() among other dirs; run from the
                # temp dir so the manifest resolves and the env file is read.
                previous_cwd = os.getcwd()
                os.chdir(manifests_dir)
                try:
                    report = lumen_adapter.dashboard_data(
                        runtime_root=runtime_root,
                        manifests_dir=manifests_dir,
                    )
                finally:
                    os.chdir(previous_cwd)

        serialized = json.dumps(report)
        self.assertNotIn("LUMENLEAK", serialized)
        self.assertNotIn("postgres://", serialized)
        # The app still shows up on the dashboard (resilience: data present).
        apps = {entry["app"] for entry in report["apps"]}
        self.assertIn("leak-app", apps)

    def test_dashboard_data_surfaces_warning_when_registry_fails(self) -> None:
        with _skip_docker_status():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runtime_root = root / "runtime"
                manifests_dir = root / "manifests"
                manifests_dir.mkdir()
                # A manifest that cannot be parsed is isolated per-file: the
                # dashboard surfaces it as a warning and never crashes (and any
                # well-formed sibling app would still render).
                (manifests_dir / "broken.ophelia.yml").write_text("this: : is: not: valid: yaml: [\n")

                report = lumen_adapter.dashboard_data(
                    runtime_root=runtime_root,
                    manifests_dir=manifests_dir,
                )

        # No crash, and the failure is recorded as a warning, not silently lost.
        self.assertTrue(report["warnings"], "expected a warning for the unreadable manifest")
        codes = {warning["code"] for warning in report["warnings"]}
        self.assertIn("manifest_registry_error", codes)

    def test_dashboard_data_surfaces_warning_when_app_subreport_fails(self) -> None:
        # A valid manifest entry whose readiness sub-report raises must become a
        # warning, never a crash, and the per-app row is still emitted.
        original = lumen_adapter.app_readiness_report

        def _boom(*_args, **_kwargs):
            raise RuntimeError("simulated sub-report failure")

        with _skip_docker_status():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runtime_root = root / "runtime"
                manifests_dir = root / "manifests"
                manifests_dir.mkdir()
                manifest_path = manifests_dir / "leakapp.ophelia.yml"
                manifest_path.write_text(_leak_manifest())
                lumen_adapter.app_readiness_report = _boom  # type: ignore[assignment]
                try:
                    report = lumen_adapter.dashboard_data(
                        runtime_root=runtime_root,
                        manifests_dir=manifests_dir,
                    )
                finally:
                    lumen_adapter.app_readiness_report = original  # type: ignore[assignment]

        codes = {warning["code"] for warning in report["warnings"]}
        self.assertIn("readiness_unavailable", codes)
        # The app row is still present despite its readiness sub-report failing.
        apps = {entry["app"] for entry in report["apps"]}
        self.assertIn("leak-app", apps)


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


def _leak_manifest() -> str:
    return """
version: 1
app: leak-app
environment: production
kind: service
image: ghcr.io/example/leak-app@sha256:dddddddd
pack:
  portability: standard
  owner: personal
services:
  web:
    port: 3000
    env:
      DATABASE_URL: postgres://u:LUMENLEAK@h/db
routes:
  - domain: leak-app.example.com
    service: web
verify:
  - name: health
    url: https://leak-app.example.com/health
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
