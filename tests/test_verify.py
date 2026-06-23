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

    def __init__(self, body: bytes = b"ok"):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


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

    def test_command_verification_runs_even_when_tls_is_not_ready(self) -> None:
        manifest = Manifest(
            version=1,
            app="mixed-app",
            kind="service",
            environment="staging",
            profile=None,
            image="example/app:latest",
            services={},
            routes=[],
            verify=[
                VerificationCheck(name="public-health", url="https://example.com/health"),
                VerificationCheck(
                    name="internal-health",
                    type="command",
                    service="web",
                    command=["npm", "run", "ophelia:health", "--", "--json"],
                    expect_exit=0,
                    expect_json={"status": "ok"},
                ),
            ],
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
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            app_root = runtime_root / "apps" / "mixed-app"
            app_root.mkdir(parents=True)
            (app_root / "compose.yml").write_text("services:\n  web:\n    image: example/app\n")
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"status":"ok"}',
                stderr="",
            )
            with mock.patch("ophelia.verify._check_tls_readiness", return_value=tls_result), mock.patch(
                "ophelia.verify.urllib.request.urlopen"
            ) as urlopen, mock.patch("ophelia.verify.subprocess.run", return_value=completed) as run:
                payload = run_verifications(manifest, runtime_root=runtime_root, attempts=1)

        self.assertFalse(payload["ok"])
        self.assertEqual("certificate_obtain", payload["phase"])
        self.assertFalse(urlopen.called)
        run.assert_called_once()
        command_result = next(item for item in payload["results"] if item["type"] == "command")
        self.assertTrue(command_result["ok"])

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

    def test_verification_results_redact_secret_shaped_urls(self) -> None:
        manifest = Manifest(
            version=1,
            app="redact-url-app",
            kind="service",
            environment="staging",
            profile=None,
            image="example/app:latest",
            services={},
            routes=[],
            verify=[
                VerificationCheck(
                    name="health",
                    url="https://user:secret@example.com/health?token=abc#frag",
                )
            ],
        )

        with mock.patch("ophelia.verify.urllib.request.urlopen", return_value=_FakeResponse()):
            payload = run_verifications(manifest, wait_for_tls=False)

        result_url = payload["results"][0]["url"]
        self.assertEqual("https://<redacted>@example.com/health?<redacted>#<redacted>", result_url)
        self.assertNotIn("secret", json.dumps(payload))
        self.assertNotIn("token=abc", json.dumps(payload))

    def test_http_verification_can_assert_json_fields(self) -> None:
        manifest = Manifest(
            version=1,
            app="json-http-app",
            kind="service",
            environment="staging",
            profile=None,
            image="example/app:latest",
            services={},
            routes=[],
            verify=[
                VerificationCheck(
                    name="health",
                    url="http://example.com/ophelia/health",
                    expect_json={"status": "ok", "checks.runtime.ok": True},
                )
            ],
        )

        response = _FakeResponse(b'{"status":"ok","checks":{"runtime":{"ok":true}}}')
        with mock.patch("ophelia.verify.urllib.request.urlopen", return_value=response):
            payload = run_verifications(manifest, wait_for_tls=False)

        self.assertTrue(payload["ok"])
        assertions = payload["results"][0]["json_assertions"]
        self.assertEqual(["checks.runtime.ok", "status"], [item["path"] for item in assertions])
        self.assertTrue(all(item["ok"] for item in assertions))

    def test_command_verification_runs_inside_compose_service_and_asserts_json(self) -> None:
        manifest = Manifest(
            version=1,
            app="command-app",
            kind="service",
            environment="staging",
            profile=None,
            image="example/app:latest",
            services={},
            routes=[],
            verify=[
                VerificationCheck(
                    name="ophelia-health",
                    type="command",
                    service="web",
                    command=["npm", "run", "ophelia:health", "--", "--json"],
                    expect_exit=0,
                    expect_json={"status": "ok", "release.version": "1.2.3"},
                )
            ],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            app_root = runtime_root / "apps" / "command-app"
            app_root.mkdir(parents=True)
            (app_root / "compose.yml").write_text("services:\n  web:\n    image: example/app\n")

            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"status":"ok","release":{"version":"1.2.3"}}',
                stderr="",
            )
            with mock.patch("ophelia.verify.subprocess.run", return_value=completed) as run:
                payload = run_verifications(manifest, runtime_root=runtime_root, wait_for_tls=False)

        self.assertTrue(payload["ok"])
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(["docker", "compose", "-f", str(app_root / "compose.yml"), "exec", "-T", "web"], command[:7])
        self.assertEqual(["npm", "run", "ophelia:health", "--", "--json"], command[7:])
        result = payload["results"][0]
        self.assertEqual("command", result["type"])
        self.assertTrue(all(item["ok"] for item in result["json_assertions"]))

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

    def test_cli_verify_rejects_invalid_overrides(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                str(repo / "cli" / "ship"),
                "verify",
                "missing",
                "--attempts",
                "0",
                "--json",
            ],
            text=True,
            capture_output=True,
            cwd=repo,
        )

        self.assertNotEqual(0, result.returncode)
        payload = json.loads(result.stdout)
        self.assertIn("attempts", payload["error"])


if __name__ == "__main__":
    unittest.main()
