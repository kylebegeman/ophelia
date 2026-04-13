from __future__ import annotations

import json
from collections import OrderedDict
from typing import Dict, List, Optional

from .config import TEMPLATES_DIR
from .manifest import Manifest, RouteConfig, ServiceConfig


def render_compose(manifest: Manifest) -> Optional[str]:
    if manifest.kind in {"static", "tunnel"}:
        return None

    template = _read_template("compose/app.compose.tpl")
    services_block = "\n".join(_render_service_block(manifest, service) for service in manifest.services.values())
    return template.replace("{{SERVICES_BLOCK}}", services_block.rstrip())


def render_caddy(manifest: Manifest) -> str:
    template = _read_template("caddy/site.caddy.tpl")
    sites_block = "\n\n".join(_render_site_block(manifest, domain, routes) for domain, routes in _group_routes_by_domain(manifest).items())
    return template.replace("{{SITES_BLOCK}}", sites_block.rstrip()) + "\n"


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
        lines.append("REDIS_URL=redis://:replace-me@redis:6379/0")

    service_secret_keys = _collect_service_env_keys(manifest)
    for key in service_secret_keys:
        if key not in manifest.env:
            lines.append(f"{key}=replace-me")

    return "\n".join(lines) + "\n"


def _render_service_block(manifest: Manifest, service: ServiceConfig) -> str:
    service_lines: List[str] = [
        f"  {service.name}:",
        f"    image: {_quote(service.image or manifest.image or '')}",
        "    restart: unless-stopped",
        "    env_file:",
        "      - ./env",
        "    environment:",
        f"      OPHELIA_APP: {_quote(manifest.app)}",
        f"      OPHELIA_SERVICE: {_quote(service.name)}",
        f"      PORT: {_quote(str(service.port))}",
    ]

    env_map = OrderedDict(sorted(manifest.env.items()))
    env_map.update(OrderedDict(sorted(service.env.items())))

    if manifest.addons.postgres:
        env_map.setdefault("DATABASE_URL", "${DATABASE_URL}")

    if manifest.addons.redis:
        env_map.setdefault("REDIS_URL", "${REDIS_URL}")

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

    if service.command:
        service_lines.append("    command:")
        for item in service.command:
            service_lines.append(f"      - {_quote(item)}")

    service_lines.extend(_render_healthcheck_block(service))
    service_lines.extend(
        [
            f"    mem_limit: {_quote(manifest.resources.memory)}",
            "    networks:",
            "      ophelia-edge:",
            "        aliases:",
            f"          - {_quote(manifest.service_alias(service.name))}",
            "      ophelia-internal:",
            "        aliases:",
            f"          - {_quote(manifest.service_alias(service.name))}",
        ]
    )
    return "\n".join(service_lines)


def _render_healthcheck_block(service: ServiceConfig) -> List[str]:
    healthcheck = service.healthcheck
    command = healthcheck.command or [
        "CMD-SHELL",
        (
            f"wget -qO- http://127.0.0.1:{service.port}{healthcheck.path} "
            ">/dev/null 2>&1 || exit 1"
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
        lines.extend(
            [
                f"    reverse_proxy {manifest.tunnel_target}",
                "}",
            ]
        )
        return "\n".join(lines)

    for route in routes:
        service = manifest.services[route.service or ""]
        upstream = f"{manifest.service_alias(service.name)}:{service.port}"

        if route.path_prefix:
            matcher = route.path_prefix.rstrip("/") + "*"
            lines.append(f"    handle {matcher} {{")
            if route.strip_prefix:
                lines.append(f"        uri strip_prefix {route.strip_prefix}")
            lines.append(f"        reverse_proxy {upstream}")
            lines.append("    }")
        else:
            lines.append("    handle {")
            lines.append(f"        reverse_proxy {upstream}")
            lines.append("    }")

    lines.append("}")
    return "\n".join(lines)


def _group_routes_by_domain(manifest: Manifest) -> "OrderedDict[str, List[RouteConfig]]":
    grouped: "OrderedDict[str, List[RouteConfig]]" = OrderedDict()
    for route in manifest.routes:
        grouped.setdefault(route.domain, []).append(route)
    return grouped


def _collect_service_env_keys(manifest: Manifest) -> List[str]:
    keys = set()
    for service in manifest.services.values():
        keys.update(service.env.keys())
    return sorted(keys)


def _read_template(relative_path: str) -> str:
    return (TEMPLATES_DIR / relative_path).read_text()


def _quote(value: str) -> str:
    return json.dumps(value)
