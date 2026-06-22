from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.findings import Finding, Remediation, attach_remediation, finding_from_issue
from ophelia.operation_schema import error_envelope, issue, operation_id


class RemediationTests(unittest.TestCase):
    def test_to_dict_shape(self) -> None:
        rem = Remediation(
            summary="Plan a restore drill.",
            commands=["ship app restore-drill plan dragon-writer --environment production --json"],
            docs=["docs/portable-app-pack-spec.md"],
        )
        payload = rem.to_dict()
        self.assertEqual("Plan a restore drill.", payload["summary"])
        self.assertEqual(1, len(payload["commands"]))
        self.assertFalse(payload["requires_human_approval"])
        self.assertNotIn("manifest_patch_hint", payload)

    def test_manifest_patch_hint_included_when_set(self) -> None:
        rem = Remediation(summary="x", manifest_patch_hint={"pack": {"portability": "critical"}})
        self.assertIn("manifest_patch_hint", rem.to_dict())


class FindingTests(unittest.TestCase):
    def test_rejects_unknown_severity(self) -> None:
        with self.assertRaises(ValueError):
            Finding(code="x", severity="fatal", area="data", message="m")

    def test_to_dict_is_superset_of_issue(self) -> None:
        legacy = issue("restore_drill_missing", "No drill receipt.", path="data.backups")
        finding = Finding(
            code="restore_drill_missing",
            severity="blocker",
            area="restore",
            message="No drill receipt.",
            path="data.backups",
            remediation=Remediation(summary="Plan a drill.", commands=["ship app restore-drill plan x --json"]),
        )
        payload = finding.to_dict()
        # Legacy keys preserved verbatim.
        for key in legacy:
            self.assertIn(key, payload)
            self.assertEqual(legacy[key], payload[key])
        # New keys added.
        self.assertEqual("blocker", payload["severity"])
        self.assertEqual("restore", payload["area"])
        self.assertIn("remediation", payload)

    def test_finding_from_issue_round_trips(self) -> None:
        legacy = issue("env_missing", "DATABASE_URL missing.", path="env")
        finding = finding_from_issue(legacy, severity="warning", area="env")
        payload = finding.to_dict()
        self.assertEqual("env_missing", payload["code"])
        self.assertEqual("warning", payload["severity"])
        self.assertEqual("env", payload["path"])


class ErrorEnvelopeTests(unittest.TestCase):
    def test_default_blocker_derived_from_code(self) -> None:
        env = error_envelope("bad input", "invalid_request")
        self.assertEqual("ophelia.error", env["kind"])
        self.assertEqual("failed", env["status"])
        self.assertEqual("bad input", env["error"])
        self.assertEqual([{"code": "invalid_request", "message": "bad input"}], env["blockers"])
        self.assertEqual([], env["warnings"])
        self.assertNotIn("next_actions", env)

    def test_next_actions_included_when_provided(self) -> None:
        env = error_envelope("x", "code", next_actions=["ship validate manifest --json"])
        self.assertEqual(["ship validate manifest --json"], env["next_actions"])


class OperationIdTests(unittest.TestCase):
    def test_operation_ids_are_unique_within_same_second(self) -> None:
        ids = {operation_id("deploy.apply", "app", "production") for _ in range(20)}
        self.assertEqual(20, len(ids))


class AttachRemediationTests(unittest.TestCase):
    def test_keeps_existing_keys(self) -> None:
        legacy = issue("c", "m", path="p")
        enriched = attach_remediation(legacy, Remediation(summary="fix"))
        self.assertEqual("c", enriched["code"])
        self.assertEqual("m", enriched["message"])
        self.assertEqual("p", enriched["path"])
        self.assertIn("remediation", enriched)


if __name__ == "__main__":
    unittest.main()
