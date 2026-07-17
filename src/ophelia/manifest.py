from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
import re
import shlex
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

from .validation import (
    CanonicalValidationError,
    HostPathCapability,
    HTTPPathKind,
    SourceRoot,
    parse_domain,
    parse_environment,
    parse_http_path,
    parse_identifier,
    resolve_host_path,
    resolve_source_path,
)


class ManifestError(ValueError):
    """Raised when a manifest is invalid."""


@dataclass(frozen=True)
class ManifestDiagnostic:
    """Immutable compatibility diagnostic attached to a loaded manifest."""

    severity: str
    code: str
    field: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "field": self.field,
            "message": self.message,
        }


@dataclass
class HealthCheck:
    path: str = "/health"
    interval: str = "10s"
    timeout: str = "5s"
    retries: int = 5
    command: List[str] = field(default_factory=list)


@dataclass
class MountConfig:
    source: str
    target: str
    read_only: bool = True
    bind: bool = False


@dataclass
class VerificationCheck:
    url: Optional[str] = None
    path: Optional[str] = None
    method: str = "GET"
    expect_status: int = 200
    contains: Optional[str] = None
    name: Optional[str] = None
    type: str = "http"
    service: Optional[str] = None
    command: Optional[List[str]] = None
    expect_exit: Optional[int] = None
    expect_json: Optional[Dict[str, Any]] = None
    json_assertions: List[Any] = field(default_factory=list)


@dataclass
class VerificationPolicy:
    attempts: int = 12
    interval: float = 5.0
    timeout: float = 10.0
    failure_mode: str = "hard"


@dataclass
class ConsoleConfig:
    admin_domain: Optional[str] = None
    console_asset_path: Optional[str] = None
    surface: str = "console"


@dataclass
class PackConfig:
    portability: Optional[str] = None
    owner: Optional[str] = None
    description: Optional[str] = None
    deploy_binding_file: Optional[str] = None


@dataclass
class HostRequirementsConfig:
    arch: Optional[str] = None
    min_memory: Optional[str] = None
    min_disk_free: Optional[str] = None
    requires_edge: Optional[bool] = None
    requires_docker: Optional[bool] = None


@dataclass
class NetworkingConfig:
    edge: str = "shared"
    internal: str = "shared"


@dataclass
class DataServiceConfig:
    mode: str
    inferred_from_addon: bool = False
    database: Optional[str] = None
    service: Optional[str] = None
    class_name: Optional[str] = None
    durable: Optional[bool] = None
    export: Dict[str, Any] = field(default_factory=dict)
    import_config: Dict[str, Any] = field(default_factory=dict)
    verify: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DataVolumeConfig:
    name: str
    mount: Optional[str] = None
    source: Optional[str] = None
    service: Optional[str] = None
    class_name: Optional[str] = None
    export: Any = None
    import_config: Any = None
    verify: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OffsiteBackupConfig:
    provider: Optional[str] = None
    target: Optional[str] = None
    retention_days: Optional[int] = None
    encryption_required: Optional[bool] = None
    restore_rehearsal_cadence_days: Optional[int] = None
    last_rehearsal_ref: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DataBackupsConfig:
    required: bool = False
    restore_drill_required: bool = False
    offsite_required: bool = False
    offsite: Optional[OffsiteBackupConfig] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LifecycleConfig:
    live: bool = True
    data_can_be_reset: bool = False
    production_apply_allowed: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DataConfig:
    postgres: Optional[DataServiceConfig] = None
    redis: Optional[DataServiceConfig] = None
    volumes: List[DataVolumeConfig] = field(default_factory=list)
    object_storage: List[Dict[str, Any]] = field(default_factory=list)
    static_assets: List[Dict[str, Any]] = field(default_factory=list)
    external_services: List[Dict[str, Any]] = field(default_factory=list)
    backups: Optional[DataBackupsConfig] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HooksConfig:
    pre_export: Optional[str] = None
    freeze: Optional[str] = None
    unfreeze: Optional[str] = None
    post_import: Optional[str] = None
    post_cutover: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ObservabilityHealth:
    url: Optional[str] = None
    expect_status: int = 200


@dataclass
class ObservabilityMetrics:
    url: Optional[str] = None
    format: str = "prometheus"
    auth: str = "none"


@dataclass
class ObservabilityLogs:
    containers: bool = True
    retain_days: int = 14


@dataclass
class ObservabilityConfig:
    health: Optional[ObservabilityHealth] = None
    metrics: Optional[ObservabilityMetrics] = None
    logs: Optional[ObservabilityLogs] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ServiceConfig:
    name: str
    port: int
    host_port: Optional[int] = None
    image: Optional[str] = None
    command: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    env_files: List[str] = field(default_factory=list)
    mounts: List[MountConfig] = field(default_factory=list)
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
class OnDemandTLSConfig:
    ask: str


@dataclass
class EdgeTLSConfig:
    mode: str = "auto"
    cert_file: Optional[str] = None
    key_file: Optional[str] = None


@dataclass
class CatchAllEdgeConfig:
    service: Optional[str] = None
    upstream: Optional[str] = None
    http_redirect: bool = True
    http_redirect_status: int = 308


@dataclass
class ResponseHeaderConfig:
    name: str
    value: str
    path: Optional[str] = None
    path_prefix: Optional[str] = None
    exclude_paths: List[str] = field(default_factory=list)
    exclude_path_prefixes: List[str] = field(default_factory=list)


@dataclass
class EdgeConfig:
    on_demand_tls: Optional[OnDemandTLSConfig] = None
    tls: Optional[EdgeTLSConfig] = None
    catch_all: Optional[CatchAllEdgeConfig] = None
    response_headers: List[ResponseHeaderConfig] = field(default_factory=list)


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
    environment: Optional[str]
    profile: Optional[str]
    image: Optional[str]
    services: Dict[str, ServiceConfig]
    routes: List[RouteConfig]
    addons: Addons = field(default_factory=Addons)
    resources: Resources = field(default_factory=Resources)
    env: Dict[str, str] = field(default_factory=dict)
    required_env: List[str] = field(default_factory=list)
    env_files: List[str] = field(default_factory=list)
    edge: EdgeConfig = field(default_factory=EdgeConfig)
    static_root: Optional[str] = None
    tunnel_target: Optional[str] = None
    redirect_to: Optional[str] = None
    redirect_status: int = 308
    verify: List[VerificationCheck] = field(default_factory=list)
    verify_policy: VerificationPolicy = field(default_factory=VerificationPolicy)
    console: Optional[ConsoleConfig] = None
    pack: PackConfig = field(default_factory=PackConfig)
    host_requirements: HostRequirementsConfig = field(default_factory=HostRequirementsConfig)
    networking: NetworkingConfig = field(default_factory=NetworkingConfig)
    data: DataConfig = field(default_factory=DataConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    lifecycle: LifecycleConfig = field(default_factory=LifecycleConfig)
    depends_on: List[str] = field(default_factory=list)
    deployment_order: Optional[int] = None
    migration_before: List[str] = field(default_factory=list)
    verify_before_next: bool = False
    diagnostics: Tuple[ManifestDiagnostic, ...] = field(default_factory=tuple)

    def service_alias(self, service_name: str) -> str:
        return f"{self.app}-{service_name}"

    def to_lock_dict(self) -> Dict[str, Any]:
        return _export_manifest_value(self)


def load_manifest(
    path: Path,
    source_root: Optional[Union[Path, SourceRoot]] = None,
    host_path_capability: Optional[HostPathCapability] = None,
) -> Manifest:
    path = Path(path).expanduser()
    yaml = _load_yaml()

    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ManifestError(f"Manifest not found: {path}") from exc
    except yaml.YAMLError as exc:
        # Surface malformed YAML as a ManifestError (path only, no file content)
        # so per-file isolation in callers like manifest_registry works and a
        # single bad file cannot blank an entire scan.
        raise ManifestError(f"Manifest is not valid YAML: {path}") from exc

    if not isinstance(raw, dict):
        raise ManifestError("Manifest root must be a mapping.")

    version = _require_int(raw, "version")
    if version != 1:
        raise ManifestError(
            f"Unsupported manifest version `{version}`; only version 1 is supported."
        )

    diagnostics = _collect_unknown_key_diagnostics(raw)
    app = _identifier_value(raw.get("app"), "app")
    kind = _require_str(raw, "kind")
    environment = _optional_environment(raw.get("environment"))
    profile = _optional_profile(raw.get("profile"))
    image = _optional_str(raw.get("image"), "image")

    env = _mapping_as_str_dict(raw.get("env", {}), "env")
    required_env = _string_list(raw.get("required_env", []), "required_env")
    env_files = _string_list(raw.get("env_files", []), "env_files")
    addons = _parse_addons(raw.get("addons", {}))
    resources = _parse_resources(raw.get("resources", {}))
    routes = _parse_routes(raw.get("routes", []))
    services = _parse_services(raw.get("services", {}))
    edge = _parse_edge(raw.get("edge", {}))
    verify = _parse_verifications(raw.get("verify", []))
    verify_policy = _parse_verification_policy(raw.get("verify_policy", {}))
    console = _parse_console(raw.get("console"))
    pack = _parse_pack(raw.get("pack"))
    host_requirements = _parse_host_requirements(raw.get("host_requirements"))
    networking = _parse_networking(raw.get("networking"))
    data = _parse_data(raw.get("data"), addons)
    hooks = _parse_hooks(raw.get("hooks"))
    observability = _parse_observability(raw.get("observability"))
    lifecycle = _parse_lifecycle(raw.get("lifecycle"))

    manifest = Manifest(
        version=version,
        app=app,
        kind=kind,
        environment=environment,
        profile=profile,
        image=image,
        services=services,
        routes=routes,
        addons=addons,
        resources=resources,
        env=env,
        required_env=required_env,
        env_files=env_files,
        edge=edge,
        static_root=_optional_str(raw.get("static_root"), "static_root"),
        tunnel_target=_optional_str(raw.get("tunnel_target"), "tunnel_target"),
        redirect_to=_optional_str(raw.get("redirect_to"), "redirect_to"),
        redirect_status=_optional_int(raw.get("redirect_status"), "redirect_status") or 308,
        verify=verify,
        verify_policy=verify_policy,
        console=console,
        pack=pack,
        host_requirements=host_requirements,
        networking=networking,
        data=data,
        hooks=hooks,
        observability=observability,
        lifecycle=lifecycle,
        depends_on=_identifier_list(raw.get("depends_on", []), "depends_on"),
        deployment_order=_optional_int(raw.get("deployment_order"), "deployment_order"),
        migration_before=_string_list(raw.get("migration_before", []), "migration_before"),
        verify_before_next=_as_bool(raw.get("verify_before_next", False), "verify_before_next"),
        diagnostics=diagnostics,
    )

    _validate_manifest(manifest)
    # Generated locks reopen canonical render state after source files have been
    # staged into a release bundle. Only that exact filename skips source
    # authority checks; ordinary JSON manifests are validated like YAML.
    if path.name != "manifest.lock.json":
        authority = _source_root(source_root, path)
        _validate_source_paths(
            manifest,
            path.parent.resolve(strict=False),
            authority,
            host_path_capability,
        )
    return manifest


_TOP_LEVEL_KEYS = frozenset(
    {
        "version",
        "app",
        "kind",
        "environment",
        "profile",
        "image",
        "services",
        "routes",
        "addons",
        "resources",
        "env",
        "required_env",
        "env_files",
        "edge",
        "static_root",
        "tunnel_target",
        "redirect_to",
        "redirect_status",
        "verify",
        "verify_policy",
        "console",
        "pack",
        "host_requirements",
        "networking",
        "data",
        "hooks",
        "observability",
        "lifecycle",
        "depends_on",
        "deployment_order",
        "migration_before",
        "verify_before_next",
    }
)

_FIXED_MAPPING_KEYS = {
    "addons": frozenset({"postgres", "redis"}),
    "resources": frozenset({"memory"}),
    "edge": frozenset({"on_demand_tls", "tls", "catch_all", "response_headers"}),
    "edge.on_demand_tls": frozenset({"ask"}),
    "edge.tls": frozenset({"mode", "cert_file", "key_file"}),
    "edge.catch_all": frozenset(
        {"service", "upstream", "http_redirect", "http_redirect_status"}
    ),
    "verify_policy": frozenset({"attempts", "interval", "timeout", "failure_mode"}),
    "console": frozenset({"admin_domain", "console_asset_path", "surface"}),
    "pack": frozenset({"portability", "owner", "description", "deploy_binding_file"}),
    "host_requirements": frozenset(
        {"arch", "min_memory", "min_disk_free", "requires_edge", "requires_docker"}
    ),
    "networking": frozenset({"edge", "internal"}),
    "data": frozenset(
        {
            "postgres",
            "redis",
            "volumes",
            "object_storage",
            "static_assets",
            "external_services",
            "backups",
        }
    ),
    "data.backups": frozenset(
        {"required", "restore_drill_required", "offsite_required", "offsite"}
    ),
    "data.backups.offsite": frozenset(
        {
            "provider",
            "target",
            "retention_days",
            "encryption_required",
            "restore_rehearsal_cadence_days",
            "last_rehearsal_ref",
        }
    ),
    "hooks": frozenset(
        {"pre_export", "freeze", "unfreeze", "post_import", "post_cutover"}
    ),
    "observability": frozenset({"health", "metrics", "logs"}),
    "observability.health": frozenset({"url", "expect_status"}),
    "observability.metrics": frozenset({"url", "format", "auth"}),
    "observability.logs": frozenset({"containers", "retain_days"}),
    "lifecycle": frozenset(
        {"live", "data_can_be_reset", "production_apply_allowed"}
    ),
}

_SERVICE_KEYS = frozenset(
    {"port", "host_port", "image", "command", "env", "env_files", "mounts", "healthcheck"}
)
_HEALTHCHECK_KEYS = frozenset({"path", "interval", "timeout", "retries", "command"})
_MOUNT_KEYS = frozenset({"source", "target", "read_only", "bind"})
_ROUTE_KEYS = frozenset(
    {
        "domain",
        "service",
        "upstream",
        "path",
        "path_prefix",
        "strip_prefix",
        "rewrite_prefix",
    }
)
_RESPONSE_HEADER_KEYS = frozenset(
    {"name", "value", "path", "path_prefix", "exclude_paths", "exclude_path_prefixes"}
)
_VERIFY_KEYS = frozenset(
    {
        "url",
        "path",
        "method",
        "expect_status",
        "contains",
        "name",
        "type",
        "service",
        "command",
        "expect_exit",
        "expect_json",
        "json_assertions",
    }
)
_DATA_SERVICE_KEYS = frozenset(
    {
        "mode",
        "inferred_from_addon",
        "database",
        "service",
        "class",
        "durable",
        "export",
        "import",
        "verify",
    }
)
_DATA_VOLUME_KEYS = frozenset(
    {"name", "mount", "source", "service", "class", "export", "import", "verify"}
)


def _collect_unknown_key_diagnostics(
    raw: Dict[str, Any],
) -> Tuple[ManifestDiagnostic, ...]:
    diagnostics: List[ManifestDiagnostic] = []
    _diagnose_mapping(raw, _TOP_LEVEL_KEYS, "", diagnostics)

    for field_name, known in _FIXED_MAPPING_KEYS.items():
        value = _mapping_at_path(raw, field_name)
        _diagnose_mapping(value, known, field_name, diagnostics)

    services = raw.get("services")
    if isinstance(services, dict):
        for service_name, service_raw in services.items():
            prefix = f"services.{service_name}"
            _diagnose_mapping(service_raw, _SERVICE_KEYS, prefix, diagnostics)
            if not isinstance(service_raw, dict):
                continue
            _diagnose_mapping(
                service_raw.get("healthcheck"),
                _HEALTHCHECK_KEYS,
                f"{prefix}.healthcheck",
                diagnostics,
            )
            mounts = service_raw.get("mounts")
            if isinstance(mounts, list):
                for index, mount in enumerate(mounts):
                    _diagnose_mapping(
                        mount,
                        _MOUNT_KEYS,
                        f"{prefix}.mounts[{index}]",
                        diagnostics,
                    )

    routes = raw.get("routes")
    if isinstance(routes, list):
        for index, route in enumerate(routes):
            _diagnose_mapping(route, _ROUTE_KEYS, f"routes[{index}]", diagnostics)

    edge = raw.get("edge")
    if isinstance(edge, dict):
        response_headers = edge.get("response_headers")
        if isinstance(response_headers, list):
            for index, header in enumerate(response_headers):
                _diagnose_mapping(
                    header,
                    _RESPONSE_HEADER_KEYS,
                    f"edge.response_headers[{index}]",
                    diagnostics,
                )

    verify = raw.get("verify")
    if isinstance(verify, list):
        for index, check in enumerate(verify):
            _diagnose_mapping(check, _VERIFY_KEYS, f"verify[{index}]", diagnostics)

    data = raw.get("data")
    if isinstance(data, dict):
        for name in ("postgres", "redis"):
            _diagnose_mapping(
                data.get(name),
                _DATA_SERVICE_KEYS,
                f"data.{name}",
                diagnostics,
            )
        volumes = data.get("volumes")
        if isinstance(volumes, list):
            for index, volume in enumerate(volumes):
                _diagnose_mapping(
                    volume,
                    _DATA_VOLUME_KEYS,
                    f"data.volumes[{index}]",
                    diagnostics,
                )

    return tuple(
        sorted(
            diagnostics,
            key=lambda item: (item.field, item.code, item.message),
        )
    )


def _mapping_at_path(raw: Dict[str, Any], path: str) -> Any:
    value: Any = raw
    for component in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(component)
    return value


def _diagnose_mapping(
    raw: Any,
    known: frozenset,
    prefix: str,
    diagnostics: List[ManifestDiagnostic],
) -> None:
    if not isinstance(raw, dict):
        return
    for key in raw:
        if not isinstance(key, str) or key in known:
            continue
        field_name = f"{prefix}.{key}" if prefix else key
        diagnostics.append(
            ManifestDiagnostic(
                severity="warning",
                code="unknown_key",
                field=field_name,
                message=f"`{field_name}` is not recognized by manifest version 1; no v1 behavior is implied.",
            )
        )


def _identifier_value(value: Any, field_name: str) -> str:
    try:
        return parse_identifier(value, field=field_name).value
    except CanonicalValidationError as exc:
        raise ManifestError(exc.issue.message) from exc


def _identifier_list(value: Any, field_name: str) -> List[str]:
    return [
        _identifier_value(item, f"{field_name}[{index}]")
        for index, item in enumerate(_string_list(value, field_name))
    ]


def _optional_identifier(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return _identifier_value(value, field_name)


def _optional_domain(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return _domain_value(value, field_name)


def _domain_value(value: Any, field_name: str) -> str:
    try:
        return parse_domain(value, field=field_name).value
    except CanonicalValidationError as exc:
        raise ManifestError(exc.issue.message) from exc


def _optional_http_path(
    value: Any,
    field_name: str,
    *,
    kind: HTTPPathKind = HTTPPathKind.EXACT,
) -> Optional[str]:
    if value is None:
        return None
    try:
        return parse_http_path(value, field=field_name, kind=kind).value
    except CanonicalValidationError as exc:
        raise ManifestError(exc.issue.message) from exc


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
        service_name = _identifier_value(name, "services.%s" % name)
        if not isinstance(service_raw, dict):
            raise ManifestError(f"Service `{service_name}` must be a mapping.")

        prefix = "services.%s" % service_name
        port = _require_int(service_raw, "port", prefix=prefix)
        host_port = _optional_int(service_raw.get("host_port"), prefix + ".host_port")
        image = _optional_str(service_raw.get("image"), prefix + ".image")
        command = _string_list(service_raw.get("command", []), prefix + ".command")
        env = _mapping_as_str_dict(service_raw.get("env", {}), prefix + ".env")
        env_files = _string_list(
            service_raw.get("env_files", []), prefix + ".env_files"
        )
        mounts = _parse_mounts(service_raw.get("mounts", []), service_name)
        health = _parse_healthcheck(service_raw.get("healthcheck", {}), service_name)

        services[service_name] = ServiceConfig(
            name=service_name,
            port=port,
            host_port=host_port,
            image=image,
            command=command,
            env=env,
            env_files=env_files,
            mounts=mounts,
            healthcheck=health,
        )
    return services

def _optional_environment(value: Any) -> Optional[str]:
    try:
        parsed = parse_environment(value, field="environment")
    except CanonicalValidationError as exc:
        raise ManifestError(exc.issue.message) from exc
    return parsed.value if parsed is not None else None

def _parse_routes(raw: Any) -> List[RouteConfig]:
    if not isinstance(raw, list) or not raw:
        raise ManifestError("`routes` must be a non-empty list.")

    routes: List[RouteConfig] = []
    for index, route_raw in enumerate(raw):
        if not isinstance(route_raw, dict):
            raise ManifestError("Route at index %s must be a mapping." % index)

        prefix = "routes[%s]" % index
        routes.append(
            RouteConfig(
                domain=_domain_value(route_raw.get("domain"), prefix + ".domain"),
                service=_optional_identifier(
                    route_raw.get("service"), prefix + ".service"
                ),
                upstream=_optional_str(route_raw.get("upstream"), prefix + ".upstream"),
                path=_optional_http_path(route_raw.get("path"), prefix + ".path"),
                path_prefix=_optional_http_path(
                    route_raw.get("path_prefix"),
                    prefix + ".path_prefix",
                    kind=HTTPPathKind.PREFIX,
                ),
                strip_prefix=_optional_http_path(
                    route_raw.get("strip_prefix"),
                    prefix + ".strip_prefix",
                    kind=HTTPPathKind.PREFIX,
                ),
                rewrite_prefix=_optional_http_path(
                    route_raw.get("rewrite_prefix"), prefix + ".rewrite_prefix"
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


def _parse_edge(raw: Any) -> EdgeConfig:
    if raw is None:
        return EdgeConfig()
    if not isinstance(raw, dict):
        raise ManifestError("`edge` must be a mapping.")

    on_demand_tls = _parse_on_demand_tls(raw.get("on_demand_tls"))
    tls = _parse_edge_tls(raw.get("tls"))
    catch_all = _parse_catch_all_edge(raw.get("catch_all"))
    response_headers = _parse_response_headers(raw.get("response_headers"))
    return EdgeConfig(
        on_demand_tls=on_demand_tls,
        tls=tls,
        catch_all=catch_all,
        response_headers=response_headers,
    )


def _parse_response_headers(raw: Any) -> List[ResponseHeaderConfig]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestError("`edge.response_headers` must be a list.")
    if len(raw) > 64:
        raise ManifestError("`edge.response_headers` may contain at most 64 entries.")

    headers: List[ResponseHeaderConfig] = []
    forbidden_names = {
        "connection",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
    for index, item in enumerate(raw):
        prefix = f"edge.response_headers[{index}]"
        if not isinstance(item, dict):
            raise ManifestError(f"`{prefix}` must be a mapping.")

        name = _require_str(item, "name", prefix=prefix)
        if re.fullmatch(r"[0-9A-Za-z][!#$%&'*+.^_`|~0-9A-Za-z-]{0,127}", name) is None:
            raise ManifestError(
                f"`{prefix}.name` must be a valid HTTP field name that starts with a letter or digit."
            )
        if name.lower() in forbidden_names:
            raise ManifestError(f"`{prefix}.name` may not set the hop-by-hop header `{name}`.")

        value = _require_str(item, "value", prefix=prefix)
        if len(value) > 8192:
            raise ManifestError(f"`{prefix}.value` may not exceed 8192 characters.")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ManifestError(f"`{prefix}.value` contains a control character.")
        if "{" in value or "}" in value:
            raise ManifestError(f"`{prefix}.value` may not contain Caddy placeholders.")

        path = _optional_http_path(item.get("path"), f"{prefix}.path")
        path_prefix = _optional_http_path(
            item.get("path_prefix"),
            f"{prefix}.path_prefix",
            kind=HTTPPathKind.PREFIX,
        )
        if path is not None and path_prefix is not None:
            raise ManifestError(f"`{prefix}` may not set both `path` and `path_prefix`.")
        exclude_paths = _parse_http_path_list(
            item.get("exclude_paths"),
            f"{prefix}.exclude_paths",
        )
        exclude_path_prefixes = _parse_http_path_list(
            item.get("exclude_path_prefixes"),
            f"{prefix}.exclude_path_prefixes",
            kind=HTTPPathKind.PREFIX,
        )
        headers.append(ResponseHeaderConfig(
            name=name,
            value=value,
            path=path,
            path_prefix=path_prefix,
            exclude_paths=exclude_paths,
            exclude_path_prefixes=exclude_path_prefixes,
        ))
    return headers


def _parse_http_path_list(
    raw: Any,
    field_name: str,
    *,
    kind: HTTPPathKind = HTTPPathKind.EXACT,
) -> List[str]:
    paths = []
    for index, value in enumerate(_string_list([] if raw is None else raw, field_name)):
        path = _optional_http_path(value, f"{field_name}[{index}]", kind=kind)
        assert path is not None
        paths.append(path)
    if len(paths) != len(set(paths)):
        raise ManifestError(f"`{field_name}` may not contain duplicate paths.")
    return paths


def _parse_on_demand_tls(raw: Any) -> Optional[OnDemandTLSConfig]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError("`edge.on_demand_tls` must be a mapping.")

    ask = _require_str(raw, "ask", prefix="edge.on_demand_tls")
    if not ask.startswith(("http://", "https://")):
        raise ManifestError("`edge.on_demand_tls.ask` must start with `http://` or `https://`.")
    return OnDemandTLSConfig(ask=ask)


def _parse_edge_tls(raw: Any) -> Optional[EdgeTLSConfig]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError("`edge.tls` must be a mapping.")

    mode = _optional_str(raw.get("mode"), "edge.tls.mode") or "auto"
    if mode not in {"auto", "internal", "custom"}:
        raise ManifestError("`edge.tls.mode` must be one of `auto`, `internal`, or `custom`.")

    cert_file = _optional_str(raw.get("cert_file"), "edge.tls.cert_file")
    key_file = _optional_str(raw.get("key_file"), "edge.tls.key_file")
    if cert_file or key_file:
        if not cert_file or not key_file:
            raise ManifestError("`edge.tls.cert_file` and `edge.tls.key_file` must be set together.")
        if mode == "auto":
            mode = "custom"
        elif mode != "custom":
            raise ManifestError("`edge.tls` certificate files require `mode: custom`.")

    if mode == "custom" and (not cert_file or not key_file):
        raise ManifestError("`edge.tls.mode: custom` requires `cert_file` and `key_file`.")

    return EdgeTLSConfig(mode=mode, cert_file=cert_file, key_file=key_file)


def _parse_catch_all_edge(raw: Any) -> Optional[CatchAllEdgeConfig]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError("`edge.catch_all` must be a mapping.")

    status = _optional_int(raw.get("http_redirect_status"), "edge.catch_all.http_redirect_status") or 308
    return CatchAllEdgeConfig(
        service=_optional_identifier(raw.get("service"), "edge.catch_all.service"),
        upstream=_optional_str(raw.get("upstream"), "edge.catch_all.upstream"),
        http_redirect=_as_bool(raw.get("http_redirect", True), "edge.catch_all.http_redirect"),
        http_redirect_status=status,
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


def _parse_mounts(raw: Any, service_name: str) -> List[MountConfig]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestError(f"`services.{service_name}.mounts` must be a list.")

    mounts: List[MountConfig] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ManifestError(f"`services.{service_name}.mounts[{index}]` must be a mapping.")

        source = _require_str(item, "source", prefix=f"services.{service_name}.mounts[{index}]")
        target = _optional_str(item.get("target"), f"services.{service_name}.mounts[{index}].target")
        if target is None or not target.startswith("/"):
            raise ManifestError(
                f"`services.{service_name}.mounts[{index}].target` must start with `/`."
            )
        read_only = item.get("read_only", True)
        if not isinstance(read_only, bool):
            raise ManifestError(
                f"`services.{service_name}.mounts[{index}].read_only` must be a boolean."
            )
        bind = item.get("bind", False)
        if not isinstance(bind, bool):
            raise ManifestError(
                f"`services.{service_name}.mounts[{index}].bind` must be a boolean."
            )
        mounts.append(MountConfig(source=source, target=target, read_only=read_only, bind=bind))
    return mounts


def _parse_verifications(raw: Any) -> List[VerificationCheck]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestError("`verify` must be a list.")

    checks: List[VerificationCheck] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ManifestError(f"`verify[{index}]` must be a mapping.")

        check_type = _optional_str(item.get("type"), f"verify[{index}].type")
        if check_type is None:
            if item.get("command") is not None:
                check_type = "command"
            elif item.get("path") is not None:
                check_type = "internal"
            else:
                check_type = "http"
        if check_type not in {"http", "command", "internal"}:
            raise ManifestError(f"`verify[{index}].type` must be `http`, `command`, or `internal`.")
        url = _optional_str(item.get("url"), f"verify[{index}].url")
        if check_type == "http":
            if url is None or not url.startswith(("http://", "https://")):
                raise ManifestError(f"`verify[{index}].url` must start with `http://` or `https://`.")
            parsed_url = urlparse(url)
            if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
                raise ManifestError(
                    f"`verify[{index}].url` must not contain credentials, query strings, or fragments."
                )
        elif check_type == "internal" and url is not None:
            parsed_url = _parse_internal_loopback_url(url, f"verify[{index}].url")
        elif url is not None:
            raise ManifestError(f"`verify[{index}].url` is only supported for `type: http` or loopback `type: internal` checks.")

        service = _optional_identifier(item.get("service"), f"verify[{index}].service")
        path = _optional_path(item.get("path"), f"verify[{index}].path")
        if check_type == "internal" and url is not None:
            url_path = parsed_url.path or "/"
            if path is not None and path != url_path:
                raise ManifestError(f"`verify[{index}].path` must match the path in `verify[{index}].url`.")
            path = path or url_path
        method = _optional_http_method(item.get("method"), f"verify[{index}].method") or "GET"
        command = _optional_command(item.get("command"), f"verify[{index}].command")
        if check_type == "command" and (service is None or not command):
            raise ManifestError(f"`verify[{index}]` command checks require `service` and `command`.")
        if check_type == "http" and command:
            raise ManifestError(f"`verify[{index}].command` is only supported for `type: command` checks.")
        if check_type == "http" and path is not None:
            raise ManifestError(f"`verify[{index}].path` is only supported for `type: internal` checks.")
        if check_type == "command" and path is not None:
            raise ManifestError(f"`verify[{index}].path` is only supported for `type: internal` checks.")
        if check_type == "internal" and (service is None or path is None):
            raise ManifestError(f"`verify[{index}]` internal checks require `service` and `path`.")
        if check_type == "internal" and command:
            raise ManifestError(f"`verify[{index}].command` is only supported for `type: command` checks.")

        expect_exit = _optional_exit_code(item.get("expect_exit"), f"verify[{index}].expect_exit")
        if check_type == "command" and expect_exit is None:
            expect_exit = 0
        expect_status = item.get("expect_status", 200)
        if not isinstance(expect_status, int) or expect_status < 100 or expect_status > 599:
            raise ManifestError(f"`verify[{index}].expect_status` must be a valid HTTP status code.")
        contains = _optional_str(item.get("contains"), f"verify[{index}].contains")
        name = _optional_str(item.get("name"), f"verify[{index}].name")
        expect_json = _parse_expect_json(item.get("expect_json"), f"verify[{index}].expect_json")
        json_assertions = _parse_json_assertions(item.get("json_assertions"), f"verify[{index}].json_assertions")
        checks.append(
            VerificationCheck(
                url=url,
                path=path,
                method=method,
                expect_status=expect_status,
                contains=contains,
                name=name,
                type=check_type,
                service=service,
                command=command,
                expect_exit=expect_exit,
                expect_json=expect_json,
                json_assertions=json_assertions,
            )
        )
    return checks


def _parse_verification_policy(raw: Any) -> VerificationPolicy:
    if raw is None:
        return VerificationPolicy()
    if not isinstance(raw, dict):
        raise ManifestError("`verify_policy` must be a mapping.")

    attempts = _optional_int(raw.get("attempts"), "verify_policy.attempts")
    if attempts is not None and attempts < 1:
        raise ManifestError("`verify_policy.attempts` must be at least 1.")

    interval = _optional_float(raw.get("interval"), "verify_policy.interval")
    if interval is not None and interval < 0:
        raise ManifestError("`verify_policy.interval` must be 0 or greater.")

    timeout = _optional_float(raw.get("timeout"), "verify_policy.timeout")
    if timeout is not None and timeout <= 0:
        raise ManifestError("`verify_policy.timeout` must be greater than 0.")

    failure_mode = _optional_str(raw.get("failure_mode"), "verify_policy.failure_mode") or "hard"
    if failure_mode not in {"hard", "warn"}:
        raise ManifestError("`verify_policy.failure_mode` must be `hard` or `warn`.")

    return VerificationPolicy(
        attempts=attempts or 12,
        interval=5.0 if interval is None else interval,
        timeout=10.0 if timeout is None else timeout,
        failure_mode=failure_mode,
    )


def _parse_console(raw: Any) -> Optional[ConsoleConfig]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError("`console` must be a mapping.")

    surface = _optional_str(raw.get("surface"), "console.surface") or "console"
    if surface not in {"console", "root"}:
        raise ManifestError("`console.surface` must be `console` or `root`.")

    return ConsoleConfig(
        admin_domain=_optional_domain(raw.get("admin_domain"), "console.admin_domain"),
        console_asset_path=_optional_str(raw.get("console_asset_path"), "console.console_asset_path"),
        surface=surface,
    )


def _parse_pack(raw: Any) -> PackConfig:
    if raw is None:
        return PackConfig()
    if not isinstance(raw, dict):
        raise ManifestError("`pack` must be a mapping.")

    portability = _optional_str(raw.get("portability"), "pack.portability")
    if portability is not None and portability not in {"critical", "standard", "static"}:
        raise ManifestError("`pack.portability` must be `critical`, `standard`, or `static`.")

    return PackConfig(
        portability=portability,
        owner=_optional_str(raw.get("owner"), "pack.owner"),
        description=_optional_str(raw.get("description"), "pack.description"),
        deploy_binding_file=_optional_str(
            raw.get("deploy_binding_file"), "pack.deploy_binding_file"
        ),
    )


def _parse_host_requirements(raw: Any) -> HostRequirementsConfig:
    if raw is None:
        return HostRequirementsConfig()
    if not isinstance(raw, dict):
        raise ManifestError("`host_requirements` must be a mapping.")

    return HostRequirementsConfig(
        arch=_optional_str(raw.get("arch"), "host_requirements.arch"),
        min_memory=_optional_str(raw.get("min_memory"), "host_requirements.min_memory"),
        min_disk_free=_optional_str(
            raw.get("min_disk_free"), "host_requirements.min_disk_free"
        ),
        requires_edge=_optional_bool(raw.get("requires_edge"), "host_requirements.requires_edge"),
        requires_docker=_optional_bool(
            raw.get("requires_docker"), "host_requirements.requires_docker"
        ),
    )


def _parse_networking(raw: Any) -> NetworkingConfig:
    if raw is None:
        return NetworkingConfig()
    if not isinstance(raw, dict):
        raise ManifestError("`networking` must be a mapping.")

    edge = _optional_str(raw.get("edge"), "networking.edge") or "shared"
    internal = _optional_str(raw.get("internal"), "networking.internal") or "shared"
    if edge != "shared":
        raise ManifestError("`networking.edge` must be `shared`.")
    if internal not in {"shared", "per-app"}:
        raise ManifestError("`networking.internal` must be `shared` or `per-app`.")
    return NetworkingConfig(edge=edge, internal=internal)


def _parse_data(raw: Any, addons: Addons) -> DataConfig:
    if raw is None:
        data = DataConfig()
    else:
        if not isinstance(raw, dict):
            raise ManifestError("`data` must be a mapping.")
        known = {
            "postgres",
            "redis",
            "volumes",
            "object_storage",
            "static_assets",
            "external_services",
            "backups",
        }
        data = DataConfig(
            postgres=_parse_data_service(
                raw.get("postgres"),
                "data.postgres",
                default_mode="shared-postgres-database",
            ),
            redis=_parse_data_service(
                raw.get("redis"),
                "data.redis",
                default_mode="redis-logical-db",
            ),
            volumes=_parse_data_volumes(raw.get("volumes", [])),
            object_storage=_parse_list_of_mappings(
                raw.get("object_storage", []), "data.object_storage"
            ),
            static_assets=_parse_list_of_mappings(
                raw.get("static_assets", []), "data.static_assets"
            ),
            external_services=_parse_list_of_mappings(
                raw.get("external_services", []), "data.external_services"
            ),
            backups=_parse_data_backups(raw.get("backups")),
            extra={key: _json_compatible(value, f"data.{key}") for key, value in raw.items() if key not in known},
        )

    if data.postgres is None and addons.postgres:
        data.postgres = DataServiceConfig(
            mode="shared-postgres-database",
            inferred_from_addon=True,
        )
    if data.redis is None and addons.redis:
        data.redis = DataServiceConfig(
            mode="redis-logical-db",
            inferred_from_addon=True,
        )
    return data


def _parse_data_service(raw: Any, field_name: str, default_mode: str) -> Optional[DataServiceConfig]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError(f"`{field_name}` must be a mapping.")

    known = {
        "mode",
        "inferred_from_addon",
        "database",
        "service",
        "class",
        "durable",
        "export",
        "import",
        "verify",
    }
    return DataServiceConfig(
        mode=_optional_str(raw.get("mode"), f"{field_name}.mode") or default_mode,
        inferred_from_addon=_as_bool(raw.get("inferred_from_addon", False), f"{field_name}.inferred_from_addon"),
        database=_optional_str(raw.get("database"), f"{field_name}.database"),
        service=_optional_identifier(raw.get("service"), f"{field_name}.service"),
        class_name=_optional_str(raw.get("class"), f"{field_name}.class"),
        durable=_optional_bool(raw.get("durable"), f"{field_name}.durable"),
        export=_parse_optional_mapping(raw.get("export"), f"{field_name}.export"),
        import_config=_parse_optional_mapping(raw.get("import"), f"{field_name}.import"),
        verify=_parse_optional_mapping(raw.get("verify"), f"{field_name}.verify"),
        extra={
            key: _json_compatible(value, f"{field_name}.{key}")
            for key, value in raw.items()
            if key not in known
        },
    )


def _parse_data_volumes(raw: Any) -> List[DataVolumeConfig]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestError("`data.volumes` must be a list.")

    volumes: List[DataVolumeConfig] = []
    for index, item in enumerate(raw):
        field_name = f"data.volumes[{index}]"
        if not isinstance(item, dict):
            raise ManifestError(f"`{field_name}` must be a mapping.")
        known = {"name", "mount", "source", "service", "class", "export", "import", "verify"}
        volumes.append(
            DataVolumeConfig(
                name=_identifier_value(item.get("name"), f"{field_name}.name"),
                mount=_optional_str(item.get("mount"), f"{field_name}.mount"),
                source=_optional_str(item.get("source"), f"{field_name}.source"),
                service=_optional_identifier(item.get("service"), f"{field_name}.service"),
                class_name=_optional_str(item.get("class"), f"{field_name}.class"),
                export=_parse_optional_data_action(item.get("export"), f"{field_name}.export"),
                import_config=_parse_optional_data_action(item.get("import"), f"{field_name}.import"),
                verify=_parse_optional_mapping(item.get("verify"), f"{field_name}.verify"),
                extra={
                    key: _json_compatible(value, f"{field_name}.{key}")
                    for key, value in item.items()
                    if key not in known
                },
            )
        )
    return volumes


def _parse_data_backups(raw: Any) -> Optional[DataBackupsConfig]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError("`data.backups` must be a mapping.")
    known = {"required", "restore_drill_required", "offsite_required", "offsite"}
    return DataBackupsConfig(
        required=_as_bool(raw.get("required", False), "data.backups.required"),
        restore_drill_required=_as_bool(
            raw.get("restore_drill_required", False), "data.backups.restore_drill_required"
        ),
        offsite_required=_as_bool(
            raw.get("offsite_required", False), "data.backups.offsite_required"
        ),
        offsite=_parse_offsite_backup(raw.get("offsite")),
        extra={
            key: _json_compatible(value, f"data.backups.{key}")
            for key, value in raw.items()
            if key not in known
        },
    )


def _parse_offsite_backup(raw: Any) -> Optional[OffsiteBackupConfig]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError("`data.backups.offsite` must be a mapping.")
    known = {
        "provider",
        "target",
        "retention_days",
        "encryption_required",
        "restore_rehearsal_cadence_days",
        "last_rehearsal_ref",
    }
    return OffsiteBackupConfig(
        provider=_optional_str(raw.get("provider"), "data.backups.offsite.provider"),
        target=_optional_offsite_target(raw.get("target"), "data.backups.offsite.target"),
        retention_days=_optional_int(raw.get("retention_days"), "data.backups.offsite.retention_days"),
        encryption_required=_optional_bool(raw.get("encryption_required"), "data.backups.offsite.encryption_required"),
        restore_rehearsal_cadence_days=_optional_int(
            raw.get("restore_rehearsal_cadence_days"),
            "data.backups.offsite.restore_rehearsal_cadence_days",
        ),
        last_rehearsal_ref=_optional_str(raw.get("last_rehearsal_ref"), "data.backups.offsite.last_rehearsal_ref"),
        extra={
            key: _json_compatible(value, f"data.backups.offsite.{key}")
            for key, value in raw.items()
            if key not in known
        },
    )


def _parse_lifecycle(raw: Any) -> LifecycleConfig:
    if raw is None:
        return LifecycleConfig()
    if not isinstance(raw, dict):
        raise ManifestError("`lifecycle` must be a mapping.")
    known = {"live", "data_can_be_reset", "production_apply_allowed"}
    return LifecycleConfig(
        live=_as_bool(raw.get("live", True), "lifecycle.live"),
        data_can_be_reset=_as_bool(raw.get("data_can_be_reset", False), "lifecycle.data_can_be_reset"),
        production_apply_allowed=_as_bool(
            raw.get("production_apply_allowed", True),
            "lifecycle.production_apply_allowed",
        ),
        extra={
            key: _json_compatible(value, f"lifecycle.{key}")
            for key, value in raw.items()
            if key not in known
        },
    )


def _parse_hooks(raw: Any) -> HooksConfig:
    if raw is None:
        return HooksConfig()
    if not isinstance(raw, dict):
        raise ManifestError("`hooks` must be a mapping.")

    known = {"pre_export", "freeze", "unfreeze", "post_import", "post_cutover"}
    return HooksConfig(
        pre_export=_optional_str(raw.get("pre_export"), "hooks.pre_export"),
        freeze=_optional_str(raw.get("freeze"), "hooks.freeze"),
        unfreeze=_optional_str(raw.get("unfreeze"), "hooks.unfreeze"),
        post_import=_optional_str(raw.get("post_import"), "hooks.post_import"),
        post_cutover=_optional_str(raw.get("post_cutover"), "hooks.post_cutover"),
        extra={
            key: _json_compatible(value, f"hooks.{key}")
            for key, value in raw.items()
            if key not in known
        },
    )


def _parse_observability(raw: Any) -> ObservabilityConfig:
    if raw is None:
        return ObservabilityConfig()
    if not isinstance(raw, dict):
        raise ManifestError("`observability` must be a mapping.")

    known = {"health", "metrics", "logs"}
    return ObservabilityConfig(
        health=_parse_observability_health(raw.get("health")),
        metrics=_parse_observability_metrics(raw.get("metrics")),
        logs=_parse_observability_logs(raw.get("logs")),
        extra={
            key: _json_compatible(value, f"observability.{key}")
            for key, value in raw.items()
            if key not in known
        },
    )


def _parse_observability_health(raw: Any) -> Optional[ObservabilityHealth]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError("`observability.health` must be a mapping.")

    url = _optional_str(raw.get("url"), "observability.health.url")
    if url is not None:
        # Reuse the shared health-URL validator so the rule cannot drift.
        from .observability import validate_health_url

        error = validate_health_url(url)
        if error is not None:
            raise ManifestError(f"`observability.health.url` {error}")

    expect_status = raw.get("expect_status", 200)
    if not isinstance(expect_status, int) or isinstance(expect_status, bool) or not 100 <= expect_status <= 599:
        raise ManifestError("`observability.health.expect_status` must be a valid HTTP status code.")
    return ObservabilityHealth(url=url, expect_status=expect_status)


def _parse_observability_metrics(raw: Any) -> Optional[ObservabilityMetrics]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError("`observability.metrics` must be a mapping.")

    url = _optional_str(raw.get("url"), "observability.metrics.url")
    if url is not None:
        from .observability import validate_health_url

        error = validate_health_url(url)
        if error is not None:
            raise ManifestError(f"`observability.metrics.url` {error}")

    fmt = _optional_str(raw.get("format"), "observability.metrics.format") or "prometheus"
    if fmt not in {"prometheus", "json", "none"}:
        raise ManifestError("`observability.metrics.format` must be one of `prometheus`, `json`, or `none`.")

    auth = _optional_str(raw.get("auth"), "observability.metrics.auth") or "none"
    if auth not in {"none", "bearer_env", "basic_env"}:
        raise ManifestError("`observability.metrics.auth` must be one of `none`, `bearer_env`, or `basic_env`.")
    return ObservabilityMetrics(url=url, format=fmt, auth=auth)


def _parse_observability_logs(raw: Any) -> Optional[ObservabilityLogs]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError("`observability.logs` must be a mapping.")

    containers = _as_bool(raw.get("containers", True), "observability.logs.containers")
    retain_days = raw.get("retain_days", 14)
    if not isinstance(retain_days, int) or isinstance(retain_days, bool) or retain_days < 1:
        raise ManifestError("`observability.logs.retain_days` must be a positive integer.")
    return ObservabilityLogs(containers=containers, retain_days=retain_days)


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

    if manifest.profile not in {None, "console"}:
        raise ManifestError("`profile` must be omitted or set to `console`.")

    _validate_env_key_names(manifest)

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

    if manifest.profile == "console":
        if manifest.kind not in {"service", "multi-service"}:
            raise ManifestError("`profile: console` requires a service-based manifest.")
        if manifest.console is None:
            raise ManifestError("`profile: console` requires a `console` mapping.")
        if manifest.console.admin_domain and not any(route.service or route.upstream for route in manifest.routes):
            raise ManifestError("Console manifests need at least one proxy route target.")

    if manifest.kind in {"service", "multi-service", "tunnel"}:
        _validate_proxy_routes(manifest)
    else:
        _validate_non_proxy_routes(manifest)

    _validate_edge(manifest)
    _validate_data_volumes(manifest)
    _validate_verification_checks(manifest)


def _validate_data_volumes(manifest: Manifest) -> None:
    names: Dict[str, int] = {}
    service_count = len(manifest.services)

    for index, volume in enumerate(manifest.data.volumes):
        field_name = f"data.volumes[{index}]"
        previous = names.get(volume.name)
        if previous is not None:
            raise ManifestError(
                f"`{field_name}.name` duplicates `data.volumes[{previous}].name`."
            )
        names[volume.name] = index

        if volume.mount is not None and not volume.mount.startswith("/"):
            raise ManifestError(f"`{field_name}.mount` must start with `/`.")

        if volume.service is not None and volume.service not in manifest.services:
            raise ManifestError(
                f"`{field_name}.service` references unknown service `{volume.service}`."
            )

        if volume.mount is not None:
            if manifest.kind not in {"service", "multi-service"}:
                raise ManifestError(
                    f"`{field_name}.mount` requires a service-based manifest."
                )
            if service_count > 1 and volume.service is None:
                raise ManifestError(
                    f"`{field_name}.service` is required when mounting a data volume in a multi-service manifest."
                )


def _validate_verification_checks(manifest: Manifest) -> None:
    for index, check in enumerate(manifest.verify):
        field_name = f"verify[{index}]"
        if check.type in {"command", "internal"}:
            if manifest.kind not in {"service", "multi-service"}:
                raise ManifestError(f"`{field_name}` {check.type} checks require a service-based manifest.")
            if check.service == "app" and len(manifest.services) == 1:
                continue
            if check.service not in manifest.services:
                raise ManifestError(f"`{field_name}.service` references unknown service `{check.service}`.")


def _validate_edge(manifest: Manifest) -> None:
    seen_headers: Dict[tuple[str, str, str], int] = {}
    for index, header in enumerate(manifest.edge.response_headers):
        selector_kind = "path" if header.path is not None else "path_prefix" if header.path_prefix is not None else "global"
        selector = header.path or header.path_prefix or "*"
        key = (header.name.lower(), selector_kind, selector)
        if key in seen_headers:
            raise ManifestError(
                f"`edge.response_headers[{index}]` duplicates `edge.response_headers[{seen_headers[key]}]` "
                f"for `{header.name}` and {selector_kind} `{selector}`."
            )
        seen_headers[key] = index

    catch_all = manifest.edge.catch_all
    if catch_all is None:
        return

    if manifest.kind not in {"service", "multi-service", "tunnel"}:
        raise ManifestError("`edge.catch_all` requires a proxy-capable manifest kind.")
    if manifest.edge.on_demand_tls is None:
        raise ManifestError("`edge.catch_all` requires `edge.on_demand_tls.ask`.")
    if catch_all.service and catch_all.upstream:
        raise ManifestError("`edge.catch_all` may not set both `service` and `upstream`.")
    if not catch_all.service and not catch_all.upstream:
        raise ManifestError("`edge.catch_all` requires `service` or `upstream`.")
    if catch_all.service and catch_all.service not in manifest.services:
        raise ManifestError(f"`edge.catch_all.service` references unknown service `{catch_all.service}`.")
    if catch_all.http_redirect_status not in {301, 302, 307, 308}:
        raise ManifestError("`edge.catch_all.http_redirect_status` must be one of 301, 302, 307, or 308.")


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


def _validate_env_key_names(manifest: Manifest) -> None:
    for field_name, keys in (
        ("env", manifest.env.keys()),
        ("required_env", manifest.required_env),
    ):
        for key in keys:
            if not _is_env_key_name(key):
                raise ManifestError(f"`{field_name}` contains invalid env key `{key}`.")
    for service_name, service in manifest.services.items():
        for key in service.env:
            if not _is_env_key_name(key):
                raise ManifestError(f"`services.{service_name}.env` contains invalid env key `{key}`.")


def _is_env_key_name(value: str) -> bool:
    if not value:
        return False
    first = value[0]
    if not (first == "_" or first.isalpha()):
        return False
    return all(char == "_" or char.isalnum() for char in value)


def _source_root(
    explicit: Optional[Union[Path, SourceRoot]],
    manifest_path: Path,
) -> SourceRoot:
    if isinstance(explicit, SourceRoot):
        return explicit
    if explicit is not None:
        try:
            return SourceRoot.from_path(explicit)
        except CanonicalValidationError as exc:
            raise ManifestError(exc.issue.message) from exc

    manifest_dir = manifest_path.resolve(strict=False).parent
    for candidate in (manifest_dir,) + tuple(manifest_dir.parents):
        if (candidate / ".git").exists():
            return SourceRoot.from_path(candidate)
    return SourceRoot.from_path(manifest_dir)


def _validate_source_paths(
    manifest: Manifest,
    manifest_dir: Path,
    source_root: SourceRoot,
    capability: Optional[HostPathCapability],
) -> None:
    _resolve_manifest_source(
        source_root,
        manifest_dir,
        ".",
        "manifest",
        capability,
        require_exists=True,
    )

    for index, source in enumerate(manifest.env_files):
        _resolve_manifest_source(
            source_root,
            manifest_dir,
            source,
            f"env_files[{index}]",
            capability,
            require_exists=True,
        )

    for service_name, service in manifest.services.items():
        for index, source in enumerate(service.env_files):
            _resolve_manifest_source(
                source_root,
                manifest_dir,
                source,
                f"services.{service_name}.env_files[{index}]",
                capability,
                require_exists=True,
            )
        for index, mount in enumerate(service.mounts):
            _resolve_manifest_source(
                source_root,
                manifest_dir,
                mount.source,
                f"services.{service_name}.mounts[{index}].source",
                capability,
                require_exists=True,
            )



def _resolve_manifest_source(
    source_root: SourceRoot,
    manifest_dir: Path,
    source: str,
    field_name: str,
    capability: Optional[HostPathCapability],
    *,
    require_exists: bool,
) -> Path:
    if any(ord(character) < 32 or ord(character) == 127 for character in source):
        raise ManifestError("%s contains a control character." % field_name)

    try:
        expanded = Path(source).expanduser()
    except (KeyError, RuntimeError) as exc:
        raise ManifestError("%s could not be expanded." % field_name) from exc

    try:
        if expanded.is_absolute():
            if capability is None:
                raise ManifestError(
                    "%s is absolute and requires an explicit HostPathCapability."
                    % field_name
                )
            resolved = resolve_host_path(
                capability,
                expanded,
                field=field_name,
            ).path
        else:
            resolved = resolve_source_path(
                source_root,
                source,
                relative_to=manifest_dir,
                field=field_name,
            ).path
    except CanonicalValidationError as exc:
        raise ManifestError(exc.issue.message) from exc

    if require_exists and not resolved.exists():
        raise ManifestError(
            "%s points to missing source %s." % (field_name, source)
        )
    return resolved

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


def _parse_internal_loopback_url(value: str, field_name: str):
    parsed = urlparse(value)
    if parsed.scheme != "http":
        raise ManifestError(f"`{field_name}` for `type: internal` must use `http://` loopback URLs.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ManifestError(f"`{field_name}` must not contain credentials, query strings, or fragments.")
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ManifestError(f"`{field_name}` for `type: internal` must be a loopback URL or use `path`.")
    if parsed.path and not parsed.path.startswith("/"):
        raise ManifestError(f"`{field_name}` path must start with `/`.")
    return parsed


def _optional_http_method(value: Any, field_name: str) -> Optional[str]:
    method = _optional_str(value, field_name)
    if method is None:
        return None
    normalized = method.upper()
    if not normalized.replace("-", "").isalpha():
        raise ManifestError(f"`{field_name}` must be an HTTP method token.")
    return normalized


def _optional_int(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ManifestError(f"`{field_name}` must be a positive integer.")
    return value


def _optional_exit_code(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        raise ManifestError(f"`{field_name}` must be an integer from 0 to 255.")
    return value


def _optional_float(value: Any, field_name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestError(f"`{field_name}` must be a number.")
    return float(value)


def _optional_profile(value: Any) -> Optional[str]:
    return _optional_str(value, "profile")


def _require_int(raw: Dict[str, Any], field_name: str, prefix: str = "") -> int:
    value = raw.get(field_name)
    label = f"{prefix}.{field_name}" if prefix else field_name
    return _require_int_value(value, label)


def _require_int_value(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
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


def _optional_command(value: Any, field_name: str) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            raise ManifestError(f"`{field_name}` must be a non-empty string or list.")
        try:
            items = shlex.split(value)
        except ValueError as exc:
            raise ManifestError(f"`{field_name}` must be shell-tokenizable.") from exc
        if not items:
            raise ManifestError(f"`{field_name}` must not be empty.")
        return items
    if not isinstance(value, list):
        raise ManifestError(f"`{field_name}` must be a non-empty string or list.")
    items: List[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ManifestError(f"`{field_name}` must contain non-empty strings.")
        items.append(item)
    if not items:
        raise ManifestError(f"`{field_name}` must not be empty.")
    return items


def _parse_expect_json(value: Any, field_name: str) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ManifestError(f"`{field_name}` must be a mapping of JSON field paths to expected values.")
    return {
        _mapping_key(key, field_name): _json_compatible(item, f"{field_name}.{key}")
        for key, item in value.items()
    }


def _parse_json_assertions(value: Any, field_name: str) -> List[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ManifestError(f"`{field_name}` must be a list of assertion strings or mappings.")
    assertions: List[Any] = []
    for index, item in enumerate(value):
        item_field = f"{field_name}[{index}]"
        if isinstance(item, str):
            if not item.strip():
                raise ManifestError(f"`{item_field}` must be a non-empty assertion string.")
            assertions.append(item)
            continue
        if isinstance(item, dict):
            assertions.append(
                {
                    _mapping_key(key, item_field): _json_compatible(assertion_value, f"{item_field}.{key}")
                    for key, assertion_value in item.items()
                }
            )
            continue
        raise ManifestError(f"`{item_field}` must be a string or mapping.")
    return assertions


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


def _parse_optional_mapping(value: Any, field_name: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ManifestError(f"`{field_name}` must be a mapping.")
    return {
        _mapping_key(key, field_name): _json_compatible(item, f"{field_name}.{key}")
        for key, item in value.items()
    }


def _parse_optional_data_action(value: Any, field_name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            raise ManifestError(f"`{field_name}` must be a non-empty string or mapping.")
        return value
    if isinstance(value, dict):
        return _parse_optional_mapping(value, field_name)
    raise ManifestError(f"`{field_name}` must be a non-empty string or mapping.")


def _parse_list_of_mappings(value: Any, field_name: str) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [_parse_optional_mapping(value, field_name)]
    if not isinstance(value, list):
        raise ManifestError(f"`{field_name}` must be a list.")

    result: List[Dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ManifestError(f"`{field_name}[{index}]` must be a mapping.")
        result.append(_parse_optional_mapping(item, f"{field_name}[{index}]"))
    return result


def _mapping_key(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"`{field_name}` keys must be non-empty strings.")
    return value


def _json_compatible(value: Any, field_name: str) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_compatible(item, f"{field_name}[]") for item in value]
    if isinstance(value, dict):
        return {
            _mapping_key(key, field_name): _json_compatible(item, f"{field_name}.{key}")
            for key, item in value.items()
        }
    raise ManifestError(f"`{field_name}` must be JSON-compatible.")


def _optional_offsite_target(value: Any, field_name: str) -> Optional[str]:
    target = _optional_str(value, field_name)
    if target is None:
        return None
    parsed = urlparse(target)
    if parsed.scheme and (parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ManifestError(
            f"`{field_name}` must not contain credentials, query strings, or fragments."
        )
    return target


def _optional_bool(value: Any, field_name: str) -> Optional[bool]:
    if value is None:
        return None
    return _as_bool(value, field_name)


def _as_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestError(f"`{field_name}` must be a boolean.")
    return value


def _export_manifest_value(value: Any) -> Any:
    if is_dataclass(value):
        result: Dict[str, Any] = {}
        for item in fields(value):
            if item.name == "diagnostics":
                continue
            exported = _export_manifest_value(getattr(value, item.name))
            if item.name == "extra":
                if isinstance(exported, dict):
                    result.update(exported)
                continue
            if exported is None:
                continue
            key = item.name
            if key == "import_config":
                key = "import"
            elif key == "class_name":
                key = "class"
            result[key] = exported
        return result
    if isinstance(value, dict):
        return {key: _export_manifest_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_export_manifest_value(item) for item in value]
    return value
