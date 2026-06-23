from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .manifest import Manifest, ManifestError, load_manifest
from .operation_schema import report_envelope
from .runtime import active_release
from .verify import verification_checks


def scan_conflicts(manifest_dir: Path, runtime_root: Optional[Path] = None) -> Dict[str, object]:
    manifests: List[Tuple[Path, Manifest, Dict[str, object]]] = []
    errors: List[Dict[str, str]] = []
    for manifest_path in sorted(manifest_dir.glob("*.ophelia.yml")) if manifest_dir.exists() else []:
        try:
            manifest = load_manifest(manifest_path)
            manifests.append((manifest_path, manifest, _owner(manifest_path, manifest, source="manifest")))
        except ManifestError as exc:
            errors.append({"path": str(manifest_path), "error": str(exc)})
    if runtime_root is not None:
        manifests.extend(_active_runtime_manifests(runtime_root))

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

    report = {
        "manifest_dir": str(manifest_dir),
        "runtime_root": str(runtime_root) if runtime_root is not None else None,
        "ok": not conflicts,
        "conflicts": conflicts,
        "warnings": warnings,
        "manifest_count": len(manifests),
        "summary": f"{len(conflicts)} conflict(s), {len(warnings)} warning(s) across {len(manifests)} manifest(s).",
    }
    report.update(
        report_envelope(
            "inspect.conflicts",
            None,
            None,
            str(report["summary"]),
            blockers=[
                {"code": str(item.get("type")), "message": _conflict_message(item), "path": _conflict_path(item)}
                for item in conflicts
            ],
            warnings=[
                {"code": str(item.get("type")), "message": _conflict_message(item), "path": _conflict_path(item)}
                for item in warnings
            ],
            checks=[{"name": "manifest_scan", "ok": not errors, "message": f"{len(manifests)} manifest(s) checked."}],
            status="ok" if not conflicts else "blocked",
        )
    )
    report["ok"] = not conflicts
    report["conflicts"] = conflicts
    report["warnings"] = warnings
    return report


def _conflict_identity(item: Dict[str, object]) -> str:
    """The most specific identifying field of a conflict/warning, as a string."""
    for key in ("domain", "route", "app", "alias", "host_port", "path"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _conflict_owner_apps(item: Dict[str, object]) -> str:
    owners = item.get("owners")
    if isinstance(owners, list):
        apps = sorted({str(o.get("app")) for o in owners if isinstance(o, dict) and o.get("app")})
        if apps:
            return ", ".join(apps)
    return ""


def _conflict_message(item: Dict[str, object]) -> str:
    """Compose a human-readable message instead of a raw dict repr.

    The structured `conflicts`/`warnings` lists keep the full machine-readable
    item; this only shapes the envelope `message` so operator UIs do not have
    to parse a Python dict string.
    """
    kind = str(item.get("type"))
    if item.get("error"):
        target = item.get("path") or _conflict_identity(item)
        return f"{kind}: {target}: {item.get('error')}".strip(": ")
    identity = _conflict_identity(item)
    message = f"{kind}: {identity}" if identity else kind
    owners = _conflict_owner_apps(item)
    if owners:
        message = f"{message} claimed by {owners}"
    return message


def _conflict_path(item: Dict[str, object]) -> str:
    return str(
        item.get("domain")
        or item.get("app")
        or item.get("alias")
        or item.get("host_port")
        or item.get("path")
        or ""
    )


def _duplicate_app_ids(manifests: List[Tuple[Path, Manifest, Dict[str, object]]], conflicts: List[Dict[str, object]]) -> None:
    by_app: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for _path, manifest, owner in manifests:
        by_app[manifest.app].append(owner)
    for app, owners in sorted(by_app.items()):
        owners = _dedupe_active_runtime_owners(owners)
        if len(owners) > 1:
            conflicts.append({"type": "duplicate_app_id", "app": app, "owners": owners})


def _duplicate_domains(manifests: List[Tuple[Path, Manifest, Dict[str, object]]], conflicts: List[Dict[str, object]]) -> None:
    by_domain: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for _path, manifest, owner in manifests:
        for route in manifest.routes:
            by_domain[_normalize_domain(route.domain)].append({**owner, "route": _route_match(route)})
    for domain, owners in sorted(by_domain.items()):
        if len(_owner_keys(owners)) > 1:
            conflicts.append({"type": "duplicate_domain", "domain": domain, "owners": owners})


def _duplicate_routes(manifests: List[Tuple[Path, Manifest, Dict[str, object]]], conflicts: List[Dict[str, object]]) -> None:
    by_route: Dict[Tuple[str, str], List[Dict[str, object]]] = defaultdict(list)
    for _path, manifest, owner in manifests:
        for route in manifest.routes:
            key = (_normalize_domain(route.domain), _route_match(route))
            by_route[key].append({**owner, "route": key[1]})
    for (domain, route), owners in sorted(by_route.items()):
        if len(_owner_keys(owners)) > 1:
            conflicts.append(
                {"type": "duplicate_route", "domain": domain, "route": route, "owners": owners}
            )


def _duplicate_host_ports(manifests: List[Tuple[Path, Manifest, Dict[str, object]]], conflicts: List[Dict[str, object]]) -> None:
    by_port: Dict[int, List[Dict[str, object]]] = defaultdict(list)
    for _path, manifest, owner in manifests:
        for service in manifest.services.values():
            if service.host_port is not None:
                by_port[service.host_port].append({**owner, "service": service.name})
    for port, owners in sorted(by_port.items()):
        owners = _dedupe_active_runtime_owners(owners)
        if len(owners) > 1:
            conflicts.append({"type": "duplicate_host_port", "host_port": port, "owners": owners})


def _duplicate_aliases(manifests: List[Tuple[Path, Manifest, Dict[str, object]]], conflicts: List[Dict[str, object]]) -> None:
    by_alias: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for _path, manifest, owner in manifests:
        for service_name in manifest.services:
            by_alias[manifest.service_alias(service_name)].append({**owner, "service": service_name})
    for alias, owners in sorted(by_alias.items()):
        owners = _dedupe_active_runtime_owners(owners)
        if len(owners) > 1:
            conflicts.append({"type": "duplicate_docker_alias", "alias": alias, "owners": owners})


def _environment_host_warnings(manifests: List[Tuple[Path, Manifest, Dict[str, object]]], warnings: List[Dict[str, object]]) -> None:
    for path, manifest, owner in manifests:
        environment = getattr(manifest, "environment", None)
        domains = sorted({_normalize_domain(route.domain) for route in manifest.routes})
        if environment == "staging":
            production_like = [domain for domain in domains if "staging" not in domain and "-staging" not in domain]
            if production_like:
                warnings.append(
                    {
                        "type": "staging_manifest_using_production_like_host",
                        "path": str(path),
                        "app": manifest.app,
                        "owner": owner,
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
                        "owner": owner,
                        "domains": staging_like,
                    }
                )


def _missing_verification_warnings(manifests: List[Tuple[Path, Manifest, Dict[str, object]]], warnings: List[Dict[str, object]]) -> None:
    for path, manifest, owner in manifests:
        if not verification_checks(manifest):
            warnings.append(
                {
                    "type": "missing_verification_checks",
                    "path": str(path),
                    "app": manifest.app,
                    "owner": owner,
                }
            )


def _active_runtime_manifests(runtime_root: Path) -> List[Tuple[Path, Manifest, Dict[str, object]]]:
    result: List[Tuple[Path, Manifest, Dict[str, object]]] = []
    apps_root = runtime_root / "apps"
    if not apps_root.exists():
        return result
    for app_root in sorted(path for path in apps_root.iterdir() if path.is_dir()):
        lock_path = app_root / "manifest.lock.json"
        if not lock_path.exists():
            continue
        try:
            manifest = load_manifest(lock_path)
        except ManifestError:
            continue
        release = active_release(runtime_root, manifest.app)
        result.append((lock_path, manifest, _owner(lock_path, manifest, source="active_runtime", release_id=release.get("release_id"))))
    return result


def _owner(path: Path, manifest: Manifest, source: str, release_id: object = None) -> Dict[str, object]:
    owner: Dict[str, object] = {
        "app": manifest.app,
        "environment": manifest.environment,
        "manifest_path": str(path),
        "source": source,
        # ``route_source`` mirrors ``source`` so route-conflict consumers
        # can tell a declared manifest claim apart from an active runtime claim
        # without re-deriving it.
        "route_source": source,
        "release_id": release_id if isinstance(release_id, str) else None,
    }
    # For an active runtime owner the lock path lives at
    # ``<runtime_root>/apps/<app>/manifest.lock.json``; expose that app dir so a
    # consumer can locate the live bundle without parsing Caddy text.
    if source == "active_runtime":
        owner["runtime_bundle_path"] = str(path.parent)
    return owner


def _normalize_domain(domain: str) -> str:
    return domain.strip().lower().rstrip(".")


def _route_match(route) -> str:
    if route.path:
        return f"path:{route.path.rstrip('/') or '/'}"
    if route.path_prefix:
        return f"path_prefix:{route.path_prefix.rstrip('/') or '/'}"
    return "default:*"


def _owner_keys(owners: List[Dict[str, object]]) -> set[Tuple[object, object, object]]:
    manifest_app_envs = {
        (owner.get("app"), owner.get("environment"))
        for owner in owners
        if owner.get("source") == "manifest"
    }
    keys: set[Tuple[object, object, object]] = set()
    for owner in owners:
        app_env = (owner.get("app"), owner.get("environment"))
        if owner.get("source") == "active_runtime" and app_env in manifest_app_envs:
            continue
        keys.add((owner.get("app"), owner.get("environment"), owner.get("manifest_path")))
    return keys


def _dedupe_active_runtime_owners(owners: List[Dict[str, object]]) -> List[Dict[str, object]]:
    manifest_app_envs = {
        (owner.get("app"), owner.get("environment"))
        for owner in owners
        if owner.get("source") == "manifest"
    }
    return [
        owner
        for owner in owners
        if not (
            owner.get("source") == "active_runtime"
            and (owner.get("app"), owner.get("environment")) in manifest_app_envs
        )
    ]
