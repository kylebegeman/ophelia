from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Sequence, Set

from ophelia.domain.receipts import VerificationStatus
from ophelia.execution.compose_backend import ComposeRevisionBackend, _HTTP_PROBE_IMAGE
from ophelia.execution.contracts import ExecutionFence
from ophelia.execution.subprocesses import ProcessResult
from ophelia.manifest_v2 import HttpProbeV2, ProbeV2, load_manifest_v2
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
            stdout = "node\n" if "--format" in command else PINNED_IMAGE + "\n"
        return ProcessResult(tuple(command), 0, "success", stdout, "", False, False, 1)


class ComposeRevisionBackendTests(unittest.TestCase):
    def test_non_root_policy_rejects_an_image_that_defaults_to_root(self) -> None:
        class RootImageRunner(FakeRunner):
            def run(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
                result = super().run(argv, **kwargs)
                if list(argv)[:3] == ["docker", "image", "inspect"] and "--format" in argv:
                    return ProcessResult(
                        tuple(argv), 0, "success", "\n", "", False, False, 1
                    )
                return result

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            manifest, revision = _stage_manifest(staging, runtime_root)
            backend = ComposeRevisionBackend(
                manifest=manifest,
                revision=revision,
                candidate_root=staging,
                runtime_root=runtime_root,
                host_id="host_fixture-1",
                operation_id="operation_fixture-1",
                owner_id="worker-1",
                fencing_token=1,
                runner=RootImageRunner(),
                require_edge_runtime=False,
            )

            preflight = backend.preflight(revision)

            self.assertFalse(preflight.ok)
            self.assertIn(
                "container_non_root_identity_unverified", preflight.blocker_codes
            )

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
            caddy_commands = [
                command
                for command in runner.commands
                if "caddy" in command
                and ("validate" in " ".join(command) or "reload" in " ".join(command))
            ]
            self.assertTrue(caddy_commands)
            validate = next(
                command for command in caddy_commands if "validate" in " ".join(command)
            )
            reload = next(
                command for command in caddy_commands if "reload" in " ".join(command)
            )
            self.assertEqual(
                ["--envfile", "/etc/caddy/env"],
                validate[
                    validate.index("--envfile") : validate.index("--envfile") + 2
                ],
            )
            self.assertIn("--envfile /etc/caddy/env", reload[-1])
            self.assertIn("caddy reload --config -", reload[-1])

    def test_candidate_readiness_and_active_liveness_use_distinct_probes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            manifest, revision = _stage_manifest(
                staging,
                runtime_root,
                probes="""
    readiness: {command: [\"/bin/ready\"]}
    liveness: {command: [\"/bin/live\"]}
""",
            )
            observed: List[Sequence[str]] = []
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
                probe_checker=lambda _workload, probe: observed.append(probe.command) or True,
                require_edge_runtime=False,
            )

            workload = manifest.workload("web")
            self.assertTrue(backend._probe(workload, candidate=True))
            self.assertTrue(backend._probe(workload, candidate=False))

            self.assertEqual([("/bin/ready",), ("/bin/live",)], observed)

    def test_command_probe_retries_until_the_workload_is_ready(self) -> None:
        class EventuallyReadyRunner(FakeRunner):
            attempts = 0

            def run(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
                command = list(argv)
                if "ps" in command and "-q" in command:
                    return ProcessResult(
                        tuple(command), 0, "success", "container-1\n", "", False, False, 1
                    )
                if command[:2] == ["docker", "exec"]:
                    self.attempts += 1
                    outcome = "success" if self.attempts == 2 else "nonzero_exit"
                    exit_code = 0 if outcome == "success" else 1
                    return ProcessResult(
                        tuple(command), exit_code, outcome, "", "", False, False, 1
                    )
                return super().run(argv, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            manifest, revision = _stage_manifest(
                staging,
                runtime_root,
                probes="""
    readiness:
      command: ["/bin/ready"]
      interval_seconds: 0.001
      timeout_seconds: 1
""",
            )
            runner = EventuallyReadyRunner()
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

            self.assertTrue(backend._probe(manifest.workload("web"), candidate=True))
            self.assertEqual(2, runner.attempts)

    def test_http_probe_runs_in_each_container_network_namespace(self) -> None:
        class ProbeRunner(FakeRunner):
            status = "503"

            def run(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
                command = list(argv)
                if "ps" in command and "-q" in command:
                    return ProcessResult(
                        tuple(command), 0, "success", "container-1\n", "", False, False, 1
                    )
                if command[:2] == ["docker", "run"]:
                    self.commands.append(command)
                    return ProcessResult(
                        tuple(command), 0, "success", self.status, "", False, False, 1
                    )
                return super().run(argv, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            manifest, revision = _stage_manifest(staging, runtime_root)
            runner = ProbeRunner()
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
            probe = ProbeV2(
                http=HttpProbeV2("/health", 8080, expect_status=503),
                timeout_seconds=12,
            )

            self.assertTrue(backend._probe_http(manifest.workload("web"), probe))

            command = next(item for item in runner.commands if item[:2] == ["docker", "run"])
            self.assertEqual("container:container-1", command[command.index("--network") + 1])
            self.assertIn(_HTTP_PROBE_IMAGE, command)
            self.assertIn("--read-only", command)
            self.assertEqual("ALL", command[command.index("--cap-drop") + 1])
            self.assertEqual("10", command[command.index("--max-time") + 1])
            self.assertEqual("GET", command[command.index("--request") + 1])
            self.assertEqual("http://127.0.0.1:8080/health", command[-1])

            runner.status = "200"
            self.assertFalse(backend._probe_http(manifest.workload("web"), probe))

    def test_preflight_pulls_the_pinned_http_probe_image_when_needed(self) -> None:
        class MissingProbeImageRunner(FakeRunner):
            def run(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
                command = list(argv)
                if command[:4] == ["docker", "image", "inspect", _HTTP_PROBE_IMAGE]:
                    self.commands.append(command)
                    return ProcessResult(
                        tuple(command), 1, "nonzero_exit", "", "missing", False, False, 1
                    )
                return super().run(argv, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            manifest, revision = _stage_manifest(
                staging,
                runtime_root,
                probes="""
    readiness:
      http: {path: /health, port: 8080}
""",
            )
            runner = MissingProbeImageRunner()
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

            result = backend.preflight(revision)

            self.assertTrue(result.ok)
            self.assertIn(_HTTP_PROBE_IMAGE.split("@", 1)[1], result.evidence_digests)
            self.assertIn(["docker", "pull", _HTTP_PROBE_IMAGE], runner.commands)

    def test_caddy_activation_prunes_unreferenced_managed_route_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            manifest, revision = _stage_manifest(staging, runtime_root)
            shared = runtime_root / "platform" / "shared"
            shared.mkdir(parents=True)
            (shared / "compose.yml").write_text("services: {caddy: {image: caddy}}\n")
            caddy = runtime_root / "caddy"
            caddy.mkdir()
            active = "OPHELIA_ROUTE_AUTH_" + "A" * 24
            stale = "OPHELIA_ROUTE_AUTH_" + "B" * 24
            (caddy / "env").write_text(
                '%s="active"\n%s="stale"\nUNMANAGED="preserved"\n'
                % (active, stale)
            )
            candidate = staging / "caddy" / "routes.caddy"
            candidate.write_text(
                "example.com {\n  reverse_proxy app:8080 {\n"
                f'    header_up Authorization "Bearer {{${active}}}"\n'
                "  }\n}\n"
            )
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
            )

            backend._activate_caddy(candidate)

            materialized = (caddy / "env").read_text()
            self.assertIn(active, materialized)
            self.assertNotIn(stale, materialized)
            self.assertIn("UNMANAGED", materialized)

    def test_failed_caddy_activation_restores_routes_and_prunes_candidate_secret(
        self,
    ) -> None:
        class FailingValidationRunner(FakeRunner):
            def __init__(self) -> None:
                super().__init__()
                self.failed = False

            def run(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
                if "validate" in argv and not self.failed:
                    self.failed = True
                    raise RuntimeError("invalid candidate")
                return super().run(argv, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            manifest, revision = _stage_manifest(staging, runtime_root)
            shared = runtime_root / "platform" / "shared"
            shared.mkdir(parents=True)
            (shared / "compose.yml").write_text("services: {caddy: {image: caddy}}\n")
            old_key = "OPHELIA_ROUTE_AUTH_" + "A" * 24
            candidate_key = "OPHELIA_ROUTE_AUTH_" + "B" * 24
            runner = FailingValidationRunner()
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
            )
            backend.caddy_include.parent.mkdir(parents=True)
            previous_routes = (
                "old.example.com {\n  reverse_proxy old:8080 {\n"
                f'    header_up Authorization "Bearer {{${old_key}}}"\n'
                "  }\n}\n"
            )
            backend.caddy_include.write_text(previous_routes)
            env_path = runtime_root / "caddy" / "env"
            env_path.write_text(f'{old_key}="old"\n{candidate_key}="candidate"\n')
            candidate = staging / "caddy" / "routes.caddy"
            candidate.write_text(
                "new.example.com {\n  reverse_proxy new:8080 {\n"
                f'    header_up Authorization "Bearer {{${candidate_key}}}"\n'
                "  }\n}\n"
            )

            with self.assertRaisesRegex(RuntimeError, "invalid candidate"):
                backend._activate_caddy(candidate)

            self.assertEqual(previous_routes, backend.caddy_include.read_text())
            restored_env = env_path.read_text()
            self.assertIn(old_key, restored_env)
            self.assertNotIn(candidate_key, restored_env)

    def test_first_v2_activation_displaces_and_compensation_restores_legacy_site(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            manifest, revision = _stage_manifest(staging, runtime_root)
            shared = runtime_root / "platform" / "shared"
            shared.mkdir(parents=True)
            (shared / "compose.yml").write_text("services: {caddy: {image: caddy}}\n")
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
            )
            backend.caddy_include.parent.mkdir(parents=True)
            legacy_include = backend.caddy_include.parent / f"{manifest.app}.caddy"
            legacy_routes = "compose-demo.example.com { reverse_proxy legacy:8080 }\n"
            legacy_include.write_text(legacy_routes)
            candidate = staging / "caddy" / "routes.caddy"

            backend._activate_caddy(candidate)

            self.assertFalse(legacy_include.exists())
            self.assertEqual(candidate.read_bytes(), backend.caddy_include.read_bytes())

            backend._deactivate_caddy()

            self.assertFalse(backend.caddy_include.exists())
            self.assertEqual(legacy_routes, legacy_include.read_text())

    def test_failed_first_v2_activation_restores_displaced_legacy_site(self) -> None:
        class FailingValidationRunner(FakeRunner):
            def __init__(self, legacy_include: Path) -> None:
                super().__init__()
                self.legacy_include = legacy_include

            def run(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
                if "validate" in argv:
                    if self.legacy_include.exists():
                        raise AssertionError("legacy site was not displaced before validation")
                    raise RuntimeError("invalid combined candidate")
                return super().run(argv, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            manifest, revision = _stage_manifest(staging, runtime_root)
            shared = runtime_root / "platform" / "shared"
            shared.mkdir(parents=True)
            (shared / "compose.yml").write_text("services: {caddy: {image: caddy}}\n")
            legacy_include = (
                runtime_root / "caddy" / "sites.d" / f"{manifest.app}.caddy"
            )
            backend = ComposeRevisionBackend(
                manifest=manifest,
                revision=revision,
                candidate_root=staging,
                runtime_root=runtime_root,
                host_id="host_fixture-1",
                operation_id="operation_fixture-1",
                owner_id="worker-1",
                fencing_token=1,
                runner=FailingValidationRunner(legacy_include),
            )
            backend.caddy_include.parent.mkdir(parents=True)
            legacy_routes = "compose-demo.example.com { reverse_proxy legacy:8080 }\n"
            legacy_include.write_text(legacy_routes)
            candidate = staging / "caddy" / "routes.caddy"

            with self.assertRaisesRegex(RuntimeError, "invalid combined candidate"):
                backend._activate_caddy(candidate)

            self.assertFalse(backend.caddy_include.exists())
            self.assertEqual(legacy_routes, legacy_include.read_text())

    def test_interrupted_legacy_displacement_resumes_and_restores_exact_site(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            manifest, revision = _stage_manifest(staging, runtime_root)
            shared = runtime_root / "platform" / "shared"
            shared.mkdir(parents=True)
            (shared / "compose.yml").write_text("services: {caddy: {image: caddy}}\n")
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
            )
            backend.caddy_include.parent.mkdir(parents=True)
            legacy_include = backend.legacy_caddy_include
            legacy_routes = "compose-demo.example.com { reverse_proxy legacy:8080 }\n"
            legacy_include.write_text(legacy_routes)

            displaced = backend._prepare_legacy_caddy_displacement()

            self.assertIsNotNone(displaced)
            self.assertFalse(legacy_include.exists())

            resumed = ComposeRevisionBackend(
                manifest=manifest,
                revision=revision,
                candidate_root=staging,
                runtime_root=runtime_root,
                host_id="host_fixture-1",
                operation_id="operation_fixture-1",
                owner_id="worker-1",
                fencing_token=1,
                runner=FakeRunner(),
            )
            resumed._activate_caddy(staging / "caddy" / "routes.caddy")
            resumed._deactivate_caddy()

            self.assertFalse(resumed.caddy_include.exists())
            self.assertEqual(legacy_routes, legacy_include.read_text())

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

    def test_invalid_compose_candidate_blocks_preflight_before_runtime_changes(self) -> None:
        class InvalidComposeRunner(FakeRunner):
            def run(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
                result = super().run(argv, **kwargs)
                if "config" in argv:
                    return ProcessResult(
                        tuple(argv),
                        1,
                        "nonzero_exit",
                        "",
                        "invalid compose candidate",
                        False,
                        False,
                        1,
                    )
                return result

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            manifest, revision = _stage_manifest(staging, runtime_root)
            runner = InvalidComposeRunner()
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

            result = backend.preflight(revision)

            self.assertFalse(result.ok)
            self.assertIn("compose_candidate_invalid", result.blocker_codes)
            config = next(command for command in runner.commands if "config" in command)
            self.assertIn("--no-env-resolution", config)
            self.assertIn("--no-path-resolution", config)
            self.assertFalse(any("pull" in command for command in runner.commands))

    def test_preflight_validates_a_secret_free_projection_and_removes_it(self) -> None:
        class ProjectionRunner(FakeRunner):
            def __init__(self) -> None:
                super().__init__()
                self.config_path: Path | None = None
                self.config_text = ""

            def run(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
                if "config" in argv:
                    self.config_path = Path(argv[argv.index("-f") + 1])
                    self.config_text = self.config_path.read_text(encoding="utf-8")
                return super().run(argv, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            manifest, revision = _stage_manifest(
                staging,
                runtime_root,
                secrets="""
secrets:
  - name: API_TOKEN
    ref: secret://compose-demo/production/api-token
""",
            )
            runner = ProjectionRunner()
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
                secret_resolver=lambda _: "secret-value",
                require_edge_runtime=False,
            )

            result = backend.preflight(revision)

            self.assertTrue(result.ok)
            self.assertIn("/dev/null", runner.config_text)
            self.assertNotIn("secret-value", runner.config_text)
            self.assertIsNotNone(runner.config_path)
            assert runner.config_path is not None
            self.assertFalse(runner.config_path.exists())

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

    def test_recreate_starts_candidate_before_readiness_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / "runtime"
            staging = base / "staging"
            manifest, revision = _stage_manifest(
                staging,
                runtime_root,
                strategy="recreate",
            )
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
            observed = backend.inspect(handle)

            self.assertEqual({"jobs", "web"}, runner.running[backend.project])
            self.assertEqual(VerificationStatus.PASSED, backend.verify(revision, observed).status)

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


def _stage_manifest(
    staging: Path,
    runtime_root: Path,
    *,
    web_replicas: int = 1,
    strategy: str = "blue_green",
    probes: str = "",
    secrets: str = "",
):
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
{probes.rstrip()}
  jobs:
    kind: worker
    artifact: app
    command: ["./worker"]
    update: {{overlap: forbid}}
routes:
  - name: public
    domain: compose-demo.example.com
    target: {{workload: web, port: 8080}}
{secrets.rstrip()}
update: {{strategy: {strategy}}}
"""
    )
    manifest = load_manifest_v2(source)
    revision = manifest.to_revision(created_at="2026-07-19T12:00:00Z")
    for relative, content in render_revision_bundle(
        manifest,
        revision,
        runtime_root=runtime_root,
        secret_runtime_root=runtime_root / "run" / "secrets",
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
