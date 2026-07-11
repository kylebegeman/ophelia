from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


class BootstrapHostTests(unittest.TestCase):
    def test_bootstrap_preserves_custom_shared_env_keys(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shared = root / "shared"
            runtime = root / "runtime"
            fake_bin = root / "bin"
            docker_log = root / "docker.log"
            shared.mkdir()
            fake_bin.mkdir()
            (shared / ".env").write_text(
                "\n".join(
                    [
                        "CUSTOM_FLAG=keep-me",
                        "POSTGRES_PASSWORD=keep-postgres",
                        "REDIS_PASSWORD=replace-me",
                        "OPHELIA_HTTP_PORT=8080",
                    ]
                )
                + "\n"
            )
            docker = fake_bin / "docker"
            docker.write_text(
                "#!/usr/bin/env sh\n"
                'echo "$@" >> "$DOCKER_LOG"\n'
                'if [ "$1" = "network" ] && [ "$2" = "inspect" ]; then exit 1; fi\n'
                "exit 0\n"
            )
            docker.chmod(0o755)

            env = os.environ.copy()
            env["OPHELIA_SHARED_DIR"] = str(shared)
            env["DOCKER_LOG"] = str(docker_log)
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

            subprocess.run(
                [str(repo / "platform" / "scripts" / "bootstrap-host.sh"), str(runtime)],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            values = _read_env(shared / ".env")
            self.assertEqual("keep-me", values["CUSTOM_FLAG"])
            self.assertEqual("keep-postgres", values["POSTGRES_PASSWORD"])
            self.assertNotEqual("replace-me", values["REDIS_PASSWORD"])
            self.assertEqual("8080", values["OPHELIA_HTTP_PORT"])
            self.assertEqual("443", values["OPHELIA_HTTPS_PORT"])
            self.assertEqual(str(runtime), values["OPHELIA_RUNTIME_ROOT"])
            self.assertEqual(0o600, stat.S_IMODE((shared / ".env").stat().st_mode))
            log = docker_log.read_text()
            self.assertIn("network inspect ophelia-edge", log)
            self.assertIn("network create ophelia-edge", log)
            self.assertIn("network inspect ophelia-internal", log)
            self.assertIn("network create --internal ophelia-internal", log)


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values
