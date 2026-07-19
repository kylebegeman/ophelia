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
  jobs:
    kind: worker
    artifact: app
    command: ["./worker"]
routes:
  - name: public
    domain: rendered-demo.example.com
    target: {{workload: web, port: 8080}}
secrets:
  - name: DATABASE_URL
    ref: secret://rendered-demo/production/database-url
"""
            )
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
        self.assertTrue(compose["services"]["web"]["read_only"])
        self.assertEqual(["ALL"], compose["services"]["web"]["cap_drop"])
        self.assertEqual(["no-new-privileges:true"], compose["services"]["web"]["security_opt"])
        self.assertNotIn("ophelia-edge", compose["services"]["jobs"]["networks"])
        self.assertIn("rendered-demo.example.com", caddy)
        self.assertIn(revision.revision_id.removeprefix("rev_")[:12], caddy)
        self.assertNotIn("secret_value", bundle[Path("manifest.lock.json")])
        refs = json.loads(bundle[Path("secret-refs.json")])
        self.assertEqual("secret://rendered-demo/production/database-url", refs["secrets"][0]["ref"])

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


if __name__ == "__main__":
    unittest.main()
