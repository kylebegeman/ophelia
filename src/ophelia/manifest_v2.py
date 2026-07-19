"""Strict Ophelia manifest v2 models and canonical kernel compilation.

Manifest v1 remains a compatibility contract. Version 2 is deliberately
strict: unknown fields fail validation, production container artifacts are
digest pinned, and workload lifecycle semantics are explicit before a plan can
be calculated.
"""

from __future__ import annotations

import hashlib
import re
import stat
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .domain import Revision, Workload, WorkloadKind, canonical_digest
from .cron import CronExpressionError, validate_cron_expression
from .validation import CanonicalValidationError, parse_domain, parse_environment, parse_identifier


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PINNED_IMAGE = re.compile(r"^\S+@(?P<digest>sha256:[0-9a-f]{64})$")
_SECRET_REF = re.compile(r"^secret://[a-z][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)+$")
_RESOURCE_MEMORY = re.compile(r"^[1-9][0-9]*(?:Ki|Mi|Gi|Ti|k|m|g|t|[KMGTP]i?B?)$")
_CPU = re.compile(r"^(?:[1-9][0-9]*(?:\.[0-9]+)?|0\.[0-9]*[1-9][0-9]*)$")
_CRON_FIELD = re.compile(r"^[0-9*/?,\-]+$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_MOUNT_TARGET = re.compile(r"^/(?!.*(?:^|/)\.\.(?:/|$))[^\x00\r\n]*$")


class ManifestV2Error(ValueError):
    """A manifest v2 document is invalid or unsafe."""


@dataclass(frozen=True)
class ArtifactV2:
    name: str
    image: Optional[str] = None
    static_root: Optional[str] = None
    digest: str = ""


@dataclass(frozen=True)
class HttpProbeV2:
    path: str
    port: int
    method: str = "GET"
    expect_status: int = 200


@dataclass(frozen=True)
class ProbeV2:
    http: Optional[HttpProbeV2] = None
    command: Tuple[str, ...] = ()
    interval_seconds: float = 2.0
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class ResourceV2:
    memory: str = "256Mi"
    cpu: str = "1.0"
    pids: int = 256


@dataclass(frozen=True)
class SecurityV2:
    run_as_non_root: bool = True
    read_only_root: bool = True
    no_new_privileges: bool = True
    drop_capabilities: Tuple[str, ...] = ("ALL",)


@dataclass(frozen=True)
class WorkloadUpdateV2:
    overlap: str = "forbid"


@dataclass(frozen=True)
class MountV2:
    source: str
    target: str
    read_only: bool = True


@dataclass(frozen=True)
class WorkloadV2:
    name: str
    kind: WorkloadKind
    artifact: str
    command: Tuple[str, ...] = ()
    port: Optional[int] = None
    replicas: int = 1
    schedule: Optional[str] = None
    concurrency_policy: str = "forbid"
    env: Tuple[Tuple[str, str], ...] = ()
    env_files: Tuple[str, ...] = ()
    mounts: Tuple[MountV2, ...] = ()
    networks: Tuple[str, ...] = ("app",)
    startup: Optional[ProbeV2] = None
    readiness: Optional[ProbeV2] = None
    liveness: Optional[ProbeV2] = None
    resources: ResourceV2 = field(default_factory=ResourceV2)
    security: SecurityV2 = field(default_factory=SecurityV2)
    shutdown_grace_seconds: int = 30
    update: WorkloadUpdateV2 = field(default_factory=WorkloadUpdateV2)
    route_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class MigrationV2:
    name: str
    workload: WorkloadV2
    compatibility: str = "backward_compatible"
    timeout_seconds: int = 300
    backup_required: bool = False


@dataclass(frozen=True)
class RouteTargetV2:
    workload: str
    port: Optional[int] = None


@dataclass(frozen=True)
class RouteV2:
    name: str
    domain: str
    target: RouteTargetV2
    path_prefix: Optional[str] = None


@dataclass(frozen=True)
class UpdateV2:
    strategy: str
    auto_rollback: bool = True
    drain_seconds: int = 30


@dataclass(frozen=True)
class SecretV2:
    name: str
    ref: str
    workloads: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ManifestV2:
    version: int
    app: str
    environment: str
    artifacts: Tuple[ArtifactV2, ...]
    workloads: Tuple[WorkloadV2, ...]
    migrations: Tuple[MigrationV2, ...]
    routes: Tuple[RouteV2, ...]
    update: UpdateV2
    secrets: Tuple[SecretV2, ...] = ()

    def artifact(self, name: str) -> ArtifactV2:
        for item in self.artifacts:
            if item.name == name:
                return item
        raise KeyError(name)

    def workload(self, name: str) -> WorkloadV2:
        for item in self.workloads:
            if item.name == name:
                return item
        raise KeyError(name)

    def to_lock_dict(self) -> Dict[str, Any]:
        return {
            "version": 2,
            "app": self.app,
            "environment": self.environment,
            "artifacts": {
                item.name: _without_none(
                    {"image": item.image, "static_root": item.static_root}
                )
                for item in self.artifacts
            },
            "workloads": {
                item.name: _workload_wire(item)
                for item in self.workloads
            },
            "migrations": {
                item.name: {
                    "workload": _workload_wire(item.workload),
                    "compatibility": item.compatibility,
                    "timeout_seconds": item.timeout_seconds,
                    "backup_required": item.backup_required,
                }
                for item in self.migrations
            },
            "routes": [
                _without_none(
                    {
                        "name": item.name,
                        "domain": item.domain,
                        "target": _without_none(
                            {
                                "workload": item.target.workload,
                                "port": item.target.port,
                            }
                        ),
                        "path_prefix": item.path_prefix,
                    }
                )
                for item in self.routes
            ],
            "update": asdict(self.update),
            "secrets": [
                {
                    "name": item.name,
                    "ref": item.ref,
                    "workloads": list(item.workloads),
                }
                for item in self.secrets
            ],
        }

    def canonical_digest(self) -> str:
        return canonical_digest(self.to_lock_dict())

    def to_revision(self, *, created_at: str, renderer_version: str = "manifest-v2") -> Revision:
        workload_values = [
            Workload(
                workload_id=item.name,
                workload_kind=item.kind,
                artifact_digest=self.artifact(item.artifact).digest,
                route_ids=item.route_ids,
                overlap_safe=(
                    item.kind is WorkloadKind.WORKER and item.update.overlap == "allow"
                ),
            )
            for item in self.workloads
        ]
        workload_values.extend(
            Workload(
                workload_id="migration-" + item.name,
                workload_kind=WorkloadKind.MIGRATION,
                artifact_digest=self.artifact(item.workload.artifact).digest,
            )
            for item in self.migrations
        )
        artifact_digests = tuple(
            sorted({self.artifact(item.artifact).digest for item in self.workloads}.union(
                self.artifact(item.workload.artifact).digest for item in self.migrations
            ))
        )
        return Revision.create(
            app=self.app,
            environment=self.environment,
            manifest_digest=self.canonical_digest(),
            artifact_digests=artifact_digests,
            renderer_version=renderer_version,
            workloads=tuple(sorted(workload_values, key=lambda item: item.workload_id)),
            created_at=created_at,
        )


_TOP_LEVEL = {
    "version", "app", "environment", "artifacts", "workloads", "migrations",
    "routes", "update", "secrets",
}
_ARTIFACT_KEYS = {"image", "static_root"}
_WORKLOAD_KEYS = {
    "kind", "artifact", "command", "port", "replicas", "schedule",
    "concurrency_policy", "env", "env_files", "mounts", "networks", "startup",
    "readiness", "liveness", "resources", "security", "shutdown_grace_seconds",
    "update",
}
_PROBE_KEYS = {"http", "command", "interval_seconds", "timeout_seconds"}
_HTTP_PROBE_KEYS = {"path", "port", "method", "expect_status"}
_RESOURCE_KEYS = {"memory", "cpu", "pids"}
_SECURITY_KEYS = {
    "run_as_non_root", "read_only_root", "no_new_privileges", "drop_capabilities",
}
_WORKLOAD_UPDATE_KEYS = {"overlap"}
_MIGRATION_KEYS = {"workload", "compatibility", "timeout_seconds", "backup_required"}
_ROUTE_KEYS = {"name", "domain", "target", "path_prefix"}
_ROUTE_TARGET_KEYS = {"workload", "port"}
_UPDATE_KEYS = {"strategy", "auto_rollback", "drain_seconds"}
_SECRET_KEYS = {"name", "ref", "workloads"}
_MOUNT_KEYS = {"source", "target", "read_only"}


def load_manifest_v2(path: Path) -> ManifestV2:
    """Load one explicit v2 manifest with strict unknown-key handling."""

    path = Path(path).expanduser()
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestV2Error("Manifest not found: %s" % path) from exc
    except (OSError, UnicodeError) as exc:
        raise ManifestV2Error("Manifest could not be read: %s" % path) from exc
    except yaml.YAMLError as exc:
        raise ManifestV2Error("Manifest is not valid YAML: %s" % path) from exc
    if not isinstance(raw, dict):
        raise ManifestV2Error("Manifest root must be a mapping.")
    return parse_manifest_v2(raw, source_root=path.parent)


def parse_manifest_v2(raw: Mapping[str, Any], *, source_root: Path) -> ManifestV2:
    _shape(raw, _TOP_LEVEL, "manifest")
    if raw.get("version") != 2:
        raise ManifestV2Error("Manifest version must be exactly 2.")
    app = _identifier(raw.get("app"), "app")
    environment = _environment(raw.get("environment"))

    artifacts_raw = _mapping(raw.get("artifacts"), "artifacts", nonempty=True)
    artifacts = tuple(
        _artifact(name, value, environment=environment, source_root=source_root)
        for name, value in sorted(artifacts_raw.items())
    )
    artifact_names = {item.name for item in artifacts}

    workloads_raw = _mapping(raw.get("workloads"), "workloads", nonempty=True)
    preliminary = tuple(
        _workload(name, value, artifact_names=artifact_names)
        for name, value in sorted(workloads_raw.items())
    )
    workload_names = {item.name for item in preliminary}

    routes_raw = _list(raw.get("routes"), "routes")
    routes = tuple(_route(value, index, workload_names) for index, value in enumerate(routes_raw))
    route_names = [item.name for item in routes]
    if len(route_names) != len(set(route_names)):
        raise ManifestV2Error("routes must use unique names.")
    route_map: Dict[str, List[str]] = {name: [] for name in workload_names}
    by_workload = {item.name: item for item in preliminary}
    for route in routes:
        workload = by_workload[route.target.workload]
        if workload.kind not in {WorkloadKind.WEB, WorkloadKind.STATIC}:
            raise ManifestV2Error(
                "routes.%s targets %s, but only web and static workloads may receive routes."
                % (route.name, workload.kind.value)
            )
        if workload.kind is WorkloadKind.WEB:
            expected_port = workload.port
            if route.target.port is None or route.target.port != expected_port:
                raise ManifestV2Error(
                    "routes.%s target port must match workload %s port."
                    % (route.name, workload.name)
                )
        elif route.target.port is not None:
            raise ManifestV2Error("Static route targets may not declare a port.")
        route_map[workload.name].append(route.name)
    workloads = tuple(
        replace(item, route_ids=tuple(sorted(route_map[item.name])))
        for item in preliminary
    )

    migrations_raw = raw.get("migrations", {})
    migrations_map = _mapping(migrations_raw, "migrations")
    migrations = tuple(
        _migration(name, value, artifact_names=artifact_names)
        for name, value in sorted(migrations_map.items())
    )

    artifacts_by_name = {item.name: item for item in artifacts}
    for workload in workloads:
        artifact = artifacts_by_name[workload.artifact]
        if workload.kind is WorkloadKind.STATIC and artifact.static_root is None:
            raise ManifestV2Error("Static workloads must reference a static artifact.")
        if workload.kind is not WorkloadKind.STATIC and artifact.image is None:
            raise ManifestV2Error(
                "%s workloads must reference a container image artifact."
                % workload.kind.value.capitalize()
            )
        if workload.kind is WorkloadKind.STATIC and (
            workload.command
            or workload.port is not None
            or workload.replicas != 1
            or workload.env
            or workload.env_files
            or workload.mounts
            or workload.startup is not None
            or workload.readiness is not None
            or workload.liveness is not None
        ):
            raise ManifestV2Error(
                "Static workloads may only declare their artifact and update behavior."
            )
    for migration in migrations:
        if artifacts_by_name[migration.workload.artifact].image is None:
            raise ManifestV2Error("Migration workloads must reference a container image artifact.")

    update = _update(raw.get("update"), workloads)
    if update.strategy == "blue_green" and any(
        item.compatibility != "backward_compatible" for item in migrations
    ):
        raise ManifestV2Error(
            "Blue-green updates require every migration to be backward compatible."
        )
    secret_workload_names = workload_names.union(
        "migration-" + item.name for item in migrations
    )
    secrets = tuple(
        _secret(value, index, secret_workload_names)
        for index, value in enumerate(_list(raw.get("secrets", []), "secrets"))
    )
    names = [item.name for item in secrets]
    if len(names) != len(set(names)):
        raise ManifestV2Error("secrets must use unique names.")

    referenced_artifacts = {item.artifact for item in workloads}.union(
        item.workload.artifact for item in migrations
    )
    unused = sorted(artifact_names - referenced_artifacts)
    if unused:
        raise ManifestV2Error("artifacts are declared but unused: %s" % ", ".join(unused))
    if not routes and any(item.kind.receives_traffic for item in workloads):
        raise ManifestV2Error("Web and static workloads require at least one route.")
    for workload in workloads:
        if workload.kind.receives_traffic and not workload.route_ids:
            raise ManifestV2Error("Workload %s requires at least one route." % workload.name)

    return ManifestV2(
        version=2,
        app=app,
        environment=environment,
        artifacts=artifacts,
        workloads=workloads,
        migrations=migrations,
        routes=routes,
        update=update,
        secrets=secrets,
    )


def migrate_v1_document(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Produce an explicit v2 candidate without mutating the v1 document."""

    if raw.get("version") != 1:
        raise ManifestV2Error("Only manifest version 1 can be migrated to version 2.")
    app = _identifier(raw.get("app"), "app")
    environment = _environment(raw.get("environment") or "staging")
    kind = raw.get("kind")
    if kind not in {"service", "multi-service", "static"}:
        raise ManifestV2Error(
            "Automatic v2 migration supports service, multi-service, and static manifests."
        )
    services = raw.get("services") or {}
    routes = raw.get("routes") or []
    if not isinstance(services, dict) or not isinstance(routes, list):
        raise ManifestV2Error("The v1 service or route declaration is malformed.")
    artifacts: Dict[str, Dict[str, Any]] = {}
    workloads: Dict[str, Dict[str, Any]] = {}
    migrated_routes = []
    routed_services = {
        value.get("service") for value in routes if isinstance(value, dict) and value.get("service")
    }
    if kind == "static":
        artifacts["static"] = {"static_root": raw.get("static_root")}
        workloads["static"] = {"kind": "static", "artifact": "static"}
        target_name = "static"
    else:
        default_image = raw.get("image")
        for service_name, service_raw in sorted(services.items()):
            service_name = _identifier(service_name, "services.%s" % service_name)
            if not isinstance(service_raw, dict):
                raise ManifestV2Error("services.%s must be a mapping." % service_name)
            image = service_raw.get("image") or default_image
            artifact_name = "app-image"
            if artifact_name in artifacts and artifacts[artifact_name].get("image") != image:
                artifact_name = service_name + "-image"
            artifacts[artifact_name] = {"image": image}
            workload_kind = "web" if service_name in routed_services else "internal"
            workload: Dict[str, Any] = {
                "kind": workload_kind,
                "artifact": artifact_name,
            }
            for key in ("command", "port", "env", "env_files"):
                if key in service_raw:
                    workload[key] = service_raw[key]
            workloads[service_name] = workload
        target_name = next(iter(workloads), "web")
    for index, route in enumerate(routes):
        if not isinstance(route, dict):
            raise ManifestV2Error("routes[%d] must be a mapping." % index)
        target = route.get("service") or target_name
        target_port = workloads[target].get("port") if workloads[target]["kind"] == "web" else None
        migrated = {
            "name": "route-%d" % (index + 1),
            "domain": route.get("domain"),
            "target": {"workload": target},
        }
        if target_port is not None:
            migrated["target"]["port"] = target_port
        if route.get("path_prefix"):
            migrated["path_prefix"] = route["path_prefix"]
        migrated_routes.append(migrated)
    required_env = raw.get("required_env") or []
    secrets = [
        {
            "name": name,
            "ref": "secret://%s/%s/%s"
            % (app, environment, str(name).lower().replace("_", "-")),
        }
        for name in required_env
    ]
    if kind == "static":
        strategy = "static_atomic"
    elif any(item["kind"] == "web" for item in workloads.values()):
        strategy = "blue_green"
    else:
        strategy = "recreate"
    return {
        "version": 2,
        "app": app,
        "environment": environment,
        "artifacts": artifacts,
        "workloads": workloads,
        "migrations": {},
        "routes": migrated_routes,
        "update": {"strategy": strategy, "auto_rollback": True, "drain_seconds": 30},
        "secrets": secrets,
    }


def _artifact(
    raw_name: Any,
    raw: Any,
    *,
    environment: str,
    source_root: Path,
) -> ArtifactV2:
    name = _identifier(raw_name, "artifact name")
    value = _mapping(raw, "artifacts.%s" % name)
    _shape(value, _ARTIFACT_KEYS, "artifacts.%s" % name)
    image = _optional_text(value.get("image"), "artifacts.%s.image" % name)
    static_root = _optional_text(value.get("static_root"), "artifacts.%s.static_root" % name)
    if (image is None) == (static_root is None):
        raise ManifestV2Error("Artifact %s must declare exactly one of image or static_root." % name)
    if image is not None:
        match = _PINNED_IMAGE.fullmatch(image)
        if environment == "production" and match is None:
            raise ManifestV2Error("Production image artifacts must be digest-pinned.")
        digest = match.group("digest") if match is not None else canonical_digest({"image": image})
        return ArtifactV2(name=name, image=image, digest=digest)
    assert static_root is not None
    root = (source_root / static_root).resolve(strict=False)
    try:
        root.relative_to(source_root.resolve(strict=False))
    except ValueError as exc:
        raise ManifestV2Error("Static artifact root must remain inside the manifest source root.") from exc
    digest = _tree_digest(root)
    return ArtifactV2(name=name, static_root=static_root, digest=digest)


def _workload(raw_name: Any, raw: Any, *, artifact_names: Iterable[str]) -> WorkloadV2:
    name = _identifier(raw_name, "workload name")
    value = _mapping(raw, "workloads.%s" % name)
    _shape(value, _WORKLOAD_KEYS, "workloads.%s" % name)
    kind = _workload_kind(value.get("kind"), "workloads.%s.kind" % name)
    if kind is WorkloadKind.MIGRATION:
        raise ManifestV2Error("Migration workloads must be declared under migrations.")
    artifact = _identifier(value.get("artifact"), "workloads.%s.artifact" % name)
    if artifact not in set(artifact_names):
        raise ManifestV2Error("Workload %s references unknown artifact %s." % (name, artifact))
    command = _command(value.get("command", []), "workloads.%s.command" % name)
    port = _optional_port(value.get("port"), "workloads.%s.port" % name)
    if kind in {WorkloadKind.WEB, WorkloadKind.INTERNAL} and port is None:
        raise ManifestV2Error("%s workload %s requires a port." % (kind.value, name))
    if kind in {WorkloadKind.CRON, WorkloadKind.TASK, WorkloadKind.WORKER} and port is not None:
        raise ManifestV2Error("%s workload %s may not expose a port." % (kind.value, name))
    replicas = _positive_int(value.get("replicas", 1), "workloads.%s.replicas" % name, maximum=64)
    if kind in {WorkloadKind.CRON, WorkloadKind.TASK} and replicas != 1:
        raise ManifestV2Error("Cron and task workloads must use one replica.")
    schedule = _optional_text(value.get("schedule"), "workloads.%s.schedule" % name)
    if kind is WorkloadKind.CRON:
        _validate_cron(schedule, "workloads.%s.schedule" % name)
    elif schedule is not None:
        raise ManifestV2Error("Only cron workloads may declare schedule.")
    concurrency = _optional_text(
        value.get("concurrency_policy"), "workloads.%s.concurrency_policy" % name
    ) or "forbid"
    if concurrency not in {"forbid", "replace", "allow"}:
        raise ManifestV2Error("concurrency_policy must be forbid, replace, or allow.")
    if kind is not WorkloadKind.CRON and "concurrency_policy" in value:
        raise ManifestV2Error("Only cron workloads may declare concurrency_policy.")
    env = _env(value.get("env", {}), "workloads.%s.env" % name)
    env_files = _text_tuple(value.get("env_files", []), "workloads.%s.env_files" % name)
    mounts = tuple(
        _mount(item, "workloads.%s.mounts[%d]" % (name, index))
        for index, item in enumerate(_list(value.get("mounts", []), "workloads.%s.mounts" % name))
    )
    networks = _text_tuple(value.get("networks", ["app"]), "workloads.%s.networks" % name)
    if any(item not in {"app", "edge", "data"} for item in networks):
        raise ManifestV2Error("Workload networks must be app, edge, or data.")
    if kind is not WorkloadKind.WEB and "edge" in networks:
        raise ManifestV2Error("Only web workloads may explicitly join the edge network.")
    startup = _probe(value.get("startup"), "workloads.%s.startup" % name)
    readiness = _probe(value.get("readiness"), "workloads.%s.readiness" % name)
    liveness = _probe(value.get("liveness"), "workloads.%s.liveness" % name)
    resources = _resources(value.get("resources"), "workloads.%s.resources" % name)
    security = _security(value.get("security"), "workloads.%s.security" % name)
    shutdown = _nonnegative_int(
        value.get("shutdown_grace_seconds", 30),
        "workloads.%s.shutdown_grace_seconds" % name,
        maximum=3600,
    )
    update = _workload_update(value.get("update"), "workloads.%s.update" % name)
    if update.overlap == "allow" and kind not in {WorkloadKind.WEB, WorkloadKind.WORKER}:
        raise ManifestV2Error("Only web and explicitly duplicate-safe worker workloads may overlap.")
    return WorkloadV2(
        name=name,
        kind=kind,
        artifact=artifact,
        command=command,
        port=port,
        replicas=replicas,
        schedule=schedule,
        concurrency_policy=concurrency,
        env=env,
        env_files=env_files,
        mounts=mounts,
        networks=networks,
        startup=startup,
        readiness=readiness,
        liveness=liveness,
        resources=resources,
        security=security,
        shutdown_grace_seconds=shutdown,
        update=update,
    )


def _migration(raw_name: Any, raw: Any, *, artifact_names: Iterable[str]) -> MigrationV2:
    name = _identifier(raw_name, "migration name")
    value = _mapping(raw, "migrations.%s" % name)
    _shape(value, _MIGRATION_KEYS, "migrations.%s" % name)
    workload_raw = _mapping(value.get("workload"), "migrations.%s.workload" % name)
    workload_value = dict(workload_raw)
    if workload_value.get("kind") != "migration":
        raise ManifestV2Error("Migration %s workload kind must be migration." % name)
    workload_value["kind"] = "task"
    parsed = _workload(name, workload_value, artifact_names=artifact_names)
    workload = replace(parsed, kind=WorkloadKind.MIGRATION)
    compatibility = _optional_text(
        value.get("compatibility"), "migrations.%s.compatibility" % name
    ) or "backward_compatible"
    if compatibility not in {"backward_compatible", "irreversible"}:
        raise ManifestV2Error("Migration compatibility must be backward_compatible or irreversible.")
    timeout = _positive_int(
        value.get("timeout_seconds", 300), "migrations.%s.timeout_seconds" % name, maximum=86400
    )
    backup_required = _boolean(
        value.get("backup_required", compatibility == "irreversible"),
        "migrations.%s.backup_required" % name,
    )
    if compatibility == "irreversible" and not backup_required:
        raise ManifestV2Error("Irreversible migrations require backup evidence.")
    return MigrationV2(name, workload, compatibility, timeout, backup_required)


def _route(raw: Any, index: int, workload_names: Iterable[str]) -> RouteV2:
    field_name = "routes[%d]" % index
    value = _mapping(raw, field_name)
    _shape(value, _ROUTE_KEYS, field_name)
    name = _identifier(value.get("name"), field_name + ".name")
    try:
        domain = str(parse_domain(value.get("domain"), field=field_name + ".domain"))
    except CanonicalValidationError as exc:
        raise ManifestV2Error(str(exc)) from exc
    target_raw = _mapping(value.get("target"), field_name + ".target")
    _shape(target_raw, _ROUTE_TARGET_KEYS, field_name + ".target")
    workload = _identifier(target_raw.get("workload"), field_name + ".target.workload")
    if workload not in set(workload_names):
        raise ManifestV2Error("%s references unknown workload %s." % (field_name, workload))
    path_prefix = _optional_text(value.get("path_prefix"), field_name + ".path_prefix")
    if path_prefix is not None and (not path_prefix.startswith("/") or any(item in path_prefix for item in "\r\n{}")):
        raise ManifestV2Error("%s.path_prefix must be an injection-safe absolute HTTP path." % field_name)
    return RouteV2(
        name=name,
        domain=domain,
        target=RouteTargetV2(
            workload=workload,
            port=_optional_port(target_raw.get("port"), field_name + ".target.port"),
        ),
        path_prefix=path_prefix,
    )


def _update(raw: Any, workloads: Tuple[WorkloadV2, ...]) -> UpdateV2:
    if raw is None:
        if all(item.kind is WorkloadKind.STATIC for item in workloads):
            strategy = "static_atomic"
        elif any(item.kind is WorkloadKind.WEB for item in workloads):
            strategy = "blue_green"
        else:
            strategy = "recreate"
        return UpdateV2(strategy)
    value = _mapping(raw, "update")
    _shape(value, _UPDATE_KEYS, "update")
    strategy = _text(value.get("strategy"), "update.strategy")
    if strategy not in {"recreate", "blue_green", "static_atomic"}:
        raise ManifestV2Error("update.strategy must be recreate, blue_green, or static_atomic.")
    kinds = {item.kind for item in workloads}
    if strategy == "blue_green" and WorkloadKind.WEB not in kinds:
        raise ManifestV2Error("blue_green requires a web workload.")
    if strategy == "static_atomic" and kinds != {WorkloadKind.STATIC}:
        raise ManifestV2Error("static_atomic requires only static workloads.")
    if strategy == "recreate" and WorkloadKind.WEB in kinds:
        # This is allowed, but it must be an intentional manifest declaration.
        pass
    return UpdateV2(
        strategy=strategy,
        auto_rollback=_boolean(value.get("auto_rollback", True), "update.auto_rollback"),
        drain_seconds=_nonnegative_int(
            value.get("drain_seconds", 30), "update.drain_seconds", maximum=3600
        ),
    )


def _secret(raw: Any, index: int, workload_names: Iterable[str]) -> SecretV2:
    field_name = "secrets[%d]" % index
    value = _mapping(raw, field_name)
    _shape(value, _SECRET_KEYS, field_name)
    name = _text(value.get("name"), field_name + ".name")
    if _ENV_NAME.fullmatch(name) is None:
        raise ManifestV2Error("%s.name must be an environment variable name." % field_name)
    reference = _text(value.get("ref"), field_name + ".ref")
    if _SECRET_REF.fullmatch(reference) is None:
        raise ManifestV2Error("%s.ref must be an opaque secret:// reference." % field_name)
    workloads = _text_tuple(value.get("workloads", []), field_name + ".workloads")
    unknown = sorted(set(workloads) - set(workload_names))
    if unknown:
        raise ManifestV2Error("%s references unknown workloads: %s" % (field_name, ", ".join(unknown)))
    return SecretV2(name, reference, tuple(sorted(workloads)))


def _probe(raw: Any, field_name: str) -> Optional[ProbeV2]:
    if raw is None:
        return None
    value = _mapping(raw, field_name)
    _shape(value, _PROBE_KEYS, field_name)
    http_raw = value.get("http")
    command = _command(value.get("command", []), field_name + ".command")
    if (http_raw is None) == (not command):
        raise ManifestV2Error("%s must declare exactly one of http or command." % field_name)
    http = None
    if http_raw is not None:
        http_value = _mapping(http_raw, field_name + ".http")
        _shape(http_value, _HTTP_PROBE_KEYS, field_name + ".http")
        path = _text(http_value.get("path"), field_name + ".http.path")
        if not path.startswith("/") or any(item in path for item in "\r\n{}"):
            raise ManifestV2Error("%s.http.path must be an injection-safe absolute path." % field_name)
        method = _optional_text(http_value.get("method"), field_name + ".http.method") or "GET"
        if method not in {"GET", "HEAD"}:
            raise ManifestV2Error("Probe HTTP method must be GET or HEAD.")
        status = _positive_int(http_value.get("expect_status", 200), field_name + ".http.expect_status", maximum=599)
        if status < 100:
            raise ManifestV2Error("Probe expected status must be an HTTP status code.")
        http = HttpProbeV2(path, _port(http_value.get("port"), field_name + ".http.port"), method, status)
    interval = _positive_number(value.get("interval_seconds", 2), field_name + ".interval_seconds")
    timeout = _positive_number(value.get("timeout_seconds", 60), field_name + ".timeout_seconds")
    return ProbeV2(http, command, interval, timeout)


def _resources(raw: Any, field_name: str) -> ResourceV2:
    if raw is None:
        return ResourceV2()
    value = _mapping(raw, field_name)
    _shape(value, _RESOURCE_KEYS, field_name)
    memory = _optional_text(value.get("memory"), field_name + ".memory") or "256Mi"
    cpu = _optional_text(value.get("cpu"), field_name + ".cpu") or "1.0"
    if _RESOURCE_MEMORY.fullmatch(memory) is None:
        raise ManifestV2Error("%s.memory must be a bounded memory value such as 512Mi." % field_name)
    if _CPU.fullmatch(cpu) is None or float(cpu) > 128:
        raise ManifestV2Error("%s.cpu must be a positive CPU value no greater than 128." % field_name)
    pids = _positive_int(value.get("pids", 256), field_name + ".pids", maximum=65536)
    return ResourceV2(memory, cpu, pids)


def _security(raw: Any, field_name: str) -> SecurityV2:
    if raw is None:
        return SecurityV2()
    value = _mapping(raw, field_name)
    _shape(value, _SECURITY_KEYS, field_name)
    capabilities = _text_tuple(
        value.get("drop_capabilities", ["ALL"]), field_name + ".drop_capabilities"
    )
    if not capabilities or any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", item) for item in capabilities):
        raise ManifestV2Error("%s.drop_capabilities contains an invalid capability." % field_name)
    return SecurityV2(
        _boolean(value.get("run_as_non_root", True), field_name + ".run_as_non_root"),
        _boolean(value.get("read_only_root", True), field_name + ".read_only_root"),
        _boolean(value.get("no_new_privileges", True), field_name + ".no_new_privileges"),
        tuple(sorted(set(capabilities))),
    )


def _workload_update(raw: Any, field_name: str) -> WorkloadUpdateV2:
    if raw is None:
        return WorkloadUpdateV2()
    value = _mapping(raw, field_name)
    _shape(value, _WORKLOAD_UPDATE_KEYS, field_name)
    overlap = _optional_text(value.get("overlap"), field_name + ".overlap") or "forbid"
    if overlap not in {"forbid", "allow"}:
        raise ManifestV2Error("%s.overlap must be forbid or allow." % field_name)
    return WorkloadUpdateV2(overlap)


def _mount(raw: Any, field_name: str) -> MountV2:
    value = _mapping(raw, field_name)
    _shape(value, _MOUNT_KEYS, field_name)
    source = _text(value.get("source"), field_name + ".source")
    if source.startswith("/") or ".." in Path(source).parts or "\x00" in source:
        raise ManifestV2Error("%s.source must be a managed relative volume name." % field_name)
    target = _text(value.get("target"), field_name + ".target")
    if _MOUNT_TARGET.fullmatch(target) is None:
        raise ManifestV2Error("%s.target must be a safe absolute container path." % field_name)
    return MountV2(source, target, _boolean(value.get("read_only", True), field_name + ".read_only"))


def _tree_digest(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise ManifestV2Error("Static artifact root must be an existing real directory.")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ManifestV2Error("Static artifacts may not contain symbolic links.")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        metadata = path.stat()
        if stat.S_ISDIR(metadata.st_mode):
            digest.update(b"d\0" + relative + b"\0")
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(b"f\0" + relative + b"\0")
            digest.update(path.read_bytes())
        else:
            raise ManifestV2Error("Static artifacts may contain only files and directories.")
    return "sha256:" + digest.hexdigest()


def _shape(value: Mapping[str, Any], allowed: Iterable[str], field_name: str) -> None:
    unknown = sorted(str(key) for key in value if key not in set(allowed))
    if unknown:
        prefix = "" if field_name == "manifest" else field_name + "."
        raise ManifestV2Error("Unknown manifest v2 field(s): %s" % ", ".join(prefix + item for item in unknown))


def _mapping(value: Any, field_name: str, *, nonempty: bool = False) -> Dict[str, Any]:
    if not isinstance(value, dict) or (nonempty and not value):
        suffix = " non-empty" if nonempty else ""
        raise ManifestV2Error("%s must be a%s mapping." % (field_name, suffix))
    return dict(value)


def _list(value: Any, field_name: str) -> List[Any]:
    if not isinstance(value, list):
        raise ManifestV2Error("%s must be a list." % field_name)
    return value


def _identifier(value: Any, field_name: str) -> str:
    try:
        return str(parse_identifier(value, field=field_name))
    except CanonicalValidationError as exc:
        raise ManifestV2Error(str(exc)) from exc


def _environment(value: Any) -> str:
    try:
        parsed = parse_environment(value, field="environment")
    except CanonicalValidationError as exc:
        raise ManifestV2Error(str(exc)) from exc
    if parsed is None:
        raise ManifestV2Error("Manifest v2 requires an explicit environment.")
    return parsed.value


def _workload_kind(value: Any, field_name: str) -> WorkloadKind:
    try:
        return WorkloadKind(_text(value, field_name))
    except ValueError as exc:
        raise ManifestV2Error("%s is not a supported workload kind." % field_name) from exc


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise ManifestV2Error("%s must be a non-empty bounded string." % field_name)
    return value


def _optional_text(value: Any, field_name: str) -> Optional[str]:
    return None if value is None else _text(value, field_name)


def _text_tuple(value: Any, field_name: str) -> Tuple[str, ...]:
    items = _list(value, field_name)
    parsed = tuple(_text(item, "%s[%d]" % (field_name, index)) for index, item in enumerate(items))
    if len(parsed) != len(set(parsed)):
        raise ManifestV2Error("%s must not contain duplicates." % field_name)
    return parsed


def _command(value: Any, field_name: str) -> Tuple[str, ...]:
    return _text_tuple(value, field_name)


def _env(value: Any, field_name: str) -> Tuple[Tuple[str, str], ...]:
    mapping = _mapping(value, field_name)
    parsed = []
    for key, item in sorted(mapping.items()):
        if not isinstance(key, str) or _ENV_NAME.fullmatch(key) is None:
            raise ManifestV2Error("%s contains an invalid environment variable name." % field_name)
        parsed.append((key, _text(item, "%s.%s" % (field_name, key))))
    return tuple(parsed)


def _boolean(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestV2Error("%s must be boolean." % field_name)
    return value


def _positive_int(value: Any, field_name: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
        raise ManifestV2Error("%s must be an integer from 1 through %d." % (field_name, maximum))
    return value


def _nonnegative_int(value: Any, field_name: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise ManifestV2Error("%s must be an integer from 0 through %d." % (field_name, maximum))
    return value


def _positive_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 or value > 86400:
        raise ManifestV2Error("%s must be a positive number no greater than 86400." % field_name)
    return float(value)


def _port(value: Any, field_name: str) -> int:
    return _positive_int(value, field_name, maximum=65535)


def _optional_port(value: Any, field_name: str) -> Optional[int]:
    return None if value is None else _port(value, field_name)


def _validate_cron(value: Optional[str], field_name: str) -> None:
    if value is None:
        raise ManifestV2Error("%s is required for cron workloads." % field_name)
    fields = value.split()
    if len(fields) != 5 or any(_CRON_FIELD.fullmatch(item) is None for item in fields):
        raise ManifestV2Error("%s must be a five-field cron expression." % field_name)
    try:
        validate_cron_expression(value)
    except CronExpressionError as exc:
        raise ManifestV2Error("%s is invalid: %s" % (field_name, exc)) from exc


def _without_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_none(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [_without_none(item) for item in value]
    if isinstance(value, tuple):
        return [_without_none(item) for item in value]
    if isinstance(value, WorkloadKind):
        return value.value
    return value


def _workload_wire(value: WorkloadV2) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "kind": value.kind.value,
        "artifact": value.artifact,
        "command": list(value.command),
        "port": value.port,
        "replicas": value.replicas,
        "schedule": value.schedule,
        "env": dict(value.env),
        "env_files": list(value.env_files),
        "mounts": [asdict(item) for item in value.mounts],
        "networks": list(value.networks),
        "startup": _probe_wire(value.startup),
        "readiness": _probe_wire(value.readiness),
        "liveness": _probe_wire(value.liveness),
        "resources": asdict(value.resources),
        "security": {
            **asdict(value.security),
            "drop_capabilities": list(value.security.drop_capabilities),
        },
        "shutdown_grace_seconds": value.shutdown_grace_seconds,
        "update": asdict(value.update),
    }
    if value.kind is WorkloadKind.MIGRATION:
        result["kind"] = WorkloadKind.MIGRATION.value
    if value.kind is WorkloadKind.CRON:
        result["concurrency_policy"] = value.concurrency_policy
    return _without_none(result)


def _probe_wire(value: Optional[ProbeV2]) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    return _without_none(
        {
            "http": None if value.http is None else asdict(value.http),
            "command": list(value.command),
            "interval_seconds": value.interval_seconds,
            "timeout_seconds": value.timeout_seconds,
        }
    )
