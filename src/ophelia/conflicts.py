from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

from .manifest import Manifest, ManifestError, load_manifest
from .verify import verification_checks


def scan_conflicts(manifest_dir: Path) -> Dict[str, object]:
    manifests: List[Tuple[Path, Manifest]] = []
    errors: List[Dict[str, str]] = []
    for manifest_path in sorted(manifest_dir.glob("*.ophelia.yml")) if manifest_dir.exists() else []:
        try:
            manifests.append((manifest_path, load_manifest(manifest_path)))
        except ManifestError as exc:
            errors.append({"path": str(manifest_path), "error": str(exc)})

    conflicts: List[Dict[str, object]] = []
    warnings: List[Dict[str, object]] = []
    _duplicate_app_ids(manifests, conflicts)
    _duplicate_domains(manifests, conflicts)
    _duplicate_routes(manifests, conflicts)
    _duplicate_host_ports(manifests, conflicts)
    _duplicate_aliases(manifests, conflicts)
    _environment_host_warnings(manifests, warnings)
    _missing_verification_warnings(manifests, warnings)

    for error in errors:
        conflicts.append({"type": "manifest_invalid", **error})

    return {
        "manifest_dir": str(manifest_dir),
        "ok": not conflicts,
        "conflicts": conflicts,
        "warnings": warnings,
        "manifest_count": len(manifests),
        "summary": f"{len(conflicts)} conflict(s), {len(warnings)} warning(s) across {len(manifests)} manifest(s).",
    }


def _duplicate_app_ids(manifests: List[Tuple[Path, Manifest]], conflicts: List[Dict[str, object]]) -> None:
    by_app: Dict[str, List[str]] = defaultdict(list)
    for path, manifest in manifests:
        by_app[manifest.app].append(str(path))
    for app, paths in sorted(by_app.items()):
        if len(paths) > 1:
            conflicts.append({"type": "duplicate_app_id", "app": app, "paths": paths})


def _duplicate_domains(manifests: List[Tuple[Path, Manifest]], conflicts: List[Dict[str, object]]) -> None:
    by_domain: Dict[str, List[str]] = defaultdict(list)
    for path, manifest in manifests:
        for route in manifest.routes:
            by_domain[route.domain].append(f"{path}:{manifest.app}")
    for domain, owners in sorted(by_domain.items()):
        if len(set(owners)) > 1:
            conflicts.append({"type": "duplicate_domain", "domain": domain, "owners": sorted(set(owners))})


def _duplicate_routes(manifests: List[Tuple[Path, Manifest]], conflicts: List[Dict[str, object]]) -> None:
    by_route: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for path, manifest in manifests:
        for route in manifest.routes:
            key = (route.domain, route.path or route.path_prefix or "*")
            by_route[key].append(f"{path}:{manifest.app}")
    for (domain, route), owners in sorted(by_route.items()):
        if len(set(owners)) > 1:
            conflicts.append(
                {"type": "duplicate_route", "domain": domain, "route": route, "owners": sorted(set(owners))}
            )


def _duplicate_host_ports(manifests: List[Tuple[Path, Manifest]], conflicts: List[Dict[str, object]]) -> None:
    by_port: Dict[int, List[str]] = defaultdict(list)
    for path, manifest in manifests:
        for service in manifest.services.values():
            if service.host_port is not None:
                by_port[service.host_port].append(f"{path}:{manifest.app}.{service.name}")
    for port, owners in sorted(by_port.items()):
        if len(owners) > 1:
            conflicts.append({"type": "duplicate_host_port", "host_port": port, "owners": owners})


def _duplicate_aliases(manifests: List[Tuple[Path, Manifest]], conflicts: List[Dict[str, object]]) -> None:
    by_alias: Dict[str, List[str]] = defaultdict(list)
    for path, manifest in manifests:
        for service_name in manifest.services:
            by_alias[manifest.service_alias(service_name)].append(f"{path}:{manifest.app}.{service_name}")
    for alias, owners in sorted(by_alias.items()):
        if len(owners) > 1:
            conflicts.append({"type": "duplicate_docker_alias", "alias": alias, "owners": owners})


def _environment_host_warnings(manifests: List[Tuple[Path, Manifest]], warnings: List[Dict[str, object]]) -> None:
    for path, manifest in manifests:
        environment = getattr(manifest, "environment", None)
        domains = sorted({route.domain for route in manifest.routes})
        if environment == "staging":
            production_like = [domain for domain in domains if "staging" not in domain and "-staging" not in domain]
            if production_like:
                warnings.append(
                    {
                        "type": "staging_manifest_using_production_like_host",
                        "path": str(path),
                        "app": manifest.app,
                        "domains": production_like,
                    }
                )
        if environment == "production":
            staging_like = [domain for domain in domains if "staging" in domain or "-staging" in domain]
            if staging_like:
                warnings.append(
                    {
                        "type": "production_manifest_using_staging_like_host",
                        "path": str(path),
                        "app": manifest.app,
                        "domains": staging_like,
                    }
                )


def _missing_verification_warnings(manifests: List[Tuple[Path, Manifest]], warnings: List[Dict[str, object]]) -> None:
    for path, manifest in manifests:
        if not verification_checks(manifest):
            warnings.append(
                {
                    "type": "missing_verification_checks",
                    "path": str(path),
                    "app": manifest.app,
                }
            )
