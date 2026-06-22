from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.main import main

REPO = Path(__file__).resolve().parents[1]


def _capture(argv: list) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exit_code = main(argv)
    return exit_code, buffer.getvalue()


class JsonOutputConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_skip = os.environ.get("OPHELIA_SKIP_DOCKER_STATUS")
        os.environ["OPHELIA_SKIP_DOCKER_STATUS"] = "1"

    def tearDown(self) -> None:
        if self._prev_skip is None:
            os.environ.pop("OPHELIA_SKIP_DOCKER_STATUS", None)
        else:
            os.environ["OPHELIA_SKIP_DOCKER_STATUS"] = self._prev_skip

    def _assert_pure_json(self, stdout: str) -> object:
        stripped = stdout.strip()
        self.assertTrue(stripped, "command produced no stdout")
        self.assertIn(stripped[0], "{[")
        self.assertIn(stripped[-1], "}]")
        return json.loads(stdout)

    def test_representative_json_commands_emit_pure_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = str(Path(temp_dir) / "runtime")
            commands = [
                ["validate", str(REPO / "examples" / "service-app.ophelia.yml"), "--json"],
                ["actions", "--json"],
                ["commands", "catalog", "--json"],
                ["receipts", "list", "--json", "--runtime-root", runtime_root],
            ]
            for command in commands:
                exit_code, stdout = _capture(command)
                self.assertIn(exit_code, (0, 1), f"unexpected exit for {command}")
                self._assert_pure_json(stdout)

    def test_validate_error_path_is_pure_json_with_envelope(self) -> None:
        exit_code, stdout = _capture(["validate", str(REPO / "does-not-exist.ophelia.yml"), "--json"])
        self.assertEqual(exit_code, 1)
        payload = self._assert_pure_json(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["kind"], "ophelia.error")
        self.assertEqual(payload["schema_version"], 1)

    def test_operations_run_error_path_is_pure_json_with_envelope(self) -> None:
        exit_code, stdout = _capture(["operations", "run", "does-not-exist", "--json"])
        self.assertEqual(exit_code, 1)
        payload = self._assert_pure_json(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["kind"], "ophelia.error")


if __name__ == "__main__":
    unittest.main()
