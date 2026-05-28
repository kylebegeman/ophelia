from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.manifest import Manifest, VerificationCheck, VerificationPolicy
from ophelia.verify import run_verifications, verification_blocks_release


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return b"ok"


class VerifyTests(unittest.TestCase):
    def test_run_verifications_retries_until_checks_pass(self) -> None:
        manifest = Manifest(
            version=1,
            app="retry-app",
            kind="service",
            environment="staging",
            profile=None,
            image="example/app:latest",
            services={},
            routes=[],
            verify=[VerificationCheck(name="health", url="https://example.com/health")],
        )
        calls = 0

        def urlopen(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("not ready")
            return _FakeResponse()

        with mock.patch("ophelia.verify._check_tls_readiness", return_value=[]), mock.patch(
            "ophelia.verify.urllib.request.urlopen", side_effect=urlopen
        ), mock.patch("ophelia.verify.time.sleep") as sleep:
            payload = run_verifications(manifest, attempts=3, delay=5)

        self.assertTrue(payload["ok"])
        self.assertEqual("external_route_verify", payload["phase"])
        self.assertEqual(2, payload["attempt"])
        self.assertEqual(3, payload["attempts"])
        self.assertEqual(2, calls)
        sleep.assert_called_once_with(5)

    def test_run_verifications_reports_certificate_obtain_phase(self) -> None:
        manifest = Manifest(
            version=1,
            app="tls-app",
            kind="service",
            environment="staging",
            profile=None,
            image="example/app:latest",
            services={},
            routes=[],
            verify=[VerificationCheck(name="health", url="https://example.com/health")],
        )
        tls_result = [
            {
                "host": "example.com",
                "port": 443,
                "phase": "certificate_obtain",
                "ok": False,
                "error": "certificate not ready",
                "error_kind": "tls_handshake",
            }
        ]

        with mock.patch("ophelia.verify._check_tls_readiness", return_value=tls_result), mock.patch(
            "ophelia.verify.urllib.request.urlopen"
        ) as urlopen, mock.patch("ophelia.verify.time.sleep") as sleep:
            payload = run_verifications(manifest, attempts=2, delay=1)

        self.assertFalse(payload["ok"])
        self.assertEqual("certificate_obtain", payload["phase"])
        self.assertEqual("tls_handshake", payload["results"][0]["error_kind"])
        self.assertEqual(2, payload["attempt"])
        self.assertFalse(urlopen.called)
        sleep.assert_called_once_with(1)

    def test_warn_failure_mode_does_not_block_release(self) -> None:
        manifest = Manifest(
            version=1,
            app="warn-app",
            kind="service",
            environment="staging",
            profile=None,
            image="example/app:latest",
            services={},
            routes=[],
            verify=[VerificationCheck(name="health", url="http://example.com/health")],
            verify_policy=VerificationPolicy(attempts=1, interval=0, timeout=1, failure_mode="warn"),
        )

        with mock.patch("ophelia.verify.urllib.request.urlopen", side_effect=ConnectionRefusedError("refused")):
            payload = run_verifications(manifest, wait_for_tls=False)

        self.assertFalse(payload["ok"])
        self.assertEqual("warning", payload["status"])
        self.assertFalse(verification_blocks_release(payload))

    def test_cli_verify_app_name_updates_current_release(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            static_root = root / "static"
            static_root.mkdir()
            manifest_path = root / "verify-app.ophelia.yml"
            manifest_path.write_text(
                f"""
version: 1
app: verify-app
kind: static
static_root: {static_root}
routes:
  - domain: verify-app.example.com
verify:
  - name: health
    url: http://127.0.0.1:1/health
verify_policy:
  attempts: 1
  interval: 0
  timeout: 0.2
  failure_mode: warn
""".strip()
                + "\n"
            )
            subprocess.run(
                [
                    str(repo / "cli" / "ship"),
                    "deploy",
                    str(manifest_path),
                    "--runtime-root",
                    str(runtime_root),
                ],
                text=True,
                capture_output=True,
                check=True,
            )

            result = subprocess.run(
                [
                    str(repo / "cli" / "ship"),
                    "verify",
                    "verify-app",
                    "--runtime-root",
                    str(runtime_root),
                ],
                text=True,
                capture_output=True,
                check=True,
            )

            self.assertIn("phase=external_route_verify", result.stdout)
            release = json.loads((runtime_root / "apps" / "verify-app" / "release.json").read_text())
            self.assertFalse(release["verified"])
            self.assertEqual("warning", release["verification"]["status"])


if __name__ == "__main__":
    unittest.main()
