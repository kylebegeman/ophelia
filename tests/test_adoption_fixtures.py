from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.adoption import adoption_plan


REPO_ROOT = Path(__file__).resolve().parents[1]
ADOPTION_FIXTURES = REPO_ROOT / "fixtures" / "adoption"


class AdoptionFixtureTests(unittest.TestCase):
    def test_all_adoption_fixture_repos_are_contract_ready(self) -> None:
        fixture_repos = sorted(path for path in ADOPTION_FIXTURES.iterdir() if path.is_dir())
        self.assertTrue(fixture_repos)

        for repo in fixture_repos:
            with self.subTest(repo=repo.name):
                plan = adoption_plan(
                    repo.name,
                    "staging",
                    repo_path=repo,
                    runtime_root=REPO_ROOT / "fixtures" / "app-suite" / "runtime",
                )

                self.assertEqual([], plan["blockers"])
                self.assertTrue(plan["pack_validation"]["ok"])
                self.assertTrue(all(item["present"] for item in plan["required_artifacts"]))
                gate_status = {item["id"]: item["status"] for item in plan["adoption_gates"]}
                self.assertEqual("passed", gate_status["manifest_contract"])
                self.assertEqual("passed", gate_status["repo_artifacts"])
                self.assertEqual("passed", gate_status["pack_validation"])
                self.assertEqual("ship", plan["next_commands"][0]["argv"][0])


if __name__ == "__main__":
    unittest.main()
