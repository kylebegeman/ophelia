from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.backup import apply_restore, backup_plan, create_backup, restore_plan
from ophelia.manifest import load_manifest
from ophelia.runtime import deploy_bundle


class BackupTests(unittest.TestCase):
    def test_backup_create_and_restore_preview_do_not_print_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest())
            manifest = load_manifest(manifest_path)
            app_root = deploy_bundle(manifest, manifest_path, runtime_root)
            (app_root / "env").write_text("SECRET_TOKEN=super-secret\n")

            plan = backup_plan(runtime_root, "backup-test")
            self.assertTrue(plan["can_apply"])
            self.assertNotIn("super-secret", str(plan))

            backup = create_backup(runtime_root, "backup-test", str(plan["confirmation_token"]))
            self.assertNotIn("super-secret", str(backup))
            backup_id = str(backup["backup_id"])

            restore = restore_plan(runtime_root, "backup-test", backup_id)
            self.assertTrue(restore["can_apply"])
            report = apply_restore(runtime_root, "backup-test", backup_id, str(restore["confirmation_token"]))

            self.assertFalse(report["active_runtime_modified"])
            self.assertTrue((Path(str(report["preview_path"])) / "restore-report.json").exists())
            self.assertIn("super-secret", (Path(str(backup["backup_path"])) / "runtime" / "env").read_text())


def _manifest() -> str:
    return """
version: 1
app: backup-test
kind: static
static_root: /tmp/backup-test-static
routes:
  - domain: backup-test.example.com
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
