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
            repo / "examples" / "portfolio.ophelia.yml",
            repo / "examples" / "dragonwriter.ophelia.yml",
            repo / "examples" / "pokedex.ophelia.yml",
            repo / "examples" / "aspectavy-staging.ophelia.yml",
            repo / "manifests" / "bagels-top-www.ophelia.yml",
            repo / "manifests" / "quark-ops-staging.ophelia.yml",
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
