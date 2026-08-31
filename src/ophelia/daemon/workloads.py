"""Revision-bound task and cron acceptance and execution."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

from ..domain import Actor, WorkloadKind, canonical_digest
from ..execution.subprocesses import ProcessResult, SubprocessRunner
from ..manifest_v2 import load_manifest_v2
from ..validation import CanonicalValidationError, parse_environment, parse_identifier
from .store import DaemonStore, WorkloadRun


class WorkloadExecutionError(RuntimeError):
    pass


class WorkloadRunManager:
    def __init__(
        self,
        *,
        runtime_root: Path,
        host_id: str,
        store: DaemonStore,
        runner: Optional[SubprocessRunner] = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.host_id = host_id
        self.store = store
        self.runner = runner or SubprocessRunner()

    def accept(
        self,
        *,
        actor: Actor,
        app: str,
        environment: str,
        workload_name: str,
        trigger_kind: str,
        idempotency_key: str,
        expected_revision_digest: Optional[str] = None,
    ) -> WorkloadRun:
        active, manifest = self._active_manifest(app, environment)
        if (
            expected_revision_digest is not None
            and active["revision_digest"] != expected_revision_digest
        ):
            raise WorkloadExecutionError("Scheduled workload belongs to a superseded revision.")
        workload = manifest.workload(workload_name)
        expected = WorkloadKind.CRON if trigger_kind == "cron" else WorkloadKind.TASK
        if workload.kind is not expected:
            raise WorkloadExecutionError(
                "%s is not a %s workload." % (workload_name, expected.value)
            )
        concurrency = workload.concurrency_policy if expected is WorkloadKind.CRON else "forbid"
        return self.store.accept_run(
            actor=actor,
            host_id=self.host_id,
            app=app,
            environment=environment,
            revision_id=active["revision_id"],
            revision_digest=active["revision_digest"],
            workload_name=workload.name,
            workload_kind=workload.kind.value,
            trigger_kind=trigger_kind,
            idempotency_key=idempotency_key,
            concurrency_policy=concurrency,
        )

    def execute(self, run: WorkloadRun, *, owner_id: str) -> WorkloadRun:
        root, project, service = self._runtime(run)
        container_name = _container_name(run.run_id)
        lease_lost = False
        next_lease_renewal = time.monotonic() + 60
        next_cancellation_check = 0.0
        cancellation = False

        def should_stop() -> bool:
            nonlocal cancellation, lease_lost, next_cancellation_check, next_lease_renewal
            now = time.monotonic()
            if now >= next_lease_renewal:
                lease_lost = not self.store.renew_run_lease(
                    run.run_id,
                    owner_id=owner_id,
                    fencing_token=run.fencing_token,
                )
                next_lease_renewal = now + 60
            if not lease_lost and now >= next_cancellation_check:
                cancellation = self.store.cancellation_requested(run.run_id)
                next_cancellation_check = now + 1
            return lease_lost or cancellation

        inspected = self.runner.run(
            [
                "docker",
                "inspect",
                container_name,
                "--format",
                "{{.State.Status}}:{{.State.ExitCode}}",
            ],
            timeout_seconds=30,
            check=False,
        )
        if inspected.exit_reason == "success":
            status = inspected.stdout.strip()
            if status.startswith("exited:"):
                try:
                    exit_code = int(status.split(":", 1)[1])
                except ValueError as exc:
                    raise WorkloadExecutionError("Retained workload container state is malformed.") from exc
                result = ProcessResult(
                    argv=("docker", "inspect", container_name),
                    exit_code=exit_code,
                    exit_reason="success" if exit_code == 0 else "nonzero_exit",
                    stdout="",
                    stderr="",
                    stdout_truncated=False,
                    stderr_truncated=False,
                    duration_ms=1,
                )
            elif status.startswith("running:"):
                result = self.runner.run(
                    ["docker", "wait", container_name],
                    timeout_seconds=86400,
                    cancellation_requested=should_stop,
                    check=False,
                )
                if result.exit_reason == "success":
                    try:
                        exit_code = int(result.stdout.strip())
                    except ValueError as exc:
                        raise WorkloadExecutionError("Docker wait returned an invalid exit code.") from exc
                    result = ProcessResult(
                        argv=result.argv,
                        exit_code=exit_code,
                        exit_reason="success" if exit_code == 0 else "nonzero_exit",
                        stdout=result.stdout,
                        stderr=result.stderr,
                        stdout_truncated=result.stdout_truncated,
                        stderr_truncated=result.stderr_truncated,
                        duration_ms=result.duration_ms,
                    )
            else:
                raise WorkloadExecutionError("Retained workload container is not recoverable.")
        else:
            result = self.runner.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    project,
                    "-f",
                    str(root / "compose.yml"),
                    "run",
                    "--name",
                    container_name,
                    "--no-deps",
                    service,
                ],
                timeout_seconds=86400,
                cancellation_requested=should_stop,
                check=False,
            )
        if lease_lost:
            raise WorkloadExecutionError("Workload-run execution fence was superseded.")
        if result.exit_reason == "cancelled" and cancellation:
            self.runner.run(
                ["docker", "stop", "--time", "10", container_name],
                timeout_seconds=30,
                check=False,
            )
        digest = canonical_digest(
            {
                "argv": list(result.argv),
                "exit_code": result.exit_code,
                "exit_reason": result.exit_reason,
                "stdout_sha256": hashlib.sha256(result.stdout.encode("utf-8")).hexdigest(),
                "stderr_sha256": hashlib.sha256(result.stderr.encode("utf-8")).hexdigest(),
                "stdout_truncated": result.stdout_truncated,
                "stderr_truncated": result.stderr_truncated,
            }
        )
        state = (
            "cancelled"
            if result.exit_reason == "cancelled"
            else ("succeeded" if result.exit_reason == "success" and result.exit_code == 0 else "failed")
        )
        return self.store.complete_run(
            run.run_id,
            owner_id=owner_id,
            fencing_token=run.fencing_token,
            state=state,
            result_digest=digest,
            exit_code=result.exit_code,
            exit_reason=result.exit_reason,
        )

    def _active_manifest(self, app: str, environment: str):
        scope = self._scope(app, environment)
        active_path = scope / "traffic" / "active.json"
        if active_path.is_symlink() or not active_path.is_file():
            raise WorkloadExecutionError("Application has no trusted active revision.")
        try:
            active = json.loads(active_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WorkloadExecutionError("Active revision evidence is malformed.") from exc
        if not isinstance(active, dict):
            raise WorkloadExecutionError("Active revision evidence is malformed.")
        revision_root = scope / "revisions" / str(active.get("revision_id", ""))
        manifest = load_manifest_v2(revision_root / "manifest.lock.json")
        revision = json.loads((revision_root / "revision.json").read_text(encoding="utf-8"))
        if (
            active.get("host_id") != self.host_id
            or active.get("app") != app
            or active.get("environment") != environment
            or revision.get("revision_id") != active.get("revision_id")
            or revision.get("revision_digest") != active.get("revision_digest")
            or manifest.app != app
            or manifest.environment != environment
        ):
            raise WorkloadExecutionError("Active revision evidence does not reconcile.")
        return active, manifest

    def _runtime(self, run: WorkloadRun):
        root = self._scope(run.app, run.environment) / "revisions" / run.revision_id
        if root.is_symlink() or not root.is_dir():
            raise WorkloadExecutionError("Workload revision runtime is unavailable.")
        try:
            manifest = load_manifest_v2(root / "manifest.lock.json")
            revision = json.loads((root / "revision.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WorkloadExecutionError("Workload revision runtime is malformed.") from exc
        workload = manifest.workload(run.workload_name)
        if (
            manifest.app != run.app
            or manifest.environment != run.environment
            or revision.get("revision_id") != run.revision_id
            or revision.get("revision_digest") != run.revision_digest
            or workload.kind.value != run.workload_kind
        ):
            raise WorkloadExecutionError("Workload run no longer matches its retained revision.")
        project = revision.get("compose_project")
        if not isinstance(project, str) or not project:
            raise WorkloadExecutionError("Workload Compose project metadata is missing.")
        return root, project, workload.name

    def _scope(self, app: str, environment: str) -> Path:
        try:
            parsed_app = str(parse_identifier(app, field="app"))
            parsed_environment = parse_environment(environment, field="environment")
        except CanonicalValidationError as exc:
            raise WorkloadExecutionError("Application scope is invalid.") from exc
        if parsed_environment is None:
            raise WorkloadExecutionError("Application environment is required.")
        return (
            self.runtime_root
            / "apps"
            / parsed_app
            / "environments"
            / parsed_environment.value
        )


def _container_name(run_id: str) -> str:
    return ("ophelia-" + run_id.lower().replace("_", "-"))[:128]
