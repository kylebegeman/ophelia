from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ophelia.manifest import ManifestError, load_manifest


class ManifestTests(unittest.TestCase):
    def test_rejects_route_with_both_service_and_upstream(self) -> None:
        manifest = """
version: 1
app: broken
kind: tunnel
tunnel_target: host.docker.internal:3501
routes:
  - domain: docs.bagels.top
    service: api
    upstream: host.docker.internal:3501
"""
        with self.assertRaises(ManifestError):
            self._load(manifest)

    def test_redirect_manifest_requires_redirect_target(self) -> None:
        manifest = """
version: 1
app: www-bagels-top
kind: redirect
routes:
  - domain: www.bagels.top
"""
        with self.assertRaises(ManifestError):
            self._load(manifest)

    def test_tunnel_manifest_supports_route_upstreams(self) -> None:
        manifest = """
version: 1
app: pokedex-dev
kind: tunnel
routes:
  - domain: dev.pokedex.begam.in
    path_prefix: /api
    strip_prefix: /api
    upstream: host.docker.internal:3711
  - domain: dev.pokedex.begam.in
    upstream: host.docker.internal:3712
"""
        loaded = self._load(manifest)

        self.assertEqual("host.docker.internal:3711", loaded.routes[0].upstream)
        self.assertEqual("/api", loaded.routes[0].path_prefix)
        self.assertIsNone(loaded.tunnel_target)

    def _load(self, content: str):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "app.ophelia.yml"
            manifest_path.write_text(content.strip() + "\n")
            return load_manifest(manifest_path)


if __name__ == "__main__":
    unittest.main()
