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
app: demo-docs
kind: tunnel
tunnel_target: host.docker.internal:3501
routes:
  - domain: docs.example.com
    path: /api/openapi.json
  - domain: docs.example.com
    path: /api/admin-cli.json
  - domain: docs.example.com
    path: /api/ai/defaults.json
  - domain: docs.example.com
    rewrite_prefix: /docs
  - domain: admin.example.com
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
app: demo-tunnel-dev
kind: tunnel
routes:
  - domain: dev.demo-tunnel.example.net
    path_prefix: /api
    strip_prefix: /api
    upstream: host.docker.internal:3711
  - domain: dev.demo-tunnel.example.net
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
app: www-example-com
kind: redirect
redirect_to: https://example.com{uri}
redirect_status: 308
routes:
  - domain: www.example.com
"""
        )

        rendered = render_caddy(manifest)

        self.assertIn("www.example.com {", rendered)
        self.assertIn("redir https://example.com{uri} 308", rendered)

    def test_edge_tls_internal_renders_site_tls_directive(self) -> None:
        manifest = self._load(
            """
version: 1
app: lumen-staging
kind: service
image: ghcr.io/example/lumen:latest
services:
  web:
    port: 3773
routes:
  - domain: lumen-staging.example.net
    service: web
edge:
  tls:
    mode: internal
"""
        )

        rendered = render_caddy(manifest)

        self.assertIn("lumen-staging.example.net {", rendered)
        self.assertIn("    tls internal", rendered)
        self.assertIn("reverse_proxy lumen-staging-web:3773", rendered)

    def test_edge_tls_custom_renders_site_tls_files(self) -> None:
        manifest = self._load(
            """
version: 1
app: lumen-production
kind: service
image: ghcr.io/example/lumen:latest
services:
  web:
    port: 3773
routes:
  - domain: lumen.example.net
    service: web
edge:
  tls:
    mode: custom
    cert_file: /etc/caddy/certs/lumen-origin.pem
    key_file: /etc/caddy/certs/lumen-origin.key
"""
        )

        rendered = render_caddy(manifest)

        self.assertIn(
            '    tls "/etc/caddy/certs/lumen-origin.pem" "/etc/caddy/certs/lumen-origin.key"',
            rendered,
        )

    def test_response_headers_render_global_and_path_scoped_policies(self) -> None:
        manifest = self._load(
            """
version: 1
app: secure-static
kind: static
static_root: public
routes:
  - domain: secure-static.example.net
edge:
  response_headers:
    - name: Content-Security-Policy
      value: "default-src 'self'; object-src 'none'"
    - name: Cache-Control
      value: no-cache
      exclude_path_prefixes:
        - /assets
    - name: Cache-Control
      value: public, max-age=31536000, immutable
      path_prefix: /assets
      exclude_paths:
        - /assets/data/career.json
    - name: Cache-Control
      value: no-cache
      path: /assets/data/career.json
"""
        )

        rendered = render_caddy(manifest)

        self.assertIn(
            "        Content-Security-Policy \"default-src 'self'; object-src 'none'\"",
            rendered,
        )
        self.assertNotIn('        Cache-Control "no-cache"', rendered)
        self.assertIn("    @ophelia_response_header_2 {", rendered)
        self.assertIn("        not path /assets /assets/*", rendered)
        self.assertIn(
            '    header @ophelia_response_header_2 Cache-Control "no-cache"',
            rendered,
        )
        self.assertIn("    @ophelia_response_header_3 {", rendered)
        self.assertIn("        path /assets /assets/*", rendered)
        self.assertIn("        not path /assets/data/career.json", rendered)
        self.assertIn(
            '    header @ophelia_response_header_3 Cache-Control "public, max-age=31536000, immutable"',
            rendered,
        )
        self.assertIn(
            "    @ophelia_response_header_4 path /assets/data/career.json",
            rendered,
        )
        self.assertIn(
            '    header @ophelia_response_header_4 Cache-Control "no-cache"',
            rendered,
        )

    def _load(self, content: str):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "app.ophelia.yml"
            manifest_path.write_text(content.strip() + "\n")
            return load_manifest(manifest_path)


if __name__ == "__main__":
    unittest.main()
