"""Recoverable process-set backend for product operations bundles."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Mapping, Optional, Tuple

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
)
from ..product_bundle import (
    ProductBundleError,
    ProductOperationsBundle,
    load_product_operations_bundle,
    verify_product_artifact,
)
from .contracts import (
    PreflightResult,
    RemoveResult,
    RuntimeHandle,
    StopResult,
    TrafficActivationResult,
)
from .runtime_fence import RuntimeSideEffectFence


_CHILDREN: dict[int, subprocess.Popen] = {}


class ProductProcessBackendError(RuntimeError):
    """A product process could not satisfy the approved runtime contract."""


class ProductProcessBackend:
    """Run one executable process set behind an atomic local TCP switch.

    One process declaration may request one or more replicas. Every candidate
    replica receives an isolated loopback port, passes the declared readiness
    probe, and is published as one atomic upstream set. Unsupported runtime
    shapes fail during preflight rather than degrading deployment safety.
    """

    backend_name = "product-process-v1"

    def __init__(
        self,
        *,
        bundle: ProductOperationsBundle,
        artifact_path: Path,
        candidate_root: Path,
        runtime_root: Path,
        environment_values: Mapping[str, str],
        host_id: str,
        operation_id: str,
        owner_id: str,
        fencing_token: int,
        data_root: Optional[Path] = None,
        startup_wait_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.bundle = bundle
        self.artifact_path = Path(artifact_path)
        self.candidate_root = Path(candidate_root)
        self.runtime_root = Path(runtime_root)
        self.environment_values = dict(environment_values)
        self.host_id = host_id
        self.operation_id = operation_id
        self.owner_id = owner_id
        self.fencing_token = fencing_token
        self.startup_wait_seconds = startup_wait_seconds
        self._clock = clock
        self.app = _product_slug(self.bundle.product_id)
        self.scope_root = (
            self.runtime_root
            / "apps"
            / self.app
            / "environments"
            / "production"
        )
        self.data_root = Path(data_root) if data_root is not None else self.scope_root / "data"
        self._data_boundary = (
            self.runtime_root.parent if data_root is not None else self.runtime_root
        )
        if data_root is not None and self.data_root != self.runtime_root.parent / "data":
            raise ProductProcessBackendError(
                "Isolated product data root must be the runtime sibling data directory."
            )

    @property
    def revisions_root(self) -> Path:
        return self.scope_root / "revisions"

    @property
    def traffic_root(self) -> Path:
        return self.scope_root / "traffic"

    @property
    def active_path(self) -> Path:
        return self.traffic_root / "active.json"

    @property
    def proxy_pid_path(self) -> Path:
        return self.traffic_root / "proxy.pid"

    def preflight(self, revision: Revision) -> PreflightResult:
        blockers = []
        try:
            self._validate_shape()
            self._validate_revision(revision)
            self._validate_managed_paths()
            verify_product_artifact(
                self.bundle,
                self._process()["artifact_id"],
                self.artifact_path,
            )
            missing = sorted(
                item["name"]
                for item in self.bundle.release.get("configuration", [])
                if item["required"] and not self.environment_values.get(item["name"])
            )
            if missing:
                blockers.append("required_configuration_missing")
            if any(
                not isinstance(value, str) or "\x00" in value
                for value in self.environment_values.values()
            ):
                blockers.append("configuration_contract_mismatch")
            probe_port = 1 if self._declared_port() != 1 else 2
            self._rewrite_argv(self._declared_port(), probe_port)
        except (OSError, ValueError, RuntimeError, ProductBundleError):
            blockers.append("product_runtime_contract_unsatisfied")
        observed = canonical_digest(
            {
                "bundle_digest": self.bundle.bundle_digest,
                "artifact_digest": self._artifact_declaration()["digest"],
                "required_configuration_present": not blockers,
                "shape": "replicated-process-http-v1",
                "replicas": self._replica_count(),
            }
        )
        return PreflightResult(
            ok=not blockers,
            observed_state_digest=observed,
            evidence_digests=tuple(sorted({
                self.bundle.bundle_digest,
                str(self._artifact_declaration()["digest"]),
            })) if not blockers else (),
            blocker_codes=tuple(sorted(set(blockers))),
        )

    def start(self, revision: Revision) -> RuntimeHandle:
        checked = self.preflight(revision)
        if not checked.ok:
            raise ProductProcessBackendError(checked.blocker_codes[0])
        handle = self.handle_for(revision)
        with self._fence():
            checked = self.preflight(revision)
            if not checked.ok:
                raise ProductProcessBackendError(checked.blocker_codes[0])
            revision_root = self._materialize(revision)
            existing = self._read_process_record(revision.revision_id, required=False)
            if existing is not None and self._record_healthy(existing):
                return handle
            if existing is not None:
                self._terminate_record(existing, self._shutdown_seconds())
            executable = revision_root / str(self._artifact_declaration()["path"])
            self.data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.data_root, 0o700)
            child_environment = {
                name: os.environ[name]
                for name in ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR")
                if name in os.environ
            }
            declared_environment = {
                item["name"]
                for item in self.bundle.release.get("configuration", [])
            }
            child_environment.update(
                {
                    name: value
                    for name, value in self.environment_values.items()
                    if name in declared_environment
                }
            )
            replicas = []
            reserved_ports: set[int] = set()
            try:
                for index in range(self._replica_count()):
                    port = self._allocate_internal_port(
                        revision.content_digest(), index, reserved_ports
                    )
                    reserved_ports.add(port)
                    argv = self._rewrite_argv(self._declared_port(), port)
                    process = subprocess.Popen(
                        [os.fspath(executable), *argv[1:]],
                        cwd=self.data_root,
                        env=child_environment,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                        close_fds=True,
                    )
                    _CHILDREN[process.pid] = process
                    replica = {
                        "index": index,
                        "pid": process.pid,
                        "internal_port": port,
                        "argv_digest": canonical_digest(argv),
                        "started_at_epoch": self._clock(),
                    }
                    replicas.append(replica)
                    self._wait_healthy(port)
            except BaseException:
                for replica in replicas:
                    self._terminate_replica(
                        replica, os.fspath(executable), self._shutdown_seconds()
                    )
                raise
            record = {
                "schema_version": 2,
                "revision_id": revision.revision_id,
                "revision_digest": revision.content_digest(),
                "artifact_digest": self._artifact_declaration()["digest"],
                "declared_port": self._declared_port(),
                "executable": os.fspath(executable),
                "replicas": replicas,
            }
            self._write_process_record(revision.revision_id, record)
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
        record = self._read_process_record(handle.revision_id, required=False)
        running = (
            record is not None
            and record.get("revision_digest") == handle.revision_digest
            and record.get("artifact_digest") == self._artifact_declaration()["digest"]
            and self._record_healthy(record)
        )
        workload = self._revision_workload(handle.revision_id)
        observed = ObservedWorkload(
            workload_id=workload.workload_id,
            workload_kind=workload.workload_kind,
            runtime_state="healthy" if running else "unavailable",
            artifact_digest=workload.artifact_digest,
            ready=running,
        )
        return ObservedRevision(
            host_id=self.host_id,
            app=self.app,
            environment="production",
            revision_id=handle.revision_id,
            revision_digest=handle.revision_digest if running else None,
            state=RevisionState.READY if running else RevisionState.FAILED,
            workloads=(observed,),
            observed_at=_utc_now(),
        )

    def verify(self, revision: Revision, observed: ObservedRevision) -> VerificationResult:
        passed = (
            observed.state is RevisionState.READY
            and observed.revision_id == revision.revision_id
            and observed.revision_digest == revision.content_digest()
            and all(item.ready for item in observed.workloads)
        )
        digest = observed.digest()
        return VerificationResult(
            status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
            observed_revision_digest=revision.content_digest() if passed else None,
            observed_state_digest=digest,
            observed_at=observed.observed_at,
            checks=(
                VerificationCheck(
                    name="product_candidate_health",
                    status=CheckStatus.PASSED if passed else CheckStatus.FAILED,
                    observed_digest=digest,
                ),
            ),
        )

    def activate(
        self,
        candidate: RuntimeHandle,
        expected_active_revision_digest: Optional[str],
    ) -> TrafficActivationResult:
        record = self._read_process_record(candidate.revision_id, required=True)
        if not self._record_healthy(record):
            raise ProductProcessBackendError("Candidate is not healthy at activation.")
        with self._fence():
            previous = self._read_active(required=False)
            previous_digest = None if previous is None else str(previous["revision_digest"])
            if previous_digest != expected_active_revision_digest:
                if previous_digest == candidate.revision_digest:
                    return self._activation_result(previous_digest, candidate.revision_digest)
                raise ProductProcessBackendError("Active product revision changed before activation.")
            self._ensure_proxy()
            _atomic_json(self.active_path, self._active_state(candidate, record))
        return self._activation_result(previous_digest, candidate.revision_digest)

    def verify_active(self, revision: Revision, handle: RuntimeHandle) -> VerificationResult:
        active = self._read_active(required=False)
        selected = (
            active is not None
            and active.get("revision_id") == handle.revision_id
            and active.get("revision_digest") == handle.revision_digest
        )
        healthy = selected and self._probe(self._declared_port())
        state_digest = canonical_digest(
            {
                "selected_revision_digest": None if active is None else active.get("revision_digest"),
                "public_port": self._declared_port(),
                "healthy": healthy,
            }
        )
        return VerificationResult(
            status=VerificationStatus.PASSED if healthy else VerificationStatus.FAILED,
            observed_revision_digest=revision.content_digest() if healthy else None,
            observed_state_digest=state_digest,
            observed_at=_utc_now(),
            checks=(
                VerificationCheck(
                    name="product_active_health",
                    status=CheckStatus.PASSED if healthy else CheckStatus.FAILED,
                    observed_digest=state_digest,
                ),
            ),
        )

    def restore(
        self, previous: RuntimeHandle, failed_candidate: RuntimeHandle
    ) -> TrafficActivationResult:
        record = self._ensure_retained_revision_running(previous)
        candidate_record = self._read_process_record(
            failed_candidate.revision_id, required=True
        )
        with self._fence():
            _atomic_json(self.active_path, self._active_state(previous, record))
        if not self._probe(self._declared_port()):
            if (
                self._record_healthy(candidate_record)
            ):
                with self._fence():
                    _atomic_json(
                        self.active_path,
                        self._active_state(failed_candidate, candidate_record),
                    )
            raise ProductProcessBackendError(
                "Restored product revision did not pass its public health probe."
            )
        return self._activation_result(failed_candidate.revision_digest, previous.revision_digest)

    def deactivate(self, failed_candidate: RuntimeHandle) -> TrafficActivationResult:
        with self._fence():
            active = self._read_active(required=False)
            previous = None if active is None else active.get("revision_digest")
            if active is not None and previous not in {failed_candidate.revision_digest, None}:
                raise ProductProcessBackendError("Refusing to deactivate a different revision.")
            try:
                self.active_path.unlink()
            except FileNotFoundError:
                pass
        return self._activation_result(
            failed_candidate.revision_digest,
            None,
        )

    def drain_previous(
        self, previous: RuntimeHandle, candidate: RuntimeHandle
    ) -> StopResult:
        active = self._read_active(required=True)
        if active["revision_digest"] != candidate.revision_digest:
            raise ProductProcessBackendError("Candidate must be active before predecessor drain.")
        return self.stop(previous, self._shutdown_seconds())

    def stop(self, handle: RuntimeHandle, grace_seconds: int) -> StopResult:
        record = self._read_process_record(handle.revision_id, required=False)
        if record is not None and self._record_process_alive(record):
            with self._fence():
                self._terminate_record(record, grace_seconds)
        stopped = record is None or not self._record_process_alive(record)
        return StopResult(
            stopped=stopped,
            observed_state_digest=canonical_digest(
                {"revision_digest": handle.revision_digest, "stopped": stopped}
            ),
        )

    def remove(self, handle: RuntimeHandle) -> RemoveResult:
        active = self._read_active(required=False)
        if active is not None and active.get("revision_digest") == handle.revision_digest:
            raise ProductProcessBackendError("An active product revision cannot be removed.")
        self.stop(handle, self._shutdown_seconds())
        root = self._revision_root(handle.revision_id)
        with self._fence():
            if root.exists():
                if root.is_symlink() or not root.is_dir():
                    raise ProductProcessBackendError("Revision root has an unsafe type.")
                _make_writable(root)
                shutil.rmtree(root)
        removed = not root.exists()
        return RemoveResult(
            removed=removed,
            observed_state_digest=canonical_digest(
                {"revision_digest": handle.revision_digest, "removed": removed}
            ),
        )

    def _materialize(self, revision: Revision) -> Path:
        target = self._revision_root(revision.revision_id)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_dir():
                raise ProductProcessBackendError(
                    "Product revision root has an unsafe type."
                )
            metadata = self._read_revision_metadata(revision.revision_id)
            if metadata.get("revision_digest") != revision.content_digest():
                raise ProductProcessBackendError("Immutable revision id collision.")
            self._verify_materialized_release(
                target,
                "Existing product revision failed verification.",
            )
            return target
        self.revisions_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.revisions_root / ("." + revision.revision_id + ".tmp")
        if temporary.exists() or temporary.is_symlink():
            if temporary.is_symlink() or not temporary.is_dir():
                raise ProductProcessBackendError(
                    "Product revision work path has an unsafe type."
                )
            _make_writable(temporary)
            shutil.rmtree(temporary)
        temporary.mkdir(mode=0o700)
        operations = temporary / ".product" / "operations"
        operations.mkdir(mode=0o700, parents=True)
        shutil.copyfile(
            self.bundle.root / "bundle.json",
            operations / "bundle.json",
            follow_symlinks=False,
        )
        for reference in self.bundle.bundle["documents"]:
            relative = Path(str(reference["path"]))
            destination = operations / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(
                self.bundle.root / relative,
                destination,
                follow_symlinks=False,
            )
        artifact_relative = Path(str(self._artifact_declaration()["path"]))
        artifact = temporary / artifact_relative
        artifact.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(self.artifact_path, artifact, follow_symlinks=False)
        self._verify_materialized_release(
            temporary,
            "Materialized product revision failed verification.",
        )
        artifact.chmod(0o500)
        _atomic_json(
            temporary / "ophelia-revision.json",
            {
                "schema_version": 1,
                "revision_id": revision.revision_id,
                "revision_digest": revision.content_digest(),
                "created_at": revision.created_at,
                "bundle_digest": self.bundle.bundle_digest,
                "release_id": self.bundle.release_id,
                "artifact_digest": self._artifact_declaration()["digest"],
            },
        )
        os.replace(temporary, target)
        _sync_directory(target.parent)
        _harden_tree(target)
        return target

    def _verify_materialized_release(self, root: Path, message: str) -> None:
        try:
            bundle = load_product_operations_bundle(root / ".product" / "operations")
            if bundle.bundle_digest != self.bundle.bundle_digest:
                raise ProductBundleError(
                    "Materialized operations bundle differs from the approved bundle."
                )
            declaration = bundle.artifact(self._process()["artifact_id"])
            verify_product_artifact(
                bundle,
                str(declaration["id"]),
                root / str(declaration["path"]),
            )
        except (OSError, ProductBundleError) as exc:
            raise ProductProcessBackendError(message) from exc

    def _ensure_proxy(self) -> None:
        self._validate_managed_paths()
        pid = _read_pid(self.proxy_pid_path)
        if pid is not None and _pid_alive(pid):
            if not _pid_command_matches(
                pid,
                ("ophelia.execution.service_proxy", os.fspath(self.active_path)),
            ):
                raise ProductProcessBackendError(
                    "Product proxy pid belongs to another process."
                )
            if self._port_accepts_connections(self._declared_port()):
                return
            raise ProductProcessBackendError("Product proxy pid is live but its port is unavailable.")
        if self.proxy_pid_path.is_symlink():
            raise ProductProcessBackendError("Product proxy pid path is symlinked.")
        try:
            self.proxy_pid_path.unlink()
        except FileNotFoundError:
            pass
        if not self._port_available(self._declared_port()):
            raise ProductProcessBackendError("Declared product port is owned by another process.")
        self.traffic_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        listen_host = "0.0.0.0" if self._port()["exposure"] == "public" else "127.0.0.1"
        environment = {
            name: os.environ[name]
            for name in ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR")
            if name in os.environ
        }
        environment["PYTHONPATH"] = os.pathsep.join(
            item for item in sys.path if isinstance(item, str) and item
        )
        proxy = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ophelia.execution.service_proxy",
                "--host",
                listen_host,
                "--port",
                str(self._declared_port()),
                "--state",
                os.fspath(self.active_path),
                "--pid-file",
                os.fspath(self.proxy_pid_path),
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        _CHILDREN[proxy.pid] = proxy
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self._port_accepts_connections(self._declared_port()):
                return
            time.sleep(0.05)
        raise ProductProcessBackendError("Product proxy did not become ready.")

    def _wait_healthy(self, port: int) -> None:
        deadline = time.monotonic() + self.startup_wait_seconds
        while time.monotonic() < deadline:
            if self._probe(port):
                return
            time.sleep(0.1)
        raise ProductProcessBackendError("Product health probe did not pass before deadline.")

    def _probe(self, port: int) -> bool:
        probe = self._health()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{probe['path']}", method="GET"
            )
            with urllib.request.urlopen(
                request, timeout=float(probe["timeout_seconds"])
            ) as response:
                return 200 <= response.status < 400
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def _rewrite_argv(self, declared_port: int, internal_port: int) -> list[str]:
        argv = list(self._process()["argv"])
        rewritten = False
        result = []
        for index, value in enumerate(argv):
            replacement = value
            if index == 0:
                result.append(value)
                continue
            if value == str(declared_port):
                replacement = str(internal_port)
            elif value.endswith("=" + str(declared_port)):
                replacement = value[: -len(str(declared_port))] + str(internal_port)
            elif value.endswith(":" + str(declared_port)):
                prefix, separator, _ = value.rpartition("=")
                address = "127.0.0.1:" + str(internal_port)
                replacement = prefix + separator + address if separator else address
            if replacement != value:
                rewritten = True
            result.append(replacement)
        if not rewritten:
            raise ProductProcessBackendError(
                "Process argv does not expose its declared port for candidate isolation."
            )
        return result

    def _allocate_internal_port(
        self, revision_digest: str, replica_index: int, reserved: set[int]
    ) -> int:
        seed = hashlib.sha256(
            f"{revision_digest}:{replica_index}".encode("utf-8")
        ).hexdigest()
        base = 20000 + int(seed[:8], 16) % 30000
        for offset in range(512):
            port = 20000 + ((base - 20000 + offset) % 30000)
            if (
                port != self._declared_port()
                and port not in reserved
                and self._port_available(port)
            ):
                return port
        raise ProductProcessBackendError("No candidate loopback port is available.")

    @staticmethod
    def _port_available(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                candidate.bind(("127.0.0.1", port))
            except OSError:
                return False
        return True

    @staticmethod
    def _port_accepts_connections(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            return False

    def _validate_shape(self) -> None:
        if (
            len(self.bundle.runtime["processes"]) != 1
            or not 1 <= self._replica_count() <= 32
            or len(self.bundle.runtime["ports"]) != 1
            or self._port()["protocol"] != "http"
            or len(self.bundle.runtime["health"]) < 1
        ):
            raise ProductProcessBackendError("product_runtime_shape_unsupported")
        if self._artifact_declaration()["kind"] != "executable":
            raise ProductProcessBackendError("product_artifact_kind_unsupported")

    def _validate_managed_paths(self) -> None:
        for managed, boundary in (
            (self.scope_root, self.runtime_root),
            (self.revisions_root, self.runtime_root),
            (self.traffic_root, self.runtime_root),
            (self.data_root, self._data_boundary),
        ):
            root = boundary.resolve(strict=False)
            try:
                relative = managed.relative_to(boundary)
            except ValueError as exc:
                raise ProductProcessBackendError(
                    "Managed product path escapes the runtime root."
                ) from exc
            cursor = boundary
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ProductProcessBackendError(
                        "Managed product path contains a symlink."
                    )
            try:
                managed.resolve(strict=False).relative_to(root)
            except ValueError as exc:
                raise ProductProcessBackendError(
                    "Managed product path escapes the runtime root."
                ) from exc

    def _validate_revision(self, revision: Revision) -> None:
        if revision.app != self.app or revision.environment != "production":
            raise ProductProcessBackendError("Revision scope differs from product bundle.")
        artifact_digest = str(self._artifact_declaration()["digest"])
        if revision.manifest_digest != self.bundle.document_digests["release-manifest"]:
            raise ProductProcessBackendError("Revision manifest differs from release document bytes.")
        if revision.artifact_digests != (artifact_digest,) or len(revision.workloads) != 1 or revision.workloads[0].artifact_digest != artifact_digest:
            raise ProductProcessBackendError("Revision workload differs from release artifact.")

    def _revision_workload(self, revision_id: str):
        # All accepted revisions for this backend have one exact process workload.
        from ..domain.revisions import Workload, WorkloadKind

        return Workload(
            workload_id=self._process()["id"].replace(".", "-"),
            workload_kind=WorkloadKind.WEB,
            artifact_digest=str(self._artifact_declaration()["digest"]),
            route_ids=(self._port()["id"].replace(".", "-"),),
            overlap_safe=True,
        )

    def _process(self):
        return self.bundle.runtime["processes"][0]

    def _port(self):
        return self.bundle.runtime["ports"][0]

    def _health(self):
        return self.bundle.runtime["health"][0]

    def _artifact_declaration(self):
        return self.bundle.artifact(self._process()["artifact_id"])

    def _declared_port(self) -> int:
        return int(self._port()["port"])

    def _shutdown_seconds(self) -> int:
        return int(self._process()["shutdown_seconds"])

    def _replica_count(self) -> int:
        return int(self._process()["replicas"])

    def shutdown_seconds(self) -> int:
        """Return the contract-declared graceful shutdown bound."""

        return self._shutdown_seconds()

    def _ensure_retained_revision_running(
        self, previous: RuntimeHandle
    ) -> Mapping[str, object]:
        record = self._read_process_record(previous.revision_id, required=False)
        if record is not None and self._record_healthy(record):
            return record

        revision_root = self._revision_root(previous.revision_id)
        metadata = self._read_revision_metadata(previous.revision_id)
        created_at = metadata.get("created_at")
        if not isinstance(created_at, str):
            raise ProductProcessBackendError(
                "Previous product revision creation evidence is unavailable."
            )
        retained_bundle = load_product_operations_bundle(
            revision_root / ".product" / "operations"
        )
        retained_port = retained_bundle.runtime["ports"][0]
        retained_health = retained_bundle.runtime["health"][0]
        if (
            retained_port["port"] != self._port()["port"]
            or retained_port["protocol"] != self._port()["protocol"]
            or retained_port["exposure"] != self._port()["exposure"]
            or retained_health["path"] != self._health()["path"]
        ):
            raise ProductProcessBackendError(
                "Previous product revision has an incompatible traffic contract."
            )
        from ..product_execution import _revision

        retained_revision = _revision(retained_bundle, created_at)
        if (
            retained_revision.revision_id != previous.revision_id
            or retained_revision.content_digest() != previous.revision_digest
        ):
            raise ProductProcessBackendError(
                "Previous product revision does not match retained evidence."
            )
        artifact = retained_bundle.artifact(
            retained_bundle.runtime["processes"][0]["artifact_id"]
        )
        declared_environment = {
            item["name"]
            for item in retained_bundle.release.get("configuration", [])
        }
        retained_backend = ProductProcessBackend(
            bundle=retained_bundle,
            artifact_path=revision_root / str(artifact["path"]),
            candidate_root=revision_root,
            runtime_root=self.runtime_root,
            environment_values={
                name: value
                for name, value in self.environment_values.items()
                if name in declared_environment
            },
            host_id=self.host_id,
            operation_id=self.operation_id,
            owner_id=self.owner_id,
            fencing_token=self.fencing_token,
        )
        if record is not None and retained_backend._record_process_alive(record):
            retained_backend.stop(previous, retained_backend.shutdown_seconds())
        retained_backend.start(retained_revision)
        return self._read_process_record(previous.revision_id, required=True)

    def _revision_root(self, revision_id: str) -> Path:
        return self.revisions_root / revision_id

    def _process_record_path(self, revision_id: str) -> Path:
        return self._revision_root(revision_id) / "ophelia-process.json"

    def _read_revision_metadata(self, revision_id: str) -> Mapping[str, object]:
        return _read_json(self._revision_root(revision_id) / "ophelia-revision.json")

    def _read_process_record(self, revision_id: str, *, required: bool) -> Optional[Mapping[str, object]]:
        path = self._process_record_path(revision_id)
        if not path.exists() and not path.is_symlink():
            if required:
                raise ProductProcessBackendError("Product process evidence is unavailable.")
            return None
        value = _read_json(path)
        common = {
            "schema_version", "revision_id", "revision_digest", "artifact_digest",
            "declared_port", "executable",
        }
        if value.get("schema_version") == 1:
            expected = common | {"pid", "internal_port", "argv_digest", "started_at_epoch"}
            valid = set(value) == expected
        elif value.get("schema_version") == 2:
            valid = set(value) == common | {"replicas"}
            replicas = value.get("replicas")
            valid = valid and isinstance(replicas, list) and len(replicas) == self._replica_count()
            if valid:
                for index, replica in enumerate(replicas):
                    if (
                        not isinstance(replica, dict)
                        or set(replica) != {
                            "index", "pid", "internal_port", "argv_digest", "started_at_epoch"
                        }
                        or replica.get("index") != index
                    ):
                        valid = False
                        break
        else:
            valid = False
        if not valid:
            raise ProductProcessBackendError("Product process evidence is malformed.")
        return value

    def _write_process_record(self, revision_id: str, value: Mapping[str, object]) -> None:
        path = self._process_record_path(revision_id)
        if path.exists():
            os.chmod(path, 0o600)
        parent = path.parent
        os.chmod(parent, 0o700)
        _atomic_json(path, value)
        os.chmod(parent, 0o500)

    def _read_active(self, *, required: bool) -> Optional[Mapping[str, object]]:
        if not self.active_path.exists() and not self.active_path.is_symlink():
            if required:
                raise ProductProcessBackendError("Active product state is unavailable.")
            return None
        value = _read_json(self.active_path)
        common = {
            "schema_version", "revision_id", "revision_digest", "upstream_host",
            "upstream_port", "artifact_digest",
        }
        legacy = value.get("schema_version") == 1 and set(value) == common
        replicated = (
            value.get("schema_version") == 2
            and set(value) == {
                "schema_version", "revision_id", "revision_digest", "upstreams",
                "artifact_digest",
            }
            and isinstance(value.get("upstreams"), list)
            and bool(value.get("upstreams"))
            and all(
                isinstance(item, dict)
                and set(item) == {"host", "port"}
                and item.get("host") == "127.0.0.1"
                and isinstance(item.get("port"), int)
                for item in value.get("upstreams", [])
            )
        )
        if not legacy and not replicated:
            raise ProductProcessBackendError("Active product state is malformed.")
        return value

    @staticmethod
    def _replica_alive(replica: Mapping[str, object], executable: object) -> bool:
        pid = replica.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or not isinstance(executable, str):
            return False
        if not _pid_alive(pid):
            return False
        try:
            command = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return executable in command

    def _replica_records(
        self, record: Mapping[str, object]
    ) -> Tuple[Mapping[str, object], ...]:
        if record.get("schema_version") == 1:
            return (record,)
        replicas = record.get("replicas")
        if not isinstance(replicas, list):
            return ()
        return tuple(item for item in replicas if isinstance(item, dict))

    def _record_process_alive(self, record: Mapping[str, object]) -> bool:
        replicas = self._replica_records(record)
        return len(replicas) == self._replica_count() and all(
            self._replica_alive(replica, record.get("executable"))
            for replica in replicas
        )

    def _record_healthy(self, record: Mapping[str, object]) -> bool:
        return self._record_process_alive(record) and all(
            self._probe(int(replica["internal_port"]))
            for replica in self._replica_records(record)
        )

    def _terminate_record(self, record: Mapping[str, object], grace_seconds: int) -> None:
        for replica in self._replica_records(record):
            self._terminate_replica(replica, record.get("executable"), grace_seconds)

    def _terminate_replica(
        self,
        replica: Mapping[str, object],
        executable: object,
        grace_seconds: int,
    ) -> None:
        if not self._replica_alive(replica, executable):
            return
        pid = int(replica["pid"])
        shutdown_signal = (
            signal.SIGINT
            if self.bundle.runtime["lifecycle"]["shutdown_signal"] == "SIGINT"
            else signal.SIGTERM
        )
        os.kill(pid, shutdown_signal)
        deadline = time.monotonic() + max(1, min(grace_seconds, 900))
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                return
            time.sleep(0.05)
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.05)

    def _active_state(
        self, handle: RuntimeHandle, record: Mapping[str, object]
    ) -> Mapping[str, object]:
        replicas = self._replica_records(record)
        if len(replicas) == 1:
            return {
                "schema_version": 1,
                "revision_id": handle.revision_id,
                "revision_digest": handle.revision_digest,
                "upstream_host": "127.0.0.1",
                "upstream_port": int(replicas[0]["internal_port"]),
                "artifact_digest": str(record["artifact_digest"]),
            }
        return {
            "schema_version": 2,
            "revision_id": handle.revision_id,
            "revision_digest": handle.revision_digest,
            "upstreams": [
                {"host": "127.0.0.1", "port": int(item["internal_port"])}
                for item in replicas
            ],
            "artifact_digest": str(record["artifact_digest"]),
        }

    def _activation_result(
        self,
        previous_digest: Optional[str],
        active_digest: Optional[str],
    ) -> TrafficActivationResult:
        observed = canonical_digest(
            {
                "previous_revision_digest": previous_digest,
                "active_revision_digest": active_digest,
                "active_state_digest": (
                    None if not self.active_path.exists() else _digest_file(self.active_path)
                ),
            }
        )
        return TrafficActivationResult(
            committed_atomically=True,
            previous_revision_digest=previous_digest,
            active_revision_digest=active_digest,
            observed_state_digest=observed,
            evidence_digests=(observed,),
        )

    def _fence(self) -> RuntimeSideEffectFence:
        self.runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return RuntimeSideEffectFence(
            self.runtime_root,
            self.host_id,
            self.app,
            "production",
            self.operation_id,
            self.owner_id,
            self.fencing_token,
        )


def load_staged_product_backend(
    *,
    candidate_root: Path,
    runtime_root: Path,
    environment_values: Mapping[str, str],
    host_id: str,
    operation_id: str,
    owner_id: str,
    fencing_token: int,
) -> ProductProcessBackend:
    bundle_root = Path(candidate_root) / ".product" / "operations"
    bundle = load_product_operations_bundle(bundle_root)
    process = bundle.runtime["processes"][0]
    artifact = bundle.artifact(process["artifact_id"])
    return ProductProcessBackend(
        bundle=bundle,
        artifact_path=Path(candidate_root) / str(artifact["path"]),
        candidate_root=Path(candidate_root),
        runtime_root=runtime_root,
        environment_values=environment_values,
        host_id=host_id,
        operation_id=operation_id,
        owner_id=owner_id,
        fencing_token=fencing_token,
    )


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or (path.exists() and path.is_symlink()):
        raise ProductProcessBackendError("Managed product path is symlinked.")
    temporary = path.parent / ("." + path.name + ".tmp-" + os.urandom(6).hex())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        data = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    _sync_directory(path.parent)


def _read_json(path: Path) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise ProductProcessBackendError("Managed product record is unavailable or unsafe.")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductProcessBackendError("Managed product record is malformed.") from exc
    if not isinstance(value, dict):
        raise ProductProcessBackendError("Managed product record must be an object.")
    return value


def _sync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _harden_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            raise ProductProcessBackendError("Product candidate contains a symlink.")
        os.chmod(path, 0o500 if path.is_dir() else (0o500 if os.access(path, os.X_OK) else 0o400))
    os.chmod(root, 0o500)


def _strict_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ProductProcessBackendError(
                f"Managed product record repeats object key: {key}"
            )
        value[key] = item
    return value


def _make_writable(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_symlink():
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
    os.chmod(root, 0o700)


def _read_pid(path: Path) -> Optional[int]:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return value if value > 1 else None


def _pid_alive(pid: int) -> bool:
    child = _CHILDREN.get(pid)
    if child is not None:
        if child.poll() is None:
            return True
        _CHILDREN.pop(pid, None)
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_command_matches(pid: int, tokens: Tuple[str, ...]) -> bool:
    try:
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return all(token in command for token in tokens)


def reap_product_children() -> None:
    """Reap tracked child handles after an external supervisor stops them."""

    deadline = time.monotonic() + 2.0
    while _CHILDREN and time.monotonic() < deadline:
        for pid, child in tuple(_CHILDREN.items()):
            if child.poll() is not None:
                _CHILDREN.pop(pid, None)
        if _CHILDREN:
            time.sleep(0.02)


def _digest_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _product_slug(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() else "-" for character in value.lower()
    )
    normalized = "-".join(part for part in normalized.split("-") if part)
    if not normalized or not normalized[0].isalpha():
        normalized = "product-" + normalized
    if len(normalized) > 63:
        suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
        normalized = normalized[:52].rstrip("-") + "-" + suffix
    return normalized
