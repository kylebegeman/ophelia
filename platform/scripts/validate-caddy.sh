#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUNTIME_ROOT="${1:-$HOME/ophelia-runtime}"
STATIC_ROOT="${OPHELIA_STATIC_ROOT:-$RUNTIME_ROOT/static}"

mkdir -p "$RUNTIME_ROOT/caddy/global.d" "$RUNTIME_ROOT/caddy/sites.d" "$STATIC_ROOT"
touch "$RUNTIME_ROOT/caddy/env"

docker run --rm \
  -v "$REPO_ROOT/platform/shared/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -v "$RUNTIME_ROOT/caddy/env:/etc/caddy/env:ro" \
  -v "$RUNTIME_ROOT/caddy/global.d:/etc/caddy/global.d:ro" \
  -v "$RUNTIME_ROOT/caddy/sites.d:/etc/caddy/sites.d:ro" \
  -v "$RUNTIME_ROOT:$RUNTIME_ROOT:ro" \
  -v "$STATIC_ROOT:$STATIC_ROOT:ro" \
  caddy:2-alpine \
  caddy validate --config /etc/caddy/Caddyfile --envfile /etc/caddy/env
