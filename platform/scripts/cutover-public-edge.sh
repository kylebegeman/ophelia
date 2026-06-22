#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUNTIME_ROOT="${1:-$HOME/ophelia-runtime}"
LEGACY_EDGE_ROOT="${2:-$HOME/edge}"
SHARED_ENV="$REPO_ROOT/platform/shared/.env"
SHARED_COMPOSE="$REPO_ROOT/platform/shared/compose.yml"

if [[ ! -f "$SHARED_ENV" ]]; then
  echo "Missing shared env file at $SHARED_ENV"
  exit 1
fi

"$REPO_ROOT/platform/scripts/apply-manifests.sh" "$RUNTIME_ROOT"
"$REPO_ROOT/platform/scripts/validate-caddy.sh" "$RUNTIME_ROOT"

legacy_running=0
if docker compose -f "$LEGACY_EDGE_ROOT/docker-compose.yml" ps --status running edge 2>/dev/null | grep -q edge; then
  legacy_running=1
fi

if [[ $legacy_running -eq 1 ]]; then
  echo "Stopping legacy edge container"
  docker compose -f "$LEGACY_EDGE_ROOT/docker-compose.yml" stop edge
fi

if ! docker compose --env-file "$SHARED_ENV" -f "$SHARED_COMPOSE" --profile edge up -d caddy; then
  echo "Shared Caddy failed to start."
  if [[ $legacy_running -eq 1 ]]; then
    echo "Restoring legacy edge container"
    docker compose -f "$LEGACY_EDGE_ROOT/docker-compose.yml" up -d edge
  fi
  exit 1
fi

echo "Shared Ophelia Caddy is now the public edge."
