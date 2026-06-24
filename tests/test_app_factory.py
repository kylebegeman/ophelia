from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia import app_factory as af
from ophelia.manifest import load_manifest
from ophelia.schema_export import manifest_json_schema

REPO_ROOT = Path(__file__).resolve().parents[1]

# Secret VALUES that must never appear in any generated file. (The NAMES are
# fine; only embedded values are forbidden.)
FORBIDDEN_SECRET_VALUES = [
    "ghp_",
    "ghs_",
    "github_pat_",
    "BEGIN OPENSSH PRIVATE KEY",
    "BEGIN RSA PRIVATE KEY",
    "BEGIN PRIVATE KEY",
]


def _jsonschema_available() -> bool:
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        return False
    return True


def _snapshot(root: Path) -> set:
    return {p for p in root.rglob("*")}


class TemplateDiscoveryTests(unittest.TestCase):
    def test_templates_list_carries_kind_and_required_templates(self) -> None:
        report = af.templates_list()
        self.assertEqual(report["kind"], "ophelia.app_templates")
        names = {t["name"] for t in report["templates"]}
        for required in {
            "static-site",
            "docker-web",
            "web-postgres",
            "web-redis",
            "worker",
            "critical-data",
        }:
            self.assertIn(required, names)
        for template in report["templates"]:
            self.assertIn("summary", template)
            self.assertIn("requires_secrets", template)
            self.assertIn("files", template)
            self.assertTrue(json.dumps(template))  # JSON-serializable

    def test_templates_explain_unknown_returns_error_blocker(self) -> None:
        report = af.templates_explain("does-not-exist")
        self.assertEqual(report["kind"], "ophelia.error")
        codes = {b["code"] for b in report["blockers"]}
        self.assertIn("unknown_template", codes)

    def test_templates_explain_known_carries_release_policy(self) -> None:
        report = af.templates_explain("web-postgres")
        self.assertEqual(report["kind"], "ophelia.app_template")
        self.assertEqual(report["manifest_kind"], "service")
        self.assertIn("OPHELIA_DEPLOY_KEY", report["requires_secrets"])
        policy = report["release_label_policy"]
        self.assertIn("release:patch", policy["valid_labels"])

    def test_static_template_declares_repo_local_asset_root(self) -> None:
        report = af.templates_explain("static-site")
        self.assertIn("public/index.html", report["files"])
        plan = af.create_plan("demo-app", "static-site")
        manifest = plan["manifest_preview"]["text"]
        self.assertIn("static_root: public", manifest)
        self.assertNotIn("ophelia-runtime/static", manifest)


class CreatePlanTests(unittest.TestCase):
    def test_create_plan_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            before = _snapshot(root)
            plan = af.create_plan(
                "demo-app", "web-postgres", owner="personal", runtime_root=runtime_root
            )
            after = _snapshot(root)
            self.assertEqual(before, after, "create_plan must not write any file")
            self.assertEqual(plan["kind"], "ophelia.app_create_plan")
            self.assertTrue(plan["confirmation_required"])
            self.assertTrue(plan["confirmation_token"])
            self.assertEqual(plan["exact_apply_input"]["app"], "demo-app")

    def test_create_plan_unknown_template_blocks_without_token(self) -> None:
        plan = af.create_plan("demo-app", "totally-unknown")
        self.assertTrue(plan["blockers"])
        codes = {b["code"] for b in plan["blockers"]}
        self.assertIn("unknown_template", codes)
        self.assertIsNone(plan["confirmation_token"])

    def test_plan_includes_github_release_model(self) -> None:
        plan = af.create_plan("demo-app", "docker-web")
        github = plan["github"]
        self.assertEqual(github["branch_policy"]["next"]["deploys_to"], "staging")
        self.assertEqual(github["branch_policy"]["master"]["deploys_to"], "production")
        self.assertTrue(github["branch_policy"]["master"]["release_label_required"])
        self.assertEqual(github["required_review_count"], 1)
        self.assertIn("release:major", github["release_label_validation"])
        self.assertIn("staging", github["environments"])
        self.assertIn("production", github["environments"])
        # GitHub provisioning is described, never executed.
        self.assertFalse(github["provisioning"]["executed"])
        for command in github["provisioning"]["commands"]:
            self.assertFalse(command["executed"])
        # Required secrets referenced by name only.
        self.assertIn("OPHELIA_DEPLOY_KEY", github["required_secrets"])

    def test_production_release_requires_release_label_policy(self) -> None:
        # With the policy present, the production path is allowed.
        plan = af.create_plan("demo-app", "docker-web", environment="production")
        self.assertFalse(plan["blockers"])
        # Model the missing-policy production case: a production plan with no
        # release-label policy must surface a release_label_required blocker.
        original = af._release_label_policy

        def _empty_policy() -> dict:
            policy = original()
            policy["valid_labels"] = []
            return policy

        af._release_label_policy = _empty_policy  # type: ignore[assignment]
        try:
            blocked = af.create_plan("demo-app", "docker-web", environment="production")
        finally:
            af._release_label_policy = original  # type: ignore[assignment]
        codes = {b["code"] for b in blocked["blockers"]}
        self.assertIn("release_label_required", codes)
        self.assertIsNone(blocked["confirmation_token"])


class GitHubProvisioningTests(unittest.TestCase):
    def test_github_provision_plan_has_typed_commands_and_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = af.github_provision_plan(
                "demo-app",
                "docker-web",
                owner="example",
                repo="example/demo-app",
                phase="all",
                runtime_root=Path(tmp),
            )

        self.assertEqual("ophelia.github_provision_plan", plan["kind"])
        self.assertTrue(plan["confirmation_required"])
        self.assertTrue(plan["confirmation_token"])
        self.assertTrue(plan["commands"])
        for command in plan["commands"]:
            self.assertIsInstance(command["argv"], list)
            self.assertEqual("gh", command["argv"][0])
            self.assertFalse(command["executed"])
        codes = {warning["code"] for warning in plan["warnings"]}
        self.assertIn("github_branch_protection_requires_branches", codes)

    def test_github_provision_apply_runs_commands_and_writes_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp) / "runtime"
            plan = af.github_provision_plan(
                "demo-app",
                "static-site",
                owner="example",
                repo="example/demo-app",
                phase="repo",
                runtime_root=runtime_root,
            )
            calls: list[dict] = []

            def _runner(command: dict, timeout: float) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                return subprocess.CompletedProcess(command["argv"], 0, stdout="", stderr="")

            receipt = af.github_provision_apply(
                "demo-app",
                "static-site",
                owner="example",
                repo="example/demo-app",
                phase="repo",
                runtime_root=runtime_root,
                confirm=plan["confirmation_token"],
                command_runner=_runner,
            )

            self.assertEqual("succeeded", receipt["status"])
            self.assertEqual("app.github.provision.apply", receipt["operation"])
            self.assertEqual(["create_repo"], [call["id"] for call in calls])
            self.assertEqual("succeeded", receipt["steps"][0]["status"])
            self.assertTrue((runtime_root / "github" / "provisioning").exists())
            self.assertTrue((runtime_root / "apps" / "demo-app" / "receipts").exists())

    def test_github_provision_apply_rejects_wrong_token_without_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls: list[dict] = []

            def _runner(command: dict, timeout: float) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                return subprocess.CompletedProcess(command["argv"], 0, stdout="", stderr="")

            receipt = af.github_provision_apply(
                "demo-app",
                "static-site",
                owner="example",
                repo="example/demo-app",
                phase="repo",
                runtime_root=Path(tmp),
                confirm="wrong",
                command_runner=_runner,
            )

            self.assertEqual("blocked", receipt["status"])
            self.assertEqual([], calls)
            codes = {blocker["code"] for blocker in receipt["blockers"]}
            self.assertIn("confirmation_token_mismatch", codes)


class ManifestValidityTests(unittest.TestCase):
    def test_every_template_manifest_loads(self) -> None:
        for name in af.TEMPLATES:
            with self.subTest(template=name):
                plan = af.create_plan("demo-app", name)
                text = plan["manifest_preview"]["text"]
                with tempfile.TemporaryDirectory() as temp_dir:
                    path = Path(temp_dir) / ".ophelia.yml"
                    path.write_text(text)
                    manifest = load_manifest(path)  # must not raise
                    self.assertEqual(manifest.app, "demo-app")

    @unittest.skipUnless(_jsonschema_available(), "jsonschema not installed")
    def test_every_template_manifest_matches_schema(self) -> None:
        import jsonschema

        schema = manifest_json_schema()
        for name in af.TEMPLATES:
            with self.subTest(template=name):
                plan = af.create_plan("demo-app", name)
                parsed = plan["manifest_preview"]["parsed"]
                jsonschema.validate(parsed, schema)


class CreateApplyTests(unittest.TestCase):
    def test_apply_writes_only_under_target_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            target = root / "work" / "demo-app"
            plan = af.create_plan("demo-app", "critical-data", runtime_root=runtime_root)
            receipt = af.create_apply(
                plan, plan["confirmation_token"], target, runtime_root=runtime_root
            )
            self.assertEqual(receipt["status"], "succeeded")
            self.assertEqual(receipt["kind"], "ophelia.receipt")
            self.assertEqual(receipt["operation"], "app.create.apply")
            resolved_target = target.resolve()
            stray = [
                p
                for p in root.rglob("*")
                if p.is_file() and not str(p.resolve()).startswith(str(resolved_target))
            ]
            self.assertEqual(stray, [], "apply wrote files outside target_dir")
            # Core scaffold files present.
            self.assertTrue((target / ".ophelia.yml").exists())
            self.assertTrue((target / ".github" / "workflows" / "ophelia-staging.yml").exists())
            self.assertTrue((target / "ophelia" / "create-receipt.json").exists())
            # Written manifest loads.
            load_manifest(target / ".ophelia.yml")

    def test_apply_rejects_wrong_token_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            target = root / "work"
            plan = af.create_plan("demo-app", "docker-web", runtime_root=runtime_root)
            receipt = af.create_apply(plan, "WRONG", target, runtime_root=runtime_root)
            self.assertEqual(receipt["status"], "blocked")
            codes = {b["code"] for b in receipt["blockers"]}
            self.assertIn("confirmation_token_mismatch", codes)
            self.assertFalse(target.exists() and any(target.rglob("*")))

    def test_apply_rejects_empty_token_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            target = root / "work"
            plan = af.create_plan("demo-app", "docker-web", runtime_root=runtime_root)
            receipt = af.create_apply(plan, "", target, runtime_root=runtime_root)
            self.assertEqual(receipt["status"], "blocked")
            codes = {b["code"] for b in receipt["blockers"]}
            self.assertIn("confirmation_token_missing", codes)
            self.assertFalse(target.exists() and any(target.rglob("*")))

    def test_apply_refuses_target_inside_runtime_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            target = runtime_root / "apps" / "demo-app"  # inside runtime root
            plan = af.create_plan("demo-app", "docker-web", runtime_root=runtime_root)
            receipt = af.create_apply(
                plan, plan["confirmation_token"], target, runtime_root=runtime_root
            )
            self.assertEqual(receipt["status"], "blocked")
            codes = {b["code"] for b in receipt["blockers"]}
            self.assertIn("unsafe_target_dir", codes)
            self.assertFalse(target.exists() and any(target.rglob("*")))

    def test_apply_refuses_target_inside_repo_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir).resolve() / "runtime"
            runtime_root.mkdir()
            target = REPO_ROOT / "src" / "ophelia" / "__scaffold_should_not_exist__"
            plan = af.create_plan("demo-app", "docker-web", runtime_root=runtime_root)
            receipt = af.create_apply(
                plan, plan["confirmation_token"], target, runtime_root=runtime_root
            )
            self.assertEqual(receipt["status"], "blocked")
            codes = {b["code"] for b in receipt["blockers"]}
            self.assertIn("unsafe_target_dir", codes)
            self.assertFalse(target.exists())

    def test_apply_refuses_existing_files_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            target = root / "work" / "demo-app"
            target.mkdir(parents=True)
            manifest = target / ".ophelia.yml"
            manifest.write_text("user-owned")

            plan = af.create_plan("demo-app", "static-site", runtime_root=runtime_root)
            blocked = af.create_apply(
                plan,
                plan["confirmation_token"],
                target,
                runtime_root=runtime_root,
            )
            self.assertEqual("blocked", blocked["status"])
            self.assertIn("target_file_exists", {b["code"] for b in blocked["blockers"]})
            self.assertEqual("user-owned", manifest.read_text())

            forced = af.create_apply(
                plan,
                plan["confirmation_token"],
                target,
                runtime_root=runtime_root,
                force=True,
            )
            self.assertEqual("succeeded", forced["status"])
            self.assertNotEqual("user-owned", manifest.read_text())
            self.assertIn("static_root: public", manifest.read_text())
            self.assertTrue((target / "public" / "index.html").exists())


class WorkflowAndSecretTests(unittest.TestCase):
    def test_generated_workflows_parse_as_yaml(self) -> None:
        import yaml

        for name in af.TEMPLATES:
            with self.subTest(template=name):
                contents = af._generated_contents(
                    "demo-app", af.TEMPLATES[name], "production", "personal"
                )
                for path, body in contents.items():
                    if path.endswith((".yml", ".yaml")):
                        loaded = yaml.safe_load(body)
                        self.assertIsInstance(loaded, dict, f"{path} did not parse to a mapping")

    def test_no_generated_file_embeds_secret_values(self) -> None:
        for name in af.TEMPLATES:
            contents = af._generated_contents(
                "demo-app", af.TEMPLATES[name], "production", "personal"
            )
            for path, body in contents.items():
                for forbidden in FORBIDDEN_SECRET_VALUES:
                    self.assertNotIn(forbidden, body, f"{path} embedded a secret value")
                # Secret names may appear, but never as `NAME=<value>` literals.
                for secret in af.DEPLOY_SECRETS + af.REGISTRY_SECRETS:
                    if f"{secret}=" in body:
                        self.fail(f"{path} assigns a literal value to {secret}")

    def test_workflows_reference_secrets_only_by_expression(self) -> None:
        staging = af._staging_workflow("demo-app")
        release = af._release_workflow("demo-app")
        for secret in af.DEPLOY_SECRETS + af.REGISTRY_SECRETS:
            # Every secret used in a workflow appears via ${{ secrets.NAME }}.
            if secret in staging:
                self.assertIn("${{ secrets." + secret + " }}", staging)
            if secret in release:
                self.assertIn("${{ secrets." + secret + " }}", release)

    def test_release_workflow_validates_release_label(self) -> None:
        release = af._release_workflow("demo-app")
        self.assertIn("release:(patch|minor|major)", release)

    def test_static_workflows_do_not_reference_docker_or_registry_secrets(self) -> None:
        template = af.TEMPLATES["static-site"]
        staging = af._staging_workflow("demo-app", template)
        release = af._release_workflow("demo-app", template)
        workflow_text = staging + release
        self.assertNotIn("docker build", workflow_text)
        self.assertNotIn("docker login", workflow_text)
        self.assertNotIn("GHCR_TOKEN", workflow_text)
        self.assertNotIn("packages: write", workflow_text)


class ReleaseMetadataTests(unittest.TestCase):
    def test_release_metadata_contract_shape(self) -> None:
        meta = af.release_metadata(
            app="demo-app",
            environment="production",
            version="1.2.3",
            git_sha="abc123",
            image="ghcr.io/owner/demo-app:1.2.3",
            image_digest="sha256:deadbeef",
            manifest_path=".ophelia.yml",
            release_notes="notes",
            previous_version="1.2.2",
        )
        self.assertEqual(meta["kind"], "ophelia.release_metadata")
        for key in (
            "app",
            "environment",
            "version",
            "git_sha",
            "image",
            "image_digest",
            "manifest_path",
            "release_notes",
            "rollback",
        ):
            self.assertIn(key, meta)
        self.assertEqual(meta["rollback"]["previous_version"], "1.2.2")
        self.assertTrue(json.dumps(meta))  # JSON-serializable


if __name__ == "__main__":
    unittest.main()
