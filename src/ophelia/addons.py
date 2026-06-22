from __future__ import annotations

import json
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import Dict

from .manifest import Manifest


def ensure_addons(manifest: Manifest, app_root: Path, ophelia_root: Path) -> None:
    if not manifest.addons.postgres and not manifest.addons.redis:
        return

    shared_dir = ophelia_root / "platform" / "shared"
    shared_compose = shared_dir / "compose.yml"
    shared_env = shared_dir / ".env"
    if not shared_compose.exists():
        raise RuntimeError(f"Shared compose file not found: {shared_compose}")
    if not shared_env.exists():
        raise RuntimeError(f"Shared env file not found: {shared_env}")

    _ensure_shared_services(shared_compose, shared_env, manifest)

    state_path = app_root / "addons.json"
    state = _load_state(state_path)
    env_updates: Dict[str, str] = {}
    shared_env_values = _load_env(shared_env)

    if manifest.addons.postgres:
        postgres_state = _ensure_postgres_database(
            manifest=manifest,
            shared_compose=shared_compose,
            shared_env=shared_env,
            shared_env_values=shared_env_values,
            existing=state.get("postgres", {}),
        )
        state["postgres"] = postgres_state
        env_updates["DATABASE_URL"] = (
            f"postgres://{postgres_state['user']}:{postgres_state['password']}"
            f"@postgres:5432/{postgres_state['database']}"
        )

    if manifest.addons.redis:
        redis_state = _ensure_redis_binding(
            manifest=manifest,
            app_root=app_root,
            shared_env_values=shared_env_values,
            existing=state.get("redis", {}),
        )
        state["redis"] = redis_state
        env_updates["REDIS_URL"] = (
            f"redis://default:{redis_state['password']}@redis:6379/{redis_state['database']}"
        )

    _write_state(state_path, state)
    _update_env_file(app_root / "env", env_updates)


def _ensure_shared_services(shared_compose: Path, shared_env: Path, manifest: Manifest) -> None:
    services = []
    if manifest.addons.postgres:
        services.append("postgres")
    if manifest.addons.redis:
        services.append("redis")

    subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(shared_env),
            "-f",
            str(shared_compose),
            "up",
            "-d",
            *services,
        ],
        check=True,
        text=True,
    )


def _ensure_postgres_database(
    manifest: Manifest,
    shared_compose: Path,
    shared_env: Path,
    shared_env_values: Dict[str, str],
    existing: Dict[str, str],
) -> Dict[str, str]:
    postgres_password = shared_env_values.get("POSTGRES_PASSWORD")
    if not postgres_password:
        raise RuntimeError("POSTGRES_PASSWORD is missing from platform/shared/.env")

    identifier = _postgres_identifier(manifest.app)
    database = existing.get("database") or identifier
    user = existing.get("user") or identifier
    password = existing.get("password") or _random_secret()

    _wait_for_postgres(shared_compose, shared_env)

    sql = "\n".join(
        [
            (
                "SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', "
                f"{_sql_literal(user)}, {_sql_literal(password)}) "
                f"WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = {_sql_literal(user)}) \\gexec"
            ),
            f"SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', {_sql_literal(user)}, {_sql_literal(password)}) \\gexec",
            (
                "SELECT format('CREATE DATABASE %I OWNER %I', "
                f"{_sql_literal(database)}, {_sql_literal(user)}) "
                f"WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = {_sql_literal(database)}) \\gexec"
            ),
            "",
        ]
    )

    subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(shared_env),
            "-f",
            str(shared_compose),
            "exec",
            "-T",
            "postgres",
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            "postgres",
        ],
        input=sql,
        check=True,
        text=True,
    )

    return {
        "database": database,
        "user": user,
        "password": password,
    }


def _wait_for_postgres(shared_compose: Path, shared_env: Path, attempts: int = 30, delay: float = 1.0) -> None:
    command = [
        "docker",
        "compose",
        "--env-file",
        str(shared_env),
        "-f",
        str(shared_compose),
        "exec",
        "-T",
        "postgres",
        "pg_isready",
        "-U",
        "postgres",
        "-d",
        "postgres",
    ]

    for _ in range(attempts):
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode == 0:
            return
        time.sleep(delay)

    raise RuntimeError("Postgres did not become ready before addon provisioning.")


def _ensure_redis_binding(
    manifest: Manifest,
    app_root: Path,
    shared_env_values: Dict[str, str],
    existing: Dict[str, str],
) -> Dict[str, str]:
    redis_password = shared_env_values.get("REDIS_PASSWORD")
    if not redis_password:
        raise RuntimeError("REDIS_PASSWORD is missing from platform/shared/.env")

    database = existing.get("database")
    if database is None:
        database = _next_redis_database(app_root)

    return {
        "database": int(database),
        "password": redis_password,
        "namespace": manifest.app,
    }


def _next_redis_database(app_root: Path) -> int:
    runtime_root = app_root.parent.parent
    used = set()
    for state_path in runtime_root.glob("apps/*/addons.json"):
        state = _load_state(state_path)
        redis_state = state.get("redis")
        if isinstance(redis_state, dict) and "database" in redis_state:
            used.add(int(redis_state["database"]))

    for candidate in range(16):
        if candidate not in used:
            return candidate

    raise RuntimeError("No Redis database slots left in the default 0-15 range.")


def _postgres_identifier(app: str) -> str:
    identifier = re.sub(r"[^a-z0-9]+", "_", app.lower()).strip("_")
    if not identifier:
        identifier = "app"
    if identifier[0].isdigit():
        identifier = f"app_{identifier}"
    return identifier[:48]


def _random_secret() -> str:
    return secrets.token_urlsafe(24)


def _load_env(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _update_env_file(path: Path, updates: Dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(updates)
    rendered = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            rendered.append(line)
            continue

        key, _ = line.split("=", 1)
        if key in remaining:
            rendered.append(f"{key}={remaining.pop(key)}")
        else:
            rendered.append(line)

    for key, value in remaining.items():
        rendered.append(f"{key}={value}")

    path.write_text("\n".join(rendered) + "\n")


def _load_state(path: Path) -> Dict[str, Dict[str, str]]:
    if not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, dict)}


def _write_state(path: Path, state: Dict[str, Dict[str, str]]) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def _sql_literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"
