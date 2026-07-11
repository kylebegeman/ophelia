"""Immutable revisions, workload semantics, and observed runtime state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional, Tuple

from ._contracts import (
    Contract,
    ContractValidationError,
    canonical_digest,
    optional_entity_id,
    require_digest,
    require_digests,
    require_entity_id,
    require_enum,
    require_slug,
    require_text,
    require_utc,
)


class WorkloadKind(str, Enum):
    WEB = "web"
    INTERNAL = "internal"
    WORKER = "worker"
    CRON = "cron"
    TASK = "task"
    MIGRATION = "migration"
    STATIC = "static"

    @property
    def activation_semantics(self) -> str:
        return {
            WorkloadKind.WEB: "blue_green",
            WorkloadKind.INTERNAL: "serial_replace",
            WorkloadKind.WORKER: "fenced_handoff",
            WorkloadKind.CRON: "fenced_singleton",
            WorkloadKind.TASK: "idempotent_once",
            WorkloadKind.MIGRATION: "exactly_once",
            WorkloadKind.STATIC: "atomic_pointer",
        }[self]

    @property
    def long_running(self) -> bool:
        return self in {
            WorkloadKind.WEB,
            WorkloadKind.INTERNAL,
            WorkloadKind.WORKER,
            WorkloadKind.CRON,
        }

    @property
    def receives_traffic(self) -> bool:
        return self in {WorkloadKind.WEB, WorkloadKind.STATIC}

    @property
    def overlap_allowed_by_default(self) -> bool:
        return self in {WorkloadKind.WEB, WorkloadKind.STATIC}

    @property
    def requires_idempotency_key(self) -> bool:
        return self in {WorkloadKind.TASK, WorkloadKind.MIGRATION}


class RevisionState(str, Enum):
    CREATED = "created"
    STAGED = "staged"
    PREFLIGHT_PASSED = "preflight_passed"
    STARTING = "starting"
    READY = "ready"
    TRAFFIC_CANDIDATE = "traffic_candidate"
    ACTIVE = "active"
    DRAINING = "draining"
    INACTIVE = "inactive"
    GARBAGE_COLLECTABLE = "garbage_collectable"
    ROLLBACK_STARTING = "rollback_starting"
    FAILED = "failed"


@dataclass(frozen=True)
class Workload(Contract):
    kind = "ophelia.kernel.workload"

    workload_id: str
    workload_kind: WorkloadKind
    artifact_digest: str
    route_ids: Tuple[str, ...] = ()
    overlap_safe: bool = False

    def __post_init__(self) -> None:
        require_slug(self.workload_id, "workload_id")
        require_enum(self.workload_kind, WorkloadKind, "workload_kind")
        require_digest(self.artifact_digest, "artifact_digest")
        if not isinstance(self.route_ids, tuple):
            raise ContractValidationError("route_ids must be an immutable tuple.")
        for route_id in self.route_ids:
            require_slug(route_id, "route_id")
        if self.route_ids != tuple(sorted(set(self.route_ids))):
            raise ContractValidationError("route_ids must be sorted and contain no duplicates.")
        if self.route_ids and not self.workload_kind.receives_traffic:
            raise ContractValidationError("Only web and static workloads may declare routes.")
        if self.overlap_safe and self.workload_kind not in {
            WorkloadKind.WEB,
            WorkloadKind.WORKER,
            WorkloadKind.STATIC,
        }:
            raise ContractValidationError("This workload kind cannot opt into revision overlap.")

    @property
    def effective_overlap_allowed(self) -> bool:
        return self.workload_kind.overlap_allowed_by_default or self.overlap_safe


@dataclass(frozen=True)
class Revision(Contract):
    kind = "ophelia.kernel.revision"

    revision_id: str
    app: str
    environment: str
    manifest_digest: str
    artifact_digests: Tuple[str, ...]
    renderer_version: str
    workloads: Tuple[Workload, ...]
    created_at: str

    def __post_init__(self) -> None:
        require_entity_id(self.revision_id, "rev", "revision_id")
        require_slug(self.app, "app")
        require_slug(self.environment, "environment")
        require_digest(self.manifest_digest, "manifest_digest")
        require_digests(self.artifact_digests, "artifact_digests")
        require_text(self.renderer_version, "renderer_version", 128)
        require_utc(self.created_at, "created_at")
        if not isinstance(self.workloads, tuple) or not self.workloads:
            raise ContractValidationError("workloads must be a non-empty immutable tuple.")
        identifiers = tuple(item.workload_id for item in self.workloads)
        if identifiers != tuple(sorted(set(identifiers))):
            raise ContractValidationError("workloads must be sorted by unique workload_id.")
        artifact_set = set(self.artifact_digests)
        missing_artifacts = sorted(
            {
                workload.artifact_digest
                for workload in self.workloads
                if workload.artifact_digest not in artifact_set
            }
        )
        if missing_artifacts:
            raise ContractValidationError(
                "Every workload artifact_digest must appear in revision artifact_digests."
            )

    def content_digest(self) -> str:
        payload = self.to_dict()
        payload.pop("revision_id")
        payload.pop("created_at")
        return canonical_digest(payload)

    @classmethod
    def create(
        cls,
        *,
        app: str,
        environment: str,
        manifest_digest: str,
        artifact_digests: Tuple[str, ...],
        renderer_version: str,
        workloads: Tuple[Workload, ...],
        created_at: str,
    ) -> "Revision":
        provisional = cls(
            revision_id="rev_pending",
            app=app,
            environment=environment,
            manifest_digest=manifest_digest,
            artifact_digests=artifact_digests,
            renderer_version=renderer_version,
            workloads=workloads,
            created_at=created_at,
        )
        return replace(provisional, revision_id="rev_" + provisional.content_digest()[7:31])


@dataclass(frozen=True)
class ObservedWorkload(Contract):
    kind = "ophelia.kernel.observed_workload"

    workload_id: str
    workload_kind: WorkloadKind
    runtime_state: str
    artifact_digest: str
    ready: bool
    fencing_token_digest: Optional[str] = None

    def __post_init__(self) -> None:
        require_slug(self.workload_id, "workload_id")
        require_enum(self.workload_kind, WorkloadKind, "workload_kind")
        require_text(self.runtime_state, "runtime_state", 128)
        require_digest(self.artifact_digest, "artifact_digest")
        if self.fencing_token_digest is not None:
            require_digest(self.fencing_token_digest, "fencing_token_digest")


@dataclass(frozen=True)
class ObservedRevision(Contract):
    kind = "ophelia.kernel.observed_revision"

    host_id: str
    app: str
    environment: str
    revision_id: Optional[str]
    revision_digest: Optional[str]
    state: RevisionState
    workloads: Tuple[ObservedWorkload, ...]
    observed_at: str

    def __post_init__(self) -> None:
        require_entity_id(self.host_id, "host", "host_id")
        require_slug(self.app, "app")
        require_slug(self.environment, "environment")
        optional_entity_id(self.revision_id, "rev", "revision_id")
        if self.revision_digest is not None:
            require_digest(self.revision_digest, "revision_digest")
        require_utc(self.observed_at, "observed_at")
        require_enum(self.state, RevisionState, "state")
        if not isinstance(self.workloads, tuple):
            raise ContractValidationError("workloads must be an immutable tuple.")
        identifiers = tuple(item.workload_id for item in self.workloads)
        if identifiers != tuple(sorted(set(identifiers))):
            raise ContractValidationError("observed workloads must be sorted by unique workload_id.")
