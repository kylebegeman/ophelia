"""Regression tests for Batch 4 (phase 11) review fixes."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ophelia.observability as obs
from ophelia.manifest import load_manifest

_MANIFEST = (
    "version: 1\n"
    "app: obs-fix\n"
    "kind: service\n"
    "image: ghcr.io/example/obs-fix@sha256:abc\n"
    "services:\n"
    "  web:\n"
    "    port: 3000\n"
    "routes:\n"
    "  - domain: obs.example.com\n"
    "    service: web\n"
    "observability:\n"
    "  health:\n"
    "    url: https://obs.example.com/health\n"
    "    expect_status: 200\n"
)


class HealthProbeStatusCodeTests(unittest.TestCase):
    def test_http_error_preserves_status_code(self) -> None:
        original = obs.request.urlopen

        def _raise_503(*_args, **_kwargs):
            raise HTTPError("https://obs.example.com/health", 503, "Service Unavailable", {}, None)

        obs.request.urlopen = _raise_503  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                mp = root / "obs-fix.ophelia.yml"
                mp.write_text(_MANIFEST)
                report = obs.observability_status(
                    "obs-fix", "production", runtime_root=root / "rt", manifest_path=mp, probe_http=True, http_timeout=0.5
                )
        finally:
            obs.request.urlopen = original  # type: ignore[assignment]

        probe = report["health"]["probe"]
        # The 503 status code is preserved, not collapsed into a generic failure.
        self.assertEqual(503, probe.get("status_code"))
        self.assertFalse(probe.get("ok"))
        codes = {w["code"] for w in report["warnings"]}
        self.assertIn("observability_health_unexpected_status", codes)
        self.assertNotIn("observability_health_probe_failed", codes)


class MetricsFormatNoneTests(unittest.TestCase):
    def test_format_none_with_url_is_not_endpoint_missing(self) -> None:
        manifest_text = _MANIFEST + "  metrics:\n    url: http://svc:9000/metrics\n    format: none\n"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mp = root / "m.ophelia.yml"
            mp.write_text(manifest_text)
            load_manifest(mp)  # loads cleanly
            report = obs.observability_status("obs-fix", "production", runtime_root=root / "rt", manifest_path=mp)
        codes = {w["code"] for w in report["warnings"]}
        self.assertNotIn("observability_metrics_endpoint_missing", codes)


if __name__ == "__main__":
    unittest.main()
