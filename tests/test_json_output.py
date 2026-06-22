from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class JsonOutputTests(unittest.TestCase):
    def test_key_operator_commands_emit_parseable_json(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            env = {**os.environ, "OPHELIA_SKIP_DOCKER_STATUS": "1", "OPHELIA_SKIP_GH_STATUS": "1"}

            commands = [
                ["validate", "examples/dragonwriter.ophelia.yml", "--json"],
                ["render", "examples/portfolio.ophelia.yml", "--output-dir", str(root / "rendered"), "--json"],
                ["deploy", "examples/portfolio.ophelia.yml", "--runtime-root", str(runtime_root), "--plan", "--json"],
                ["diff", "examples/portfolio.ophelia.yml", "--runtime-root", str(runtime_root), "--json"],
                ["status", "--runtime-root", str(runtime_root), "--json"],
                ["doctor", "--runtime-root", str(runtime_root), "--ophelia-root", str(repo), "--json"],
            ]

            for command in commands:
                result = subprocess.run(
                    [str(repo / "cli" / "ship"), *command],
                    text=True,
                    capture_output=True,
                    check=True,
                    env=env,
                    cwd=repo,
                )
                json.loads(result.stdout)


if __name__ == "__main__":
    unittest.main()
