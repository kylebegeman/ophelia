"""Regression tests for fixes from the Phases 1-7 adversarial review."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.command_catalog import catalog
from ophelia.redaction import deep_redact
from ophelia.secrets_audit import secrets_audit
from ophelia.state_db import rebuild_state


class DeepRedactTests(unittest.TestCase):
    def test_masks_nested_connection_strings_and_sensitive_keys(self) -> None:
        payload = {
            "database": {"connection": "postgres://u:NESTEDSECRET@h/db", "mode": "metadata-only", "postgres": True},
            "items": [{"api_token": "tok-LEAK", "port": 8080}],
        }
        out = deep_redact(payload)
        blob = json.dumps(out)
        self.assertNotIn("NESTEDSECRET", blob)
        self.assertNotIn("tok-LEAK", blob)
        # Non-secret scalars preserved.
        self.assertEqual("metadata-only", out["database"]["mode"])
        self.assertEqual(True, out["database"]["postgres"])
        self.assertEqual(8080, out["items"][0]["port"])

    def test_preserves_plain_structure(self) -> None:
        payload = {"coverage": {"release_metadata": True, "app_env": True}}
        self.assertEqual(payload, deep_redact(payload))


class StateRebuildRobustnessTests(unittest.TestCase):
    def test_rebuild_does_not_crash_on_non_dict_release_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "rt"
            releases = root / "apps" / "x" / "releases"
            releases.mkdir(parents=True)
            (releases / "list.json").write_text("[1, 2, 3]")
            (releases / "null.json").write_text("null")
            (releases / "str.json").write_text('"hello"')
            result = rebuild_state(root, manifests_dir=root / "missing")
            self.assertNotEqual("error", result.get("status"))
            self.assertIn(result.get("status"), {"ok", "warn", "blocked"})


class CatalogPairingTests(unittest.TestCase):
    def test_plan_pairs_with_create_apply_form(self) -> None:
        by_op = {d["operation"]: d for d in catalog()}
        for plan_op, create_op in [("app.export.plan", "app.export.create"), ("backup.plan", "backup.create")]:
            self.assertIn(create_op, by_op)
            self.assertEqual(by_op[create_op]["plan_command"], by_op[plan_op]["plan_command"])
            self.assertEqual(by_op[create_op]["apply_command"], by_op[create_op]["apply_command"])
            self.assertIsNotNone(by_op[create_op]["plan_command"])
            self.assertIsNotNone(by_op[create_op]["apply_command"])

    def test_cli_only_descriptors_present_via_registry(self) -> None:
        commands = {d["command"] for d in catalog()}
        # CLI-only descriptors register via command-module import side effects;
        # command_registry() must force-load them so the catalog is complete.
        for expected in ["ship policy validate", "ship state status", "ship providers validate"]:
            self.assertIn(expected, commands)


class SecretsAuditProviderRefTests(unittest.TestCase):
    def test_missing_required_provider_ref_blocks_status(self) -> None:
        manifest = (
            "version: 1\n"
            "app: prov-audit\n"
            "kind: service\n"
            "image: ghcr.io/example/prov-audit@sha256:abc\n"
            "services:\n"
            "  web:\n"
            "    port: 3000\n"
            "routes:\n"
            "  - domain: prov.example.com\n"
            "    service: web\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "prov-audit.ophelia.yml"
            manifest_path.write_text(manifest)
            # A provider config next to the manifest declares a required env ref.
            (root / "providers.json").write_text(
                '{"dns":{"cloudflare":{"zone_id":"z","api_token_env":"CF_AUDIT_TOKEN"}}}'
            )
            report = secrets_audit(manifest_path, environment="production", runtime_root=root / "rt")
            names = {key["name"] for key in report["keys"]}
            self.assertIn("CF_AUDIT_TOKEN", names)
            # The required provider ref is reflected in status (not silently "ok").
            self.assertEqual("blocked", report["status"])
            codes = {b.get("code") for b in report["blockers"]}
            self.assertIn("required_provider_secret_missing", codes)
            # required_secrets_present check agrees with status.
            present_check = next(c for c in report["checks"] if c["name"] == "required_secrets_present")
            self.assertFalse(present_check["ok"])
            # Never a value, only the env-var name.
            self.assertNotIn("api_token", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
