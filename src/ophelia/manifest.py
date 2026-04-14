from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class ManifestError(ValueError):
    """Raised when a manifest is invalid."""


@dataclass
class HealthCheck:
    path: str = "/health"
    interval: str = "10s"
    timeout: str = "5s"
    retries: int = 5
    command: List[str] = field(default_factory=list)


@dataclass
class ServiceConfig:
    name: str
    port: int
    host_port: Optional[int] = None
    image: Optional[str] = None
    command: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    healthcheck: HealthCheck = field(default_factory=HealthCheck)


@dataclass
class RouteConfig:
    domain: str
    service: Optional[str] = None
    upstream: Optional[str] = None
    path: Optional[str] = None
    path_prefix: Optional[str] = None
    strip_prefix: Optional[str] = None
    rewrite_prefix: Optional[str] = None


@dataclass
class Addons:
    postgres: bool = False
    redis: bool = False


@dataclass
class Resources:
    memory: str = "256m"


@dataclass
class Manifest:
    version: int
    app: str
    kind: str
    image: Optional[str]
    services: Dict[str, ServiceConfig]
    routes: List[RouteConfig]
    addons: Addons = field(default_factory=Addons)
    resources: Resources = field(default_factory=Resources)
    env: Dict[str, str] = field(default_factory=dict)
    static_root: Optional[str] = None
    tunnel_target: Optional[str] = None
    redirect_to: Optional[str] = None
    redirect_status: int = 308

    def service_alias(self, service_name: str) -> str:
        return f"{self.app}-{service_name}"

    def to_lock_dict(self) -> Dict[str, Any]:
        return asdict(self)


def load_manifest(path: Path) -> Manifest:
    yaml = _load_yaml()

    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ManifestError(f"Manifest not found: {path}") from exc

    if not isinstance(raw, dict):
        raise ManifestError("Manifest root must be a mapping.")

    version = _require_int(raw, "version")
    app = _require_str(raw, "app")
    kind = _require_str(raw, "kind")
    image = _optional_str(raw.get("image"), "image")

    env = _mapping_as_str_dict(raw.get("env", {}), "env")
    addons = _parse_addons(raw.get("addons", {}))
    resources = _parse_resources(raw.get("resources", {}))
    routes = _parse_routes(raw.get("routes", []))
    services = _parse_services(raw.get("services", {}))

    manifest = Manifest(
        version=version,
        app=app,
        kind=kind,
        image=image,
        services=services,
        routes=routes,
        addons=addons,
        resources=resources,
        env=env,
        static_root=_optional_str(raw.get("static_root"), "static_root"),
        tunnel_target=_optional_str(raw.get("tunnel_target"), "tunnel_target"),
        redirect_to=_optional_str(raw.get("redirect_to"), "redirect_to"),
        redirect_status=_optional_int(raw.get("redirect_status"), "redirect_status") or 308,
    )

    _validate_manifest(manifest)
    return manifest


def _load_yaml():
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise ManifestError(
            "PyYAML is required to read manifests. Run "
            "`python3 -m venv .venv && .venv/bin/python -m ensurepip --upgrade "
            "&& .venv/bin/python -m pip install PyYAML` from the "
            "ophelia repo."
        ) from exc
    return yaml


def _parse_services(raw: Any) -> Dict[str, ServiceConfig]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError("`services` must be a mapping.")

    services: Dict[str, ServiceConfig] = {}
    for name, service_raw in raw.items():
        if not isinstance(service_raw, dict):
            raise ManifestError(f"Service `{name}` must be a mapping.")

        port = _require_int(service_raw, "port", prefix=f"services.{name}")
        host_port = _optional_int(service_raw.get("host_port"), f"services.{name}.host_port")
        image = _optional_str(service_raw.get("image"), f"services.{name}.image")
        command = _string_list(service_raw.get("command", []), f"services.{name}.command")
        env = _mapping_as_str_dict(service_raw.get("env", {}), f"services.{name}.env")
        health = _parse_healthcheck(service_raw.get("healthcheck", {}), name)

        services[name] = ServiceConfig(
            name=name,
            port=port,
            host_port=host_port,
            image=image,
            command=command,
            env=env,
            healthcheck=health,
        )
    return services


def _parse_routes(raw: Any) -> List[RouteConfig]:
    if not isinstance(raw, list) or not raw:
        raise ManifestError("`routes` must be a non-empty list.")

    routes: List[RouteConfig] = []
    for index, route_raw in enumerate(raw):
        if not isinstance(route_raw, dict):
            raise ManifestError(f"Route at index {index} must be a mapping.")

        routes.append(
            RouteConfig(
                domain=_require_str(route_raw, "domain", prefix=f"routes[{index}]"),
                service=_optional_str(route_raw.get("service"), f"routes[{index}].service"),
                upstream=_optional_str(route_raw.get("upstream"), f"routes[{index}].upstream"),
                path=_optional_path(route_raw.get("path"), f"routes[{index}].path"),
                path_prefix=_optional_path(
                    route_raw.get("path_prefix"), f"routes[{index}].path_prefix"
                ),
                strip_prefix=_optional_path(
                    route_raw.get("strip_prefix"), f"routes[{index}].strip_prefix"
                ),
                rewrite_prefix=_optional_path(
                    route_raw.get("rewrite_prefix"), f"routes[{index}].rewrite_prefix"
                ),
            )
        )
    return routes


def _parse_addons(raw: Any) -> Addons:
    if raw is None:
        return Addons()
    if not isinstance(raw, dict):
        raise ManifestError("`addons` must be a mapping.")

    return Addons(
        postgres=_as_bool(raw.get("postgres", False), "addons.postgres"),
        redis=_as_bool(raw.get("redis", False), "addons.redis"),
    )


def _parse_resources(raw: Any) -> Resources:
    if raw is None:
        return Resources()
    if not isinstance(raw, dict):
        raise ManifestError("`resources` must be a mapping.")

    memory = raw.get("memory", "256m")
    if not isinstance(memory, str) or not memory:
        raise ManifestError("`resources.memory` must be a non-empty string.")
    return Resources(memory=memory)


def _parse_healthcheck(raw: Any, service_name: str) -> HealthCheck:
    if raw is None:
        return HealthCheck()
    if not isinstance(raw, dict):
        raise ManifestError(f"`services.{service_name}.healthcheck` must be a mapping.")

    path = raw.get("path", "/health")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ManifestError(
            f"`services.{service_name}.healthcheck.path` must start with `/`."
        )

    return HealthCheck(
        path=path,
        interval=_optional_str(raw.get("interval"), "healthcheck.interval") or "10s",
        timeout=_optional_str(raw.get("timeout"), "healthcheck.timeout") or "5s",
        retries=_require_int_value(raw.get("retries", 5), "healthcheck.retries"),
        command=_string_list(raw.get("command", []), "healthcheck.command"),
    )


def _validate_manifest(manifest: Manifest) -> None:
    allowed_kinds = {"service", "multi-service", "static", "tunnel", "redirect"}
    if manifest.kind not in allowed_kinds:
        raise ManifestError(
            f"`kind` must be one of {sorted(allowed_kinds)}, got `{manifest.kind}`."
        )

    if manifest.kind in {"service", "multi-service"}:
        if not manifest.services:
            raise ManifestError("Service-based apps require at least one service.")
        for service_name, service in manifest.services.items():
            if not service.image and not manifest.image:
                raise ManifestError(
                    f"Service `{service_name}` must define `image` or inherit a top-level `image`."
                )
        _validate_host_ports(manifest)

    if manifest.kind == "static":
        if manifest.services:
            raise ManifestError("Static apps may not declare `services`.")
        if manifest.image:
            raise ManifestError("Static apps may not declare a top-level `image`.")
    if manifest.kind == "redirect":
        if manifest.services:
            raise ManifestError("Redirect apps may not declare `services`.")
        if manifest.image:
            raise ManifestError("Redirect apps may not declare a top-level `image`.")
        if not manifest.redirect_to:
            raise ManifestError("Redirect apps require `redirect_to`.")
        if manifest.redirect_status not in {301, 302, 307, 308}:
            raise ManifestError("`redirect_status` must be one of 301, 302, 307, or 308.")

    if manifest.kind == "static" and not manifest.static_root:
        raise ManifestError("Static apps require `static_root`.")

    if manifest.kind == "tunnel" and not manifest.tunnel_target and not any(
        route.upstream for route in manifest.routes
    ):
        raise ManifestError("Tunnel apps require `tunnel_target` or per-route `upstream` values.")

    if manifest.kind in {"service", "multi-service", "tunnel"}:
        _validate_proxy_routes(manifest)
    else:
        _validate_non_proxy_routes(manifest)


def _validate_proxy_routes(manifest: Manifest) -> None:
    default_routes_by_domain: Dict[str, int] = {}
    seen_route_matches: Dict[tuple[str, str, str], None] = {}

    for route in manifest.routes:
        if route.service and route.upstream:
            raise ManifestError(
                f"Route for `{route.domain}` may not set both `service` and `upstream`."
            )
        if route.path and route.path_prefix:
            raise ManifestError(
                f"Route for `{route.domain}` may not set both `path` and `path_prefix`."
            )
        if route.path and route.strip_prefix:
            raise ManifestError(
                f"Route for `{route.domain}` may not use `strip_prefix` with exact `path`."
            )

        if route.service and route.service not in manifest.services:
            raise ManifestError(
                f"Route for `{route.domain}` references unknown service `{route.service}`."
            )
        if manifest.kind in {"service", "multi-service"} and not route.service and not route.upstream:
            raise ManifestError(
                f"Route for `{route.domain}` must include `service` or `upstream`."
            )
        if manifest.kind == "tunnel" and not route.service and not route.upstream and not manifest.tunnel_target:
            raise ManifestError(
                f"Route for `{route.domain}` must include `upstream`, `service`, or inherit `tunnel_target`."
            )

        if route.path is not None:
            route_key = (route.domain, "path", route.path)
        elif route.path_prefix is not None:
            route_key = (route.domain, "path_prefix", route.path_prefix)
        else:
            route_key = (route.domain, "default", "*")
            default_routes_by_domain[route.domain] = default_routes_by_domain.get(route.domain, 0) + 1

        if route_key in seen_route_matches:
            kind = "exact path" if route_key[1] == "path" else "path prefix" if route_key[1] == "path_prefix" else "catch-all"
            raise ManifestError(
                f"Duplicate {kind} route for `{route.domain}` with matcher `{route_key[2]}`."
            )
        seen_route_matches[route_key] = None

    duplicates = [domain for domain, count in default_routes_by_domain.items() if count > 1]
    if duplicates:
        joined = ", ".join(sorted(duplicates))
        raise ManifestError(f"Each domain may only have one catch-all route. Duplicate defaults: {joined}")


def _validate_non_proxy_routes(manifest: Manifest) -> None:
    for route in manifest.routes:
        if route.service or route.upstream:
            raise ManifestError(
                f"{manifest.kind.title()} apps may not declare `service` or `upstream` routes."
            )
        if route.path or route.path_prefix or route.strip_prefix or route.rewrite_prefix:
            raise ManifestError(
                f"{manifest.kind.title()} apps may not declare path-matching or rewrite route options."
            )


def _validate_host_ports(manifest: Manifest) -> None:
    host_ports: Dict[int, str] = {}
    for service_name, service in manifest.services.items():
        if service.host_port is None:
            continue
        owner = host_ports.get(service.host_port)
        if owner is not None:
            raise ManifestError(
                f"Services `{owner}` and `{service_name}` both request host_port {service.host_port}."
            )
        host_ports[service.host_port] = service_name


def _require_str(raw: Dict[str, Any], field_name: str, prefix: str = "") -> str:
    value = raw.get(field_name)
    label = f"{prefix}.{field_name}" if prefix else field_name
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"`{label}` must be a non-empty string.")
    return value


def _optional_str(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"`{field_name}` must be a non-empty string.")
    return value


def _optional_path(value: Any, field_name: str) -> Optional[str]:
    result = _optional_str(value, field_name)
    if result is None:
        return None
    if not result.startswith("/"):
        raise ManifestError(f"`{field_name}` must start with `/`.")
    return result


def _optional_int(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    if not isinstance(value, int) or value <= 0:
        raise ManifestError(f"`{field_name}` must be a positive integer.")
    return value


def _require_int(raw: Dict[str, Any], field_name: str, prefix: str = "") -> int:
    value = raw.get(field_name)
    label = f"{prefix}.{field_name}" if prefix else field_name
    return _require_int_value(value, label)


def _require_int_value(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise ManifestError(f"`{field_name}` must be a positive integer.")
    return value


def _string_list(value: Any, field_name: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ManifestError(f"`{field_name}` must be a list.")

    items: List[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ManifestError(f"`{field_name}` must contain non-empty strings.")
        items.append(item)
    return items


def _mapping_as_str_dict(value: Any, field_name: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ManifestError(f"`{field_name}` must be a mapping.")

    result: Dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ManifestError(f"`{field_name}` keys must be non-empty strings.")
        if not isinstance(item, (str, int, float, bool)):
            raise ManifestError(
                f"`{field_name}.{key}` must be a string, number, or boolean."
            )
        result[key] = str(item)
    return result


def _as_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestError(f"`{field_name}` must be a boolean.")
    return value
