from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ophelia.manifest import ManifestError, load_manifest
from ophelia.templates import render_caddy, render_compose
from ophelia.verify import verification_checks


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

    def test_prism_profile_supports_env_files_mounts_and_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "env").mkdir()
            (root / "assets" / "console").mkdir(parents=True)
            (root / "env" / "shared.env").write_text("APP_ENV=production\n")
            (root / "assets" / "console" / "index.html").write_text("<html></html>\n")
            manifest_path = root / "quark.ophelia.yml"
            manifest_path.write_text(
                """
version: 1
app: quark-ops
profile: prism
kind: service
image: ghcr.io/example/quark-ops:latest
env_files:
  - env/shared.env
services:
  web:
    port: 8080
    mounts:
      - source: assets/console
        target: /opt/quark/console
        read_only: true
routes:
  - domain: ops.begam.in
    service: web
prism:
  admin_domain: admin.ops.begam.in
  console_asset_path: /opt/quark/console
  surface: quark
verify:
  - name: health
    url: https://ops.begam.in/health
""".strip()
                + "\n"
            )

            loaded = load_manifest(manifest_path)

        self.assertEqual("prism", loaded.profile)
        self.assertEqual(["env/shared.env"], loaded.env_files)
        self.assertEqual("admin.ops.begam.in", loaded.prism.admin_domain if loaded.prism else None)
        self.assertEqual("quark", loaded.prism.surface if loaded.prism else None)
        self.assertEqual(1, len(loaded.services["web"].mounts))
        self.assertEqual("/opt/quark/console", loaded.services["web"].mounts[0].target)
        self.assertEqual(1, len(loaded.verify))
        self.assertEqual("https://ops.begam.in/health", loaded.verify[0].url)

    def test_prism_profile_synthesizes_admin_domain_route(self) -> None:
        manifest = """
version: 1
app: quark-ops
profile: prism
kind: service
image: ghcr.io/example/quark-ops:latest
services:
  web:
    port: 8080
routes:
  - domain: ops.begam.in
    service: web
prism:
  admin_domain: admin.ops.begam.in
  surface: quark
"""
        loaded = self._load(manifest)

        caddy = render_caddy(loaded)

        self.assertIn("ops.begam.in {", caddy)
        self.assertIn("admin.ops.begam.in {", caddy)

    def test_render_compose_includes_env_file_fragments_and_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "env").mkdir()
            (root / "assets" / "console").mkdir(parents=True)
            (root / "env" / "shared.env").write_text("APP_ENV=production\n")
            (root / "assets" / "console" / "index.html").write_text("<html></html>\n")
            manifest_path = root / "quark.ophelia.yml"
            manifest_path.write_text(
                """
version: 1
app: quark-ops
profile: prism
kind: service
image: ghcr.io/example/quark-ops:latest
env_files:
  - env/shared.env
services:
  web:
    port: 8080
    env_files:
      - env/shared.env
    mounts:
      - source: assets/console
        target: /opt/quark/console
routes:
  - domain: ops.begam.in
    service: web
prism:
  admin_domain: ops.begam.in
""".strip()
                + "\n"
            )

            loaded = load_manifest(manifest_path)

        compose = render_compose(loaded)

        assert compose is not None
        self.assertIn("./env.d/01-shared.env", compose)
        self.assertIn("./env.d/web-01-shared.env", compose)
        self.assertIn("./artifacts/web-01-console:/opt/quark/console:ro", compose)

    def test_quark_surface_infers_root_host_verification_checks(self) -> None:
        manifest = """
version: 1
app: quark-ops
profile: prism
kind: service
image: ghcr.io/example/prism:latest
services:
  web:
    port: 8080
routes:
  - domain: ops.begam.in
    service: web
prism:
  admin_domain: ops.begam.in
  surface: quark
"""
        loaded = self._load(manifest)

        checks = verification_checks(loaded)

        self.assertEqual(
            [
                "https://ops.begam.in/health",
                "https://ops.begam.in/",
                "https://ops.begam.in/console",
            ],
            [item.url for item in checks],
        )

    def _load(self, content: str):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "app.ophelia.yml"
            manifest_path.write_text(content.strip() + "\n")
            return load_manifest(manifest_path)


if __name__ == "__main__":
    unittest.main()
