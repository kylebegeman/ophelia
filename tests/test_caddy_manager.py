from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.caddy_manager import validate_caddy  # noqa: E402


class CaddyManagerTests(unittest.TestCase):
    def test_validate_caddy_uses_configurable_static_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ophelia_root = root / "ophelia"
            caddy_dir = ophelia_root / "platform" / "shared" / "caddy"
            caddy_dir.mkdir(parents=True)
            (caddy_dir / "Caddyfile").write_text("{}\n")
            runtime_root = root / "runtime"
            static_root = root / "public-static"

            with mock.patch("ophelia.caddy_manager.subprocess.run") as run:
                run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
                report = validate_caddy(runtime_root=runtime_root, ophelia_root=ophelia_root, static_root=static_root)
                static_root_exists = static_root.exists()

        command = report["command"]
        self.assertEqual(0, report["returncode"])
        self.assertIn(f"{static_root}:{static_root}:ro", command)
        old_private_mount = "/home/" + "kyle" + "/websites:/home/" + "kyle" + "/websites:ro"
        self.assertNotIn(old_private_mount, command)
        self.assertTrue(static_root_exists)


if __name__ == "__main__":
    unittest.main()
