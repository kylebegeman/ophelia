from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.manifest import ManifestError, load_manifest
from ophelia.runtime import render_bundle
from ophelia.templates import caddy_env_keys, render_caddy, render_caddy_global, render_compose
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

    def test_environment_metadata_is_optional_and_validated(self) -> None:
        manifest = """
version: 1
app: env-aware
environment: staging
kind: static
static_root: /tmp/env-aware
routes:
  - domain: env-aware.example.com
"""
        loaded = self._load(manifest)
        self.assertEqual("staging", loaded.environment)

        invalid = manifest.replace("staging", "qa")
        with self.assertRaises(ManifestError):
            self._load(invalid)

    def test_addons_infer_default_data_contracts(self) -> None:
        manifest = """
version: 1
app: addon-data
kind: service
image: ghcr.io/example/addon-data:latest
services:
  web:
    port: 3000
routes:
  - domain: addon-data.example.com
    service: web
addons:
  postgres: true
  redis: true
"""
        loaded = self._load(manifest)

        self.assertIsNotNone(loaded.data.postgres)
        self.assertIsNotNone(loaded.data.redis)
        assert loaded.data.postgres is not None
        assert loaded.data.redis is not None
        self.assertEqual("shared-postgres-database", loaded.data.postgres.mode)
        self.assertTrue(loaded.data.postgres.inferred_from_addon)
        self.assertEqual("redis-logical-db", loaded.data.redis.mode)
        self.assertTrue(loaded.data.redis.inferred_from_addon)

    def test_portable_pack_sections_round_trip_to_lock_dict(self) -> None:
        manifest = """
version: 1
app: portable
environment: production
kind: service
image: ghcr.io/example/portable@sha256:aaaaaaaa
pack:
  portability: critical
  owner: personal
  description: Portable app
  deploy_binding_file: ophelia/deploy.json
host_requirements:
  arch: amd64
  min_memory: 1g
  min_disk_free: 20g
  requires_edge: true
  requires_docker: true
networking:
  edge: shared
  internal: per-app
services:
  web:
    port: 3000
routes:
  - domain: portable.example.com
    service: web
data:
  postgres:
    mode: shared-postgres-database
    database: portable
    export:
      format: custom
      command: pg_dump
    import:
      command: pg_restore
    verify:
      command: ophelia/checks/data-verify.sh
  volumes:
    - name: uploads
      mount: /app/uploads
      class: critical
      export: tar-zstd
      import: tar-zstd
  backups:
    required: true
    restore_drill_required: true
    offsite_required: true
hooks:
  freeze: ophelia/hooks/freeze.sh
verify:
  - name: health
    url: https://portable.example.com/health
"""
        loaded = self._load(manifest)
        lock = loaded.to_lock_dict()

        self.assertEqual("critical", lock["pack"]["portability"])
        self.assertEqual("amd64", lock["host_requirements"]["arch"])
        self.assertEqual("per-app", lock["networking"]["internal"])
        self.assertEqual("pg_restore", lock["data"]["postgres"]["import"]["command"])
        self.assertEqual("critical", lock["data"]["volumes"][0]["class"])
        self.assertEqual("ophelia/hooks/freeze.sh", lock["hooks"]["freeze"])

    def test_rejects_unknown_networking_mode(self) -> None:
        manifest = """
version: 1
app: invalid-networking
kind: service
image: ghcr.io/example/invalid-networking:latest
networking:
  internal: global
services:
  web:
    port: 3000
routes:
  - domain: invalid-networking.example.com
    service: web
"""
        with self.assertRaises(ManifestError):
            self._load(manifest)

    def test_verify_policy_configures_retry_and_failure_mode(self) -> None:
        manifest = """
version: 1
app: verify-policy
kind: static
static_root: /tmp/verify-policy
routes:
  - domain: verify-policy.example.com
verify:
  - name: health
    url: https://verify-policy.example.com/health
verify_policy:
  attempts: 4
  interval: 2.5
  timeout: 3
  failure_mode: warn
"""
        loaded = self._load(manifest)

        self.assertEqual(4, loaded.verify_policy.attempts)
        self.assertEqual(2.5, loaded.verify_policy.interval)
        self.assertEqual(3.0, loaded.verify_policy.timeout)
        self.assertEqual("warn", loaded.verify_policy.failure_mode)

    def test_verify_url_rejects_credentials_query_and_fragment(self) -> None:
        for url in [
            "https://user:secret@example.com/health",
            "https://example.com/health?token=abc",
            "https://example.com/health#fragment",
        ]:
            manifest = f"""
version: 1
app: verify-url
kind: static
static_root: /tmp/verify-url
routes:
  - domain: verify-url.example.com
verify:
  - url: {url}
"""
            with self.assertRaisesRegex(ManifestError, "must not contain credentials"):
                self._load(manifest)

    def test_rejects_boolean_values_for_integer_fields(self) -> None:
        manifest = """
version: 1
app: bool-port
kind: service
image: ghcr.io/example/bool-port:latest
services:
  web:
    port: true
routes:
  - domain: bool-port.example.com
    service: web
"""
        with self.assertRaises(ManifestError):
            self._load(manifest)

    def test_tunnel_manifest_supports_route_upstreams(self) -> None:
        manifest = """
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
        loaded = self._load(manifest)

        self.assertEqual("host.docker.internal:3711", loaded.routes[0].upstream)
        self.assertEqual("/api", loaded.routes[0].path_prefix)
        self.assertIsNone(loaded.tunnel_target)

    def test_edge_catch_all_requires_on_demand_tls(self) -> None:
        manifest = """
version: 1
app: broken-edge
kind: multi-service
services:
  redirector:
    image: ghcr.io/example/redirector:latest
    port: 8081
routes:
  - domain: app.example.com
    service: redirector
edge:
  catch_all:
    service: redirector
"""
        with self.assertRaises(ManifestError):
            self._load(manifest)

    def test_edge_tls_custom_requires_cert_and_key_files(self) -> None:
        manifest = """
version: 1
app: broken-edge-tls
kind: service
services:
  web:
    port: 3000
routes:
  - domain: app.example.com
    service: web
edge:
  tls:
    mode: custom
    cert_file: /etc/caddy/certs/app.pem
"""
        with self.assertRaises(ManifestError):
            self._load(manifest)

    def test_edge_catch_all_renders_global_and_site_blocks(self) -> None:
        manifest = """
version: 1
app: boop
kind: multi-service
services:
  web:
    image: ghcr.io/example/boop-web:latest
    port: 3000
  redirector:
    image: ghcr.io/example/boop-backend:latest
    port: 8081
routes:
  - domain: app.boop.at
    service: web
edge:
  on_demand_tls:
    ask: http://boop-control-plane:8080/boop/internal/caddy/allow?token={$BOOP_INTERNAL_RUNTIME_TOKEN}
  catch_all:
    service: redirector
"""
        loaded = self._load(manifest)

        caddy = render_caddy(loaded)
        caddy_global = render_caddy_global(loaded)
        bundle = render_bundle(loaded)

        self.assertIn("http:// {", caddy)
        self.assertIn("redir https://{host}{uri} 308", caddy)
        self.assertIn("https:// {", caddy)
        self.assertIn("on_demand", caddy)
        self.assertIn("reverse_proxy boop-redirector:8081", caddy)
        self.assertEqual(["BOOP_INTERNAL_RUNTIME_TOKEN"], caddy_env_keys(loaded))
        self.assertIsNotNone(caddy_global)
        self.assertIn("on_demand_tls", caddy_global or "")
        self.assertIn("BOOP_INTERNAL_RUNTIME_TOKEN=replace-me", bundle[Path("env.example")])
        self.assertNotIn(Path("caddy/global.d/boop.caddy"), bundle)

    def test_console_profile_supports_env_files_mounts_and_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "env").mkdir()
            (root / "assets" / "console").mkdir(parents=True)
            (root / "env" / "shared.env").write_text("APP_ENV=production\n")
            (root / "assets" / "console" / "index.html").write_text("<html></html>\n")
            manifest_path = root / "root.ophelia.yml"
            manifest_path.write_text(
                """
version: 1
app: root-ops
profile: console
kind: service
image: ghcr.io/example/root-ops:latest
env_files:
  - env/shared.env
services:
  web:
    port: 8080
    mounts:
      - source: assets/console
        target: /opt/root/console
        read_only: true
routes:
  - domain: ops.example.net
    service: web
console:
  admin_domain: admin.ops.example.net
  console_asset_path: /opt/root/console
  surface: root
verify:
  - name: health
    url: https://ops.example.net/health
""".strip()
                + "\n"
            )

            loaded = load_manifest(manifest_path)

        self.assertEqual("console", loaded.profile)
        self.assertEqual(["env/shared.env"], loaded.env_files)
        self.assertEqual("admin.ops.example.net", loaded.console.admin_domain if loaded.console else None)
        self.assertEqual("root", loaded.console.surface if loaded.console else None)
        self.assertEqual(1, len(loaded.services["web"].mounts))
        self.assertEqual("/opt/root/console", loaded.services["web"].mounts[0].target)
        self.assertEqual(1, len(loaded.verify))
        self.assertEqual("https://ops.example.net/health", loaded.verify[0].url)

    def test_console_profile_synthesizes_admin_domain_route(self) -> None:
        manifest = """
version: 1
app: root-ops
profile: console
kind: service
image: ghcr.io/example/root-ops:latest
services:
  web:
    port: 8080
routes:
  - domain: ops.example.net
    service: web
console:
  admin_domain: admin.ops.example.net
  surface: root
"""
        loaded = self._load(manifest)

        caddy = render_caddy(loaded)

        self.assertIn("ops.example.net {", caddy)
        self.assertIn("admin.ops.example.net {", caddy)

    def test_render_compose_includes_env_file_fragments_and_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "env").mkdir()
            (root / "assets" / "console").mkdir(parents=True)
            (root / "env" / "shared.env").write_text("APP_ENV=production\n")
            (root / "assets" / "console" / "index.html").write_text("<html></html>\n")
            manifest_path = root / "root.ophelia.yml"
            manifest_path.write_text(
                """
version: 1
app: root-ops
profile: console
kind: service
image: ghcr.io/example/root-ops:latest
env_files:
  - env/shared.env
services:
  web:
    port: 8080
    env_files:
      - env/shared.env
    mounts:
      - source: assets/console
        target: /opt/root/console
routes:
  - domain: ops.example.net
    service: web
console:
  admin_domain: ops.example.net
""".strip()
                + "\n"
            )

            loaded = load_manifest(manifest_path)

        compose = render_compose(loaded)

        assert compose is not None
        self.assertIn("./env.d/01-shared.env", compose)
        self.assertIn("./env.d/web-01-shared.env", compose)
        self.assertIn("./artifacts/web-01-console:/opt/root/console:ro", compose)

    def test_render_compose_supports_bind_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "persistent" / "uploads").mkdir(parents=True)
            manifest_path = root / "demo-service.ophelia.yml"
            manifest_path.write_text(
                """
version: 1
app: demo-service
kind: multi-service
image: ghcr.io/example/demo-service:latest
services:
  web:
    port: 3000
    mounts:
      - source: persistent/uploads
        target: /app/public/uploads
        read_only: false
        bind: true
routes:
  - domain: demo-service.example.net
    service: web
""".strip()
                + "\n"
            )

            manifest = load_manifest(manifest_path)

        compose = render_compose(manifest)

        assert compose is not None
        self.assertIn(
            "persistent/uploads:/app/public/uploads",
            compose,
        )

    def test_render_compose_mounts_declared_data_volume(self) -> None:
        manifest = self._load(
            """
version: 1
app: lumen-staging
environment: staging
kind: service
image: ghcr.io/example/lumen:next
services:
  web:
    port: 3773
routes:
  - domain: lumen-staging.example.com
    service: web
data:
  volumes:
    - name: lumen-home
      mount: /data/lumen
      class: critical
      export: tar-zstd
      import: tar-zstd
"""
        )

        compose = render_compose(manifest)

        assert compose is not None
        self.assertIn('"lumen-staging-staging-lumen-home:/data/lumen"', compose)
        self.assertIn("volumes:\n  lumen-staging-staging-lumen-home:", compose)
        self.assertIn('    name: "lumen-staging-staging-lumen-home"', compose)
        self.assertIn('      ophelia.data.volume: "lumen-home"', compose)

    def test_render_compose_mounts_data_volume_on_declared_service(self) -> None:
        manifest = self._load(
            """
version: 1
app: workers-app
environment: production
kind: multi-service
image: ghcr.io/example/workers-app:latest
services:
  web:
    port: 3000
  worker:
    port: 3001
routes:
  - domain: workers.example.com
    service: web
data:
  volumes:
    - name: worker-cache
      service: worker
      mount: /data/cache
"""
        )

        compose = render_compose(manifest)

        assert compose is not None
        self.assertNotIn('"workers-app-production-worker-cache:/data/cache"', compose.split("  worker:")[0])
        self.assertIn('"workers-app-production-worker-cache:/data/cache"', compose.split("  worker:")[1])

    def test_mounted_data_volume_requires_service_in_multi_service_manifest(self) -> None:
        manifest = """
version: 1
app: ambiguous-volumes
kind: multi-service
image: ghcr.io/example/ambiguous-volumes:latest
services:
  web:
    port: 3000
  worker:
    port: 3001
routes:
  - domain: ambiguous.example.com
    service: web
data:
  volumes:
    - name: shared-data
      mount: /data/shared
"""

        with self.assertRaisesRegex(ManifestError, "service.*required"):
            self._load(manifest)

    def test_render_compose_keeps_shared_internal_network_by_default(self) -> None:
        manifest = self._load(
            """
version: 1
app: shared-network
kind: service
image: ghcr.io/example/shared-network:latest
services:
  web:
    port: 3000
routes:
  - domain: shared-network.example.com
    service: web
"""
        )

        compose = render_compose(manifest)

        assert compose is not None
        self.assertIn("      ophelia-internal:", compose)
        self.assertIn("  ophelia-internal:\n    external: true", compose)

    def test_render_compose_supports_per_app_internal_network(self) -> None:
        manifest = self._load(
            """
version: 1
app: isolated-app
environment: production
kind: service
image: ghcr.io/example/isolated-app:latest
networking:
  internal: per-app
services:
  web:
    port: 3000
routes:
  - domain: isolated-app.example.com
    service: web
"""
        )

        compose = render_compose(manifest)

        assert compose is not None
        self.assertIn("      isolated-app-production-internal:", compose)
        self.assertIn("  isolated-app-production-internal:", compose)
        self.assertIn('    name: "isolated-app-production-internal"', compose)
        self.assertIn('          - "web"', compose)
        self.assertNotIn("  ophelia-internal:\n    external: true", compose)

    def test_root_surface_infers_root_host_verification_checks(self) -> None:
        manifest = """
version: 1
app: root-ops
profile: console
kind: service
image: ghcr.io/example/console:latest
services:
  web:
    port: 8080
routes:
  - domain: ops.example.net
    service: web
console:
  admin_domain: ops.example.net
  surface: root
"""
        loaded = self._load(manifest)

        checks = verification_checks(loaded)

        self.assertEqual(
            [
                "https://ops.example.net/health",
                "https://ops.example.net/",
                "https://ops.example.net/console",
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
