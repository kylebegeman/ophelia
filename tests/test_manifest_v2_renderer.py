from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from ophelia.manifest_v2 import load_manifest_v2
from ophelia.manifest_v2_renderer import render_revision_bundle


PINNED_IMAGE = "ghcr.io/example/demo@sha256:" + "a" * 64


class ManifestV2RendererTests(unittest.TestCase):
    def test_renders_revision_isolated_compose_and_caddy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(
                f"""
version: 2
app: rendered-demo
environment: production
artifacts:
  app: {{image: "{PINNED_IMAGE}"}}
workloads:
  web:
    kind: web
    artifact: app
    command: ["./server"]
    port: 8080
    replicas: 2
    env: {{NODE_ENV: production}}
    readiness:
      http: {{path: /ready, port: 8080}}
      timeout_seconds: 90
    resources: {{memory: 512Mi}}
  jobs:
    kind: worker
    artifact: app
    command: ["/bin/sh", "-ec", 'echo "$TOKEN"']
    liveness:
      command: ["./worker", "health"]
    security:
      run_as_user: 1000
      no_new_privileges: false
      privileged: true
      seccomp_profile: unconfined
      apparmor_profile: unconfined
      add_capabilities: [SYS_ADMIN, SETUID, SETGID, DAC_OVERRIDE]
    devices:
      - {{source: /dev/net/tun, permissions: rwm}}
  database:
    kind: internal
    artifact: app
    entrypoint: ["/bin/sh", "-ec"]
    command: ["exec database-server"]
    port: 5432
    networks: [data]
    network_aliases:
      data: [rendered-demo-database]
    file_mounts:
      - source: config/init.sql
        target: /docker-entrypoint-initdb.d/010-init.sql
routes:
  - name: public
    domain: rendered-demo.example.com
    target: {{workload: web, port: 8080}}
secrets:
  - name: DATABASE_URL
    ref: secret://rendered-demo/production/database-url
update:
  strategy: recreate
"""
            )
            (root / "config").mkdir()
            (root / "config" / "init.sql").write_text("SELECT 1;\n")
            manifest = load_manifest_v2(manifest_path)
            revision = manifest.to_revision(created_at="2026-07-19T12:00:00Z")

            bundle = render_revision_bundle(manifest, revision)
            compose = yaml.safe_load(bundle[Path("compose.yml")])
            caddy = bundle[Path("caddy/routes.caddy")]
            revision_doc = json.loads(bundle[Path("revision.json")])

        project = revision_doc["compose_project"]
        self.assertTrue(project.startswith("ophelia-rendered-demo-production-"))
        self.assertIn("rendered-demo-production", compose["name"])
        self.assertIn("web", compose["services"])
        self.assertIn("jobs", compose["services"])
        self.assertTrue(compose["networks"]["ophelia-app"]["external"])
        self.assertNotIn("labels", compose["networks"]["ophelia-app"])
        self.assertTrue(compose["services"]["web"]["read_only"])
        self.assertEqual(536870912, compose["services"]["web"]["mem_limit"])
        self.assertEqual(["ALL"], compose["services"]["web"]["cap_drop"])
        self.assertEqual({"disable": True}, compose["services"]["web"]["healthcheck"])
        self.assertEqual(["no-new-privileges:true"], compose["services"]["web"]["security_opt"])
        self.assertEqual(
            ["seccomp=unconfined", "apparmor=unconfined"],
            compose["services"]["jobs"]["security_opt"],
        )
        self.assertEqual(
            ["DAC_OVERRIDE", "SETGID", "SETUID", "SYS_ADMIN"],
            compose["services"]["jobs"]["cap_add"],
        )
        self.assertEqual(
            ["/dev/net/tun:/dev/net/tun:rwm"], compose["services"]["jobs"]["devices"]
        )
        self.assertTrue(compose["services"]["jobs"]["privileged"])
        self.assertEqual(
            ["/bin/sh", "-ec", 'echo "$$TOKEN"'],
            compose["services"]["jobs"]["command"],
        )
        self.assertEqual(
            ["CMD", "./worker", "health"],
            compose["services"]["jobs"]["healthcheck"]["test"],
        )
        self.assertEqual("1000", compose["services"]["jobs"]["user"])
        self.assertNotIn("ophelia-edge", compose["services"]["jobs"]["networks"])
        self.assertEqual(
            ["rendered-demo-database"],
            compose["services"]["database"]["networks"]["ophelia-data"]["aliases"],
        )
        self.assertEqual(
            ["/bin/sh", "-ec"], compose["services"]["database"]["entrypoint"]
        )
        self.assertIn(
            "./support/database/files/00-init.sql:/docker-entrypoint-initdb.d/010-init.sql:ro",
            compose["services"]["database"]["volumes"],
        )
        self.assertIn("rendered-demo.example.com", caddy)
        self.assertIn(revision.revision_id.removeprefix("rev_")[:12], caddy)
        self.assertNotIn("secret_value", bundle[Path("manifest.lock.json")])
        refs = json.loads(bundle[Path("secret-refs.json")])
        self.assertEqual("secret://rendered-demo/production/database-url", refs["secrets"][0]["ref"])

    def test_groups_same_domain_routes_into_ordered_handle_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "lumen.ophelia.yml"
            path.write_text(
                f"""
version: 2
app: lumen-staging
environment: staging
artifacts: {{server: {{image: "{PINNED_IMAGE}"}}}}
workloads:
  core: {{kind: web, artifact: server, port: 4773}}
  product: {{kind: web, artifact: server, port: 3773}}
routes:
  - name: product-runtime
    domain: lumen-staging.example.com
    target: {{workload: core, port: 4773}}
    path_prefix: /ophelia
    tls: {{mode: internal}}
  - name: product
    domain: lumen-staging.example.com
    target: {{workload: product, port: 3773}}
    tls: {{mode: internal}}
update: {{strategy: recreate}}
"""
            )
            manifest = load_manifest_v2(path)
            revision = manifest.to_revision(created_at="2026-07-20T22:00:00Z")
            caddy = render_revision_bundle(manifest, revision)[Path("caddy/routes.caddy")]

        self.assertEqual(1, caddy.count("lumen-staging.example.com {"))
        self.assertEqual(1, caddy.count("  tls internal"))
        self.assertIn("  handle /ophelia* {", caddy)
        self.assertIn("  handle {", caddy)
        self.assertLess(caddy.index("  handle /ophelia* {"), caddy.index("  handle {"))
        self.assertIn("lumen-staging-core-", caddy)
        self.assertIn("lumen-staging-product-", caddy)

    def test_cron_task_and_migration_are_rendered_as_profiles_not_started_services(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "app.ophelia.yml"
            path.write_text(
                f"""
version: 2
app: jobs-demo
environment: staging
artifacts:
  app: {{image: "{PINNED_IMAGE}"}}
workloads:
  cleanup:
    kind: cron
    artifact: app
    command: ["./cleanup"]
    schedule: "17 3 * * *"
  rebuild:
    kind: task
    artifact: app
    command: ["./rebuild"]
migrations:
  schema:
    workload:
      kind: migration
      artifact: app
      command: ["./migrate"]
routes: []
update: {{strategy: recreate}}
"""
            )
            manifest = load_manifest_v2(path)
            revision = manifest.to_revision(created_at="2026-07-19T12:00:00Z")
            compose = yaml.safe_load(render_revision_bundle(manifest, revision)[Path("compose.yml")])

        self.assertEqual(["ophelia-cron"], compose["services"]["cleanup"]["profiles"])
        self.assertEqual(["ophelia-task"], compose["services"]["rebuild"]["profiles"])
        self.assertEqual(["ophelia-migration"], compose["services"]["migration-schema"]["profiles"])
        self.assertEqual("no", compose["services"]["cleanup"]["restart"])

    def test_renders_release_metadata_file_secrets_endpoints_and_client_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "lumen.ophelia.yml"
            path.write_text(
                f"""
version: 2
app: lumen-staging
environment: staging
release:
  id: staging-123-abcdef
  commit_sha: {"b" * 40}
  build_time: 2026-07-19T20:00:00Z
artifacts: {{server: {{image: "{PINNED_IMAGE}"}}}}
workloads:
  core:
    kind: web
    artifact: server
    port: 4773
    endpoints: {{runner-control: 4774}}
routes:
  - name: product
    domain: lumen-staging.example.com
    target: {{workload: core, port: 4773}}
    tls: {{mode: internal}}
  - name: machine-control
    domain: control-staging.example.com
    target: {{workload: core, port: 4774}}
    client_auth:
      mode: verify_if_given
      trust_pool_ref: secret://lumen-staging/staging/lumen-ca-base64
      trust_pool_encoding: base64
      forward:
        authorization_ref: secret://lumen-staging/staging/lumen-proxy-token
secrets:
  - name: LUMEN_CA_PATH
    ref: secret://lumen-staging/staging/lumen-ca-base64
    mode: file
    target: /run/lumen/ca.pem
    encoding: base64
"""
            )
            manifest = load_manifest_v2(path)
            revision = manifest.to_revision(created_at="2026-07-19T20:00:00Z")
            bundle = render_revision_bundle(
                manifest,
                revision,
                runtime_root=root / "runtime",
                secret_runtime_root=root / "runtime" / "run" / "secrets",
            )
            compose = yaml.safe_load(bundle[Path("compose.yml")])
            caddy = bundle[Path("caddy/routes.caddy")]

        service = compose["services"]["core"]
        self.assertEqual([4773, 4774], service["expose"])
        self.assertEqual("staging-123-abcdef", service["environment"]["OPHELIA_RELEASE_ID"])
        self.assertEqual("b" * 40, service["environment"]["OPHELIA_COMMIT_SHA"])
        self.assertEqual("/run/lumen/ca.pem", service["environment"]["LUMEN_CA_PATH"])
        self.assertTrue(any(value.endswith(":/run/lumen/ca.pem:ro") for value in service["volumes"]))
        self.assertIn("tls internal", caddy)
        self.assertIn("mode verify_if_given", caddy)
        self.assertIn("trust_pool file", caddy)
        self.assertIn("header_up X-Ophelia-Proxy-Authorization", caddy)
        self.assertIn("sha256:{tls_client_fingerprint}", caddy)


if __name__ == "__main__":
    unittest.main()
