#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUNTIME_ROOT="${1:-$HOME/ophelia-runtime}"

mkdir -p "$RUNTIME_ROOT/caddy/sites.d"

docker run --rm \
  -v "$REPO_ROOT/platform/shared/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -v "$RUNTIME_ROOT/caddy/sites.d:/etc/caddy/sites.d:ro" \
  -v "$RUNTIME_ROOT:$RUNTIME_ROOT:ro" \
  -v /home/kyle/websites:/home/kyle/websites:ro \
  caddy:2-alpine \
  caddy validate --config /etc/caddy/Caddyfile
