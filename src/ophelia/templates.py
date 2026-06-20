from __future__ import annotations

import json
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import TEMPLATES_DIR
from .manifest import CatchAllEdgeConfig, Manifest, RouteConfig, ServiceConfig


def render_compose(manifest: Manifest) -> Optional[str]:
    if manifest.kind in {"static", "tunnel", "redirect"}:
        return None

    template = _read_template("compose/app.compose.tpl")
    services_block = "\n".join(_render_service_block(manifest, service) for service in manifest.services.values())
    networks_block = _render_networks_block(manifest)
    return (
        template.replace("{{SERVICES_BLOCK}}", services_block.rstrip())
        .replace("{{NETWORKS_BLOCK}}", networks_block.rstrip())
    )


def compose_network_summary(manifest: Manifest) -> Dict[str, Any]:
    internal_name = internal_network_name(manifest)
    return {
        "edge": "ophelia-edge",
        "internal": internal_name,
        "internal_mode": manifest.networking.internal,
        "edge_mode": manifest.networking.edge,
        "internal_external": manifest.networking.internal == "shared",
        "current_compatibility": manifest.networking.internal == "shared",
    }


def internal_network_name(manifest: Manifest) -> str:
    if manifest.networking.internal == "per-app":
        environment = manifest.environment or "default"
        return f"{manifest.app}-{environment}-internal"
    return "ophelia-internal"


def render_caddy(manifest: Manifest) -> str:
    template = _read_template("caddy/site.caddy.tpl")
    blocks = [
        _render_site_block(manifest, domain, routes)
        for domain, routes in _group_routes_by_domain(manifest).items()
    ]
    if manifest.edge.catch_all is not None:
        blocks.append(_render_catch_all_edge_block(manifest, manifest.edge.catch_all))
    sites_block = "\n\n".join(blocks)
    return template.replace("{{SITES_BLOCK}}", sites_block.rstrip()) + "\n"


def render_caddy_global(manifest: Manifest) -> Optional[str]:
    if manifest.edge.on_demand_tls is None:
        return None

    lines = [
        "on_demand_tls {",
        f"    ask {manifest.edge.on_demand_tls.ask}",
        "}",
    ]
    return "\n".join(lines) + "\n"


def render_env_example(manifest: Manifest) -> str:
    lines = [
        f"# Generated env template for {manifest.app}",
        f"OPHELIA_APP={manifest.app}",
    ]

    for key, value in sorted(manifest.env.items()):
        lines.append(f"{key}={value}")

    if manifest.addons.postgres:
        lines.append("DATABASE_URL=postgres://replace-me:replace-me@postgres:5432/replace-me")

    if manifest.addons.redis:
        lines.append("REDIS_URL=redis://default:replace-me@redis:6379/0")

    service_secret_keys = _collect_service_env_keys(manifest)
    for key in service_secret_keys:
        if key not in manifest.env:
            lines.append(f"{key}=replace-me")

    for key in caddy_env_keys(manifest):
        if key not in manifest.env and key not in service_secret_keys:
            lines.append(f"{key}=replace-me")

    if manifest.profile == "prism" and manifest.prism is not None:
        lines.extend(
            [
                "",
                "# Prism profile defaults",
                f"PRISM_CONSOLE_SURFACE={manifest.prism.surface}",
                "PRISM_CONSOLE_SETUP_TOKEN=replace-me",
                "PRISM_MFA_ENCRYPTION_KEY=replace-me",
                "PRISM_CREDENTIAL_ENCRYPTION_KEY=replace-me",
            ]
        )
        if manifest.prism.console_asset_path:
            lines.append(f"PRISM_CONSOLE_ASSET_PATH={manifest.prism.console_asset_path}")
        if manifest.prism.admin_domain:
            lines.append(f"# Dedicated Prism admin host routed by Ophelia: {manifest.prism.admin_domain}")

    return "\n".join(lines) + "\n"


def _render_service_block(manifest: Manifest, service: ServiceConfig) -> str:
    service_lines: List[str] = [
        f"  {service.name}:",
        f"    image: {_quote(service.image or manifest.image or '')}",
        "    restart: unless-stopped",
        "    env_file:",
        "      - ./env",
    ]

    for index, source in enumerate(manifest.env_files):
        service_lines.append(f"      - {_quote(f'./{bundle_env_file_path(source, index=index)}')}")
    for index, source in enumerate(service.env_files):
        service_lines.append(
            f"      - {_quote(f'./{bundle_env_file_path(source, service_name=service.name, index=index)}')}"
        )

    service_lines.extend(
        [
            "    environment:",
            f"      OPHELIA_APP: {_quote(manifest.app)}",
            f"      OPHELIA_SERVICE: {_quote(service.name)}",
            f"      PORT: {_quote(str(service.port))}",
        ]
    )

    env_map = OrderedDict(sorted(manifest.env.items()))
    env_map.update(OrderedDict(sorted(service.env.items())))

    for key, value in env_map.items():
        service_lines.append(f"      {key}: {_quote(value)}")

    service_lines.extend(
        [
            "    expose:",
            f"      - {_quote(str(service.port))}",
        ]
    )

    if service.host_port is not None:
        service_lines.extend(
            [
                "    ports:",
                f"      - {_quote(f'127.0.0.1:{service.host_port}:{service.port}')}",
            ]
        )

    if service.mounts:
        service_lines.append("    volumes:")
        for index, mount in enumerate(service.mounts):
            bundle_source = (
                mount.source
                if mount.bind
                else f"./{bundle_mount_path(service.name, mount.source, index)}"
            )
            suffix = ":ro" if mount.read_only else ""
            service_lines.append(f"      - {_quote(f'{bundle_source}:{mount.target}{suffix}')}")

    if service.command:
        service_lines.append("    command:")
        for item in service.command:
            service_lines.append(f"      - {_quote(item)}")

    service_lines.extend(_render_healthcheck_block(service))
    internal_network = internal_network_name(manifest)
    service_lines.extend(
        [
            f"    mem_limit: {_quote(manifest.resources.memory)}",
            "    networks:",
            "      ophelia-edge:",
            "        aliases:",
            f"          - {_quote(manifest.service_alias(service.name))}",
            f"      {internal_network}:",
            "        aliases:",
            f"          - {_quote(manifest.service_alias(service.name))}",
        ]
    )
    if manifest.networking.internal == "per-app":
        service_lines.append(f"          - {_quote(service.name)}")
    return "\n".join(service_lines)


def _render_networks_block(manifest: Manifest) -> str:
    lines = [
        "  ophelia-edge:",
        "    external: true",
    ]
    internal_network = internal_network_name(manifest)
    if manifest.networking.internal == "per-app":
        lines.extend(
            [
                f"  {internal_network}:",
                f"    name: {_quote(internal_network)}",
                "    labels:",
                f"      ophelia.app: {_quote(manifest.app)}",
                f"      ophelia.environment: {_quote(manifest.environment or 'unknown')}",
            ]
        )
    else:
        lines.extend(
            [
                "  ophelia-internal:",
                "    external: true",
            ]
        )
    return "\n".join(lines)


def _render_healthcheck_block(service: ServiceConfig) -> List[str]:
    healthcheck = service.healthcheck
    command = healthcheck.command or [
        "CMD-SHELL",
        (
            "if command -v curl >/dev/null 2>&1; then "
            f"curl -fsS http://127.0.0.1:{service.port}{healthcheck.path} >/dev/null; "
            "elif command -v wget >/dev/null 2>&1; then "
            f"wget -qO- http://127.0.0.1:{service.port}{healthcheck.path} >/dev/null; "
            "else "
            "exit 1; "
            "fi"
        ),
    ]
    rendered_command = ", ".join(_quote(item) for item in command)
    return [
        "    healthcheck:",
        f"      test: [{rendered_command}]",
        f"      interval: {_quote(healthcheck.interval)}",
        f"      timeout: {_quote(healthcheck.timeout)}",
        f"      retries: {healthcheck.retries}",
    ]


def _render_site_block(manifest: Manifest, domain: str, routes: List[RouteConfig]) -> str:
    lines = [
        f"{domain} {{",
        "    encode zstd gzip",
        "    header {",
        '        Strict-Transport-Security "max-age=31536000; includeSubDomains"',
        "        X-Content-Type-Options nosniff",
        "        X-Frame-Options DENY",
        "        Referrer-Policy strict-origin-when-cross-origin",
        "    }",
    ]

    if manifest.kind == "static":
        lines.extend(
            [
                f"    root * {manifest.static_root}",
                "    file_server",
                "}",
            ]
        )
        return "\n".join(lines)

    if manifest.kind == "tunnel":
        for index, route in enumerate(_sort_routes(routes)):
            _render_proxy_route(lines, manifest, route, domain, index)
        lines.append("}")
        return "\n".join(lines)

    if manifest.kind == "redirect":
        lines.extend(
            [
                f"    redir {manifest.redirect_to} {manifest.redirect_status}",
                "}",
            ]
        )
        return "\n".join(lines)

    for index, route in enumerate(_sort_routes(routes)):
        _render_proxy_route(lines, manifest, route, domain, index)

    lines.append("}")
    return "\n".join(lines)


def _render_catch_all_edge_block(manifest: Manifest, catch_all: CatchAllEdgeConfig) -> str:
    lines: List[str] = []
    if catch_all.http_redirect:
        lines.extend(
            [
                "http:// {",
                f"    redir https://{{host}}{{uri}} {catch_all.http_redirect_status}",
                "}",
                "",
            ]
        )

    lines.extend(
        [
            "https:// {",
            "    encode zstd gzip",
            "    header {",
            '        Strict-Transport-Security "max-age=31536000; includeSubDomains"',
            "        X-Content-Type-Options nosniff",
            "        X-Frame-Options DENY",
            "        Referrer-Policy strict-origin-when-cross-origin",
            "    }",
            "    tls {",
            "        on_demand",
            "    }",
            "    handle {",
            f"        reverse_proxy {_resolve_catch_all_upstream(manifest, catch_all)}",
            "    }",
            "}",
        ]
    )
    return "\n".join(lines)


def _render_proxy_route(
    lines: List[str],
    manifest: Manifest,
    route: RouteConfig,
    domain: str,
    index: int,
) -> None:
    upstream = _resolve_upstream(manifest, route)
    matcher = _render_matcher(route)

    if route.rewrite_prefix and not matcher:
        normalized_prefix = route.rewrite_prefix.rstrip("/")
        if normalized_prefix:
            lines.append("    handle / {")
            lines.append(f"        rewrite * {normalized_prefix}")
            lines.append(f"        reverse_proxy {upstream}")
            lines.append("    }")

        lines.append("    handle {")
        lines.append(f"        rewrite * {_rewrite_target(route.rewrite_prefix)}")
        lines.append(f"        reverse_proxy {upstream}")
        lines.append("    }")
        return

    if matcher and len(matcher) == 1:
        lines.append(f"    handle {matcher[0]} {{")
    elif matcher:
        matcher_name = _matcher_name(manifest, domain, index)
        lines.append(f"    @{matcher_name} path {' '.join(matcher)}")
        lines.append(f"    handle @{matcher_name} {{")
    else:
        lines.append("    handle {")

    if route.strip_prefix:
        lines.append(f"        uri strip_prefix {route.strip_prefix}")
    if route.rewrite_prefix:
        lines.append(f"        rewrite * {_rewrite_target(route.rewrite_prefix)}")

    lines.append(f"        reverse_proxy {upstream}")
    lines.append("    }")


def _group_routes_by_domain(manifest: Manifest) -> "OrderedDict[str, List[RouteConfig]]":
    grouped: "OrderedDict[str, List[RouteConfig]]" = OrderedDict()
    for route in effective_routes(manifest):
        grouped.setdefault(route.domain, []).append(route)
    return grouped


def effective_routes(manifest: Manifest) -> List[RouteConfig]:
    routes = list(manifest.routes)
    if manifest.profile == "prism" and manifest.prism and manifest.prism.admin_domain:
        domains = {route.domain for route in routes}
        if manifest.prism.admin_domain not in domains:
            routes.append(RouteConfig(domain=manifest.prism.admin_domain, service=_default_service_name(manifest)))
    return routes


def _sort_routes(routes: List[RouteConfig]) -> List[RouteConfig]:
    def sort_key(route: RouteConfig) -> tuple[int, int]:
        if route.path is not None:
            return (0, -len(route.path))
        if route.path_prefix is not None:
            return (1, -len(route.path_prefix))
        return (2, 0)

    return sorted(routes, key=sort_key)


def _resolve_upstream(manifest: Manifest, route: RouteConfig) -> str:
    if route.upstream:
        return route.upstream
    if route.service:
        service = manifest.services[route.service]
        return f"{manifest.service_alias(service.name)}:{service.port}"
    if manifest.tunnel_target:
        return manifest.tunnel_target
    raise ValueError(f"Route for {route.domain} does not resolve to an upstream.")


def _resolve_catch_all_upstream(manifest: Manifest, catch_all: CatchAllEdgeConfig) -> str:
    if catch_all.upstream:
        return catch_all.upstream
    if catch_all.service:
        service = manifest.services[catch_all.service]
        return f"{manifest.service_alias(service.name)}:{service.port}"
    raise ValueError(f"Catch-all edge for {manifest.app} does not resolve to an upstream.")


def _render_matcher(route: RouteConfig) -> Optional[List[str]]:
    if route.path is not None:
        return [route.path]
    if route.path_prefix is not None:
        prefix = route.path_prefix.rstrip("/")
        if not prefix:
            return ["/*"]
        return [prefix, f"{prefix}/*"]
    return None


def _rewrite_target(prefix: str) -> str:
    normalized = prefix.rstrip("/")
    if not normalized:
        return "{uri}"
    return f"{normalized}{{uri}}"


def _matcher_name(manifest: Manifest, domain: str, index: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", f"{manifest.app}_{domain}_{index}".lower()).strip("_")
    return f"ophelia_{slug}"


def bundle_env_file_path(source: str, service_name: Optional[str] = None, index: int = 0) -> str:
    name = Path(source).name
    prefix = f"{service_name}-" if service_name else ""
    return f"env.d/{prefix}{index + 1:02d}-{name}"


def bundle_mount_path(service_name: str, source: str, index: int) -> str:
    name = Path(source).name
    return f"artifacts/{service_name}-{index + 1:02d}-{name}"


def _collect_service_env_keys(manifest: Manifest) -> List[str]:
    keys = set()
    for service in manifest.services.values():
        keys.update(service.env.keys())
    return sorted(keys)


def caddy_env_keys(manifest: Manifest) -> List[str]:
    values: List[str] = []
    if manifest.edge.on_demand_tls is not None:
        values.append(manifest.edge.on_demand_tls.ask)

    keys = set()
    for value in values:
        keys.update(re.findall(r"\{\$([A-Za-z_][A-Za-z0-9_]*)\}", value))
    return sorted(keys)


def _read_template(relative_path: str) -> str:
    return (TEMPLATES_DIR / relative_path).read_text()


def _quote(value: str) -> str:
    return json.dumps(value)


def _default_service_name(manifest: Manifest) -> str:
    if "web" in manifest.services:
        return "web"
    if manifest.services:
        return next(iter(manifest.services.keys()))
    raise ValueError(f"Manifest {manifest.app} does not expose a service for Prism routing.")
