from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.actions import action_catalog
from ophelia.command_catalog import catalog, command_registry
from ophelia.main import main

_SECRET_LIKE = ("secret", "token", "password", "private_key", "api_key", "database_url", "redis_url")


class CommandCatalogTests(unittest.TestCase):
    def test_every_descriptor_has_operation(self) -> None:
        for descriptor in command_registry():
            self.assertTrue(descriptor.operation, f"{descriptor.command} has empty operation")

    def test_mutating_descriptors_have_plan_or_confirmation(self) -> None:
        for descriptor in command_registry():
            if descriptor.mutates_state:
                self.assertTrue(
                    descriptor.plan_command is not None or descriptor.requires_confirmation,
                    f"{descriptor.command} mutates state without a plan command or confirmation",
                )

    def test_no_raw_secret_default_in_args_schema(self) -> None:
        for descriptor in command_registry():
            properties = (descriptor.args_schema or {}).get("properties", {})
            for prop_name, spec in properties.items():
                lowered = prop_name.lower()
                if any(token in lowered for token in _SECRET_LIKE):
                    self.assertNotIn(
                        "default",
                        spec,
                        f"{descriptor.command} args_schema property {prop_name} has a default",
                    )

    def test_catalog_round_trips_through_json(self) -> None:
        data = catalog()
        self.assertEqual(json.loads(json.dumps(data)), data)

    def test_catalog_sorted_by_command(self) -> None:
        commands = [item["command"] for item in catalog()]
        self.assertEqual(commands, sorted(commands))

    def test_every_action_id_is_a_descriptor_operation(self) -> None:
        action_ids = {item["id"] for item in action_catalog()}
        operations = {descriptor.operation for descriptor in command_registry()}
        missing = action_ids - operations
        self.assertEqual(missing, set(), f"action ids missing from catalog: {missing}")

    def test_main_commands_catalog_json_is_pure_json(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(["commands", "catalog", "--json"])
        self.assertEqual(exit_code, 0)
        stdout = buffer.getvalue()
        stripped = stdout.strip()
        self.assertIn(stripped[0], "{[")
        payload = json.loads(stdout)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["kind"], "ophelia.command_catalog")
        self.assertEqual(payload["commands"], catalog())

    def test_main_commands_catalog_defaults_to_json(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(["commands", "catalog"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["kind"], "ophelia.command_catalog")

    def test_main_commands_catalog_human_is_table(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(["commands", "catalog", "--human"])
        self.assertEqual(exit_code, 0)
        output = buffer.getvalue()
        self.assertIn("COMMAND", output)
        self.assertIn("OPERATION", output)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(output)


if __name__ == "__main__":
    unittest.main()
