from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


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
class MountConfig:
    source: str
    target: str
    read_only: bool = True
    bind: bool = False


@dataclass
class VerificationCheck:
    url: str
    expect_status: int = 200
    contains: Optional[str] = None
    name: Optional[str] = None


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
class DataBackupsConfig:
    required: bool = False
    restore_drill_required: bool = False
    offsite_required: bool = False
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
class EdgeConfig:
    on_demand_tls: Optional[OnDemandTLSConfig] = None
    tls: Optional[EdgeTLSConfig] = None
    catch_all: Optional[CatchAllEdgeConfig] = None


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
    depends_on: List[str] = field(default_factory=list)
    deployment_order: Optional[int] = None
    migration_before: List[str] = field(default_factory=list)
    verify_before_next: bool = False

    def service_alias(self, service_name: str) -> str:
        return f"{self.app}-{service_name}"

    def to_lock_dict(self) -> Dict[str, Any]:
        return _export_manifest_value(self)


def load_manifest(path: Path) -> Manifest:
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
    app = _require_str(raw, "app")
    kind = _require_str(raw, "kind")
    environment = _optional_environment(raw.get("environment"))
    profile = _optional_profile(raw.get("profile"))
    image = _optional_str(raw.get("image"), "image")

    env = _mapping_as_str_dict(raw.get("env", {}), "env")
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
        depends_on=_string_list(raw.get("depends_on", []), "depends_on"),
        deployment_order=_optional_int(raw.get("deployment_order"), "deployment_order"),
        migration_before=_string_list(raw.get("migration_before", []), "migration_before"),
        verify_before_next=_as_bool(raw.get("verify_before_next", False), "verify_before_next"),
    )

    _validate_manifest(manifest)
    if path.suffix != ".json":
        _validate_source_paths(manifest, path.parent)
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
        env_files = _string_list(service_raw.get("env_files", []), f"services.{name}.env_files")
        mounts = _parse_mounts(service_raw.get("mounts", []), name)
        health = _parse_healthcheck(service_raw.get("healthcheck", {}), name)

        services[name] = ServiceConfig(
            name=name,
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
    environment = _optional_str(value, "environment")
    if environment is None:
        return None
    if environment not in {"dev", "staging", "production"}:
        raise ManifestError("`environment` must be one of `dev`, `staging`, or `production`.")
    return environment


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


def _parse_edge(raw: Any) -> EdgeConfig:
    if raw is None:
        return EdgeConfig()
    if not isinstance(raw, dict):
        raise ManifestError("`edge` must be a mapping.")

    on_demand_tls = _parse_on_demand_tls(raw.get("on_demand_tls"))
    tls = _parse_edge_tls(raw.get("tls"))
    catch_all = _parse_catch_all_edge(raw.get("catch_all"))
    return EdgeConfig(on_demand_tls=on_demand_tls, tls=tls, catch_all=catch_all)


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
        service=_optional_str(raw.get("service"), "edge.catch_all.service"),
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

        url = _optional_str(item.get("url"), f"verify[{index}].url")
        if url is None or not url.startswith(("http://", "https://")):
            raise ManifestError(f"`verify[{index}].url` must start with `http://` or `https://`.")
        parsed_url = urlparse(url)
        if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
            raise ManifestError(
                f"`verify[{index}].url` must not contain credentials, query strings, or fragments."
            )
        expect_status = item.get("expect_status", 200)
        if not isinstance(expect_status, int) or expect_status < 100 or expect_status > 599:
            raise ManifestError(f"`verify[{index}].expect_status` must be a valid HTTP status code.")
        contains = _optional_str(item.get("contains"), f"verify[{index}].contains")
        name = _optional_str(item.get("name"), f"verify[{index}].name")
        checks.append(
            VerificationCheck(
                url=url,
                expect_status=expect_status,
                contains=contains,
                name=name,
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
        admin_domain=_optional_str(raw.get("admin_domain"), "console.admin_domain"),
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
        service=_optional_str(raw.get("service"), f"{field_name}.service"),
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
                name=_require_str(item, "name", prefix=field_name),
                mount=_optional_str(item.get("mount"), f"{field_name}.mount"),
                source=_optional_str(item.get("source"), f"{field_name}.source"),
                service=_optional_str(item.get("service"), f"{field_name}.service"),
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
    known = {"required", "restore_drill_required", "offsite_required"}
    return DataBackupsConfig(
        required=_as_bool(raw.get("required", False), "data.backups.required"),
        restore_drill_required=_as_bool(
            raw.get("restore_drill_required", False), "data.backups.restore_drill_required"
        ),
        offsite_required=_as_bool(
            raw.get("offsite_required", False), "data.backups.offsite_required"
        ),
        extra={
            key: _json_compatible(value, f"data.backups.{key}")
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


def _validate_edge(manifest: Manifest) -> None:
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


def _validate_source_paths(manifest: Manifest, manifest_dir: Path) -> None:
    for index, source in enumerate(manifest.env_files):
        _resolve_source_path(manifest_dir, source, f"env_files[{index}]")

    for service_name, service in manifest.services.items():
        for index, source in enumerate(service.env_files):
            _resolve_source_path(manifest_dir, source, f"services.{service_name}.env_files[{index}]")
        for index, mount in enumerate(service.mounts):
            _resolve_source_path(manifest_dir, mount.source, f"services.{service_name}.mounts[{index}].source")


def _resolve_source_path(manifest_dir: Path, source: str, field_name: str) -> Path:
    raw = Path(source).expanduser()
    resolved = raw if raw.is_absolute() else (manifest_dir / raw)
    if not resolved.exists():
        raise ManifestError(f"`{field_name}` points to missing source `{source}`.")
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


def _optional_int(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ManifestError(f"`{field_name}` must be a positive integer.")
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
