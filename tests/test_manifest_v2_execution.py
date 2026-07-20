from __future__ import annotations

import base64
import concurrent.futures
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Sequence, Set
from unittest import mock

from ophelia.domain import Actor, ApprovedPlanRef, AuthorizationKind
from ophelia.domain.receipts import ReceiptOutcome
from ophelia.execution.subprocesses import ProcessFailure, ProcessResult
from ophelia.manifest_v2_execution import (
    apply_manifest_v2_plan,
    local_approval_key,
    load_manifest_v2_plan,
    plan_manifest_v2,
    submit_manifest_v2_plan,
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
        if command[:3] == ["docker", "image", "inspect"] and "--format" in command:
            stdout = "node\n"
        elif "up" in command:
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


class FailingStartRunner(FakeRunner):
    def run(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
        if "up" in argv:
            raise ProcessFailure(
                ProcessResult(
                    tuple(argv),
                    1,
                    "nonzero_exit",
                    "",
                    "permission denied: private-value",
                    False,
                    False,
                    1,
                )
            )
        return super().run(argv, **kwargs)


class ManifestV2ExecutionTests(unittest.TestCase):
    def test_remote_approval_uses_the_shorter_decision_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(
                f"""
version: 2
app: remote-decision-demo
environment: staging
artifacts: {{app: {{image: "{PINNED_IMAGE}"}}}}
workloads:
  worker: {{kind: worker, artifact: app}}
routes: []
update: {{strategy: recreate}}
"""
            )
            plan_report = plan_manifest_v2(
                manifest_path,
                runtime_root=runtime_root,
                host_id="host_fixture-1",
                approval_key=b"fixture-approval-key-with-enough-entropy",
                require_edge_runtime=False,
            )
            loaded = load_manifest_v2_plan(runtime_root, plan_report["plan_id"])
            actor = Actor("actor_lumen-1", "test", "mutual-tls-host-agent")
            approved_at = datetime.now(timezone.utc)
            expires_at = (approved_at + timedelta(minutes=10)).isoformat().replace(
                "+00:00", "Z"
            )
            approval = ApprovedPlanRef.bind(
                loaded["plan"],
                actor_id=actor.actor_id,
                decision_id="decision_lumen-1",
                authorization_kind=AuthorizationKind.LUMEN_DECISION,
                issuer="lumen-control-plane",
                audience=loaded["plan"].host_id,
                approved_at=approved_at.isoformat().replace("+00:00", "Z"),
                expires_at=expires_at,
                approval_nonce="fixture-nonce",
            )

            result = submit_manifest_v2_plan(
                loaded,
                actor=actor,
                approval=approval,
                runtime_root=runtime_root,
                runner=FakeRunner(),
                require_edge_runtime=False,
                execute=False,
            )

            self.assertEqual("accepted", result["operation"]["state"])
            self.assertIsNone(result["receipt"])

    def test_local_approval_key_creation_is_single_winner_under_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_root = Path(directory) / "runtime"
            barrier = threading.Barrier(8)
            from ophelia import manifest_v2_execution as execution

            original = execution._atomic_bytes

            def synchronized(path: Path, data: bytes) -> bool:
                barrier.wait(timeout=5)
                return original(path, data)

            with mock.patch.object(execution, "_atomic_bytes", synchronized):
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    values = list(
                        pool.map(
                            lambda _: local_approval_key(runtime_root, create=True),
                            range(8),
                        )
                    )

            self.assertEqual(1, len(set(values)))
            self.assertEqual(values[0], local_approval_key(runtime_root, create=False))

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

    def test_failed_apply_returns_safe_failure_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_root = root / "runtime"
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(
                f"""
version: 2
app: failed-execution-demo
environment: staging
artifacts:
  app: {{image: "{PINNED_IMAGE}"}}
workloads:
  worker: {{kind: worker, artifact: app}}
routes: []
update: {{strategy: recreate}}
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

            result = apply_manifest_v2_plan(
                plan["plan_id"],
                runtime_root=runtime_root,
                approval_key=approval_key,
                confirmation=plan["confirmation_token"],
                runner=FailingStartRunner(),
                require_edge_runtime=False,
            )

        self.assertEqual("failed_compensated", result["receipt"]["outcome"])
        self.assertEqual("start_candidate", result["failure"]["phase"])
        self.assertEqual("permission_denied", result["failure"]["code"])
        self.assertNotIn("private-value", str(result))

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
  web: {{kind: web, artifact: app, port: 8080}}
routes:
  - name: machine
    domain: secret-demo.example.com
    target: {{workload: web, port: 8080}}
    client_auth:
      mode: verify_if_given
      trust_pool_ref: secret://secret-demo/staging/trust-pem-base64
      trust_pool_encoding: base64
      forward:
        authorization_ref: secret://secret-demo/staging/proxy-token
update: {{strategy: blue_green}}
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
            (root / "config" / "worker.sql").write_text("SELECT 1;\n")
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
    file_mounts:
      - source: config/worker.sql
        target: /opt/app/worker.sql
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
            mounted = (
                loaded["staging"].candidate
                / "support"
                / "worker"
                / "files"
                / "00-worker.sql"
            )
            self.assertEqual("SELECT 1;\n", mounted.read_text())
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

    def test_secret_references_block_without_a_resolver_and_materialize_with_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_root = root / "runtime"
            path = root / "app.ophelia.yml"
            path.write_text(
                f"""
version: 2
app: secret-demo
environment: staging
artifacts: {{app: {{image: "{PINNED_IMAGE}"}}}}
workloads:
  web: {{kind: web, artifact: app, port: 8080}}
routes:
  - name: machine
    domain: secret-demo.example.com
    target: {{workload: web, port: 8080}}
    client_auth:
      mode: verify_if_given
      trust_pool_ref: secret://secret-demo/staging/trust-pem-base64
      trust_pool_encoding: base64
      forward:
        authorization_ref: secret://secret-demo/staging/proxy-token
update: {{strategy: blue_green}}
secrets:
  - name: API_TOKEN
    ref: secret://secret-demo/staging/api-token
  - name: TRUST_PATH
    ref: secret://secret-demo/staging/trust-pem-base64
    mode: file
    target: /run/secret-demo/trust.pem
    encoding: base64
"""
            )
            key = b"fixture-approval-key-with-enough-entropy"
            blocked = plan_manifest_v2(
                path,
                runtime_root=runtime_root,
                host_id="host_fixture-1",
                approval_key=key,
                require_edge_runtime=False,
            )
            self.assertFalse(blocked["can_apply"])
            self.assertIn("secret_bindings_unavailable", blocked["blockers"])

            values = {
                "secret://secret-demo/staging/api-token": "token-value",
                "secret://secret-demo/staging/proxy-token": "p" * 64,
                "secret://secret-demo/staging/trust-pem-base64": base64.b64encode(
                    b"-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n"
                ).decode("ascii"),
            }
            resolver = values.__getitem__
            plan = plan_manifest_v2(
                path,
                runtime_root=runtime_root,
                host_id="host_fixture-1",
                approval_key=key,
                require_edge_runtime=False,
                secret_resolver=resolver,
            )
            result = apply_manifest_v2_plan(
                plan["plan_id"],
                runtime_root=runtime_root,
                approval_key=key,
                confirmation=plan["confirmation_token"],
                runner=FakeRunner(),
                require_edge_runtime=False,
                secret_resolver=resolver,
                external_verifier=lambda _: True,
            )

            secret_root = (
                runtime_root
                / "run"
                / "secrets"
                / "secret-demo"
                / "staging"
                / plan["revision_id"]
            )
            self.assertEqual(ReceiptOutcome.SUCCEEDED.value, result["receipt"]["outcome"])
            self.assertEqual("API_TOKEN=token-value\n", (secret_root / "web.env").read_text())
            self.assertIn(
                "-----BEGIN CERTIFICATE-----",
                (secret_root / "web.files" / "TRUST_PATH").read_text(),
            )
            route_ca = (
                runtime_root
                / "run"
                / "route-auth"
                / "secret-demo"
                / "staging"
                / plan["revision_id"]
                / "machine"
                / "client-ca.pem"
            )
            self.assertIn("BEGIN CERTIFICATE", route_ca.read_text())
            self.assertIn("OPHELIA_ROUTE_AUTH_", (runtime_root / "caddy" / "env").read_text())


def _option(command: Sequence[str], name: str) -> str:
    try:
        return command[command.index(name) + 1]
    except (ValueError, IndexError):
        return ""


if __name__ == "__main__":
    unittest.main()
