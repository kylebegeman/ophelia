"""Recoverable filesystem backend for confirmed production static revisions."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

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
from ..manifest import Manifest
from ..runtime import (
    ConfirmedCandidatePreflight,
    DeployMetadata,
    deployment_baseline_digest,
    preflight_confirmed_candidate,
    static_runtime_root,
)
from .contracts import (
    LogBatch,
    PreflightResult,
    RemoveResult,
    RuntimeHandle,
    StopResult,
    TrafficActivationResult,
)
from .runtime_fence import RuntimeSideEffectFence
from .staging import tree_digest


class StaticBackendError(RuntimeError):
    """Fail-closed static backend error with no raw candidate content."""


@dataclass(frozen=True)
class _Predecessor:
    caddy_present: bool
    caddy_digest: Optional[str]
    static_target: Optional[str]
    revision_digest: Optional[str]


class StaticRuntimeBackend:
    """Materialize, observe, activate, and restore one confirmed static candidate.

    Candidate bindings are constructor configuration so the RuntimeBackend
    methods remain compatible with the narrow kernel protocols.
    """

    backend_name = "production-static"

    def __init__(
        self,
        *,
        manifest: Manifest,
        manifest_path: Path,
        candidate_root: Path,
        generated_files: List[Path],
        runtime_root: Path,
        deploy_metadata: DeployMetadata,
        expected_candidate_digest: str,
        expected_bundle_hash: str,
        expected_baseline_digest: str,
        host_id: str,
        operation_id: str,
        owner_id: str,
        fencing_token: int,
        fault_injector: Optional[Callable[[str], None]] = None,
    ) -> None:
        if manifest.kind != "static":
            raise ValueError("StaticRuntimeBackend requires a static manifest.")
        self.manifest = manifest
        self.manifest_path = Path(manifest_path)
        self.candidate_root = Path(candidate_root)
        self.generated_files = list(generated_files)
        self.runtime_root = Path(runtime_root)
        self.deploy_metadata = deploy_metadata
        self.expected_candidate_digest = expected_candidate_digest
        self.expected_bundle_hash = expected_bundle_hash
        self.expected_baseline_digest = expected_baseline_digest
        self.host_id = host_id
        self.operation_id = operation_id
        self.owner_id = owner_id
        self.fencing_token = fencing_token
        self._fault_injector = fault_injector
        self._last_preflight: Optional[ConfirmedCandidatePreflight] = None

    @property
    def release_id(self) -> str:
        if not self.deploy_metadata.release_id:
            raise StaticBackendError("Locked deploy metadata is missing a release id.")
        return self.deploy_metadata.release_id

    @property
    def app_root(self) -> Path:
        return self.runtime_root / "apps" / self.manifest.app

    @property
    def bundle_release(self) -> Path:
        return self.app_root / "release-bundles" / self.release_id

    @property
    def static_root(self) -> Path:
        return static_runtime_root(self.runtime_root, self.manifest)

    @property
    def static_release(self) -> Path:
        return self.static_root / "releases" / self.release_id

    @property
    def caddy_source(self) -> Path:
        return self.bundle_release / "caddy" / f"{self.manifest.app}.caddy"

    @property
    def caddy_live(self) -> Path:
        return self.runtime_root / "caddy" / "sites.d" / f"{self.manifest.app}.caddy"

    @property
    def static_current(self) -> Path:
        return self.static_root / "current"

    @property
    def revision_record(self) -> Path:
        return self.app_root / "static-revisions" / f"{self.release_id}.json"

    @property
    def activation_record(self) -> Path:
        return self.app_root / "static-activations" / f"{self.operation_id}.json"

    def preflight(self, revision: Revision) -> PreflightResult:
        try:
            self._validate_revision(revision)
            checked = preflight_confirmed_candidate(
                self.manifest,
                self.manifest_path,
                self.candidate_root,
                self.generated_files,
                self.runtime_root,
                self.deploy_metadata,
                expected_candidate_digest=self.expected_candidate_digest,
                expected_bundle_hash=self.expected_bundle_hash,
                expected_baseline_digest=self.expected_baseline_digest,
            )
            self._validate_existing_release(checked)
            self._last_preflight = checked
            return PreflightResult(
                ok=True,
                observed_state_digest=_digest_text(self.expected_baseline_digest),
                evidence_digests=tuple(sorted({
                    _as_digest(self.expected_candidate_digest),
                    _as_digest(self.expected_bundle_hash),
                    _digest_text(",".join(str(path) for path in self.generated_files)),
                })),
            )
        except (OSError, ValueError, RuntimeError) as exc:
            self._last_preflight = None
            return PreflightResult(
                ok=False,
                observed_state_digest=_digest_text(
                    deployment_baseline_digest(self.manifest, self.runtime_root)
                ),
                evidence_digests=(),
                blocker_codes=(_blocker_code(exc),),
            )

    def start(self, revision: Revision) -> RuntimeHandle:
        result = self.preflight(revision)
        if not result.ok:
            raise StaticBackendError(
                f"Static candidate preflight failed: {result.blocker_codes[0]}."
            )
        with self._fence():
            # Revalidate under the mutation fence so baseline and immutable
            # collision checks cannot race publication.
            result = self.preflight(revision)
            if not result.ok:
                raise StaticBackendError(
                    f"Static candidate preflight failed: {result.blocker_codes[0]}."
                )
            checked = self._last_preflight
            if checked is None or checked.static_source is None:
                raise StaticBackendError(
                    "Static candidate preflight did not produce materialization inputs."
                )

            _publish_immutable_tree(checked.candidate_root, self.bundle_release)
            _publish_immutable_tree(checked.static_source, self.static_release)
            _reject_symlink_parent(self.revision_record, self.runtime_root)
            record = {
                "schema_version": 1,
                "revision_id": revision.revision_id,
                "revision_digest": revision.content_digest(),
                "candidate_digest": _as_digest(self.expected_candidate_digest),
                "bundle_digest": _as_digest(tree_digest(self.bundle_release)),
                "static_digest": _tree_content_digest(self.static_release),
            }
            _publish_immutable_file(self.revision_record, _json_bytes(record))
            self._verify_materialized(record)
            return RuntimeHandle(
                backend=self.backend_name,
                handle_id=self.release_id,
                revision_id=revision.revision_id,
                revision_digest=revision.content_digest(),
            )

    materialize = start

    def handle_for(self, revision: Revision) -> RuntimeHandle:
        """Reconstruct the deterministic immutable runtime handle after restart."""

        self._validate_revision(revision)
        return RuntimeHandle(
            backend=self.backend_name,
            handle_id=self.release_id,
            revision_id=revision.revision_id,
            revision_digest=revision.content_digest(),
        )

    def inspect(self, handle: RuntimeHandle) -> ObservedRevision:
        self._validate_handle(handle)
        record = self._read_revision_record()
        actual_static = _tree_content_digest(self.static_release)
        expected_static = record.get("static_digest")
        bundle_matches = _as_digest(tree_digest(self.bundle_release)) == record.get("bundle_digest")
        ready = bundle_matches and actual_static == expected_static and self.caddy_source.is_file()
        workload = self._static_workload()
        observed_workload = ObservedWorkload(
            workload_id=workload.workload_id,
            workload_kind=WorkloadKind.STATIC,
            runtime_state="materialized" if ready else "materialized_mismatch",
            artifact_digest=actual_static,
            ready=ready,
        )
        return ObservedRevision(
            host_id=self.host_id,
            app=self.manifest.app,
            environment=self.manifest.environment or "unknown",
            revision_id=handle.revision_id,
            revision_digest=handle.revision_digest if ready else None,
            state=RevisionState.READY if ready else RevisionState.FAILED,
            workloads=(observed_workload,),
            observed_at=_utc_now(),
        )

    def verify(self, revision: Revision, observed: ObservedRevision) -> VerificationResult:
        expected = revision.content_digest()
        passed = (
            observed.state is RevisionState.READY
            and observed.revision_id == revision.revision_id
            and observed.revision_digest == expected
            and all(workload.ready for workload in observed.workloads)
        )
        state_digest = observed.digest()
        check = VerificationCheck(
            name="materialized_bytes",
            status=CheckStatus.PASSED if passed else CheckStatus.FAILED,
            observed_digest=state_digest,
        )
        return VerificationResult(
            status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
            observed_revision_digest=expected if passed else observed.revision_digest,
            observed_state_digest=state_digest,
            observed_at=observed.observed_at,
            checks=(check,),
        )

    readiness = verify

    def verify_active(
        self, revision: Revision, handle: RuntimeHandle
    ) -> VerificationResult:
        """Verify live traffic pointers and bytes from observed filesystem state."""

        self._validate_revision(revision)
        self._validate_handle(handle)
        checks = []
        try:
            record = self._read_revision_record()
            caddy_ok = (
                not self.caddy_live.is_symlink()
                and self.caddy_live.is_file()
                and _digest_bytes(self.caddy_live.read_bytes())
                == _digest_bytes(self.caddy_source.read_bytes())
            )
            static_ok = (
                self.static_current.is_symlink()
                and self.static_current.readlink() == Path("releases") / self.release_id
                and _tree_content_digest(self.static_current) == record.get("static_digest")
            )
        except (OSError, RuntimeError, ValueError):
            caddy_ok = False
            static_ok = False
        checks.append(
            VerificationCheck(
                name="live_caddy_bytes",
                status=CheckStatus.PASSED if caddy_ok else CheckStatus.FAILED,
                observed_digest=(
                    _digest_bytes(self.caddy_live.read_bytes())
                    if caddy_ok
                    else _digest_text("live-caddy-mismatch")
                ),
            )
        )
        checks.append(
            VerificationCheck(
                name="live_static_pointer",
                status=CheckStatus.PASSED if static_ok else CheckStatus.FAILED,
                observed_digest=(
                    _tree_content_digest(self.static_current)
                    if static_ok
                    else _digest_text("live-static-mismatch")
                ),
            )
        )
        passed = caddy_ok and static_ok and handle.revision_digest == revision.content_digest()
        observed_at = _utc_now()
        state_digest = canonical_digest(
            {"checks": tuple(check.observed_digest for check in checks), "passed": passed}
        )
        return VerificationResult(
            status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
            observed_revision_digest=revision.content_digest() if passed else None,
            observed_state_digest=state_digest,
            observed_at=observed_at,
            checks=tuple(checks),
        )

    def activate(
        self,
        candidate: RuntimeHandle,
        expected_active_revision_digest: Optional[str],
    ) -> TrafficActivationResult:
        self._validate_handle(candidate)
        self._verify_materialized(self._read_revision_record())
        with self._fence():
            if (self.activation_record / "predecessor.json").exists():
                predecessor = self._load_predecessor()
                if self._candidate_is_active(candidate.revision_digest):
                    return self._activation_result(
                        predecessor.revision_digest, candidate.revision_digest
                    )
                self._restore_predecessor(predecessor)
                raise StaticBackendError(
                    "Recovered a partial static activation and restored its predecessor."
                )
            predecessor = self._capture_predecessor()
            if predecessor.revision_digest != expected_active_revision_digest:
                raise StaticBackendError("Active static revision changed before activation.")
            self._persist_predecessor(predecessor)
            try:
                self._fault("before_caddy_switch")
                _atomic_replace_file(self.caddy_live, self.caddy_source.read_bytes())
                self._fault("after_caddy_switch")
                self._fault("before_static_switch")
                _atomic_replace_symlink(
                    self.static_current, Path("releases") / self.release_id
                )
                self._fault("after_static_switch")
                self._assert_active(candidate.revision_digest)
            except BaseException:
                self._restore_predecessor(predecessor)
                raise
            return self._activation_result(predecessor.revision_digest, candidate.revision_digest)

    def restore(
        self,
        previous: Optional[RuntimeHandle],
        failed_candidate: RuntimeHandle,
    ) -> TrafficActivationResult:
        self._validate_handle(failed_candidate)
        with self._fence():
            predecessor_path = self.activation_record / "predecessor.json"
            if not predecessor_path.exists():
                observed = self._capture_predecessor()
                expected_digest = (
                    None if previous is None else previous.revision_digest
                )
                if observed.revision_digest != expected_digest:
                    raise StaticBackendError(
                        "Live static state does not match the expected predecessor."
                    )
                active = expected_digest or _digest_text("static-absent")
                return self._activation_result(
                    failed_candidate.revision_digest,
                    active,
                    previous_revision_digest=failed_candidate.revision_digest,
                )
            predecessor = self._load_predecessor()
            if previous is None:
                if predecessor.revision_digest is not None:
                    raise StaticBackendError("Restore predecessor handle is required.")
            else:
                if previous.revision_digest != predecessor.revision_digest:
                    raise StaticBackendError("Restore predecessor does not match captured state.")
            self._restore_predecessor(predecessor)
            active = predecessor.revision_digest or _digest_text("static-absent")
            return self._activation_result(
                failed_candidate.revision_digest,
                active,
                previous_revision_digest=failed_candidate.revision_digest,
            )

    def deactivate(self, failed_candidate: RuntimeHandle) -> TrafficActivationResult:
        return self.restore(None, failed_candidate)

    def stop(self, handle: RuntimeHandle, grace_seconds: int) -> StopResult:
        observed = self.inspect(handle)
        return StopResult(stopped=False, observed_state_digest=observed.digest())

    def remove(self, handle: RuntimeHandle) -> RemoveResult:
        self._validate_handle(handle)
        return RemoveResult(removed=False, observed_state_digest=self.inspect(handle).digest())

    def logs(self, handle: RuntimeHandle, cursor: Optional[str]) -> LogBatch:
        self._validate_handle(handle)
        return LogBatch(next_cursor=None, entry_digests=(), truncated=False)

    def _validate_revision(self, revision: Revision) -> None:
        if revision.app != self.manifest.app or revision.environment != self.manifest.environment:
            raise ValueError("Revision scope does not match the confirmed candidate.")
        manifest_digest = _digest_bytes(self.manifest_path.read_bytes())
        candidate_digest = _as_digest(self.expected_candidate_digest)
        if revision.manifest_digest != manifest_digest:
            raise ValueError("Revision manifest digest does not match the confirmed lockfile.")
        if revision.artifact_digests != (candidate_digest,):
            raise ValueError("Revision artifacts do not bind the confirmed candidate tree.")
        static = [item for item in revision.workloads if item.workload_kind is WorkloadKind.STATIC]
        if len(static) != 1 or len(static) != len(revision.workloads):
            raise ValueError("Static backend requires exactly one static workload.")
        if static[0].artifact_digest != candidate_digest:
            raise ValueError("Static workload does not bind the confirmed candidate tree.")

    def _static_workload(self):
        # The revision record is authoritative for identity, while the candidate
        # manifest determines the one static workload name used by this backend.
        from ..domain.revisions import Workload
        record = self._read_revision_record()
        digest = str(record["static_digest"])
        return Workload(
            workload_id="static",
            workload_kind=WorkloadKind.STATIC,
            artifact_digest=digest,
        )

    def _validate_existing_release(self, checked: ConfirmedCandidatePreflight) -> None:
        _reject_symlink_parent(self.bundle_release, self.runtime_root)
        _reject_symlink_parent(self.static_release, self.runtime_root)
        _reject_symlink_parent(self.revision_record, self.runtime_root)
        _reject_symlink_parent(self.activation_record, self.runtime_root)
        if self.bundle_release.exists():
            if self.bundle_release.is_symlink() or tree_digest(self.bundle_release) != self.expected_candidate_digest:
                raise ValueError("Immutable bundle release id collides with different content.")
        if self.static_release.exists():
            if (
                self.static_release.is_symlink()
                or checked.static_source is None
                or _tree_content_digest(self.static_release)
                != _tree_content_digest(checked.static_source)
            ):
                raise ValueError("Immutable static release id collides with different content.")

    def _verify_materialized(self, record: dict) -> None:
        if self.bundle_release.is_symlink() or self.static_release.is_symlink():
            raise StaticBackendError("Materialized release must not be a symlink.")
        if _as_digest(tree_digest(self.bundle_release)) != record.get("bundle_digest"):
            raise StaticBackendError("Materialized bundle bytes do not match the candidate.")
        if _tree_content_digest(self.static_release) != record.get("static_digest"):
            raise StaticBackendError("Materialized static bytes do not match the candidate.")
        if not self.caddy_source.is_file() or self.caddy_source.is_symlink():
            raise StaticBackendError("Materialized Caddy input is not a regular file.")

    def _validate_handle(self, handle: RuntimeHandle) -> None:
        if handle.backend != self.backend_name or handle.handle_id != self.release_id:
            raise StaticBackendError("Runtime handle does not belong to this static backend.")

    def _read_revision_record(self) -> dict:
        if self.revision_record.is_symlink():
            raise StaticBackendError("Static revision evidence must not be a symlink.")
        try:
            value = json.loads(self.revision_record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StaticBackendError("Static revision evidence is unavailable.") from exc
        if not isinstance(value, dict):
            raise StaticBackendError("Static revision evidence is malformed.")
        return value

    def _capture_predecessor(self) -> _Predecessor:
        _reject_symlink_parent(self.caddy_live, self.runtime_root)
        _reject_symlink_parent(self.static_current, self.runtime_root)
        if self.caddy_live.is_symlink():
            raise StaticBackendError("Live Caddy site must not be a symlink.")
        if self.static_current.exists() and not self.static_current.is_symlink():
            raise StaticBackendError("Static current pointer must be a symlink.")
        caddy_present = self.caddy_live.exists()
        caddy_digest = _digest_bytes(self.caddy_live.read_bytes()) if caddy_present else None
        target = str(self.static_current.readlink()) if self.static_current.is_symlink() else None
        if target is not None:
            _validate_static_target(Path(target))
        revision_digest = self._revision_digest_for_target(target)
        return _Predecessor(caddy_present, caddy_digest, target, revision_digest)

    def _persist_predecessor(self, predecessor: _Predecessor) -> None:
        root = self.activation_record
        _reject_symlink_parent(root, self.runtime_root)
        if root.exists() and root.is_symlink():
            raise StaticBackendError("Activation evidence path must not be a symlink.")
        root.mkdir(parents=True, exist_ok=True)
        caddy_snapshot = root / "previous.caddy"
        if predecessor.caddy_present:
            _publish_immutable_file(caddy_snapshot, self.caddy_live.read_bytes())
        payload = {
            "schema_version": 1,
            "caddy_present": predecessor.caddy_present,
            "caddy_digest": predecessor.caddy_digest,
            "static_target": predecessor.static_target,
            "revision_digest": predecessor.revision_digest,
        }
        state = root / "predecessor.json"
        if state.exists():
            if state.read_bytes() != _json_bytes(payload):
                raise StaticBackendError("Activation predecessor evidence conflicts.")
        else:
            _publish_immutable_file(state, _json_bytes(payload))

    def _load_predecessor(self) -> _Predecessor:
        try:
            payload = json.loads((self.activation_record / "predecessor.json").read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise StaticBackendError("Activation predecessor evidence is unavailable.") from exc
        return _Predecessor(
            bool(payload["caddy_present"]),
            payload.get("caddy_digest"),
            payload.get("static_target"),
            payload.get("revision_digest"),
        )

    def _restore_predecessor(self, predecessor: _Predecessor) -> None:
        if predecessor.caddy_present:
            snapshot = self.activation_record / "previous.caddy"
            data = snapshot.read_bytes()
            if _digest_bytes(data) != predecessor.caddy_digest:
                raise StaticBackendError("Caddy predecessor evidence does not match its digest.")
            _atomic_replace_file(self.caddy_live, data)
        else:
            _atomic_remove(self.caddy_live)
        if predecessor.static_target is None:
            _atomic_remove(self.static_current)
        else:
            target = Path(predecessor.static_target)
            _validate_static_target(target)
            _atomic_replace_symlink(self.static_current, target)

    def _candidate_is_active(self, revision_digest: str) -> bool:
        try:
            self._assert_active(revision_digest)
        except (OSError, RuntimeError, ValueError):
            return False
        return True

    def _assert_active(self, revision_digest: str) -> None:
        if self.caddy_live.is_symlink() or not self.caddy_live.is_file():
            raise StaticBackendError("Caddy activation verification failed.")
        if _digest_bytes(self.caddy_live.read_bytes()) != _digest_bytes(self.caddy_source.read_bytes()):
            raise StaticBackendError("Caddy activation bytes do not match.")
        if not self.static_current.is_symlink():
            raise StaticBackendError("Static activation pointer is missing.")
        if self.static_current.readlink() != Path("releases") / self.release_id:
            raise StaticBackendError("Static activation pointer does not match.")
        if _tree_content_digest(self.static_current) != _tree_content_digest(self.static_release):
            raise StaticBackendError("Static activation bytes do not match.")
        if revision_digest != self._read_revision_record().get("revision_digest"):
            raise StaticBackendError("Activated revision evidence does not match.")

    def _revision_digest_for_target(self, target: Optional[str]) -> Optional[str]:
        if target is None:
            return None
        release_id = Path(target).name
        record = self.app_root / "static-revisions" / f"{release_id}.json"
        try:
            payload = json.loads(record.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        value = payload.get("revision_digest") if isinstance(payload, dict) else None
        return value if isinstance(value, str) else None

    def _activation_result(
        self,
        old_digest: Optional[str],
        active_digest: str,
        *,
        previous_revision_digest: Optional[str] = None,
    ) -> TrafficActivationResult:
        evidence = (
            _digest_bytes(self.caddy_live.read_bytes()) if self.caddy_live.exists() else _digest_text("caddy-absent"),
            _tree_content_digest(self.static_current) if self.static_current.exists() else _digest_text("static-absent"),
        )
        return TrafficActivationResult(
            committed_atomically=True,
            previous_revision_digest=old_digest if previous_revision_digest is None else previous_revision_digest,
            active_revision_digest=active_digest,
            observed_state_digest=canonical_digest({"caddy": evidence[0], "static": evidence[1]}),
            evidence_digests=tuple(sorted(evidence)),
        )

    def _fence(self) -> RuntimeSideEffectFence:
        return RuntimeSideEffectFence(
            self.runtime_root,
            self.host_id,
            self.manifest.app,
            self.manifest.environment or "unknown",
            self.operation_id,
            self.owner_id,
            self.fencing_token,
        )

    def _fault(self, boundary: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(boundary)


StaticBackend = StaticRuntimeBackend


def _blocker_code(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    if "env" in str(exc).lower():
        return "invalid_env"
    if "support" in str(exc).lower():
        return "invalid_support"
    if "immutable" in str(exc).lower() or "collides" in str(exc).lower():
        return "immutable_collision"
    return f"static_preflight_{name}"[:128]


def _as_digest(value: str) -> str:
    if value.startswith("sha256:") and len(value) == 71:
        return value
    return "sha256:" + value


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest_text(value: str) -> str:
    return _digest_bytes(value.encode("utf-8"))


def _tree_content_digest(path: Path) -> str:
    if path.is_symlink():
        path = path.resolve(strict=True)
    if not path.is_dir():
        return _digest_text("missing")
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        relative = child.relative_to(path).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        if child.is_symlink():
            digest.update(b"symlink\0")
            digest.update(str(child.readlink()).encode())
        elif child.is_file():
            digest.update(b"file\0")
            digest.update(child.read_bytes())
        elif child.is_dir():
            digest.update(b"dir\0")
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _publish_immutable_tree(source: Path, destination: Path) -> None:
    _reject_symlink_parent(destination, destination.parents[3])
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or tree_digest(source) != tree_digest(destination):
            raise StaticBackendError("Immutable release collision.")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.parent / f".{destination.name}.{secrets.token_hex(8)}.tmp"
    try:
        shutil.copytree(source, temp, symlinks=True)
        _fsync_tree(temp)
        try:
            os.rename(temp, destination)
        except FileExistsError:
            if destination.is_symlink() or tree_digest(source) != tree_digest(destination):
                raise StaticBackendError("Immutable release collision.")
        _fsync_directory(destination.parent)
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def _publish_immutable_file(destination: Path, content: bytes) -> None:
    if destination.is_symlink():
        raise StaticBackendError("Immutable evidence destination must not be a symlink.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != content:
            raise StaticBackendError("Immutable evidence collision.")
        return
    temp = destination.parent / f".{destination.name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp, destination, follow_symlinks=False)
        except FileExistsError:
            if destination.is_symlink() or destination.read_bytes() != content:
                raise StaticBackendError("Immutable evidence collision.")
        os.unlink(temp)
        _fsync_directory(destination.parent)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _atomic_replace_file(destination: Path, content: bytes) -> None:
    _reject_symlink_parent(destination, destination.parents[2])
    if destination.is_symlink():
        raise StaticBackendError("Managed live file must not be a symlink.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.parent / f".{destination.name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, destination)
        _fsync_directory(destination.parent)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _atomic_replace_symlink(destination: Path, target: Path) -> None:
    _validate_static_target(target)
    _reject_symlink_parent(destination, destination.parents[1])
    if destination.exists() and not destination.is_symlink():
        raise StaticBackendError("Managed static pointer must be a symlink.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.parent / f".{destination.name}.{secrets.token_hex(8)}.tmp"
    try:
        temp.symlink_to(target, target_is_directory=True)
        os.replace(temp, destination)
        _fsync_directory(destination.parent)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _validate_static_target(target: Path) -> None:
    if (
        target.is_absolute()
        or len(target.parts) != 2
        or target.parts[0] != "releases"
        or target.parts[1] in {"", ".", ".."}
        or "/" in target.parts[1]
        or "\\" in target.parts[1]
    ):
        raise StaticBackendError("Static pointer target is outside immutable releases.")


def _atomic_remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        _fsync_directory(path.parent)
    elif path.exists():
        raise StaticBackendError("Managed live path has an unsafe type.")


def _reject_symlink_parent(path: Path, trusted_root: Path) -> None:
    try:
        relative = path.relative_to(trusted_root)
    except ValueError as exc:
        raise StaticBackendError("Managed path escapes the runtime root.") from exc
    current = trusted_root
    if current.is_symlink() or not current.is_dir():
        raise StaticBackendError("Runtime root must be a real directory.")
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise StaticBackendError("Managed path parent must not be a symlink.")
        if current.exists() and not current.is_dir():
            raise StaticBackendError("Managed path parent must be a directory.")


def _fsync_tree(root: Path) -> None:
    for child in root.rglob("*"):
        if child.is_file() and not child.is_symlink():
            fd = os.open(child, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    for directory in sorted(
        (item for item in root.rglob("*") if item.is_dir() and not item.is_symlink()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        _fsync_directory(directory)
    _fsync_directory(root)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
