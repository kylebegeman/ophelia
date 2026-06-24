from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class ExplainTests(unittest.TestCase):
    def test_explain_supported_manifest_shapes(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        manifests = [
            repo / "examples" / "static-site.ophelia.yml",
            repo / "examples" / "service-app.ophelia.yml",
            repo / "examples" / "multi-service-app.ophelia.yml",
            repo / "examples" / "tunnel-app.ophelia.yml",
            repo / "manifests" / "demo-static-www.ophelia.yml",
            repo / "manifests" / "demo-service.ophelia.yml",
        ]

        for manifest_path in manifests:
            result = subprocess.run(
                [str(repo / "cli" / "ship"), "explain", str(manifest_path), "--json"],
                text=True,
                capture_output=True,
                check=True,
            )
            payload = json.loads(result.stdout)
            self.assertIn(payload["kind"], {"static", "service", "multi-service", "tunnel", "redirect"})
            self.assertIn("release_behavior", payload)
            self.assertIn("risk_notes", payload)


if __name__ == "__main__":
    unittest.main()
