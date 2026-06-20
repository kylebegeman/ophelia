from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.manifest import load_manifest
from ophelia.templates import render_caddy


class TemplateRenderTests(unittest.TestCase):
    def test_docs_and_admin_hosts_render_rewrites_and_passthroughs(self) -> None:
        manifest = self._load(
            """
version: 1
app: aspectavy
kind: tunnel
tunnel_target: host.docker.internal:3501
routes:
  - domain: docs.bagels.top
    path: /api/openapi.json
  - domain: docs.bagels.top
    path: /api/admin-cli.json
  - domain: docs.bagels.top
    path: /api/ai/defaults.json
  - domain: docs.bagels.top
    rewrite_prefix: /docs
  - domain: admin.bagels.top
    rewrite_prefix: /admin
"""
        )

        rendered = render_caddy(manifest)

        self.assertIn("handle /api/openapi.json {", rendered)
        self.assertIn("handle /api/admin-cli.json {", rendered)
        self.assertIn("handle /api/ai/defaults.json {", rendered)
        self.assertIn("handle / {\n        rewrite * /docs\n        reverse_proxy", rendered)
        self.assertIn("rewrite * /docs{uri}", rendered)
        self.assertIn("handle / {\n        rewrite * /admin\n        reverse_proxy", rendered)
        self.assertIn("rewrite * /admin{uri}", rendered)

        docs_root_index = rendered.index("rewrite * /docs\n")
        docs_catchall_index = rendered.index("rewrite * /docs{uri}")
        docs_passthrough_index = rendered.index("handle /api/openapi.json {")
        self.assertLess(docs_passthrough_index, docs_root_index)
        self.assertLess(docs_root_index, docs_catchall_index)

    def test_route_specific_upstreams_render(self) -> None:
        manifest = self._load(
            """
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
        )

        rendered = render_caddy(manifest)

        self.assertIn("path /api /api/*", rendered)
        self.assertIn("handle @ophelia_", rendered)
        self.assertIn("uri strip_prefix /api", rendered)
        self.assertIn("reverse_proxy host.docker.internal:3711", rendered)
        self.assertIn("reverse_proxy host.docker.internal:3712", rendered)

    def test_redirect_manifest_renders_redirect(self) -> None:
        manifest = self._load(
            """
version: 1
app: www-bagels-top
kind: redirect
redirect_to: https://bagels.top{uri}
redirect_status: 308
routes:
  - domain: www.bagels.top
"""
        )

        rendered = render_caddy(manifest)

        self.assertIn("www.bagels.top {", rendered)
        self.assertIn("redir https://bagels.top{uri} 308", rendered)

    def _load(self, content: str):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "app.ophelia.yml"
            manifest_path.write_text(content.strip() + "\n")
            return load_manifest(manifest_path)


if __name__ == "__main__":
    unittest.main()
