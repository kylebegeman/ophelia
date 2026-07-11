from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.domain.revisions import Revision, Workload, WorkloadKind
from ophelia.execution.runtime_fence import RuntimeFenceRejected
from ophelia.execution.staging import find_confirmed_staging
from ophelia.execution.static_backend import StaticBackendError, StaticRuntimeBackend
from ophelia.manifest import load_manifest
from ophelia.planning import deploy_plan
from ophelia.runtime import DeployMetadata


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


class StaticBackendTests(unittest.TestCase):
    def test_invalid_env_preflight_has_no_live_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend, revision = _backend(root, app="invalid-env", required_env=True)
            result = backend.preflight(revision)
            self.assertFalse(result.ok)
            self.assertEqual(("invalid_env",), result.blocker_codes)
            self.assertFalse((root / "runtime" / "apps" / "invalid-env").exists())
            self.assertFalse((root / "runtime" / "caddy").exists())
            self.assertFalse((root / "runtime" / "static").exists())

    def test_preflight_rejects_revision_not_bound_to_confirmed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            backend, revision = _backend(Path(temp))
            wrong_manifest = replace(revision, manifest_digest=_digest(b"wrong-manifest"))
            self.assertFalse(backend.preflight(wrong_manifest).ok)

            wrong_artifact = _digest(b"wrong-candidate")
            wrong_workload = replace(revision.workloads[0], artifact_digest=wrong_artifact)
            wrong_candidate = replace(
                revision,
                artifact_digests=(wrong_artifact,),
                workloads=(wrong_workload,),
            )
            self.assertFalse(backend.preflight(wrong_candidate).ok)
            self.assertFalse(backend.app_root.exists())

    def test_materializes_exact_immutable_bytes_without_activation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend, revision = _backend(root)
            handle = backend.start(revision)
            self.assertEqual(
                (backend.candidate_root / "public" / "index.html").read_bytes(),
                (backend.bundle_release / "public" / "index.html").read_bytes(),
            )
            self.assertEqual(b"reviewed\n", (backend.static_release / "index.html").read_bytes())
            self.assertFalse(backend.caddy_live.exists())
            self.assertFalse(backend.static_current.exists())
            self.assertFalse((backend.app_root / "active_release.json").exists())
            self.assertTrue(backend.inspect(handle).workloads[0].ready)
            self.assertTrue(backend.verify(revision, backend.inspect(handle)).status.value == "passed")

            backend.start(revision)
            (backend.bundle_release / "public" / "index.html").write_text("collision\n")
            result = backend.preflight(revision)
            self.assertFalse(result.ok)
            self.assertEqual(("immutable_collision",), result.blocker_codes)

    def test_readiness_is_derived_from_observed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            backend, revision = _backend(Path(temp))
            handle = backend.start(revision)
            (backend.static_release / "index.html").write_text("tampered\n")
            observed = backend.inspect(handle)
            self.assertFalse(observed.workloads[0].ready)
            self.assertEqual("failed", observed.state.value)
            self.assertEqual("failed", backend.verify(revision, observed).status.value)

    def test_atomic_activate_and_first_deploy_deactivate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            backend, revision = _backend(Path(temp))
            handle = backend.start(revision)
            activated = backend.activate(handle, None)
            self.assertEqual(revision.content_digest(), activated.active_revision_digest)
            self.assertEqual(
                backend.caddy_source.read_bytes(), backend.caddy_live.read_bytes()
            )
            self.assertEqual(
                Path("releases") / backend.release_id, backend.static_current.readlink()
            )

            backend.deactivate(handle)
            self.assertFalse(backend.caddy_live.exists())
            self.assertFalse(backend.static_current.exists())

    def test_activate_retry_after_success_is_idempotent_and_live_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            backend, revision = _backend(Path(temp))
            handle = backend.start(revision)
            first = backend.activate(handle, None)
            retry = backend.activate(backend.handle_for(revision), None)
            self.assertEqual(first.previous_revision_digest, retry.previous_revision_digest)
            self.assertEqual(first.active_revision_digest, retry.active_revision_digest)
            self.assertEqual(first.observed_state_digest, retry.observed_state_digest)
            self.assertEqual(
                "passed", backend.verify_active(revision, handle).status.value
            )
            backend.caddy_live.write_text("tampered\n")
            self.assertEqual(
                "failed", backend.verify_active(revision, handle).status.value
            )

    def test_restore_reinstates_predecessor_caddy_and_static_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, first_revision = _backend(root, release_id="release-one", content="first\n")
            first_handle = first.start(first_revision)
            first.activate(first_handle, None)
            first_caddy = first.caddy_live.read_bytes()
            first_target = first.static_current.readlink()

            second, second_revision = _backend(
                root,
                release_id="release-two",
                content="second\n",
                operation_id="operation_two",
                token=2,
            )
            second_handle = second.start(second_revision)
            second.activate(second_handle, first_revision.content_digest())
            self.assertEqual(b"second\n", (second.static_current / "index.html").read_bytes())

            second.restore(first_handle, second_handle)
            self.assertEqual(first_caddy, second.caddy_live.read_bytes())
            self.assertEqual(first_target, second.static_current.readlink())
            self.assertEqual(b"first\n", (second.static_current / "index.html").read_bytes())

    def test_switch_boundary_failures_restore_absence(self) -> None:
        for boundary in (
            "before_caddy_switch",
            "after_caddy_switch",
            "before_static_switch",
            "after_static_switch",
        ):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temp:
                def fail(at: str) -> None:
                    if at == boundary:
                        raise RuntimeError("injected")

                backend, revision = _backend(Path(temp), fault_injector=fail)
                handle = backend.start(revision)
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    backend.activate(handle, None)
                self.assertFalse(backend.caddy_live.exists())
                self.assertFalse(backend.static_current.exists())

    def test_stale_fence_cannot_materialize_even_identical_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend, revision = _backend(root, token=2)
            backend.start(revision)
            stale, stale_revision = _backend_from_existing(
                backend, token=1, operation_id="operation_stale"
            )
            with self.assertRaises(RuntimeFenceRejected):
                stale.start(stale_revision)

    def test_stale_fence_cannot_change_live_pointers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend, revision = _backend(root, token=2)
            handle = backend.start(revision)
            backend.activate(handle, None)
            caddy = backend.caddy_live.read_bytes()
            target = backend.static_current.readlink()

            stale, _ = _backend_from_existing(backend, token=1, operation_id="operation_stale")
            with self.assertRaises(RuntimeFenceRejected):
                stale.activate(handle, revision.content_digest())
            self.assertEqual(caddy, backend.caddy_live.read_bytes())
            self.assertEqual(target, backend.static_current.readlink())

    def test_symlink_attack_is_rejected_before_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend, revision = _backend(root)
            handle = backend.start(revision)
            outside = root / "outside"
            outside.mkdir()
            backend.caddy_live.parent.mkdir(parents=True)
            backend.caddy_live.symlink_to(outside / "site")
            with self.assertRaises(StaticBackendError):
                backend.activate(handle, None)
            self.assertTrue(backend.caddy_live.is_symlink())
            self.assertFalse(backend.static_current.exists())


def _backend(
    root: Path,
    *,
    app: str = "static-app",
    required_env: bool = False,
    token: int = 1,
    fault_injector=None,
    release_id: str = "release-one",
    content: str = "reviewed\n",
    operation_id: str = "operation_one",
):
    runtime_root = root / "runtime"
    public = root / "public"
    public.mkdir(exist_ok=True)
    (public / "index.html").write_text(content)
    required = "\nrequired_env:\n  - API_TOKEN" if required_env else ""
    manifest_path = root / "app.ophelia.yml"
    manifest_path.write_text(
        f"""version: 1
app: {app}
environment: production
kind: static
static_root: public{required}
routes:
  - domain: {app}.example.com
"""
    )
    metadata = DeployMetadata(
        release_id=release_id,
        commit_sha="abc123",
        build_time="2026-07-11T09:00:00+00:00",
    )
    plan = deploy_plan(
        load_manifest(manifest_path), manifest_path, runtime_root, deploy_metadata=metadata
    )
    confirmed = find_confirmed_staging(
        runtime_root, app, str(plan["confirmation_token"])
    )
    locked = DeployMetadata(**confirmed.binding["deploy_metadata"], locked=True)
    revision = Revision.create(
        app=app,
        environment="production",
        manifest_digest=_digest(confirmed.manifest_path.read_bytes()),
        artifact_digests=("sha256:" + confirmed.candidate_digest,),
        renderer_version="test",
        workloads=(
            Workload(
                workload_id="static",
                workload_kind=WorkloadKind.STATIC,
                artifact_digest="sha256:" + confirmed.candidate_digest,
            ),
        ),
        created_at="2026-07-11T09:00:00Z",
    )
    backend = StaticRuntimeBackend(
        manifest=load_manifest(confirmed.manifest_path),
        manifest_path=confirmed.manifest_path,
        candidate_root=confirmed.staging.candidate,
        generated_files=list(confirmed.generated_files),
        runtime_root=runtime_root,
        deploy_metadata=locked,
        expected_candidate_digest=confirmed.candidate_digest,
        expected_bundle_hash=confirmed.rendered_bundle_hash,
        expected_baseline_digest=confirmed.baseline_digest,
        host_id="host_test",
        operation_id=operation_id,
        owner_id="worker_one",
        fencing_token=token,
        fault_injector=fault_injector,
    )
    return backend, revision


def _backend_from_existing(
    original: StaticRuntimeBackend, *, token: int, operation_id: str
):
    backend = StaticRuntimeBackend(
        manifest=original.manifest,
        manifest_path=original.manifest_path,
        candidate_root=original.candidate_root,
        generated_files=original.generated_files,
        runtime_root=original.runtime_root,
        deploy_metadata=original.deploy_metadata,
        expected_candidate_digest=original.expected_candidate_digest,
        expected_bundle_hash=original.expected_bundle_hash,
        expected_baseline_digest=original.expected_baseline_digest,
        host_id=original.host_id,
        operation_id=operation_id,
        owner_id="worker_stale",
        fencing_token=token,
    )
    revision = Revision.create(
        app=original.manifest.app,
        environment="production",
        manifest_digest=_digest(original.manifest_path.read_bytes()),
        artifact_digests=("sha256:" + original.expected_candidate_digest,),
        renderer_version="test",
        workloads=(
            Workload(
                workload_id="static",
                workload_kind=WorkloadKind.STATIC,
                artifact_digest="sha256:" + original.expected_candidate_digest,
            ),
        ),
        created_at="2026-07-11T09:00:00Z",
    )
    return backend, revision


if __name__ == "__main__":
    unittest.main()
