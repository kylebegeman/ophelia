from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.image_lock import image_lock_apply, image_lock_plan
from ophelia.main import main


class ImageLockTests(unittest.TestCase):
    def test_image_lock_plan_and_apply_write_digest_artifacts(self) -> None:
        digest = "sha256:" + ("a" * 64)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "app.ophelia.yml"
            lock_path = root / "release.image-lock.json"
            pinned_path = root / "app.pinned.ophelia.yml"
            manifest_path.write_text(_production_manifest(), encoding="utf-8")

            with mock.patch("ophelia.image_lock.subprocess.run", side_effect=_digest_resolver(digest)) as run:
                plan = image_lock_plan(manifest_path, output=lock_path, pinned_manifest=pinned_path)
                receipt = image_lock_apply(
                    manifest_path,
                    output=lock_path,
                    pinned_manifest=pinned_path,
                    confirm=str(plan["confirmation_token"]),
                )
                lock = json.loads(lock_path.read_text(encoding="utf-8"))
                pinned_manifest = pinned_path.read_text(encoding="utf-8")

        self.assertEqual([], plan["blockers"])
        self.assertEqual("succeeded", receipt["status"])
        self.assertTrue(plan["all_pinned"])
        self.assertEqual(2, run.call_count)
        self.assertEqual(
            [{"path": "image", "service": "default", "from": "ghcr.io/example/image-lock-demo:1.2.3", "to": f"ghcr.io/example/image-lock-demo@{digest}"}],
            plan["changes"],
        )
        self.assertEqual("ophelia.image_lock", lock["kind"])
        self.assertEqual(f"ghcr.io/example/image-lock-demo@{digest}", lock["changes"][0]["to"])
        self.assertIn(f"image: ghcr.io/example/image-lock-demo@{digest}", pinned_manifest)

    def test_image_lock_cli_emits_json_and_blocks_unresolved_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_production_manifest(), encoding="utf-8")
            buffer = io.StringIO()

            with mock.patch("ophelia.image_lock.subprocess.run", return_value=subprocess.CompletedProcess(["docker"], 1, "", "token=super-secret")):
                with contextlib.redirect_stdout(buffer):
                    exit_code = main(["release", "image-lock", "plan", str(manifest_path), "--json"])

        payload = json.loads(buffer.getvalue())
        self.assertEqual(1, exit_code)
        self.assertEqual("release.image-lock.plan", payload["operation"])
        self.assertEqual("image_digest_unresolved", payload["blockers"][0]["code"])
        self.assertNotIn("super-secret", json.dumps(payload))
        self.assertIn("<redacted>", json.dumps(payload))


def _digest_resolver(digest: str):
    def _run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, f"Name: ghcr.io/example/image-lock-demo:1.2.3\nDigest: {digest}\n", "")

    return _run


def _production_manifest() -> str:
    return """
version: 1
app: image-lock-demo
environment: production
kind: service
image: ghcr.io/example/image-lock-demo:1.2.3
services:
  web:
    port: 3000
routes:
  - domain: image-lock-demo.example.com
    service: web
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
