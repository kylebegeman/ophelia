from __future__ import annotations

import hashlib
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.execution.staging import StagingError, tree_digest
from ophelia.manifest import load_manifest
from ophelia.planning import deploy_plan


class DeployPlanReadOnlyTests(unittest.TestCase):
    def test_local_plan_changes_only_its_operation_staging_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest())

            protected = [
                runtime_root / "apps",
                runtime_root / "caddy",
                runtime_root / "static",
                runtime_root / "addons",
                runtime_root / "providers",
                runtime_root / "backups",
                runtime_root / "state",
            ]
            for surface in protected:
                surface.mkdir(parents=True)
                (surface / "sentinel").write_text(surface.name + "\n")
            before = {_relative_digest(surface, runtime_root) for surface in protected}

            with (
                patch("subprocess.run", side_effect=AssertionError("Docker/subprocess mutation")),
                patch("ophelia.caddy_manager.reload_caddy", side_effect=AssertionError("Caddy reload")),
                patch("ophelia.runtime.ensure_addons", side_effect=AssertionError("addon mutation")),
                patch(
                    "ophelia.portability._apply_traffic_provider_mutations",
                    side_effect=AssertionError("DNS/provider mutation"),
                ),
                patch("ophelia.runtime.activate_release", side_effect=AssertionError("active release mutation")),
                patch("ophelia.runtime._write_active_release", side_effect=AssertionError("active release write")),
                patch("ophelia.remote._sync_bundle", side_effect=AssertionError("live-directory rsync")),
            ):
                plan = deploy_plan(load_manifest(manifest_path), manifest_path, runtime_root)

            after = {_relative_digest(surface, runtime_root) for surface in protected}
            self.assertEqual(before, after)

            operation_root = (runtime_root / "staging" / plan["operation_id"]).resolve()
            self.assertEqual(operation_root, Path(plan["staging"]["root"]))
            self.assertEqual(operation_root / "candidate", Path(plan["staging"]["candidate"]))
            self.assertEqual(operation_root / "evidence", Path(plan["staging"]["evidence"]))
            self.assertEqual(plan["candidate_digest"], tree_digest(operation_root / "candidate"))
            self.assertTrue((operation_root / "candidate" / "manifest.lock.json").is_file())
            self.assertEqual(0o700, stat.S_IMODE(operation_root.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE((operation_root / "candidate").stat().st_mode))
            self.assertEqual(
                0o600,
                stat.S_IMODE((operation_root / "candidate" / "manifest.lock.json").stat().st_mode),
            )
            for artifact in plan["artifacts"]:
                self.assertTrue(Path(artifact["path"]).is_relative_to(operation_root))
                self.assertIn(artifact["sha256"], plan["evidence_digests"])
            self.assertTrue((operation_root / "plan-binding.json").is_file())

    def test_plan_evidence_write_failure_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest())
            with patch(
                "ophelia.execution.staging.OperationStaging.write_evidence",
                side_effect=StagingError("evidence unavailable"),
            ):
                with self.assertRaisesRegex(StagingError, "evidence unavailable"):
                    deploy_plan(load_manifest(manifest_path), manifest_path, root / "runtime")


def _relative_digest(surface: Path, runtime_root: Path) -> tuple[str, str]:
    digest = hashlib.sha256()
    for path in sorted(surface.rglob("*")):
        digest.update(path.relative_to(runtime_root).as_posix().encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return surface.relative_to(runtime_root).as_posix(), digest.hexdigest()


def _manifest() -> str:
    return """
version: 1
app: read-only-plan
environment: production
kind: service
image: ghcr.io/example/read-only-plan:latest
services:
  web:
    port: 3000
routes:
  - domain: read-only-plan.example.com
    service: web
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
