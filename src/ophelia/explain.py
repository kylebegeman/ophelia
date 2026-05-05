from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from .manifest import Manifest
from .runtime import image_digests, image_references
from .templates import render_env_example
from .verify import verification_checks


def explain_manifest(manifest: Manifest, manifest_path: Path) -> Dict[str, object]:
    env_requirements = _required_env_keys(manifest)
    checks = verification_checks(manifest)
    services = [
        {
            "name": service.name,
            "image": service.image or manifest.image,
            "port": service.port,
            "host_port": service.host_port,
            "env_files": service.env_files,
            "mounts": [
                {
                    "source": mount.source,
                    "target": mount.target,
                    "read_only": mount.read_only,
                    "bind": mount.bind,
                }
                for mount in service.mounts
            ],
        }
        for service in manifest.services.values()
    ]
    routes = [
        {
            "domain": route.domain,
            "service": route.service,
            "upstream": route.upstream,
            "path": route.path,
            "path_prefix": route.path_prefix,
            "strip_prefix": route.strip_prefix,
            "rewrite_prefix": route.rewrite_prefix,
        }
        for route in manifest.routes
    ]
    return {
        "app": manifest.app,
        "manifest_path": str(manifest_path),
        "kind": manifest.kind,
        "profile": manifest.profile,
        "environment": getattr(manifest, "environment", None),
        "services": services,
        "images": image_references(manifest),
        "image_digests": image_digests(manifest),
        "ports": [
            {"service": service.name, "container_port": service.port, "host_port": service.host_port}
            for service in manifest.services.values()
        ],
        "routes": routes,
        "domains": sorted({route.domain for route in manifest.routes}),
        "env_files": manifest.env_files,
        "required_secrets": env_requirements,
        "addons": {
            "postgres": manifest.addons.postgres,
            "redis": manifest.addons.redis,
        },
        "mounts": [
            {
                "service": service.name,
                "source": mount.source,
                "target": mount.target,
                "read_only": mount.read_only,
                "bind": mount.bind,
            }
            for service in manifest.services.values()
            for mount in service.mounts
        ],
        "verification_checks": [
            {
                "name": check.name or check.url,
                "url": check.url,
                "expect_status": check.expect_status,
                "contains_required": check.contains is not None,
            }
            for check in checks
        ],
        "release_behavior": {
            "writes_release_record": True,
            "writes_rendered_bundle_snapshot": True,
            "rollback_eligible_after_deploy": True,
        },
        "deployment_ordering": {
            "depends_on": manifest.depends_on,
            "deployment_order": manifest.deployment_order,
            "migration_before": manifest.migration_before,
            "verify_before_next": manifest.verify_before_next,
        },
        "risk_notes": _risk_notes(manifest, env_requirements, checks),
        "summary": (
            f"{manifest.app} is a {manifest.kind} manifest with "
            f"{len(manifest.services)} service(s), {len(manifest.routes)} route(s), "
            f"{len(env_requirements)} required secret placeholder(s), and {len(checks)} verification check(s)."
        ),
    }


def _required_env_keys(manifest: Manifest) -> List[Dict[str, object]]:
    values: Dict[str, str] = {}
    for raw_line in render_env_example(manifest).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return [
        {"key": key, "placeholder": True, "blocking_for_apply": True}
        for key, value in sorted(values.items())
        if _is_placeholder(value)
    ]


def _risk_notes(manifest: Manifest, env_requirements: List[Dict[str, object]], checks: object) -> List[str]:
    notes: List[str] = []
    if env_requirements:
        notes.append("Apply is blocked until placeholder env values are replaced or provisioned.")
    if manifest.addons.postgres:
        notes.append("Requires shared Postgres availability during apply and backup planning.")
    if manifest.addons.redis:
        notes.append("Requires shared Redis availability during apply.")
    if not checks:
        notes.append("No verification checks are configured or inferred.")
    if getattr(manifest, "environment", None) == "production":
        notes.append("Production apply requires a confirmation token from deploy plan.")
    return notes


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered in {"", "replace-me", "changeme", "todo"} or "replace-me" in lowered
