from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Sequence, Set

from ophelia.domain.receipts import ReceiptOutcome
from ophelia.execution.subprocesses import ProcessResult
from ophelia.manifest_v2_execution import (
    apply_manifest_v2_plan,
    load_manifest_v2_plan,
    plan_manifest_v2,
)


PINNED_IMAGE = "ghcr.io/example/demo@sha256:" + "c" * 64


class FakeRunner:
    def __init__(self) -> None:
        self.commands: List[List[str]] = []
        self.running: Dict[str, Set[str]] = {}

    def run(self, argv: Sequence[str], **_: object) -> ProcessResult:
        command = list(argv)
        self.commands.append(command)
        project = _option(command, "-p")
        stdout = ""
        if "up" in command:
            values = command[command.index("up") + 1 :]
            services = [item for item in values if not item.startswith("-") and "=" not in item]
            self.running.setdefault(project, set()).update(services)
        elif "stop" in command:
            self.running.setdefault(project, set()).difference_update(command[command.index("stop") + 1 :])
        elif "down" in command:
            self.running.pop(project, None)
        elif "ps" in command and "--services" in command:
            stdout = "\n".join(sorted(self.running.get(project, set()))) + "\n"
        return ProcessResult(tuple(command), 0, "success", stdout, "", False, False, 1)


class ManifestV2ExecutionTests(unittest.TestCase):
    def test_plan_apply_and_retry_use_one_durable_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(
                f"""
version: 2
app: execution-demo
environment: staging
artifacts:
  app: {{image: "{PINNED_IMAGE}"}}
workloads:
  web: {{kind: web, artifact: app, port: 8080}}
routes:
  - name: public
    domain: execution-demo.example.com
    target: {{workload: web, port: 8080}}
"""
            )
            approval_key = b"fixture-approval-key-with-enough-entropy"
            plan = plan_manifest_v2(
                manifest_path,
                runtime_root=runtime_root,
                host_id="host_fixture-1",
                approval_key=approval_key,
                require_edge_runtime=False,
            )
            self.assertTrue(plan["can_apply"])
            protected = runtime_root / "apps"
            self.assertFalse(protected.exists())
            runner = FakeRunner()

            first = apply_manifest_v2_plan(
                plan["plan_id"],
                runtime_root=runtime_root,
                approval_key=approval_key,
                confirmation=plan["confirmation_token"],
                runner=runner,
                require_edge_runtime=False,
                external_verifier=lambda _: True,
            )
            second = apply_manifest_v2_plan(
                plan["plan_id"],
                runtime_root=runtime_root,
                approval_key=approval_key,
                confirmation=plan["confirmation_token"],
                runner=runner,
                require_edge_runtime=False,
                external_verifier=lambda _: True,
            )

        self.assertEqual(ReceiptOutcome.SUCCEEDED.value, first["receipt"]["outcome"])
        self.assertEqual(first["operation"]["operation_id"], second["operation"]["operation_id"])
        self.assertEqual(first["receipt"]["receipt_id"], second["receipt"]["receipt_id"])

    def test_wrong_confirmation_never_accepts_an_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_root = root / "runtime"
            path = root / "app.ophelia.yml"
            path.write_text(
                f"""
version: 2
app: rejected-demo
environment: staging
artifacts: {{app: {{image: "{PINNED_IMAGE}"}}}}
workloads:
  worker: {{kind: worker, artifact: app}}
routes: []
update: {{strategy: recreate}}
"""
            )
            key = b"fixture-approval-key-with-enough-entropy"
            plan = plan_manifest_v2(
                path,
                runtime_root=runtime_root,
                host_id="host_fixture-1",
                approval_key=key,
                require_edge_runtime=False,
            )

            with self.assertRaisesRegex(ValueError, "confirmation"):
                apply_manifest_v2_plan(
                    plan["plan_id"],
                    runtime_root=runtime_root,
                    approval_key=key,
                    confirmation="wrong",
                    runner=FakeRunner(),
                    require_edge_runtime=False,
                )

            self.assertFalse((runtime_root / "host-state" / "operations.db").exists())

    def test_env_file_bytes_are_staged_and_bound_to_the_exact_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_root = root / "runtime"
            (root / "config").mkdir()
            (root / "config" / "worker.env").write_text("QUEUE=primary\n")
            path = root / "app.ophelia.yml"
            path.write_text(
                f"""
version: 2
app: source-demo
environment: staging
artifacts: {{app: {{image: "{PINNED_IMAGE}"}}}}
workloads:
  worker:
    kind: worker
    artifact: app
    env_files: [config/worker.env]
routes: []
update: {{strategy: recreate}}
"""
            )
            key = b"fixture-approval-key-with-enough-entropy"
            plan = plan_manifest_v2(
                path,
                runtime_root=runtime_root,
                host_id="host_fixture-1",
                approval_key=key,
                require_edge_runtime=False,
            )
            loaded = load_manifest_v2_plan(runtime_root, plan["plan_id"])
            staged = loaded["staging"].candidate / "support" / "worker" / "00-worker.env"
            self.assertEqual("QUEUE=primary\n", staged.read_text())
            staged.chmod(0o600)
            staged.write_text("QUEUE=tampered\n")

            with self.assertRaisesRegex(ValueError, "integrity"):
                apply_manifest_v2_plan(
                    plan["plan_id"],
                    runtime_root=runtime_root,
                    approval_key=key,
                    confirmation=plan["confirmation_token"],
                    runner=FakeRunner(),
                    require_edge_runtime=False,
                )

    def test_static_artifact_is_published_to_revision_isolated_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_root = root / "runtime"
            (root / "dist").mkdir()
            (root / "dist" / "index.html").write_text("<h1>Ophelia V2</h1>\n")
            path = root / "app.ophelia.yml"
            path.write_text(
                """
version: 2
app: static-demo
environment: staging
artifacts:
  site: {static_root: dist}
workloads:
  site: {kind: static, artifact: site}
routes:
  - name: public
    domain: static-demo.example.com
    target: {workload: site}
update: {strategy: static_atomic}
"""
            )
            key = b"fixture-approval-key-with-enough-entropy"
            plan = plan_manifest_v2(
                path,
                runtime_root=runtime_root,
                host_id="host_fixture-1",
                approval_key=key,
                require_edge_runtime=False,
            )

            result = apply_manifest_v2_plan(
                plan["plan_id"],
                runtime_root=runtime_root,
                approval_key=key,
                confirmation=plan["confirmation_token"],
                runner=FakeRunner(),
                require_edge_runtime=False,
                external_verifier=lambda _: True,
            )

            published = (
                runtime_root
                / "static"
                / "static-demo"
                / "staging"
                / "revisions"
                / plan["revision_id"]
                / "site"
                / "index.html"
            )
            self.assertEqual(ReceiptOutcome.SUCCEEDED.value, result["receipt"]["outcome"])
            self.assertEqual("<h1>Ophelia V2</h1>\n", published.read_text())


def _option(command: Sequence[str], name: str) -> str:
    try:
        return command[command.index(name) + 1]
    except (ValueError, IndexError):
        return ""


if __name__ == "__main__":
    unittest.main()
