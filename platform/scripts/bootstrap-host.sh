#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUNTIME_ROOT="${1:-$HOME/ophelia-runtime}"

mkdir -p "$RUNTIME_ROOT/apps" "$RUNTIME_ROOT/backups" "$RUNTIME_ROOT/cache" "$RUNTIME_ROOT/static"
mkdir -p "$REPO_ROOT/platform/shared/caddy/sites.d"

docker network inspect ophelia-edge >/dev/null 2>&1 || docker network create ophelia-edge
docker network inspect ophelia-internal >/dev/null 2>&1 || docker network create --internal ophelia-internal

echo "Prepared runtime root at $RUNTIME_ROOT"
echo "Prepared Docker networks: ophelia-edge, ophelia-internal"
