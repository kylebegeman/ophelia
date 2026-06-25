from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.main import main
from ophelia.self_test import run_self_test


class SelfTestTests(unittest.TestCase):
    def test_result_round_trips_through_json(self) -> None:
        result = run_self_test()
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_result_identity_keys(self) -> None:
        result = run_self_test()
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["kind"], "ophelia.self_test")
        self.assertEqual(result["package"]["name"], "ophelia")

    def test_status_not_blocked_in_this_environment(self) -> None:
        result = run_self_test()
        self.assertIn(result["status"], {"ok", "warn"})
        self.assertEqual(result["blockers"], [])

    def test_templates_check_passed_via_importlib_resources(self) -> None:
        result = run_self_test()
        templates = [c for c in result["checks"] if c["name"] == "templates"]
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]["status"], "passed")
        self.assertIn("importlib.resources", templates[0].get("message", ""))

    def test_no_secret_value_appears_in_output(self) -> None:
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = "postgres://u:secret@h/db"
        try:
            blob = json.dumps(run_self_test())
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous
        self.assertNotIn("secret", blob)
        self.assertNotIn("postgres://", blob)

    def test_cli_json_is_pure_json(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(["self-test", "--json"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["kind"], "ophelia.self_test")
        self.assertIn(payload["status"], {"ok", "warn"})

    def test_version_command_json_is_pure_json(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(["version", "--json"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["kind"], "ophelia.version")
        self.assertEqual(payload["name"], "ophelia")
        self.assertTrue(payload["version"])

    def test_global_version_flag_prints_version(self) -> None:
        buffer = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            with contextlib.redirect_stdout(buffer):
                main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("ship", buffer.getvalue())
        self.assertIn("0.", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
