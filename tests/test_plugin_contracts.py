from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ophelia import lumen_adapter  # noqa: E402
from ophelia.command_catalog import command_registry  # noqa: E402
from ophelia.main import main  # noqa: E402
from ophelia.plugin_contracts import (  # noqa: E402
    PLUGIN_CATALOG_KIND,
    PLUGIN_VALIDATION_KIND,
    plugin_inventory,
    validate_plugin_manifest,
)


FIXTURE_PLUGINS = REPO_ROOT / "fixtures" / "app-suite" / "plugins"
FIXTURE_PLUGIN_MANIFEST = FIXTURE_PLUGINS / "fixture-app-suite" / "ophelia-plugin.yml"


class PluginContractTests(unittest.TestCase):
    def test_fixture_plugin_inventory_is_valid_and_redacted(self) -> None:
        report = plugin_inventory(FIXTURE_PLUGINS)
        serialized = json.dumps(report, sort_keys=True)
        self.assertEqual(PLUGIN_CATALOG_KIND, report["kind"])
        self.assertEqual("ok", report["status"])
        self.assertEqual(1, len(report["plugins"]))
        plugin = report["plugins"][0]
        self.assertEqual("fixture-app-suite", plugin["name"])
        self.assertFalse(plugin["enabled"])
        self.assertEqual(8, plugin["capability_counts"]["app_template"])
        self.assertEqual(1, plugin["capability_counts"]["provider_adapter"])
        self.assertEqual(1, plugin["capability_counts"]["secret_provider"])
        self.assertIn("fixture-live-readiness", {item["id"] for item in plugin["lumen_surfaces"]})
        self.assertNotIn("fixture-provider-token-value", serialized)

    def test_plugin_validate_rejects_mutation_without_plan_and_unbounded_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plugin_dir = root / "bad-plugin"
            plugin_dir.mkdir()
            manifest = plugin_dir / "ophelia-plugin.yml"
            manifest.write_text(
                """
schema_version: 1
kind: ophelia.plugin_manifest
name: bad-plugin
version: 0.1.0
enabled_by_default: false
commands:
  - command: ship bad apply
    operation: plugin.bad-plugin.apply
    summary: Unsafe mutating command.
    risk: high
    mutates_state: true
    requires_confirmation: false
    args_schema:
      type: object
      properties: {}
      additionalProperties: true
""".strip()
                + "\n"
            )
            report = validate_plugin_manifest(manifest, trusted_root=root)

        self.assertEqual(PLUGIN_VALIDATION_KIND, report["kind"])
        self.assertEqual("blocked", report["status"])
        codes = {item["code"] for item in report["blockers"]}
        self.assertIn("plugin_mutation_without_plan", codes)
        self.assertIn("plugin_args_schema_unbounded", codes)

    def test_plugin_validate_rejects_literal_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plugin_dir = root / "secret-plugin"
            plugin_dir.mkdir()
            manifest = plugin_dir / "ophelia-plugin.yml"
            manifest.write_text(
                """
schema_version: 1
kind: ophelia.plugin_manifest
name: secret-plugin
version: 0.1.0
enabled_by_default: false
metadata:
  api_token: literal-token-value
""".strip()
                + "\n"
            )
            report = validate_plugin_manifest(manifest, trusted_root=root)

        self.assertEqual("blocked", report["status"])
        codes = {item["code"] for item in report["blockers"]}
        self.assertIn("plugin_literal_secret_forbidden", codes)
        self.assertNotIn("literal-token-value", json.dumps(report))

    def test_plugin_validate_rejects_manifest_outside_trusted_root(self) -> None:
        report = validate_plugin_manifest(FIXTURE_PLUGIN_MANIFEST, trusted_root=REPO_ROOT / "plugins")
        self.assertEqual("blocked", report["status"])
        codes = {item["code"] for item in report["blockers"]}
        self.assertIn("plugin_manifest_outside_trusted_root", codes)

    def test_plugin_commands_are_not_injected_into_executable_catalog(self) -> None:
        operations = {descriptor.operation for descriptor in command_registry()}
        self.assertIn("plugins.list", operations)
        self.assertIn("plugins.catalog", operations)
        self.assertIn("plugins.validate", operations)
        self.assertNotIn("plugin.fixture-app-suite.live-readiness", operations)

    def test_plugins_cli_catalog_emits_json(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(["plugins", "catalog", "--plugins-dir", str(FIXTURE_PLUGINS), "--json"])
        self.assertEqual(0, exit_code)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(PLUGIN_CATALOG_KIND, payload["kind"])
        self.assertEqual("ok", payload["status"])

    def test_lumen_capabilities_include_plugin_inventory(self) -> None:
        report = lumen_adapter.capabilities()
        self.assertIn("plugins", report["surfaces"])
        self.assertIn("plugins", report)
        self.assertEqual(PLUGIN_CATALOG_KIND, report["plugins"]["kind"])


if __name__ == "__main__":
    unittest.main()

