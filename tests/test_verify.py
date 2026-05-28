from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.manifest import Manifest, VerificationCheck
from ophelia.verify import run_verifications


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

        with mock.patch("ophelia.verify.urllib.request.urlopen", side_effect=urlopen), mock.patch(
            "ophelia.verify.time.sleep"
        ) as sleep:
            payload = run_verifications(manifest, attempts=3, delay=5)

        self.assertTrue(payload["ok"])
        self.assertEqual(2, payload["attempt"])
        self.assertEqual(3, payload["attempts"])
        self.assertEqual(2, calls)
        sleep.assert_called_once_with(5)


if __name__ == "__main__":
    unittest.main()
