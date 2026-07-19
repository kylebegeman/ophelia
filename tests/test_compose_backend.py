from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Sequence, Set

from ophelia.domain.receipts import VerificationStatus
from ophelia.execution.compose_backend import ComposeRevisionBackend
from ophelia.execution.contracts import ExecutionFence
from ophelia.execution.subprocesses import ProcessResult
from ophelia.manifest_v2 import load_manifest_v2
from ophelia.manifest_v2_renderer import render_revision_bundle


PINNED_IMAGE = "ghcr.io/example/demo@sha256:" + "b" * 64


class FakeRunner:
    def __init__(self) -> None:
        self.commands: List[List[str]] = []
        self.running: Dict[str, Set[str]] = {}
        self.replicas: Dict[str, Dict[str, int]] = {}

    def run(self, argv: Sequence[str], **_: object) -> ProcessResult:
        command = list(argv)
        self.commands.append(command)
        project = _option(command, "-p")
        stdout = ""
        if " up " in " " + " ".join(command) + " ":
            arguments = command[command.index("up") + 1 :]
            scales: Dict[str, int] = {}
            services = []
            index = 0
            while index < len(arguments):
                item = arguments[index]
                if item == "--scale":
                    name, count = arguments[index + 1].split("=", 1)
                    scales[name] = int(count)
                    index += 2
                    continue
                if not item.startswith("-"):
                    services.append(item)
                index += 1
            self.running.setdefault(project, set()).update(services)
            counts = self.replicas.setdefault(project, {})
            for service in services:
                counts[service] = scales.get(service, 1)
        elif "stop" in command:
            services = command[command.index("stop") + 1 :]
            self.running.setdefault(project, set()).difference_update(services)
        elif "down" in command:
            self.running.pop(project, None)
            self.replicas.pop(project, None)
        elif "ps" in command and "--services" in command:
            stdout = "\n".join(sorted(self.running.get(project, set()))) + "\n"
        elif "ps" in command and "-q" in command:
            service = command[-1]
            count = self.replicas.get(project, {}).get(service, 0)
            stdout = "\n".join("%s-%d" % (service, index) for index in range(1, count + 1))
            if stdout:
                stdout += "\n"
        elif command[:3] == ["docker", "image", "inspect"]:
            stdout = PINNED_IMAGE + "\n"
        return ProcessResult(tuple(command), 0, "success", stdout, "", False, False, 1)


class ComposeRevisionBackendTests(unittest.TestCase):
    def test_blue_green_candidate_defers_fenced_worker_until_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            manifest, revision = _stage_manifest(staging, runtime_root)
            (runtime_root / "platform" / "shared").mkdir(parents=True)
            (runtime_root / "platform" / "shared" / "compose.yml").write_text("services: {caddy: {image: caddy}}\n")
            runner = FakeRunner()
            backend = ComposeRevisionBackend(
                manifest=manifest,
                revision=revision,
                candidate_root=staging,
                runtime_root=runtime_root,
                host_id="host_fixture-1",
                operation_id="operation_fixture-1",
                owner_id="worker-1",
                fencing_token=1,
                runner=runner,
                external_verifier=lambda _: True,
            )

            self.assertTrue(backend.preflight(revision).ok)
            handle = backend.start(revision)
            candidate = backend.inspect(handle)
            self.assertEqual("running", candidate.workloads[1].runtime_state)
            self.assertEqual("activation_pending", candidate.workloads[0].runtime_state)
            self.assertEqual(VerificationStatus.PASSED, backend.verify(revision, candidate).status)

            activated = backend.activate(handle, None)
            verified = backend.verify_active(revision, handle)

            self.assertEqual(revision.content_digest(), activated.active_revision_digest)
            self.assertEqual(VerificationStatus.PASSED, verified.status)
            active = json.loads(backend.active_path.read_text())
            self.assertEqual(revision.revision_id, active["revision_id"])
            self.assertIn("jobs", runner.running[backend.project])
            self.assertIn("web", runner.running[backend.project])

    def test_failed_candidate_removal_cleans_runtime_and_never_removes_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            manifest, revision = _stage_manifest(staging, runtime_root)
            runner = FakeRunner()
            backend = ComposeRevisionBackend(
                manifest=manifest,
                revision=revision,
                candidate_root=staging,
                runtime_root=runtime_root,
                host_id="host_fixture-1",
                operation_id="operation_fixture-1",
                owner_id="worker-1",
                fencing_token=1,
                runner=runner,
                require_edge_runtime=False,
                external_verifier=lambda _: True,
            )
            handle = backend.start(revision)

            removed = backend.remove(handle)

            self.assertTrue(removed.removed)
            self.assertFalse(backend.revision_root.exists())

    def test_rendered_candidate_tampering_blocks_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            manifest, revision = _stage_manifest(staging, runtime_root)
            compose = staging / "compose.yml"
            compose.write_text(compose.read_text() + "# tampered\n")
            backend = ComposeRevisionBackend(
                manifest=manifest,
                revision=revision,
                candidate_root=staging,
                runtime_root=runtime_root,
                host_id="host_fixture-1",
                operation_id="operation_fixture-1",
                owner_id="worker-1",
                fencing_token=1,
                runner=FakeRunner(),
                require_edge_runtime=False,
            )

            result = backend.preflight(revision)

            self.assertFalse(result.ok)
            self.assertIn("candidate_bundle_mismatch", result.blocker_codes)

    def test_replica_count_is_scaled_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            manifest, revision = _stage_manifest(staging, runtime_root, web_replicas=2)
            runner = FakeRunner()
            backend = ComposeRevisionBackend(
                manifest=manifest,
                revision=revision,
                candidate_root=staging,
                runtime_root=runtime_root,
                host_id="host_fixture-1",
                operation_id="operation_fixture-1",
                owner_id="worker-1",
                fencing_token=1,
                runner=runner,
                require_edge_runtime=False,
                external_verifier=lambda _: True,
            )

            handle = backend.start(revision)

            self.assertEqual(VerificationStatus.PASSED, backend.verify(revision, backend.inspect(handle)).status)
            up = next(command for command in runner.commands if "up" in command)
            self.assertIn("web=2", up)

    def test_cron_is_registered_but_never_started_as_a_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            source = base / "app.ophelia.yml"
            source.write_text(
                f"""
version: 2
app: cron-demo
environment: staging
artifacts: {{app: {{image: "{PINNED_IMAGE}"}}}}
workloads:
  cleanup:
    kind: cron
    artifact: app
    command: ["./cleanup"]
    schedule: "17 3 * * *"
routes: []
update: {{strategy: recreate}}
"""
            )
            manifest = load_manifest_v2(source)
            revision = manifest.to_revision(created_at="2026-07-19T12:00:00Z")
            staging.mkdir()
            for relative, content in render_revision_bundle(
                manifest, revision, runtime_root=runtime_root
            ).items():
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            runner = FakeRunner()
            backend = ComposeRevisionBackend(
                manifest=manifest,
                revision=revision,
                candidate_root=staging,
                runtime_root=runtime_root,
                host_id="host_fixture-1",
                operation_id="operation_fixture-1",
                owner_id="worker-1",
                fencing_token=1,
                runner=runner,
                require_edge_runtime=False,
            )

            handle = backend.start(revision)
            backend.activate(handle, None)

            self.assertEqual(VerificationStatus.PASSED, backend.verify_active(revision, handle).status)
            self.assertFalse(any("up" in command and "cleanup" in command for command in runner.commands))
            registry = json.loads((backend.scope_root / "cron" / "active.json").read_text())
            self.assertEqual("cleanup", registry["schedules"][0]["name"])


def _stage_manifest(staging: Path, runtime_root: Path, *, web_replicas: int = 1):
    staging.mkdir(parents=True)
    source = staging.parent / "app.ophelia.yml"
    source.write_text(
        f"""
version: 2
app: compose-demo
environment: production
artifacts:
  app: {{image: "{PINNED_IMAGE}"}}
workloads:
  web:
    kind: web
    artifact: app
    port: 8080
    replicas: {web_replicas}
  jobs:
    kind: worker
    artifact: app
    command: ["./worker"]
    update: {{overlap: forbid}}
routes:
  - name: public
    domain: compose-demo.example.com
    target: {{workload: web, port: 8080}}
update: {{strategy: blue_green}}
"""
    )
    manifest = load_manifest_v2(source)
    revision = manifest.to_revision(created_at="2026-07-19T12:00:00Z")
    for relative, content in render_revision_bundle(
        manifest,
        revision,
        runtime_root=runtime_root,
    ).items():
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return manifest, revision


def _option(command: Sequence[str], name: str) -> str:
    try:
        return command[command.index(name) + 1]
    except (ValueError, IndexError):
        return ""


if __name__ == "__main__":
    unittest.main()
