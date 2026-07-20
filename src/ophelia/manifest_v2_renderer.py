"""Deterministic revision bundle renderer for manifest v2."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping

import yaml

from .domain import Revision, WorkloadKind
from .manifest_v2 import ManifestV2, ProbeV2, WorkloadV2
from .manifest_v2_sources import support_file_target


RENDERER_VERSION = "manifest-v2"
DEFAULT_RUNTIME_ROOT = Path("/var/lib/ophelia")
DEFAULT_SECRET_RUNTIME_ROOT = Path("/run/ophelia/secrets")


def compose_project_name(manifest: ManifestV2, revision: Revision) -> str:
    suffix = revision.revision_id.removeprefix("rev_")[:12]
    return _docker_name("ophelia-%s-%s-%s" % (manifest.app, manifest.environment, suffix))


def route_auth_root(
    manifest: ManifestV2,
    revision: Revision,
    route_name: str,
    *,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> Path:
    return (
        Path(runtime_root)
        / "run"
        / "route-auth"
        / manifest.app
        / manifest.environment
        / revision.revision_id
        / route_name
    )


def route_auth_env_key(manifest: ManifestV2, route_name: str) -> str:
    import hashlib

    identity = "%s\0%s\0%s" % (manifest.app, manifest.environment, route_name)
    return "OPHELIA_ROUTE_AUTH_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24].upper()


def render_revision_bundle(
    manifest: ManifestV2,
    revision: Revision,
    *,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    secret_runtime_root: Path = DEFAULT_SECRET_RUNTIME_ROOT,
) -> Dict[Path, str]:
    """Render immutable, secret-free runtime inputs for one exact revision."""

    if revision.app != manifest.app or revision.environment != manifest.environment:
        raise ValueError("Revision scope does not match manifest v2 scope.")
    if revision.manifest_digest != manifest.canonical_digest():
        raise ValueError("Revision does not bind the supplied manifest v2 document.")
    project = compose_project_name(manifest, revision)
    revision_root = (
        Path(runtime_root)
        / "apps"
        / manifest.app
        / "environments"
        / manifest.environment
        / "revisions"
        / revision.revision_id
    )
    compose = _compose_document(
        manifest,
        revision,
        project=project,
        secret_runtime_root=secret_runtime_root,
    )
    revision_document = {
        "schema_version": 1,
        "kind": "ophelia.revision-runtime",
        "app": manifest.app,
        "environment": manifest.environment,
        "revision_id": revision.revision_id,
        "revision_digest": revision.content_digest(),
        "manifest_digest": revision.manifest_digest,
        "artifact_digests": list(revision.artifact_digests),
        "renderer_version": RENDERER_VERSION,
        "compose_project": project,
        "revision_root": str(revision_root),
        "update": _json_value(manifest.update),
        "workloads": [
            {
                "name": workload.name,
                "kind": workload.kind.value,
                "replicas": workload.replicas,
                "route_ids": list(workload.route_ids),
                "activation_semantics": workload.kind.activation_semantics,
                "overlap": workload.update.overlap,
            }
            for workload in manifest.workloads
        ],
        "migrations": [
            {
                "name": migration.name,
                "compatibility": migration.compatibility,
                "timeout_seconds": migration.timeout_seconds,
                "backup_required": migration.backup_required,
            }
            for migration in manifest.migrations
        ],
    }
    secret_refs = {
        "schema_version": 1,
        "kind": "ophelia.secret-references",
        "app": manifest.app,
        "environment": manifest.environment,
        "revision_id": revision.revision_id,
        "secrets": [
            {
                "name": secret.name,
                "ref": secret.ref,
                "workloads": list(secret.workloads),
                "mode": secret.mode,
                "target": secret.target,
                "encoding": secret.encoding,
            }
            for secret in manifest.secrets
        ]
        + [
            {
                "name": "route:%s:trust-pool" % route.name,
                "ref": route.client_auth.trust_pool_ref,
                "workloads": [route.target.workload],
                "mode": "route-trust-pool",
                "target": str(
                    route_auth_root(manifest, revision, route.name, runtime_root=runtime_root)
                    / "client-ca.pem"
                ),
                "encoding": route.client_auth.trust_pool_encoding,
            }
            for route in manifest.routes
            if route.client_auth is not None
        ]
        + [
            {
                "name": "route:%s:forward-authorization" % route.name,
                "ref": route.client_auth.forward.authorization_ref,
                "workloads": [route.target.workload],
                "mode": "caddy-env",
                "target": route_auth_env_key(manifest, route.name),
                "encoding": "plain",
            }
            for route in manifest.routes
            if route.client_auth is not None and route.client_auth.forward is not None
        ],
    }
    return {
        Path("manifest.lock.json"): _json_text(manifest.to_lock_dict()),
        Path("revision.json"): _json_text(revision_document),
        Path("artifact-lock.json"): _json_text(
            {
                "schema_version": 1,
                "kind": "ophelia.artifact-lock",
                "artifacts": [
                    {
                        "name": item.name,
                        "image": item.image,
                        "static_root": item.static_root,
                        "digest": item.digest,
                    }
                    for item in manifest.artifacts
                ],
            }
        ),
        Path("secret-refs.json"): _json_text(secret_refs),
        Path("compose.yml"): yaml.safe_dump(
            compose,
            default_flow_style=False,
            sort_keys=True,
        ),
        Path("caddy/routes.caddy"): _render_caddy(
            manifest,
            revision,
            runtime_root=runtime_root,
        ),
    }


def _compose_document(
    manifest: ManifestV2,
    revision: Revision,
    *,
    project: str,
    secret_runtime_root: Path,
) -> Dict[str, Any]:
    services: Dict[str, Any] = {}
    revision_suffix = revision.revision_id.removeprefix("rev_")[:12]
    for workload in manifest.workloads:
        if workload.kind is WorkloadKind.STATIC:
            continue
        services[workload.name] = _compose_service(
            manifest,
            revision,
            workload,
            service_name=workload.name,
            revision_suffix=revision_suffix,
            secret_runtime_root=secret_runtime_root,
        )
    for migration in manifest.migrations:
        service_name = "migration-" + migration.name
        services[service_name] = _compose_service(
            manifest,
            revision,
            migration.workload,
            service_name=service_name,
            revision_suffix=revision_suffix,
            secret_runtime_root=secret_runtime_root,
        )
    volumes: Dict[str, Any] = {}
    for workload in tuple(manifest.workloads) + tuple(
        migration.workload for migration in manifest.migrations
    ):
        for mount in workload.mounts:
            name = _volume_name(manifest, mount.source)
            volumes[name] = {
                "name": name,
                "labels": {
                    "ophelia.app": manifest.app,
                    "ophelia.environment": manifest.environment,
                    "ophelia.volume": mount.source,
                },
            }
    document: Dict[str, Any] = {
        "name": project,
        "services": services,
        "networks": {
            "ophelia-app": {
                "name": _docker_name("ophelia-%s-%s-app" % (manifest.app, manifest.environment)),
                "labels": {
                    "ophelia.app": manifest.app,
                    "ophelia.environment": manifest.environment,
                },
            },
            "ophelia-edge": {"external": True, "name": "ophelia-edge"},
            "ophelia-data": {"external": True, "name": "ophelia-data"},
        },
    }
    if volumes:
        document["volumes"] = volumes
    return document


def _compose_service(
    manifest: ManifestV2,
    revision: Revision,
    workload: WorkloadV2,
    *,
    service_name: str,
    revision_suffix: str,
    secret_runtime_root: Path,
) -> Dict[str, Any]:
    artifact = manifest.artifact(workload.artifact)
    if artifact.image is None:
        raise ValueError("Only static workloads may reference static artifacts.")
    one_shot_profile = {
        WorkloadKind.CRON: "ophelia-cron",
        WorkloadKind.TASK: "ophelia-task",
        WorkloadKind.MIGRATION: "ophelia-migration",
    }.get(workload.kind)
    environment = dict(workload.env)
    environment.update(
        {
            "OPHELIA_APP": manifest.app,
            "OPHELIA_ENVIRONMENT": manifest.environment,
            "OPHELIA_REVISION_ID": revision.revision_id,
            "OPHELIA_REVISION_DIGEST": revision.content_digest(),
            "OPHELIA_WORKLOAD": workload.name,
            "OPHELIA_SERVICE": workload.name,
            "OPHELIA_RELEASE_ID": manifest.release.release_id or revision.revision_id,
            "OPHELIA_IMAGE_REF": artifact.image,
            "OPHELIA_IMAGE_DIGEST": artifact.digest,
            "OPHELIA_MANIFEST_HASH": manifest.canonical_digest(),
        }
    )
    if manifest.release.commit_sha is not None:
        environment["OPHELIA_COMMIT_SHA"] = manifest.release.commit_sha
    if manifest.release.build_time is not None:
        environment["OPHELIA_BUILD_TIME"] = manifest.release.build_time
    networks: Dict[str, Any] = {
        "ophelia-app": {
            "aliases": [
                _docker_name("%s-%s" % (workload.name, revision_suffix)),
            ]
        }
    }
    if workload.kind is WorkloadKind.WEB:
        networks["ophelia-edge"] = {
            "aliases": [
                _docker_name("%s-%s-%s" % (manifest.app, workload.name, revision_suffix)),
            ]
        }
    if "data" in workload.networks:
        data_aliases = dict(workload.network_aliases).get("data", ())
        networks["ophelia-data"] = (
            {"aliases": list(data_aliases)} if data_aliases else {}
        )
    security_options: List[str] = []
    if workload.security.no_new_privileges:
        security_options.append("no-new-privileges:true")
    if workload.security.seccomp_profile == "unconfined":
        security_options.append("seccomp=unconfined")
    if workload.security.apparmor_profile == "unconfined":
        security_options.append("apparmor=unconfined")
    service: Dict[str, Any] = {
        "image": artifact.image,
        "restart": "no" if one_shot_profile else "unless-stopped",
        "environment": environment,
        "networks": networks,
        "privileged": workload.security.privileged,
        "read_only": workload.security.read_only_root,
        "security_opt": security_options,
        "cap_add": list(workload.security.add_capabilities),
        "cap_drop": list(workload.security.drop_capabilities),
        "pids_limit": workload.resources.pids,
        "mem_limit": workload.resources.memory,
        "cpus": workload.resources.cpu,
        "labels": {
            "ophelia.managed": "true",
            "ophelia.app": manifest.app,
            "ophelia.environment": manifest.environment,
            "ophelia.revision": revision.revision_id,
            "ophelia.revision-digest": revision.content_digest(),
            "ophelia.workload": workload.name,
            "ophelia.workload-kind": workload.kind.value,
        },
        "stop_grace_period": "%ds" % workload.shutdown_grace_seconds,
    }
    if workload.security.run_as_user is not None:
        service["user"] = str(workload.security.run_as_user)
    if workload.command:
        service["command"] = list(workload.command)
    exposed_ports = sorted(
        {
            port
            for port in [workload.port, *(item[1] for item in workload.endpoints)]
            if port is not None
        }
    )
    if exposed_ports:
        service["expose"] = exposed_ports
    if one_shot_profile:
        service["profiles"] = [one_shot_profile]
    env_files = [
        "./" + support_file_target(
            workload,
            index,
            source,
            service_name=service_name,
        ).as_posix()
        for index, source in enumerate(workload.env_files)
    ]
    targeted_secrets = [
        item
        for item in manifest.secrets
        if not item.workloads
        or workload.name in item.workloads
        or service_name in item.workloads
    ]
    env_secrets = [item for item in targeted_secrets if item.mode == "env"]
    file_secrets = [item for item in targeted_secrets if item.mode == "file"]
    if env_secrets:
        env_files.append(
            str(
                Path(secret_runtime_root)
                / manifest.app
                / manifest.environment
                / revision.revision_id
                / (service_name + ".env")
            )
        )
    if env_files:
        service["env_file"] = env_files
    volume_mounts = [
        "%s:%s%s"
        % (
            _volume_name(manifest, mount.source),
            mount.target,
            ":ro" if mount.read_only else "",
        )
        for mount in workload.mounts
    ]
    for secret in file_secrets:
        assert secret.target is not None
        environment[secret.name] = secret.target
        volume_mounts.append(
            "%s:%s:ro"
            % (
                Path(secret_runtime_root)
                / manifest.app
                / manifest.environment
                / revision.revision_id
                / (service_name + ".files")
                / secret.name,
                secret.target,
            )
        )
    if volume_mounts:
        service["volumes"] = volume_mounts
    if workload.devices:
        service["devices"] = [
            "%s:%s:%s" % (item.source, item.target, item.permissions)
            for item in workload.devices
        ]
    health = workload.liveness or workload.readiness or workload.startup
    healthcheck = _compose_healthcheck(health)
    if healthcheck is not None:
        service["healthcheck"] = healthcheck
    return service


def _compose_healthcheck(probe: ProbeV2 | None) -> Dict[str, Any] | None:
    if probe is None or not probe.command:
        # HTTP probes are executed by the backend against the isolated
        # candidate. Avoid assuming curl, wget, or Python exists in the image.
        return None
    return {
        "test": list(probe.command),
        "interval": "%ss" % _number(probe.interval_seconds),
        "timeout": "%ss" % _number(min(probe.timeout_seconds, 30.0)),
        "retries": max(1, int(probe.timeout_seconds / probe.interval_seconds)),
    }


def _render_caddy(manifest: ManifestV2, revision: Revision, *, runtime_root: Path) -> str:
    revision_suffix = revision.revision_id.removeprefix("rev_")[:12]
    blocks = []
    for route in manifest.routes:
        workload = manifest.workload(route.target.workload)
        lines = [route.domain + " {"]
        if route.client_auth is not None:
            trust_pool = (
                route_auth_root(
                    manifest,
                    revision,
                    route.name,
                    runtime_root=runtime_root,
                )
                / "client-ca.pem"
            )
            lines.extend(
                [
                    "  tls {",
                    "    client_auth {",
                    "      mode %s" % route.client_auth.mode,
                    "      trust_pool file %s" % trust_pool,
                    "    }",
                    "  }",
                ]
            )
        elif route.tls.mode == "internal":
            lines.append("  tls internal")
        if workload.kind is WorkloadKind.STATIC:
            artifact = manifest.artifact(workload.artifact)
            root = (
                Path(runtime_root)
                / "static"
                / manifest.app
                / manifest.environment
                / "revisions"
                / revision.revision_id
                / artifact.name
            )
            lines.extend(["  root * %s" % root, "  file_server"])
        else:
            alias = _docker_name("%s-%s-%s" % (manifest.app, workload.name, revision_suffix))
            proxy_lines = ["reverse_proxy %s:%d {" % (alias, route.target.port)]
            if route.client_auth is not None and route.client_auth.forward is not None:
                forward = route.client_auth.forward
                proxy_lines.extend(
                    [
                        "  header_up %s \"%s {$%s}\""
                        % (
                            forward.authorization_header,
                            forward.authorization_scheme,
                            route_auth_env_key(manifest, route.name),
                        ),
                        "  header_up %s \"sha256:{tls_client_fingerprint}\""
                        % forward.fingerprint_header,
                    ]
                )
            proxy_lines.append("}")
            if route.path_prefix:
                lines.append("  handle %s* {" % route.path_prefix)
                lines.extend("    " + item for item in proxy_lines)
                lines.append("  }")
            else:
                lines.extend("  " + item for item in proxy_lines)
        lines.append("}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _volume_name(manifest: ManifestV2, source: str) -> str:
    return _docker_name("ophelia-%s-%s-%s" % (manifest.app, manifest.environment, source))


def _docker_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-_")
    if not normalized:
        raise ValueError("Docker resource name cannot be empty.")
    return normalized[:128]


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _json_value(value: Any) -> Any:
    from dataclasses import asdict

    return asdict(value)


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
