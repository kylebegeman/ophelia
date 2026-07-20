from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ophelia.edge_runtime import bootstrap_edge_runtime


class EdgeRuntimeTests(unittest.TestCase):
    def test_materializes_packaged_edge_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            first = bootstrap_edge_runtime(root, http_port=8080, https_port=8443)
            second = bootstrap_edge_runtime(root, http_port=8080, https_port=8443)

            compose = (root / "platform" / "shared" / "compose.yml").read_text()
            environment = (root / "platform" / "shared" / ".env").read_text()
            caddyfile = (root / "platform" / "shared" / "caddy" / "Caddyfile").read_text()

        self.assertTrue(first["changed"])
        self.assertEqual([], second["changed"])
        self.assertIn("name: ophelia-shared", compose)
        self.assertIn("OPHELIA_HTTP_PORT=8080", environment)
        self.assertIn("import /etc/caddy/sites.d/*.caddy", caddyfile)

    def test_start_creates_network_and_starts_caddy(self) -> None:
        commands: list[list[str]] = []

        def runner(command):
            commands.append(list(command))
            return SimpleNamespace(returncode=1 if command[:3] == ["docker", "network", "inspect"] else 0)

        with tempfile.TemporaryDirectory() as directory:
            report = bootstrap_edge_runtime(Path(directory) / "runtime", start=True, runner=runner)

        self.assertTrue(report["started"])
        self.assertEqual(["docker", "network", "create", "ophelia-edge"], commands[1])
        self.assertEqual("up", commands[2][-3])
        self.assertEqual(["-d", "caddy"], commands[2][-2:])

    def test_rejects_symlinked_managed_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            shared = root / "platform" / "shared"
            shared.mkdir(parents=True)
            target = root / "outside"
            target.write_text("outside")
            (shared / "compose.yml").symlink_to(target)

            with self.assertRaisesRegex(ValueError, "unsafe"):
                bootstrap_edge_runtime(root)


if __name__ == "__main__":
    unittest.main()
