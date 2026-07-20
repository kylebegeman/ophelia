"""Journal-compatible Compose backend for immutable manifest v2 revisions."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Protocol, Sequence, Set, Tuple

from ..domain._contracts import canonical_digest
from ..domain.receipts import (
    CheckStatus,
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
)
from ..domain.revisions import (
    ObservedRevision,
    ObservedWorkload,
    Revision,
    RevisionState,
    WorkloadKind,
)
from ..manifest_v2 import ManifestV2, ProbeV2, RouteV2, WorkloadV2, load_manifest_v2
from ..manifest_v2_renderer import (
    compose_project_name,
    render_revision_bundle,
    route_auth_env_key,
    route_auth_root,
)
from .contracts import (
    PreflightResult,
    RemoveResult,
    RuntimeHandle,
    StopResult,
    TrafficActivationResult,
)
from .runtime_fence import RuntimeSideEffectFence
from .subprocesses import ProcessFailure, ProcessResult, SubprocessRunner


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
        ...


class ComposeBackendError(RuntimeError):
    """A revision could not satisfy its approved Compose runtime contract."""


class ComposeRevisionBackend:
    """Activate one V2 revision with web overlap and fenced background handoff."""

    backend_name = "compose-manifest-v2"

    def __init__(
        self,
        *,
        manifest: ManifestV2,
        revision: Revision,
        candidate_root: Path,
        runtime_root: Path,
        host_id: str,
        operation_id: str,
        owner_id: str,
        fencing_token: int,
        runner: Optional[CommandRunner] = None,
        secret_resolver: Optional[Callable[[str], str]] = None,
        probe_checker: Optional[Callable[[WorkloadV2, ProbeV2], bool]] = None,
        external_verifier: Optional[Callable[[RouteV2], bool]] = None,
        require_edge_runtime: bool = True,
        command_timeout_seconds: float = 300.0,
    ) -> None:
        self.manifest = manifest
        self.revision = revision
        self.candidate_root = Path(candidate_root)
        self.runtime_root = Path(runtime_root)
        self.runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.runtime_root.is_symlink() or not self.runtime_root.is_dir():
            raise ComposeBackendError("Runtime root must be a trusted real directory.")
        self.host_id = host_id
        self.operation_id = operation_id
        self.owner_id = owner_id
        self.fencing_token = fencing_token
        self.runner = runner or SubprocessRunner()
        self.secret_resolver = secret_resolver
        self.probe_checker = probe_checker
        self.external_verifier = external_verifier or self._verify_route
        self.require_edge_runtime = require_edge_runtime
        self.command_timeout_seconds = command_timeout_seconds
        self.project = compose_project_name(manifest, revision)
        self.scope_root = (
            self.runtime_root
            / "apps"
            / manifest.app
            / "environments"
            / manifest.environment
        )
        self.revisions_root = self.scope_root / "revisions"
        self.revision_root = self.revisions_root / revision.revision_id
        self.traffic_root = self.scope_root / "traffic"
        self.active_path = self.traffic_root / "active.json"
        self.caddy_include = (
            self.runtime_root
            / "caddy"
            / "sites.d"
            / (manifest.app + "-" + manifest.environment + ".caddy")
        )
        self.shared_compose = self.runtime_root / "platform" / "shared" / "compose.yml"
        self.secret_root = (
            self.runtime_root
            / "run"
            / "secrets"
            / manifest.app
            / manifest.environment
            / revision.revision_id
        )
        self.route_auth_root = (
            self.runtime_root
            / "run"
            / "route-auth"
            / manifest.app
            / manifest.environment
            / revision.revision_id
        )
        self.static_revision_root = (
            self.runtime_root
            / "static"
            / manifest.app
            / manifest.environment
            / "revisions"
            / revision.revision_id
        )

    def preflight(self, revision: Revision) -> PreflightResult:
        blockers = []
        evidence = []
        try:
            self._validate_revision(revision)
            self._verify_candidate_bundle()
            evidence.extend((revision.manifest_digest, revision.content_digest()))
        except (OSError, ValueError, RuntimeError):
            blockers.append("candidate_bundle_mismatch")
        if self.manifest.routes and self.require_edge_runtime and not self.shared_compose.is_file():
            blockers.append("edge_runtime_missing")
        secret_references = self.manifest.secret_references()
        if secret_references and self.secret_resolver is None:
            blockers.append("secret_resolver_missing")
        elif secret_references:
            try:
                assert self.secret_resolver is not None
                for reference in secret_references:
                    value = self.secret_resolver(reference)
                    if not isinstance(value, str) or not value or "\x00" in value:
                        raise ValueError("invalid secret scalar")
            except (OSError, RuntimeError, ValueError):
                blockers.append("secret_binding_unavailable")
        if not blockers:
            try:
                for artifact in self.manifest.artifacts:
                    if artifact.image is None:
                        continue
                    result = self.runner.run(
                        ["docker", "image", "inspect", artifact.image],
                        timeout_seconds=min(self.command_timeout_seconds, 60.0),
                        check=False,
                    )
                    if result.exit_reason != "success":
                        self.runner.run(
                            ["docker", "pull", artifact.image],
                            timeout_seconds=self.command_timeout_seconds,
                        )
                    evidence.append(artifact.digest)
                self._ensure_network("ophelia-app", external=False)
                if any(item.kind is WorkloadKind.WEB for item in self.manifest.workloads):
                    self._ensure_network("ophelia-edge", external=True)
                if any("data" in item.networks for item in self.manifest.workloads):
                    self._ensure_network("ophelia-data", external=True)
            except (OSError, RuntimeError, ProcessFailure):
                blockers.append("container_runtime_unavailable")
        return PreflightResult(
            ok=not blockers,
            observed_state_digest=canonical_digest(
                {
                    "revision_digest": revision.content_digest(),
                    "candidate_verified": "candidate_bundle_mismatch" not in blockers,
                    "runtime_available": "container_runtime_unavailable" not in blockers,
                    "edge_available": "edge_runtime_missing" not in blockers,
                    "secret_bindings_available": not any(
                        item in blockers
                        for item in (
                            "secret_resolver_missing",
                            "secret_binding_unavailable",
                        )
                    ),
                }
            ),
            evidence_digests=tuple(sorted(set(evidence))) if not blockers else (),
            blocker_codes=tuple(sorted(set(blockers))),
        )

    def start(self, revision: Revision) -> RuntimeHandle:
        checked = self.preflight(revision)
        if not checked.ok:
            raise ComposeBackendError(checked.blocker_codes[0])
        handle = self.handle_for(revision)
        with self._fence():
            self._materialize_revision()
            self._materialize_static_artifacts()
            self._materialize_secrets()
            self._materialize_route_auth()
            self._run_migrations()
            if self.manifest.update.strategy == "recreate":
                previous = self._previous_runtime(self._read_active(required=False))
                if previous is not None:
                    self._stop_services(
                        previous["project"],
                        previous["root"],
                        previous["long_running"],
                    )
            services = self._candidate_start_services()
            if services:
                self._start_services(self.project, self.revision_root, services)
        return handle

    def handle_for(self, revision: Revision) -> RuntimeHandle:
        self._validate_revision(revision)
        return RuntimeHandle(
            backend=self.backend_name,
            handle_id=revision.revision_id,
            revision_id=revision.revision_id,
            revision_digest=revision.content_digest(),
        )

    def inspect(self, handle: RuntimeHandle) -> ObservedRevision:
        self._validate_handle(handle)
        running = self._running_services(self.project, self.revision_root)
        candidate_services = set(self._candidate_start_services())
        observed = []
        candidate_ready = True
        for workload in self.manifest.workloads:
            if workload.kind in {WorkloadKind.CRON, WorkloadKind.TASK}:
                state = "schedule_pending" if workload.kind is WorkloadKind.CRON else "task_available"
                ready = False
            elif workload.kind is WorkloadKind.STATIC:
                state = "activation_pending"
                ready = False
            elif workload.name not in candidate_services:
                state = "activation_pending"
                ready = False
            else:
                ready = (
                    workload.name in running
                    and self._replicas_ready(workload, self.project, self.revision_root)
                    and self._probe(workload, candidate=True)
                )
                state = "running" if ready else "unavailable"
                candidate_ready = candidate_ready and ready
            observed.append(
                ObservedWorkload(
                    workload_id=workload.name,
                    workload_kind=workload.kind,
                    runtime_state=state,
                    artifact_digest=self.manifest.artifact(workload.artifact).digest,
                    ready=ready,
                    fencing_token_digest=(
                        canonical_digest({"token": self.fencing_token})
                        if workload.kind in {WorkloadKind.WORKER, WorkloadKind.CRON}
                        else None
                    ),
                )
            )
        return ObservedRevision(
            host_id=self.host_id,
            app=self.manifest.app,
            environment=self.manifest.environment,
            revision_id=handle.revision_id,
            revision_digest=handle.revision_digest if candidate_ready else None,
            state=RevisionState.READY if candidate_ready else RevisionState.FAILED,
            workloads=tuple(sorted(observed, key=lambda item: item.workload_id)),
            observed_at=_utc_now(),
        )

    def verify(self, revision: Revision, observed: ObservedRevision) -> VerificationResult:
        expected_started = set(self._candidate_start_services())
        checks = []
        passed = observed.state is RevisionState.READY
        for item in observed.workloads:
            should_run = item.workload_id in expected_started
            okay = item.ready if should_run else item.runtime_state in {
                "activation_pending", "schedule_pending", "task_available"
            }
            passed = passed and okay
            checks.append(
                VerificationCheck(
                    name="candidate_" + item.workload_id,
                    status=CheckStatus.PASSED if okay else CheckStatus.FAILED,
                    observed_digest=canonical_digest(
                        {
                            "workload": item.workload_id,
                            "runtime_state": item.runtime_state,
                            "ready": item.ready,
                        }
                    ),
                )
            )
        return VerificationResult(
            status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
            observed_revision_digest=revision.content_digest() if passed else None,
            observed_state_digest=observed.digest(),
            observed_at=observed.observed_at,
            checks=tuple(checks),
        )

    def activate(
        self,
        candidate: RuntimeHandle,
        expected_active_revision_digest: Optional[str],
    ) -> TrafficActivationResult:
        self._validate_handle(candidate)
        with self._fence():
            active = self._read_active(required=False)
            current_digest = None if active is None else active["revision_digest"]
            if current_digest == candidate.revision_digest:
                self._activate_cron_registry()
                self._activate_caddy(self.revision_root / "caddy" / "routes.caddy")
                return self._activation_result(current_digest, current_digest)
            if current_digest != expected_active_revision_digest:
                raise ComposeBackendError("Active revision changed after planning.")
            previous = self._previous_runtime(active)
            if previous is not None:
                if self.manifest.update.strategy == "recreate":
                    self._stop_services(previous["project"], previous["root"], previous["long_running"])
                else:
                    self._stop_services(previous["project"], previous["root"], previous["background"])
            deferred = self._activation_services()
            if deferred:
                self._start_services(self.project, self.revision_root, deferred)
            self._activate_cron_registry()
            self._activate_caddy(self.revision_root / "caddy" / "routes.caddy")
            self.traffic_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            _atomic_json(
                self.active_path,
                {
                    "schema_version": 1,
                    "kind": "ophelia.active-revision",
                    "host_id": self.host_id,
                    "app": self.manifest.app,
                    "environment": self.manifest.environment,
                    "revision_id": candidate.revision_id,
                    "revision_digest": candidate.revision_digest,
                    "compose_project": self.project,
                    "activated_at": _utc_now(),
                },
            )
        return self._activation_result(current_digest, candidate.revision_digest)

    def verify_active(self, revision: Revision, handle: RuntimeHandle) -> VerificationResult:
        active = self._read_active(required=False)
        selected = (
            active is not None
            and active.get("revision_id") == handle.revision_id
            and active.get("revision_digest") == handle.revision_digest
            and active.get("compose_project") == self.project
        )
        running = self._running_services(self.project, self.revision_root) if selected else set()
        checks = []
        passed = selected
        for workload in self.manifest.workloads:
            if workload.kind in {
                WorkloadKind.WEB,
                WorkloadKind.INTERNAL,
                WorkloadKind.WORKER,
            }:
                okay = (
                    workload.name in running
                    and self._replicas_ready(workload, self.project, self.revision_root)
                    and self._probe(workload, candidate=False)
                )
            elif workload.kind is WorkloadKind.CRON:
                okay = self._cron_active(workload.name)
            elif workload.kind is WorkloadKind.STATIC:
                okay = self._static_artifact_ready(workload.artifact)
            else:
                okay = True
            passed = passed and okay
            checks.append(
                VerificationCheck(
                    name="active_" + workload.name,
                    status=CheckStatus.PASSED if okay else CheckStatus.FAILED,
                    observed_digest=canonical_digest(
                        {"workload": workload.name, "active": okay}
                    ),
                )
            )
        for route in self.manifest.routes:
            okay = bool(self.external_verifier(route))
            passed = passed and okay
            checks.append(
                VerificationCheck(
                    name="route_" + route.name,
                    status=CheckStatus.PASSED if okay else CheckStatus.FAILED,
                    observed_digest=canonical_digest(
                        {"route": route.name, "domain": route.domain, "verified": okay}
                    ),
                )
            )
        observed_digest = canonical_digest(
            {
                "selected": selected,
                "revision_digest": handle.revision_digest,
                "running_services": sorted(running),
                "checks": [item.observed_digest for item in checks],
            }
        )
        return VerificationResult(
            status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
            observed_revision_digest=revision.content_digest() if passed else None,
            observed_state_digest=observed_digest,
            observed_at=_utc_now(),
            checks=tuple(checks),
        )

    def restore(
        self, previous: RuntimeHandle, failed_candidate: RuntimeHandle
    ) -> TrafficActivationResult:
        previous_runtime = self._runtime_for_handle(previous)
        with self._fence():
            self._stop_services(self.project, self.revision_root, self._all_long_running())
            self._start_services(
                previous_runtime["project"],
                previous_runtime["root"],
                previous_runtime["long_running"],
                replicas=previous_runtime["replicas"],
            )
            self._activate_caddy(previous_runtime["root"] / "caddy" / "routes.caddy")
            self._activate_previous_cron(previous_runtime)
            _atomic_json(
                self.active_path,
                {
                    "schema_version": 1,
                    "kind": "ophelia.active-revision",
                    "host_id": self.host_id,
                    "app": self.manifest.app,
                    "environment": self.manifest.environment,
                    "revision_id": previous.revision_id,
                    "revision_digest": previous.revision_digest,
                    "compose_project": previous_runtime["project"],
                    "activated_at": _utc_now(),
                },
            )
        running = self._running_services(previous_runtime["project"], previous_runtime["root"])
        if not set(previous_runtime["long_running"]).issubset(running):
            raise ComposeBackendError("Restored predecessor did not return to running state.")
        return self._activation_result(failed_candidate.revision_digest, previous.revision_digest)

    def deactivate(self, failed_candidate: RuntimeHandle) -> TrafficActivationResult:
        with self._fence():
            active = self._read_active(required=False)
            previous_digest = None if active is None else active.get("revision_digest")
            if previous_digest not in {None, failed_candidate.revision_digest}:
                raise ComposeBackendError("Refusing to deactivate another revision.")
            self._stop_services(self.project, self.revision_root, self._all_long_running())
            self._deactivate_caddy()
            self._clear_cron_registry()
            self.active_path.unlink(missing_ok=True)
        return self._activation_result(failed_candidate.revision_digest, None)

    def drain_previous(
        self, previous: RuntimeHandle, candidate: RuntimeHandle
    ) -> StopResult:
        active = self._read_active(required=True)
        if active["revision_digest"] != candidate.revision_digest:
            raise ComposeBackendError("Candidate must be active before predecessor drain.")
        return self.stop(previous, self.manifest.update.drain_seconds)

    def stop(self, handle: RuntimeHandle, grace_seconds: int) -> StopResult:
        runtime = self._runtime_for_handle(handle)
        with self._fence():
            self._compose(
                runtime["project"],
                runtime["root"],
                "down",
                "--timeout",
                str(max(0, min(int(grace_seconds), 3600))),
                check=False,
            )
        running = self._running_services(runtime["project"], runtime["root"])
        stopped = not running
        return StopResult(
            stopped=stopped,
            observed_state_digest=canonical_digest(
                {"revision_digest": handle.revision_digest, "running": sorted(running)}
            ),
        )

    def remove(self, handle: RuntimeHandle) -> RemoveResult:
        active = self._read_active(required=False)
        if active is not None and active.get("revision_digest") == handle.revision_digest:
            raise ComposeBackendError("An active revision cannot be removed.")
        runtime = self._runtime_for_handle(handle, allow_candidate=True)
        with self._fence():
            self._compose(runtime["project"], runtime["root"], "down", check=False)
            root = Path(runtime["root"])
            if root.exists():
                if root.is_symlink() or not root.is_dir():
                    raise ComposeBackendError("Revision root has an unsafe type.")
                _make_writable(root)
                shutil.rmtree(root)
            if handle.revision_id == self.revision.revision_id and self.secret_root.exists():
                _make_writable(self.secret_root)
                shutil.rmtree(self.secret_root)
            if handle.revision_id == self.revision.revision_id and self.route_auth_root.exists():
                _make_writable(self.route_auth_root)
                shutil.rmtree(self.route_auth_root)
            static_root = (
                self.runtime_root
                / "static"
                / self.manifest.app
                / self.manifest.environment
                / "revisions"
                / handle.revision_id
            )
            if static_root.exists():
                _make_writable(static_root)
                shutil.rmtree(static_root)
        removed = not Path(runtime["root"]).exists()
        return RemoveResult(
            removed=removed,
            observed_state_digest=canonical_digest(
                {"revision_digest": handle.revision_digest, "removed": removed}
            ),
        )

    def _validate_revision(self, revision: Revision) -> None:
        if (
            revision.revision_id != self.revision.revision_id
            or revision.content_digest() != self.revision.content_digest()
            or revision.manifest_digest != self.manifest.canonical_digest()
        ):
            raise ComposeBackendError("Revision does not match the bound manifest v2 input.")

    def _validate_handle(self, handle: RuntimeHandle) -> None:
        if (
            handle.backend != self.backend_name
            or handle.revision_id != self.revision.revision_id
            or handle.revision_digest != self.revision.content_digest()
        ):
            raise ComposeBackendError("Runtime handle does not match this candidate revision.")

    def _verify_candidate_bundle(self) -> None:
        expected = render_revision_bundle(
            self.manifest,
            self.revision,
            runtime_root=self.runtime_root,
            secret_runtime_root=self.runtime_root / "run" / "secrets",
        )
        if self.candidate_root.is_symlink() or not self.candidate_root.is_dir():
            raise ComposeBackendError("Candidate root must be a real directory.")
        for relative, content in expected.items():
            target = self.candidate_root / relative
            if target.is_symlink() or not target.is_file() or target.read_text(encoding="utf-8") != content:
                raise ComposeBackendError("Candidate bundle differs at %s." % relative)

    def _materialize_revision(self) -> None:
        self._verify_candidate_bundle()
        if self.revision_root.exists():
            self._verify_materialized_revision()
            return
        self.revisions_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.revisions_root / ("." + self.revision.revision_id + ".tmp-" + self.operation_id[-12:])
        if temporary.exists():
            _make_writable(temporary)
            shutil.rmtree(temporary)
        _copy_tree(self.candidate_root, temporary)
        os.replace(temporary, self.revision_root)
        _sync_directory(self.revisions_root)
        self._verify_materialized_revision()

    def _verify_materialized_revision(self) -> None:
        if self.revision_root.is_symlink() or not self.revision_root.is_dir():
            raise ComposeBackendError("Materialized revision has an unsafe type.")
        original_root = self.candidate_root
        try:
            self.candidate_root = self.revision_root
            self._verify_candidate_bundle()
        finally:
            self.candidate_root = original_root

    def _materialize_static_artifacts(self) -> None:
        static_artifacts = [item for item in self.manifest.artifacts if item.static_root is not None]
        if not static_artifacts:
            return
        self.static_revision_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for artifact in static_artifacts:
            assert artifact.static_root is not None
            source = self.revision_root / artifact.static_root
            target = self.static_revision_root / artifact.name
            if target.exists():
                if _static_tree_digest(target) != artifact.digest:
                    raise ComposeBackendError("Published static artifact does not match its revision.")
                continue
            temporary = self.static_revision_root / (
                "." + artifact.name + ".tmp-" + self.operation_id[-12:]
            )
            if temporary.exists():
                _make_writable(temporary)
                shutil.rmtree(temporary)
            _copy_tree(source, temporary)
            if _static_tree_digest(temporary) != artifact.digest:
                raise ComposeBackendError("Static artifact changed during publication.")
            os.replace(temporary, target)
            _sync_directory(self.static_revision_root)

    def _static_artifact_ready(self, artifact_name: str) -> bool:
        artifact = self.manifest.artifact(artifact_name)
        if artifact.static_root is None:
            return False
        try:
            return (
                _static_tree_digest(self.static_revision_root / artifact.name)
                == artifact.digest
            )
        except (OSError, ComposeBackendError):
            return False

    def _materialize_secrets(self) -> None:
        if not self.manifest.secrets:
            return
        if self.secret_resolver is None:
            raise ComposeBackendError("No secret resolver is configured.")
        self.secret_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.secret_root, 0o700)
        for service_name, workload in self._runtime_workloads():
            selected = [
                secret
                for secret in self.manifest.secrets
                if not secret.workloads
                or workload.name in secret.workloads
                or service_name in secret.workloads
            ]
            if not selected:
                continue
            lines = []
            for secret in selected:
                value = self.secret_resolver(secret.ref)
                if not isinstance(value, str) or not value or "\x00" in value:
                    raise ComposeBackendError("Secret resolver returned an invalid value.")
                if secret.mode == "env":
                    if any(character in value for character in "\r\n"):
                        raise ComposeBackendError(
                            "Environment secret resolver returned a multiline value."
                        )
                    lines.append(secret.name + "=" + value)
                    continue
                encoded = _decode_secret_file(value, secret.encoding)
                _atomic_bytes(
                    self.secret_root
                    / (service_name + ".files")
                    / secret.name,
                    encoded,
                    mode=0o600,
                )
            if lines:
                _atomic_bytes(
                    self.secret_root / (service_name + ".env"),
                    ("\n".join(lines) + "\n").encode("utf-8"),
                    mode=0o600,
                )

    def _materialize_route_auth(self) -> None:
        protected = [route for route in self.manifest.routes if route.client_auth is not None]
        if not protected:
            return
        if self.secret_resolver is None:
            raise ComposeBackendError("No secret resolver is configured.")
        caddy_values: Dict[str, str] = {}
        for route in protected:
            assert route.client_auth is not None
            trust_value = self.secret_resolver(route.client_auth.trust_pool_ref)
            if not isinstance(trust_value, str) or not trust_value or "\x00" in trust_value:
                raise ComposeBackendError("Route trust resolver returned an invalid value.")
            trust_bytes = _decode_secret_file(
                trust_value,
                route.client_auth.trust_pool_encoding,
            )
            if b"-----BEGIN CERTIFICATE-----" not in trust_bytes:
                raise ComposeBackendError("Route trust pool is not a PEM certificate bundle.")
            target_root = route_auth_root(
                self.manifest,
                self.revision,
                route.name,
                runtime_root=self.runtime_root,
            )
            _atomic_bytes(target_root / "client-ca.pem", trust_bytes, mode=0o600)
            if route.client_auth.forward is None:
                continue
            authorization = self.secret_resolver(
                route.client_auth.forward.authorization_ref
            )
            if (
                not isinstance(authorization, str)
                or len(authorization) < 32
                or len(authorization) > 4096
                or any(character in authorization for character in "\x00\r\n")
            ):
                raise ComposeBackendError(
                    "Route authorization resolver returned an invalid value."
                )
            caddy_values[route_auth_env_key(self.manifest, route.name)] = authorization
        if caddy_values:
            _update_private_env(self.runtime_root / "caddy" / "env", caddy_values)

    def _run_migrations(self) -> None:
        marker_root = self.revision_root / ".ophelia" / "migrations"
        marker_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for migration in self.manifest.migrations:
            marker = marker_root / (migration.name + ".json")
            if marker.is_file():
                value = json.loads(marker.read_text(encoding="utf-8"))
                if value.get("revision_digest") != self.revision.content_digest():
                    raise ComposeBackendError("Migration marker binds another revision.")
                continue
            container_name = _docker_name(
                "ophelia-%s-%s" % (self.operation_id, migration.name)
            )
            inspected = self.runner.run(
                ["docker", "inspect", container_name, "--format", "{{.State.Status}}:{{.State.ExitCode}}"],
                timeout_seconds=30,
                check=False,
            )
            if inspected.exit_reason == "success" and inspected.stdout.strip() == "exited:0":
                pass
            elif inspected.exit_reason == "success":
                raise ComposeBackendError("Migration container exists without a successful result.")
            else:
                self._compose(
                    self.project,
                    self.revision_root,
                    "run",
                    "--name",
                    container_name,
                    "--no-deps",
                    "migration-" + migration.name,
                    timeout_seconds=migration.timeout_seconds,
                )
            _atomic_json(
                marker,
                {
                    "schema_version": 1,
                    "migration": migration.name,
                    "revision_id": self.revision.revision_id,
                    "revision_digest": self.revision.content_digest(),
                    "container_name": container_name,
                    "completed_at": _utc_now(),
                },
            )

    def _candidate_start_services(self) -> Tuple[str, ...]:
        if self.manifest.update.strategy == "recreate":
            return self._all_long_running()
        return tuple(
            item.name
            for item in self.manifest.workloads
            if item.kind is WorkloadKind.WEB
            or (item.kind is WorkloadKind.WORKER and item.update.overlap == "allow")
        )

    def _runtime_workloads(self) -> Tuple[Tuple[str, WorkloadV2], ...]:
        return tuple((item.name, item) for item in self.manifest.workloads) + tuple(
            ("migration-" + item.name, item.workload)
            for item in self.manifest.migrations
        )

    def _activation_services(self) -> Tuple[str, ...]:
        candidate = set(self._candidate_start_services())
        return tuple(
            item.name
            for item in self.manifest.workloads
            if item.kind in {WorkloadKind.WEB, WorkloadKind.INTERNAL, WorkloadKind.WORKER}
            and item.name not in candidate
        )

    def _all_long_running(self) -> Tuple[str, ...]:
        return tuple(
            item.name
            for item in self.manifest.workloads
            if item.kind in {WorkloadKind.WEB, WorkloadKind.INTERNAL, WorkloadKind.WORKER}
        )

    def _start_services(
        self,
        project: str,
        root: Path,
        services: Iterable[str],
        *,
        replicas: Optional[Mapping[str, int]] = None,
    ) -> None:
        selected = tuple(sorted(set(services)))
        if not selected:
            return
        scale = replicas or {item.name: item.replicas for item in self.manifest.workloads}
        scale_arguments = tuple(
            argument
            for name in selected
            if scale.get(name, 1) != 1
            for argument in ("--scale", "%s=%d" % (name, scale[name]))
        )
        self._compose(
            project,
            root,
            "up",
            "-d",
            "--no-deps",
            *scale_arguments,
            *selected,
        )

    def _stop_services(self, project: str, root: Path, services: Iterable[str]) -> None:
        selected = tuple(sorted(set(services)))
        if not selected or not Path(root).is_dir():
            return
        self._compose(project, root, "stop", *selected, check=False)

    def _running_services(self, project: str, root: Path) -> Set[str]:
        if not Path(root).is_dir():
            return set()
        result = self._compose(
            project,
            root,
            "ps",
            "--status",
            "running",
            "--services",
            check=False,
        )
        if result.exit_reason != "success":
            return set()
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def _replicas_ready(self, workload: WorkloadV2, project: str, root: Path) -> bool:
        if workload.replicas == 1:
            return True
        result = self._compose(
            project,
            root,
            "ps",
            "-q",
            workload.name,
            check=False,
            timeout_seconds=30,
        )
        if result.exit_reason != "success":
            return False
        return len([line for line in result.stdout.splitlines() if line.strip()]) == workload.replicas

    def _compose(
        self,
        project: str,
        root: Path,
        *arguments: str,
        check: bool = True,
        timeout_seconds: Optional[float] = None,
    ) -> ProcessResult:
        return self.runner.run(
            [
                "docker", "compose", "-p", project,
                "-f", str(Path(root) / "compose.yml"),
                *arguments,
            ],
            timeout_seconds=timeout_seconds or self.command_timeout_seconds,
            check=check,
        )

    def _ensure_network(self, name: str, *, external: bool) -> None:
        actual = name
        if name == "ophelia-app":
            actual = _docker_name("ophelia-%s-%s-app" % (self.manifest.app, self.manifest.environment))
        result = self.runner.run(
            ["docker", "network", "inspect", actual],
            timeout_seconds=30,
            check=False,
        )
        if result.exit_reason == "success":
            return
        labels = [] if external else [
            "--label", "ophelia.app=" + self.manifest.app,
            "--label", "ophelia.environment=" + self.manifest.environment,
        ]
        self.runner.run(
            ["docker", "network", "create", *labels, actual],
            timeout_seconds=30,
        )

    def _probe(self, workload: WorkloadV2, *, candidate: bool) -> bool:
        probe = workload.readiness or workload.startup
        if probe is None:
            return True
        if self.probe_checker is not None:
            return bool(self.probe_checker(workload, probe))
        if probe.command:
            containers = self._service_containers(workload.name)
            if len(containers) != workload.replicas:
                return False
            return all(
                self.runner.run(
                    ["docker", "exec", container, *probe.command],
                    check=False,
                    timeout_seconds=probe.timeout_seconds,
                ).exit_reason == "success"
                for container in containers
            )
        if probe.http is None:
            return False
        deadline = time.monotonic() + probe.timeout_seconds
        while True:
            if self._probe_http(workload, probe):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(probe.interval_seconds, max(0.0, deadline - time.monotonic())))

    def _probe_http(self, workload: WorkloadV2, probe: ProbeV2) -> bool:
        assert probe.http is not None
        containers = self._service_containers(workload.name)
        if len(containers) != workload.replicas:
            return False
        for container in containers:
            inspected = self.runner.run(
                [
                    "docker", "inspect", container, "--format",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                ],
                timeout_seconds=30,
                check=False,
            )
            host = inspected.stdout.strip()
            if inspected.exit_reason != "success" or not host:
                return False
            request = urllib.request.Request(
                "http://%s:%d%s" % (host, probe.http.port, probe.http.path),
                method=probe.http.method,
            )
            try:
                with urllib.request.urlopen(request, timeout=min(probe.timeout_seconds, 10.0)) as response:
                    if response.status != probe.http.expect_status:
                        return False
            except (OSError, urllib.error.URLError):
                return False
        return True

    def _service_containers(self, workload_name: str) -> Tuple[str, ...]:
        result = self._compose(
            self.project,
            self.revision_root,
            "ps", "-q", workload_name,
            check=False,
            timeout_seconds=30,
        )
        if result.exit_reason != "success":
            return ()
        return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())

    def _activate_caddy(self, candidate: Path) -> None:
        if not self.manifest.routes and not candidate.exists():
            return
        if candidate.is_symlink() or not candidate.is_file():
            raise ComposeBackendError("Candidate Caddy routes are missing or unsafe.")
        self.caddy_include.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        previous = self.caddy_include.read_bytes() if self.caddy_include.is_file() else None
        _atomic_bytes(self.caddy_include, candidate.read_bytes(), mode=0o600)
        if not self.shared_compose.is_file():
            if self.require_edge_runtime and self.manifest.routes:
                self._restore_caddy_bytes(previous)
                raise ComposeBackendError("Shared Caddy runtime is unavailable.")
            return
        try:
            self._caddy("validate", "--config", "/etc/caddy/Caddyfile")
            self._caddy("reload", "--config", "/etc/caddy/Caddyfile")
        except Exception:
            self._restore_caddy_bytes(previous)
            if previous is not None:
                self._caddy("reload", "--config", "/etc/caddy/Caddyfile", check=False)
            raise

    def _deactivate_caddy(self) -> None:
        self.caddy_include.unlink(missing_ok=True)
        if self.shared_compose.is_file():
            self._caddy("reload", "--config", "/etc/caddy/Caddyfile", check=False)

    def _restore_caddy_bytes(self, previous: Optional[bytes]) -> None:
        if previous is None:
            self.caddy_include.unlink(missing_ok=True)
        else:
            _atomic_bytes(self.caddy_include, previous, mode=0o600)

    def _caddy(self, *arguments: str, check: bool = True) -> ProcessResult:
        return self.runner.run(
            [
                "docker", "compose", "-f", str(self.shared_compose),
                "exec", "-T", "caddy", "caddy", *arguments,
            ],
            timeout_seconds=60,
            check=check,
        )

    def _activate_cron_registry(self) -> None:
        cron = [
            {
                "name": item.name,
                "schedule": item.schedule,
                "concurrency_policy": item.concurrency_policy,
                "compose_project": self.project,
                "compose_file": str(self.revision_root / "compose.yml"),
                "service": item.name,
                "revision_id": self.revision.revision_id,
                "revision_digest": self.revision.content_digest(),
                "fencing_token": self.fencing_token,
            }
            for item in self.manifest.workloads
            if item.kind is WorkloadKind.CRON
        ]
        path = self.scope_root / "cron" / "active.json"
        if cron:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _atomic_json(path, {"schema_version": 1, "schedules": cron})
        else:
            path.unlink(missing_ok=True)

    def _activate_previous_cron(self, runtime: Mapping[str, Any]) -> None:
        schedules = runtime.get("cron", [])
        path = self.scope_root / "cron" / "active.json"
        if schedules:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _atomic_json(path, {"schema_version": 1, "schedules": schedules})
        else:
            path.unlink(missing_ok=True)

    def _clear_cron_registry(self) -> None:
        (self.scope_root / "cron" / "active.json").unlink(missing_ok=True)

    def _cron_active(self, name: str) -> bool:
        path = self.scope_root / "cron" / "active.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return any(
            item.get("name") == name
            and item.get("revision_digest") == self.revision.content_digest()
            and item.get("fencing_token") == self.fencing_token
            for item in value.get("schedules", [])
            if isinstance(item, dict)
        )

    def _read_active(self, *, required: bool) -> Optional[Dict[str, Any]]:
        try:
            if self.active_path.is_symlink() or not self.active_path.is_file():
                raise FileNotFoundError
            value = json.loads(self.active_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if required:
                raise ComposeBackendError("Active revision evidence is missing.")
            return None
        except (OSError, ValueError) as exc:
            raise ComposeBackendError("Active revision evidence is malformed.") from exc
        expected = {
            "schema_version", "kind", "host_id", "app", "environment",
            "revision_id", "revision_digest", "compose_project", "activated_at",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ComposeBackendError("Active revision evidence is malformed.")
        if (
            value["host_id"] != self.host_id
            or value["app"] != self.manifest.app
            or value["environment"] != self.manifest.environment
        ):
            raise ComposeBackendError("Active revision evidence belongs to another scope.")
        return value

    def _previous_runtime(self, active: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        if active is None:
            return None
        handle = RuntimeHandle(
            backend=self.backend_name,
            handle_id=str(active["revision_id"]),
            revision_id=str(active["revision_id"]),
            revision_digest=str(active["revision_digest"]),
        )
        return self._runtime_for_handle(handle)

    def _runtime_for_handle(
        self,
        handle: RuntimeHandle,
        *,
        allow_candidate: bool = False,
    ) -> Dict[str, Any]:
        root = self.revisions_root / handle.revision_id
        if allow_candidate and handle.revision_id == self.revision.revision_id and not root.exists():
            root = self.revision_root
        document_path = root / "revision.json"
        if document_path.is_symlink() or not document_path.is_file():
            if allow_candidate and handle.revision_id == self.revision.revision_id:
                return {
                    "root": root,
                    "project": self.project,
                    "long_running": self._all_long_running(),
                    "background": tuple(
                        item.name
                        for item in self.manifest.workloads
                        if item.kind in {WorkloadKind.INTERNAL, WorkloadKind.WORKER}
                    ),
                    "cron": [],
                    "replicas": {
                        item.name: item.replicas
                        for item in self.manifest.workloads
                        if item.kind in {
                            WorkloadKind.WEB,
                            WorkloadKind.INTERNAL,
                            WorkloadKind.WORKER,
                        }
                    },
                }
            raise ComposeBackendError("Retained revision runtime is unavailable.")
        try:
            document = json.loads(document_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ComposeBackendError("Retained revision runtime is malformed.") from exc
        if (
            document.get("revision_id") != handle.revision_id
            or document.get("revision_digest") != handle.revision_digest
        ):
            raise ComposeBackendError("Retained revision runtime does not match its handle.")
        workloads = document.get("workloads")
        if not isinstance(workloads, list):
            raise ComposeBackendError("Retained revision workload inventory is malformed.")
        long_running = tuple(
            item["name"]
            for item in workloads
            if isinstance(item, dict) and item.get("kind") in {"web", "internal", "worker"}
        )
        background = tuple(
            item["name"]
            for item in workloads
            if isinstance(item, dict) and item.get("kind") in {"internal", "worker"}
        )
        replicas = {
            item["name"]: item["replicas"]
            for item in workloads
            if isinstance(item, dict)
            and item.get("kind") in {"web", "internal", "worker"}
            and isinstance(item.get("replicas"), int)
            and not isinstance(item.get("replicas"), bool)
            and item["replicas"] >= 1
        }
        if set(replicas) != set(long_running):
            raise ComposeBackendError("Retained revision replica inventory is malformed.")
        try:
            previous_manifest = load_manifest_v2(root / "manifest.lock.json")
        except (OSError, ValueError) as exc:
            raise ComposeBackendError("Retained revision manifest is malformed.") from exc
        cron = [
            {
                "name": item.name,
                "schedule": item.schedule,
                "concurrency_policy": item.concurrency_policy,
                "compose_project": document["compose_project"],
                "compose_file": str(root / "compose.yml"),
                "service": item.name,
                "revision_id": handle.revision_id,
                "revision_digest": handle.revision_digest,
                "fencing_token": self.fencing_token,
            }
            for item in previous_manifest.workloads
            if item.kind is WorkloadKind.CRON
        ]
        return {
            "root": root,
            "project": document["compose_project"],
            "long_running": long_running,
            "background": background,
            "cron": cron,
            "replicas": replicas,
        }

    def _activation_result(
        self,
        previous: Optional[str],
        active: Optional[str],
    ) -> TrafficActivationResult:
        digest = canonical_digest(
            {
                "previous_revision_digest": previous,
                "active_revision_digest": active,
                "caddy_include_digest": _file_digest(self.caddy_include),
            }
        )
        return TrafficActivationResult(
            committed_atomically=True,
            previous_revision_digest=previous,
            active_revision_digest=active,
            observed_state_digest=digest,
            evidence_digests=(digest,),
        )

    def _verify_route(self, route: RouteV2) -> bool:
        request = urllib.request.Request("https://" + route.domain + (route.path_prefix or "/"), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return 200 <= response.status < 500
        except (OSError, urllib.error.URLError):
            return False

    def _fence(self) -> RuntimeSideEffectFence:
        return RuntimeSideEffectFence(
            runtime_root=self.runtime_root,
            host_id=self.host_id,
            app=self.manifest.app,
            environment=self.manifest.environment,
            operation_id=self.operation_id,
            owner_id=self.owner_id,
            fencing_token=self.fencing_token,
        )


def _copy_tree(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ComposeBackendError("Candidate source tree is unsafe.")
    target.mkdir(mode=0o700, parents=True)
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ComposeBackendError("Candidate source tree contains a symbolic link.")
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_dir():
            destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        elif path.is_file():
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(path, destination, follow_symlinks=False)
            os.chmod(destination, 0o600)
        else:
            raise ComposeBackendError("Candidate source tree contains an unsupported file type.")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"), mode=0o600)


def _decode_secret_file(value: str, encoding: str) -> bytes:
    try:
        if encoding == "plain":
            decoded = value.encode("utf-8")
        elif encoding == "base64":
            decoded = base64.b64decode(value.encode("ascii"), validate=True)
        else:
            raise ValueError("unsupported secret encoding")
    except (UnicodeError, binascii.Error, ValueError) as exc:
        raise ComposeBackendError("File secret uses invalid encoding.") from exc
    if not decoded or len(decoded) > 2 * 1024 * 1024 or b"\x00" in decoded:
        raise ComposeBackendError("File secret exceeds its content boundary.")
    return decoded


def _update_private_env(path: Path, updates: Mapping[str, str]) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ComposeBackendError("Caddy environment path is unsafe.")
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    output = []
    seen = set()
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        name = line.split("=", 1)[0].strip()
        if not name or name in seen:
            raise ComposeBackendError("Caddy environment contains an invalid assignment.")
        seen.add(name)
        if name in pending:
            output.append(name + "=" + json.dumps(pending.pop(name)))
        else:
            output.append(line)
    for name, value in sorted(pending.items()):
        output.append(name + "=" + json.dumps(value))
    _atomic_bytes(path, ("\n".join(output) + "\n").encode("utf-8"), mode=0o600)


def _atomic_bytes(path: Path, value: bytes, *, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ComposeBackendError("Refusing to write through a symbolic link.")
    temporary = path.parent / ("." + path.name + ".tmp-" + os.urandom(6).hex())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, mode)
    try:
        offset = 0
        while offset < len(value):
            offset += os.write(descriptor, value[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, mode)
    _sync_directory(path.parent)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _make_writable(root: Path) -> None:
    if root.is_symlink():
        raise ComposeBackendError("Managed runtime root may not be a symbolic link.")
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            raise ComposeBackendError("Managed runtime tree contains a symbolic link.")
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    os.chmod(root, 0o700)


def _file_digest(path: Path) -> Optional[str]:
    if path.is_symlink() or not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _static_tree_digest(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise ComposeBackendError("Static artifact runtime must be a real directory.")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ComposeBackendError("Static artifact runtime may not contain symbolic links.")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_dir():
            digest.update(b"d\0" + relative + b"\0")
        elif path.is_file():
            digest.update(b"f\0" + relative + b"\0")
            digest.update(path.read_bytes())
        else:
            raise ComposeBackendError("Static artifact runtime contains a special file.")
    return "sha256:" + digest.hexdigest()


def _docker_name(value: str) -> str:
    allowed = "".join(character if character.isalnum() or character in "_.-" else "-" for character in value.lower())
    return allowed.strip(".-_")[:128]


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
