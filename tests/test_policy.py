from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.config import REPO_ROOT
from ophelia.manifest import load_manifest
from ophelia.planning import deploy_plan
from ophelia.policy import (
    POLICY_RESULT_KIND,
    PolicyError,
    evaluate_policy,
    explain_policy,
    load_policy,
    validate_policy,
)


REPO_POLICY_PATH = REPO_ROOT / "config" / "ophelia-policy.yml"


def _write_yaml(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text)
    return path


class LoadAndValidateTests(unittest.TestCase):
    def test_repo_default_policy_validates_clean(self) -> None:
        policy = load_policy(REPO_POLICY_PATH)
        report = validate_policy(policy)
        self.assertEqual("ophelia.policy_validation", report["kind"])
        self.assertEqual([], report["blockers"])
        self.assertEqual("ok" if not report["warnings"] else "warn", report["status"])
        self.assertGreater(report["rules_checked"], 0)

    def test_missing_explicit_policy_path_raises(self) -> None:
        with self.assertRaises(PolicyError):
            load_policy(Path("/nonexistent/ophelia-policy.yml"))

    def test_invalid_policy_rules_not_list_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_yaml(Path(tmp), "p.yml", "version: 1\nrules: not-a-list\n")
            report = validate_policy(load_policy(path))
            self.assertEqual("blocked", report["status"])
            codes = {item["code"] for item in report["blockers"]}
            self.assertIn("policy_rules_not_list", codes)

    def test_rule_missing_severity_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_yaml(
                Path(tmp),
                "p.yml",
                "version: 1\nrules:\n  - id: r1\n    operation: deploy.apply\n    require:\n      plan_exists: true\n",
            )
            report = validate_policy(load_policy(path))
            self.assertEqual("blocked", report["status"])
            codes = {item["code"] for item in report["blockers"]}
            self.assertIn("policy_rule_invalid_severity", codes)

    def test_unknown_advisory_key_is_warning_not_blocker(self) -> None:
        # Fail-open: an unknown top-level key and an unknown rule key both become
        # warnings; the policy still validates without blockers.
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_yaml(
                Path(tmp),
                "p.yml",
                (
                    "version: 1\n"
                    "future_feature: enabled\n"
                    "rules:\n"
                    "  - id: r1\n"
                    "    operation: deploy.apply\n"
                    "    severity: blocker\n"
                    "    notes: advisory\n"
                    "    require:\n"
                    "      plan_exists: true\n"
                ),
            )
            report = validate_policy(load_policy(path))
            self.assertEqual([], report["blockers"])
            warning_codes = {item["code"] for item in report["warnings"]}
            self.assertIn("policy_unknown_top_level_key", warning_codes)
            self.assertIn("policy_unknown_rule_key", warning_codes)

    def test_unknown_condition_key_flagged_at_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_yaml(
                Path(tmp),
                "p.yml",
                (
                    "version: 1\n"
                    "rules:\n"
                    "  - id: r1\n"
                    "    operation: deploy.apply\n"
                    "    severity: warning\n"
                    "    require:\n"
                    "      no_such_condition: true\n"
                ),
            )
            report = validate_policy(load_policy(path))
            self.assertEqual([], report["blockers"])
            warning_codes = {item["code"] for item in report["warnings"]}
            self.assertIn("policy_unknown_condition_key", warning_codes)


class EvaluateTests(unittest.TestCase):
    def test_production_traffic_without_health_check_is_blocked(self) -> None:
        policy = load_policy(REPO_POLICY_PATH)
        result = evaluate_policy(
            "app.traffic.apply",
            "demo-service",
            "production",
            {"rollback_available": True},
            policy=policy,
        )
        self.assertEqual(POLICY_RESULT_KIND, result["kind"])
        self.assertEqual("blocked", result["status"])
        failing_ids = {rule["id"] for rule in result["rules"] if rule["status"] == "blocked"}
        self.assertIn("production-traffic-health-check", failing_ids)

    def test_production_traffic_with_health_and_rollback_is_ok(self) -> None:
        policy = load_policy(REPO_POLICY_PATH)
        result = evaluate_policy(
            "app.traffic.apply",
            "demo-service",
            "production",
            {
                "target_health_check": True,
                "rollback_available": True,
                "plan_exists": True,
                "confirmation_required": True,
                "json_receipts": True,
            },
            policy=policy,
        )
        self.assertEqual("ok", result["status"])
        self.assertEqual([], result["blockers"])
        statuses = {rule["id"]: rule["status"] for rule in result["rules"]}
        self.assertEqual("ok", statuses["production-traffic-health-check"])
        self.assertEqual("ok", statuses["default-production-requires-plan"])
        self.assertEqual("ok", statuses["default-production-requires-confirmation"])
        self.assertEqual("ok", statuses["default-require-json-receipts"])

    def test_production_defaults_are_enforced_for_mutating_operations(self) -> None:
        policy = load_policy(REPO_POLICY_PATH)
        result = evaluate_policy(
            "app.export.create",
            "demo-service",
            "production",
            {},
            policy=policy,
        )
        self.assertEqual("blocked", result["status"])
        failing_ids = {rule["id"] for rule in result["rules"] if rule["status"] == "blocked"}
        self.assertIn("default-production-requires-plan", failing_ids)
        self.assertIn("default-production-requires-confirmation", failing_ids)
        self.assertIn("default-require-json-receipts", failing_ids)

    def test_unknown_required_condition_fails_closed(self) -> None:
        # Fail-closed: a warning-severity rule whose required condition the engine
        # cannot evaluate is escalated to a blocker.
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_yaml(
                Path(tmp),
                "p.yml",
                (
                    "version: 1\n"
                    "rules:\n"
                    "  - id: mystery\n"
                    "    operation: deploy.apply\n"
                    "    environment: production\n"
                    "    severity: warning\n"
                    "    require:\n"
                    "      no_such_condition: true\n"
                ),
            )
            policy = load_policy(path)
            result = evaluate_policy("deploy.apply", "app", "production", {}, policy=policy)
            self.assertEqual("blocked", result["status"])
            codes = {item["code"] for item in result["blockers"]}
            self.assertIn("policy_unknown_required_condition", codes)

    def test_unknown_operation_has_no_matching_rules_and_is_ok(self) -> None:
        # An operation the policy does not mention is OK with zero matched rules;
        # the policy intentionally does not assert anything about unlisted ops.
        policy = load_policy(REPO_POLICY_PATH)
        result = evaluate_policy("some.unlisted.operation", None, "production", {}, policy=policy)
        self.assertEqual("ok", result["status"])
        self.assertEqual([], result["rules"])

    def test_environment_scoping_excludes_non_matching_environment(self) -> None:
        # The production-only traffic rule must not fire for staging.
        policy = load_policy(REPO_POLICY_PATH)
        result = evaluate_policy("app.traffic.apply", "app", "staging", {}, policy=policy)
        self.assertEqual([], result["rules"])
        self.assertEqual("ok", result["status"])

    def test_explain_reports_evaluable_flag(self) -> None:
        policy = load_policy(REPO_POLICY_PATH)
        explanation = explain_policy(policy)
        self.assertEqual("ophelia.policy_explanation", explanation["kind"])
        self.assertTrue(explanation["rules"])
        for rule in explanation["rules"]:
            for condition in rule["requires"]:
                self.assertIn("evaluable", condition)


class PlanIntegrationTests(unittest.TestCase):
    def _manifest(self, secret: str) -> str:
        return (
            """
version: 1
app: policy-test
environment: production
kind: service
image: ghcr.io/example/policy-test:latest
env:
  API_TOKEN: %s
services:
  web:
    port: 3000
routes:
  - domain: policy-test.example.com
    service: web
verify:
  - name: health
    url: https://policy-test.example.com/health
"""
            % secret
        ).strip() + "\n"

    def test_deploy_plan_includes_policy_check_under_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_root = root / "runtime"
            manifest_path = root / "policy-test.ophelia.yml"
            manifest_path.write_text(self._manifest("super-secret-value"))
            manifest = load_manifest(manifest_path)

            plan = deploy_plan(manifest, manifest_path, runtime_root)

            self.assertIn("checks", plan)
            policy_checks = [c for c in plan["checks"] if c.get("name") == "policy"]
            self.assertEqual(1, len(policy_checks))
            entry = policy_checks[0]
            self.assertEqual(POLICY_RESULT_KIND, entry["kind"])
            self.assertIsNotNone(entry["result"])
            self.assertEqual("deploy.apply", entry["result"]["operation"])


if __name__ == "__main__":
    unittest.main()
