from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ophelia.api_routes import HTTP_ROUTE_PATTERNS  # noqa: E402
from ophelia.command_catalog import command_registry  # noqa: E402
from ophelia.live_hydration import (  # noqa: E402
    LIVE_HYDRATION_EVIDENCE_KIND,
    LIVE_HYDRATION_KIND,
    LIVE_HYDRATION_PROMOTION_PLAN_KIND,
    LIVE_HYDRATION_PROBE_GATE_KIND,
    LIVE_HYDRATION_SCAFFOLD_KIND,
    live_hydration_evidence_validate,
    live_hydration_promotion_plan,
    live_hydration_probe_gate,
    live_hydration_report,
    live_hydration_scaffold,
)
from ophelia.main import main  # noqa: E402


FIXTURE_PROFILES = REPO_ROOT / "fixtures" / "app-suite" / "live-drills.yml"
FIXTURE_REVIEWED_EVIDENCE = REPO_ROOT / "fixtures" / "app-suite" / "hydration" / "fixture-postgres-api" / "staging"
LOCAL_PROFILES = REPO_ROOT / "config" / "ophelia-live-drills.yml"


class LiveHydrationTests(unittest.TestCase):
    def test_fixture_postgres_hydration_reports_only_drift_step(self) -> None:
        report = live_hydration_report(profile="fixture-postgres-focused", profiles_path=FIXTURE_PROFILES)
        serialized = json.dumps(report, sort_keys=True)

        self.assertEqual(LIVE_HYDRATION_KIND, report["kind"])
        self.assertEqual("warning", report["status"])
        self.assertTrue(report["read_only"])
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["mutates_state"])
        self.assertEqual("fixture-postgres-api", report["app"])
        self.assertEqual("ok", report["sections"]["env"]["status"])
        self.assertEqual("ok", report["sections"]["secrets"]["status"])
        self.assertEqual("ok", report["sections"]["release"]["status"])
        self.assertEqual("ready", report["sections"]["host"]["status"])
        self.assertEqual(["refresh_or_review_runtime_drift", "rerun_live_profile"], [step["code"] for step in report["hydration_steps"]])
        self.assertIn("ship drift", report["hydration_steps"][0]["target"])
        self.assertIn("fixture-postgres-api.ophelia.yml", report["hydration_steps"][0]["target"])
        self.assertNotIn("fixture-postgres-token-value", serialized)
        self.assertNotIn("fixture-db-password", serialized)

    def test_local_quark_staging_hydration_is_blocked_and_actionable(self) -> None:
        report = live_hydration_report(profile="quark-ops-staging-file-baseline", profiles_path=LOCAL_PROFILES)
        step_codes = {step["code"] for step in report["hydration_steps"]}

        self.assertEqual("blocked", report["status"])
        self.assertEqual("quark-ops-staging", report["app"])
        self.assertEqual("staging", report["environment"])
        self.assertEqual(8, report["sections"]["env"]["required_count"])
        self.assertEqual(8, len(report["sections"]["env"]["missing_required"]))
        self.assertEqual(8, len(report["sections"]["secrets"]["missing_required"]))
        self.assertFalse(report["sections"]["release"]["active_present"])
        self.assertIn("hydrate_runtime_env_shape", step_codes)
        self.assertIn("record_secret_name_observations", step_codes)
        self.assertIn("record_release_metadata", step_codes)
        self.assertIn("complete_host_capability_inventory", step_codes)

    def test_cli_allow_blocked_preserves_blocked_payload_with_zero_exit(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(
                [
                    "live-hydration",
                    "report",
                    "--profile",
                    "quark-ops-staging-file-baseline",
                    "--profiles",
                    str(LOCAL_PROFILES),
                    "--allow-blocked",
                    "--json",
                ]
            )

        payload = json.loads(buffer.getvalue())

        self.assertEqual(0, exit_code)
        self.assertEqual("blocked", payload["status"])
        self.assertEqual("quark-ops-staging", payload["app"])

    def test_cli_catalog_and_api_route(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(
                [
                    "live-hydration",
                    "report",
                    "--profile",
                    "fixture-postgres-focused",
                    "--profiles",
                    str(FIXTURE_PROFILES),
                    "--json",
                ]
            )

        payload = json.loads(buffer.getvalue())
        operations = {descriptor.operation for descriptor in command_registry()}
        descriptor = next(descriptor for descriptor in command_registry() if descriptor.operation == "live_hydration.report")

        self.assertEqual(0, exit_code)
        self.assertEqual(LIVE_HYDRATION_KIND, payload["kind"])
        self.assertIn("live_hydration.report", operations)
        self.assertTrue(any("config/ophelia-live-drills.yml" in example for example in descriptor.examples))
        self.assertIn("/live-hydration/<profile>", HTTP_ROUTE_PATTERNS)

    def test_scaffold_dry_run_plans_template_files_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "hydration-kit"
            report = live_hydration_scaffold(
                profile="quark-ops-staging-file-baseline",
                profiles_path=LOCAL_PROFILES,
                output_dir=output_dir,
            )

            self.assertFalse(output_dir.exists())

        self.assertEqual(LIVE_HYDRATION_SCAFFOLD_KIND, report["kind"])
        self.assertEqual("warning", report["status"])
        self.assertTrue(report["read_only"])
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["mutates_state"])
        self.assertTrue(report["template_only"])
        self.assertEqual("blocked", report["hydration_status"])
        self.assertEqual(5, len(report["files"]))
        self.assertIn("DATABASE_URL=", next(item["content"] for item in report["files"] if item["kind"] == "runtime_env_template"))
        self.assertIn("github/secret-observations/quark-ops-staging.staging.json", report["target_paths"]["github_secret_observation"])
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("fixture-db-password", serialized)
        self.assertNotIn("runtime-secret-value", serialized)

    def test_scaffold_write_creates_templates_and_refuses_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "hydration-kit"
            first = live_hydration_scaffold(
                profile="quark-ops-staging-file-baseline",
                profiles_path=LOCAL_PROFILES,
                output_dir=output_dir,
                write=True,
            )
            second = live_hydration_scaffold(
                profile="quark-ops-staging-file-baseline",
                profiles_path=LOCAL_PROFILES,
                output_dir=output_dir,
                write=True,
            )
            forced = live_hydration_scaffold(
                profile="quark-ops-staging-file-baseline",
                profiles_path=LOCAL_PROFILES,
                output_dir=output_dir,
                write=True,
                force=True,
            )

            self.assertTrue((output_dir / "README.md").exists())
            self.assertTrue((output_dir / "env.required.template").exists())
            self.assertTrue((output_dir / "github-secret-observation.template.json").exists())
            self.assertTrue((output_dir / "release-metadata.template.json").exists())
            self.assertTrue((output_dir / "host-capabilities.template.yml").exists())
            self.assertIn("DATABASE_URL=", (output_dir / "env.required.template").read_text())
            secret_template = json.loads((output_dir / "github-secret-observation.template.json").read_text())

        self.assertEqual("warning", first["status"])
        self.assertFalse(first["read_only"])
        self.assertTrue(first["mutates_state"])
        self.assertTrue(all(item["written"] for item in first["files"]))
        self.assertEqual("blocked", second["status"])
        self.assertIn("live_hydration_scaffold_file_exists", {item["code"] for item in second["blockers"]})
        self.assertEqual("warning", forced["status"])
        self.assertTrue(secret_template["template"])
        self.assertTrue(secret_template["values_redacted"])
        self.assertIn({"name": "DATABASE_URL", "observed": False}, secret_template["environments"]["staging"]["secrets"])

    def test_cli_scaffold_and_catalog_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(
                    [
                        "live-hydration",
                        "scaffold",
                        "--profile",
                        "quark-ops-staging-file-baseline",
                        "--profiles",
                        str(LOCAL_PROFILES),
                        "--output-dir",
                        str(Path(temp_dir) / "kit"),
                        "--json",
                    ]
                )

        payload = json.loads(buffer.getvalue())
        operations = {descriptor.operation for descriptor in command_registry()}

        self.assertEqual(0, exit_code)
        self.assertEqual(LIVE_HYDRATION_SCAFFOLD_KIND, payload["kind"])
        self.assertIn("live_hydration.scaffold", operations)

    def test_validate_evidence_blocks_missing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report = live_hydration_evidence_validate(
                profile="quark-ops-staging-file-baseline",
                profiles_path=LOCAL_PROFILES,
                input_dir=Path(temp_dir) / "missing-kit",
            )

        self.assertEqual(LIVE_HYDRATION_EVIDENCE_KIND, report["kind"])
        self.assertEqual("blocked", report["status"])
        self.assertTrue(report["read_only"])
        self.assertFalse(report["mutates_state"])
        self.assertIn("live_hydration_evidence_dir_missing", {item["code"] for item in report["blockers"]})

    def test_validate_evidence_accepts_template_kit_with_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "hydration-kit"
            live_hydration_scaffold(
                profile="quark-ops-staging-file-baseline",
                profiles_path=LOCAL_PROFILES,
                output_dir=output_dir,
                write=True,
            )
            report = live_hydration_evidence_validate(
                profile="quark-ops-staging-file-baseline",
                profiles_path=LOCAL_PROFILES,
                input_dir=output_dir,
            )

        self.assertEqual("warning", report["status"])
        self.assertEqual(5, len(report["file_checks"]))
        codes = {item["code"] for item in report["warnings"]}
        self.assertIn("live_hydration_evidence_env_placeholder", codes)
        self.assertIn("live_hydration_evidence_secret_template", codes)
        self.assertNotIn("replace-with-real-release-id", json.dumps(report))

    def test_validate_evidence_blocks_secret_values_without_emitting_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "hydration-kit"
            live_hydration_scaffold(
                profile="quark-ops-staging-file-baseline",
                profiles_path=LOCAL_PROFILES,
                output_dir=output_dir,
                write=True,
            )
            (output_dir / "env.required.template").write_text("DATABASE_URL=postgres://user:supersecret@db/app\n")
            report = live_hydration_evidence_validate(
                profile="quark-ops-staging-file-baseline",
                profiles_path=LOCAL_PROFILES,
                input_dir=output_dir,
            )

        encoded = json.dumps(report)
        self.assertEqual("blocked", report["status"])
        self.assertIn("live_hydration_evidence_env_secret_value", {item["code"] for item in report["blockers"]})
        self.assertNotIn("supersecret", encoded)
        self.assertNotIn("postgres://user", encoded)

    def test_cli_validate_evidence_and_catalog_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "hydration-kit"
            live_hydration_scaffold(
                profile="quark-ops-staging-file-baseline",
                profiles_path=LOCAL_PROFILES,
                output_dir=output_dir,
                write=True,
            )
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(
                    [
                        "live-hydration",
                        "validate-evidence",
                        "--profile",
                        "quark-ops-staging-file-baseline",
                        "--profiles",
                        str(LOCAL_PROFILES),
                        "--input-dir",
                        str(output_dir),
                        "--json",
                    ]
                )

        payload = json.loads(buffer.getvalue())
        operations = {descriptor.operation for descriptor in command_registry()}

        self.assertEqual(0, exit_code)
        self.assertEqual(LIVE_HYDRATION_EVIDENCE_KIND, payload["kind"])
        self.assertIn("live_hydration.evidence.validate", operations)

    def test_promotion_plan_blocks_missing_evidence_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report = live_hydration_promotion_plan(
                profile="quark-ops-staging-file-baseline",
                profiles_path=LOCAL_PROFILES,
                input_dir=Path(temp_dir) / "missing-kit",
            )

        self.assertEqual(LIVE_HYDRATION_PROMOTION_PLAN_KIND, report["kind"])
        self.assertEqual("blocked", report["status"])
        self.assertTrue(report["read_only"])
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["mutates_state"])
        self.assertFalse(any(action["copy_performed"] for action in report["file_actions"]))
        self.assertIn("promotion_plan_evidence_blocked", {item["code"] for item in report["blockers"]})

    def test_promotion_plan_uses_hashes_and_paths_without_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "hydration-kit"
            live_hydration_scaffold(
                profile="quark-ops-staging-file-baseline",
                profiles_path=LOCAL_PROFILES,
                output_dir=output_dir,
                write=True,
            )
            report = live_hydration_promotion_plan(
                profile="quark-ops-staging-file-baseline",
                profiles_path=LOCAL_PROFILES,
                input_dir=output_dir,
            )

        encoded = json.dumps(report, sort_keys=True)
        actions_by_kind = {action["target_kind"]: action for action in report["file_actions"]}

        self.assertEqual("warning", report["status"])
        self.assertEqual(5, len(report["file_actions"]))
        self.assertTrue(report["future_apply_requires_confirmation"])
        self.assertTrue(report["manual_promotion_required"])
        self.assertEqual("blocked", report["probe_gate_status"])
        self.assertEqual("no_go", report["probe_gate_go_no_go"])
        self.assertIn("runtime_env", actions_by_kind)
        self.assertIn("active_release", actions_by_kind)
        self.assertIn("legacy_release", actions_by_kind)
        self.assertRegex(actions_by_kind["runtime_env"]["source"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("apps/quark-ops-staging/env", actions_by_kind["runtime_env"]["target"]["path"])
        self.assertFalse(any(action["copy_performed"] for action in report["file_actions"]))
        self.assertNotIn("DATABASE_URL=", encoded)
        self.assertNotIn("replace-with-real-release-id", encoded)

    def test_reviewed_fixture_evidence_is_probe_review_ready_without_mutation(self) -> None:
        validation = live_hydration_evidence_validate(
            profile="fixture-postgres-focused",
            profiles_path=FIXTURE_PROFILES,
            input_dir=FIXTURE_REVIEWED_EVIDENCE,
        )
        gate = live_hydration_probe_gate(
            profile="fixture-postgres-focused",
            profiles_path=FIXTURE_PROFILES,
            input_dir=FIXTURE_REVIEWED_EVIDENCE,
        )
        plan = live_hydration_promotion_plan(
            profile="fixture-postgres-focused",
            profiles_path=FIXTURE_PROFILES,
            input_dir=FIXTURE_REVIEWED_EVIDENCE,
        )
        encoded = json.dumps(plan, sort_keys=True)

        self.assertEqual("ok", validation["status"])
        self.assertEqual("warning", gate["status"])
        self.assertEqual("review", gate["go_no_go"])
        self.assertFalse(gate["probes_executed"])
        self.assertGreaterEqual(len(gate["probe_commands"]), 1)
        self.assertEqual("warning", plan["status"])
        self.assertEqual("ok", plan["evidence_status"])
        self.assertEqual("review", plan["probe_gate_go_no_go"])
        self.assertFalse(plan["mutates_state"])
        self.assertFalse(any(action["copy_performed"] for action in plan["file_actions"]))
        self.assertTrue(all(action["source"]["sha256"] for action in plan["file_actions"]))
        self.assertNotIn("fixture-postgres-token-value", encoded)
        self.assertNotIn("fixture-db-password", encoded)

    def test_promotion_plan_blocks_secret_values_without_emitting_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "hydration-kit"
            live_hydration_scaffold(
                profile="quark-ops-staging-file-baseline",
                profiles_path=LOCAL_PROFILES,
                output_dir=output_dir,
                write=True,
            )
            (output_dir / "env.required.template").write_text("DATABASE_URL=postgres://user:supersecret@db/app\n")
            report = live_hydration_promotion_plan(
                profile="quark-ops-staging-file-baseline",
                profiles_path=LOCAL_PROFILES,
                input_dir=output_dir,
            )

        encoded = json.dumps(report, sort_keys=True)

        self.assertEqual("blocked", report["status"])
        self.assertIn("promotion_plan_evidence_blocked", {item["code"] for item in report["blockers"]})
        self.assertNotIn("supersecret", encoded)
        self.assertNotIn("postgres://user", encoded)

    def test_cli_promotion_plan_and_catalog_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "hydration-kit"
            live_hydration_scaffold(
                profile="quark-ops-staging-file-baseline",
                profiles_path=LOCAL_PROFILES,
                output_dir=output_dir,
                write=True,
            )
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(
                    [
                        "live-hydration",
                        "promotion-plan",
                        "--profile",
                        "quark-ops-staging-file-baseline",
                        "--profiles",
                        str(LOCAL_PROFILES),
                        "--input-dir",
                        str(output_dir),
                        "--json",
                    ]
                )

        payload = json.loads(buffer.getvalue())
        operations = {descriptor.operation for descriptor in command_registry()}

        self.assertEqual(0, exit_code)
        self.assertEqual(LIVE_HYDRATION_PROMOTION_PLAN_KIND, payload["kind"])
        self.assertIn("live_hydration.promotion_plan", operations)

    def test_probe_gate_blocks_current_quark_staging_without_running_probes(self) -> None:
        report = live_hydration_probe_gate(profile="quark-ops-staging-file-baseline", profiles_path=LOCAL_PROFILES)

        self.assertEqual(LIVE_HYDRATION_PROBE_GATE_KIND, report["kind"])
        self.assertEqual("blocked", report["status"])
        self.assertEqual("no_go", report["go_no_go"])
        self.assertFalse(report["probes_executed"])
        self.assertFalse(report["mutates_state"])
        self.assertEqual([], report["probe_commands"])
        self.assertIn("probe_gate_hydration_blocked", {item["code"] for item in report["blockers"]})
        self.assertIn("probe_gate_evidence_blocked", {item["code"] for item in report["blockers"]})

    def test_probe_gate_for_fixture_review_emits_probe_commands_without_running_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "hydration-kit"
            live_hydration_scaffold(
                profile="fixture-postgres-focused",
                profiles_path=FIXTURE_PROFILES,
                output_dir=output_dir,
                write=True,
            )
            report = live_hydration_probe_gate(
                profile="fixture-postgres-focused",
                profiles_path=FIXTURE_PROFILES,
                input_dir=output_dir,
            )

        self.assertEqual("warning", report["status"])
        self.assertEqual("review", report["go_no_go"])
        self.assertFalse(report["probes_executed"])
        self.assertGreaterEqual(len(report["probe_commands"]), 1)
        command = report["probe_commands"][0]["command"]
        self.assertIn("--probe-http", command)
        self.assertIn("--check-docker", command)

    def test_cli_probe_gate_and_catalog_descriptor(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(
                [
                    "live-hydration",
                    "probe-gate",
                    "--profile",
                    "quark-ops-staging-file-baseline",
                    "--profiles",
                    str(LOCAL_PROFILES),
                    "--allow-blocked",
                    "--json",
                ]
            )

        payload = json.loads(buffer.getvalue())
        operations = {descriptor.operation for descriptor in command_registry()}

        self.assertEqual(0, exit_code)
        self.assertEqual(LIVE_HYDRATION_PROBE_GATE_KIND, payload["kind"])
        self.assertIn("live_hydration.probe_gate", operations)

    def test_profile_without_app_blocks(self) -> None:
        report = live_hydration_report(profile="local-file-baseline", profiles_path=LOCAL_PROFILES)
        self.assertEqual("blocked", report["status"])
        codes = {item["code"] for item in report["blockers"]}
        self.assertIn("live_hydration_profile_app_missing", codes)


if __name__ == "__main__":
    unittest.main()
