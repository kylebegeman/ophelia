#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUNTIME_ROOT="${1:-$HOME/ophelia-runtime}"
SHARED_DIR="$REPO_ROOT/platform/shared"
SHARED_ENV="$SHARED_DIR/.env"

mkdir -p "$RUNTIME_ROOT/apps" "$RUNTIME_ROOT/backups" "$RUNTIME_ROOT/cache" "$RUNTIME_ROOT/static"
mkdir -p "$RUNTIME_ROOT/caddy/global.d" "$RUNTIME_ROOT/caddy/sites.d"
touch "$RUNTIME_ROOT/caddy/env"

mkdir -p "$SHARED_DIR"
umask 077
SHARED_ENV="$SHARED_ENV" RUNTIME_ROOT="$RUNTIME_ROOT" python3 - <<'PY'
from pathlib import Path
import os
import secrets

path = Path(os.environ["SHARED_ENV"])
runtime_root = os.environ["RUNTIME_ROOT"]
values = {}

if path.exists():
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value

def secret(key: str) -> str:
    current = values.get(key, "")
    if current and current != "replace-me":
        return current
    return secrets.token_urlsafe(32)

values = {
    "OPHELIA_RUNTIME_ROOT": values.get("OPHELIA_RUNTIME_ROOT") or runtime_root,
    "OPHELIA_HTTP_PORT": values.get("OPHELIA_HTTP_PORT") or "80",
    "OPHELIA_HTTPS_PORT": values.get("OPHELIA_HTTPS_PORT") or "443",
    "POSTGRES_PASSWORD": secret("POSTGRES_PASSWORD"),
    "REDIS_PASSWORD": secret("REDIS_PASSWORD"),
}

path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
path.chmod(0o600)
PY

docker network inspect ophelia-edge >/dev/null 2>&1 || docker network create ophelia-edge
docker network inspect ophelia-internal >/dev/null 2>&1 || docker network create --internal ophelia-internal

echo "Prepared runtime root at $RUNTIME_ROOT"
echo "Prepared Docker networks: ophelia-edge, ophelia-internal"
echo "Prepared shared service env at $SHARED_ENV"
