from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from ophelia.manifest_v2 import (
    ManifestV2Error,
    load_manifest_v2,
    migrate_v1_document,
    resource_memory_bytes,
)


PINNED_IMAGE = (
    "ghcr.io/example/demo@sha256:"
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)


class ManifestV2Tests(unittest.TestCase):
    def test_memory_quantities_have_unambiguous_byte_semantics(self) -> None:
        expected = {
            "1k": 1024,
            "2Mi": 2 * 1024**2,
            "3GiB": 3 * 1024**3,
            "4GB": 4 * 1000**3,
            "5P": 5 * 1000**5,
        }

        for quantity, byte_count in expected.items():
            with self.subTest(quantity=quantity):
                self.assertEqual(byte_count, resource_memory_bytes(quantity))

    def test_stable_data_aliases_require_data_network_and_recreate_updates(self) -> None:
        valid = self._load(
            f"""
version: 2
app: database-service
environment: staging
artifacts:
  database: {{image: "{PINNED_IMAGE}"}}
workloads:
  postgres:
    kind: internal
    artifact: database
    port: 5432
    networks: [data]
    network_aliases:
      data: [database-service-postgres]
routes: []
update: {{strategy: recreate}}
"""
        )

        self.assertEqual(
            (("data", ("database-service-postgres",)),),
            valid.workload("postgres").network_aliases,
        )

        missing_network = f"""
version: 2
app: database-service
environment: staging
artifacts:
  database: {{image: "{PINNED_IMAGE}"}}
workloads:
  postgres:
    kind: internal
    artifact: database
    port: 5432
    network_aliases:
      data: [database-service-postgres]
routes: []
update: {{strategy: recreate}}
"""
        with self.assertRaisesRegex(ManifestV2Error, "requires the workload to join"):
            self._load(missing_network)

        overlapping = missing_network.replace(
            "    network_aliases:\n",
            "    networks: [data]\n    network_aliases:\n",
        ).replace("kind: internal", "kind: web").replace("recreate", "blue_green")
        with self.assertRaisesRegex(ManifestV2Error, "require update.strategy recreate"):
            self._load(overlapping)

        invalid_alias = missing_network.replace(
            "    network_aliases:\n",
            "    networks: [data]\n    network_aliases:\n",
        ).replace("database-service-postgres", "DATABASE_SERVICE")
        with self.assertRaisesRegex(ManifestV2Error, "lowercase alphanumeric"):
            self._load(invalid_alias)

        duplicate_alias = valid.to_lock_dict()
        duplicate_alias["workloads"]["replica"] = {
            **duplicate_alias["workloads"]["postgres"],
        }
        with self.assertRaisesRegex(ManifestV2Error, "unique across workloads"):
            self._load(yaml.safe_dump(duplicate_alias))

    def test_loads_and_normalizes_all_workload_kinds(self) -> None:
        manifest = self._load(
            f"""
version: 2
app: demo-service
environment: production
artifacts:
  app-image:
    image: {PINNED_IMAGE}
workloads:
  web:
    kind: web
    artifact: app-image
    command: ["./server"]
    port: 8080
    readiness:
      http:
        path: /ready
        port: 8080
      timeout_seconds: 90
    resources:
      memory: 512Mi
      cpu: "1.0"
      pids: 256
    security:
      run_as_non_root: true
      read_only_root: true
      no_new_privileges: true
      drop_capabilities: [ALL]
  jobs:
    kind: worker
    artifact: app-image
    command: ["./worker"]
    update:
      overlap: forbid
  cleanup:
    kind: cron
    artifact: app-image
    command: ["./cleanup"]
    schedule: "17 3 * * *"
    concurrency_policy: forbid
  maintenance:
    kind: task
    artifact: app-image
    command: ["./maintenance"]
  private-api:
    kind: internal
    artifact: app-image
    command: ["./private-api"]
    port: 8081
migrations:
  pre-traffic:
    workload:
      kind: migration
      artifact: app-image
      command: ["./migrate"]
    compatibility: backward_compatible
    timeout_seconds: 300
routes:
  - name: public
    domain: demo-service.example.com
    target:
      workload: web
      port: 8080
update:
  strategy: blue_green
  auto_rollback: true
  drain_seconds: 30
secrets:
  - name: DATABASE_URL
    ref: secret://demo-service/production/database-url
"""
        )

        self.assertEqual(2, manifest.version)
        self.assertEqual("blue_green", manifest.update.strategy)
        self.assertEqual(
            ["cleanup", "jobs", "maintenance", "private-api", "web"],
            [item.name for item in manifest.workloads],
        )
        self.assertEqual(["pre-traffic"], [item.name for item in manifest.migrations])
        self.assertEqual(("public",), manifest.workload("web").route_ids)
        self.assertEqual("sha256:" + "0123456789abcdef" * 4, manifest.artifact("app-image").digest)
        self.assertEqual(manifest.canonical_digest(), manifest.canonical_digest())

    def test_rejects_unknown_keys_and_unpinned_production_images(self) -> None:
        unknown = f"""
version: 2
app: invalid
environment: staging
artifacts:
  app:
    image: {PINNED_IMAGE}
workloads:
  web:
    kind: web
    artifact: app
    port: 8080
    mystery: true
routes: []
"""
        with self.assertRaisesRegex(ManifestV2Error, "workloads.web.mystery"):
            self._load(unknown)

        mutable = unknown.replace("staging", "production").replace(PINNED_IMAGE, "ghcr.io/example/demo:latest").replace("    mystery: true\n", "")
        with self.assertRaisesRegex(ManifestV2Error, "digest-pinned"):
            self._load(mutable)

    def test_security_profiles_are_strict_and_default_to_runtime_confinement(self) -> None:
        manifest = self._load(
            f"""
version: 2
app: sandbox-runner
environment: staging
artifacts: {{app: {{image: "{PINNED_IMAGE}"}}}}
workloads:
  runner:
    kind: worker
    artifact: app
    security:
      run_as_user: 1000
      no_new_privileges: false
      privileged: true
      seccomp_profile: unconfined
      apparmor_profile: unconfined
      add_capabilities: [SYS_ADMIN, SETUID, SETGID, DAC_OVERRIDE]
    devices:
      - {{source: /dev/net/tun}}
routes: []
"""
        )
        security = manifest.workload("runner").security
        self.assertFalse(security.no_new_privileges)
        self.assertTrue(security.privileged)
        self.assertEqual(1000, security.run_as_user)
        self.assertEqual("unconfined", security.seccomp_profile)
        self.assertEqual("unconfined", security.apparmor_profile)
        self.assertEqual(
            ("DAC_OVERRIDE", "SETGID", "SETUID", "SYS_ADMIN"),
            security.add_capabilities,
        )
        self.assertEqual("/dev/net/tun", manifest.workload("runner").devices[0].target)

        invalid = f"""
version: 2
app: sandbox-runner
environment: staging
artifacts: {{app: {{image: "{PINNED_IMAGE}"}}}}
workloads:
  runner:
    kind: worker
    artifact: app
    security: {{seccomp_profile: custom.json}}
routes: []
"""
        with self.assertRaisesRegex(ManifestV2Error, "seccomp_profile"):
            self._load(invalid)

        unsafe_privileged = invalid.replace(
            "security: {seccomp_profile: custom.json}",
            "security: {privileged: true, no_new_privileges: false, "
            "seccomp_profile: unconfined, apparmor_profile: unconfined}",
        )
        with self.assertRaisesRegex(ManifestV2Error, "explicit non-zero run_as_user"):
            self._load(unsafe_privileged)

        unsafe_security_cases = {
            "root uid": (
                "{run_as_user: 0}",
                "run_as_user must be non-zero",
            ),
            "privilege escalation remains blocked": (
                "{privileged: true, run_as_user: 1000, seccomp_profile: unconfined, "
                "apparmor_profile: unconfined}",
                "requires no_new_privileges: false",
            ),
            "implicit profiles": (
                "{privileged: true, run_as_user: 1000, no_new_privileges: false}",
                "requires explicit unconfined",
            ),
            "all capabilities": (
                "{add_capabilities: [ALL]}",
                "may not grant ALL",
            ),
        }
        for label, (security_yaml, message) in unsafe_security_cases.items():
            with self.subTest(label=label):
                candidate = invalid.replace(
                    "security: {seccomp_profile: custom.json}",
                    "security: " + security_yaml,
                )
                with self.assertRaisesRegex(ManifestV2Error, message):
                    self._load(candidate)

        unsafe_device = invalid.replace(
            "security: {seccomp_profile: custom.json}",
            "devices: [{source: /dev/../etc/shadow}]",
        )
        with self.assertRaisesRegex(ManifestV2Error, "bounded /dev paths"):
            self._load(unsafe_device)

    def test_rejects_invalid_workload_route_and_update_combinations(self) -> None:
        cases = {
            "worker route": f"""
version: 2
app: invalid
environment: staging
artifacts: {{app: {{image: "{PINNED_IMAGE}"}}}}
workloads:
  jobs: {{kind: worker, artifact: app}}
routes:
  - name: public
    domain: invalid.example.com
    target: {{workload: jobs, port: 8080}}
""",
            "blue green without web": f"""
version: 2
app: invalid
environment: staging
artifacts: {{app: {{image: "{PINNED_IMAGE}"}}}}
workloads:
  jobs: {{kind: worker, artifact: app}}
routes: []
update: {{strategy: blue_green}}
""",
            "cron without schedule": f"""
version: 2
app: invalid
environment: staging
artifacts: {{app: {{image: "{PINNED_IMAGE}"}}}}
workloads:
  cleanup: {{kind: cron, artifact: app}}
routes: []
update: {{strategy: recreate}}
""",
        }
        for name, value in cases.items():
            with self.subTest(name=name), self.assertRaises(ManifestV2Error):
                self._load(value)

    def test_compiles_to_kernel_revision_with_stable_semantics(self) -> None:
        manifest = self._load(
            f"""
version: 2
app: compile-demo
environment: staging
artifacts:
  app: {{image: "{PINNED_IMAGE}"}}
workloads:
  web: {{kind: web, artifact: app, port: 8080}}
  jobs:
    kind: worker
    artifact: app
    update: {{overlap: allow}}
routes:
  - name: public
    domain: compile-demo.example.com
    target: {{workload: web, port: 8080}}
"""
        )

        revision = manifest.to_revision(created_at="2026-07-19T12:00:00Z")

        self.assertEqual("compile-demo", revision.app)
        self.assertEqual("manifest-v2", revision.renderer_version)
        self.assertEqual(["jobs", "web"], [item.workload_id for item in revision.workloads])
        self.assertTrue(revision.workloads[0].overlap_safe)
        self.assertEqual(("public",), revision.workloads[1].route_ids)

    def test_models_release_endpoints_file_secrets_and_mtls_edge(self) -> None:
        manifest = self._load(
            f"""
version: 2
app: lumen-staging
environment: staging
release:
  id: staging-123-abcdef
  commit_sha: {"a" * 40}
  build_time: 2026-07-19T20:00:00Z
artifacts:
  server: {{image: "{PINNED_IMAGE}"}}
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

        self.assertEqual((("runner-control", 4774),), manifest.workload("core").endpoints)
        self.assertEqual("staging-123-abcdef", manifest.release.release_id)
        self.assertEqual("internal", manifest.routes[0].tls.mode)
        self.assertEqual("verify_if_given", manifest.routes[1].client_auth.mode)
        self.assertEqual("file", manifest.secrets[0].mode)
        self.assertEqual(
            (
                "secret://lumen-staging/staging/lumen-ca-base64",
                "secret://lumen-staging/staging/lumen-proxy-token",
            ),
            manifest.secret_references(),
        )

    def test_rejects_routes_to_undeclared_endpoint_ports(self) -> None:
        value = f"""
version: 2
app: invalid-endpoint
environment: staging
artifacts: {{app: {{image: "{PINNED_IMAGE}"}}}}
workloads:
  web: {{kind: web, artifact: app, port: 8080, endpoints: {{metrics: 9090}}}}
routes:
  - name: invalid
    domain: invalid.example.com
    target: {{workload: web, port: 7070}}
"""
        with self.assertRaisesRegex(ManifestV2Error, "named endpoint"):
            self._load(value)

    def test_migrates_v1_service_to_explicit_v2_candidate(self) -> None:
        candidate = migrate_v1_document(
            {
                "version": 1,
                "app": "legacy-demo",
                "environment": "staging",
                "kind": "service",
                "image": PINNED_IMAGE,
                "required_env": ["DATABASE_URL"],
                "services": {
                    "web": {"port": 8080, "command": ["./server"]},
                    "worker": {"port": 9000, "command": ["./worker"]},
                },
                "routes": [
                    {
                        "domain": "legacy-demo.example.com",
                        "service": "web",
                    }
                ],
            }
        )

        self.assertEqual(2, candidate["version"])
        self.assertEqual("web", candidate["workloads"]["web"]["kind"])
        self.assertEqual("internal", candidate["workloads"]["worker"]["kind"])
        self.assertEqual(
            "secret://legacy-demo/staging/database-url",
            candidate["secrets"][0]["ref"],
        )

    def _load(self, value: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.ophelia.yml"
            path.write_text(value)
            return load_manifest_v2(path)


if __name__ == "__main__":
    unittest.main()
