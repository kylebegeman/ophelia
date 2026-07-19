from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ophelia.manifest_v2 import ManifestV2Error, load_manifest_v2, migrate_v1_document


PINNED_IMAGE = (
    "ghcr.io/example/demo@sha256:"
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)


class ManifestV2Tests(unittest.TestCase):
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
