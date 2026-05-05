#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUNTIME_ROOT="${OPHELIA_RUNTIME_ROOT:-$HOME/ophelia-runtime}"
ENVIRONMENT="production"
VERIFY=0
MANIFEST=""
CONFIRM=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment)
      ENVIRONMENT="${2:?Missing value for --environment}"
      shift 2
      ;;
    --manifest)
      MANIFEST="${2:?Missing value for --manifest}"
      shift 2
      ;;
    --verify)
      VERIFY=1
      shift
      ;;
    --confirm)
      CONFIRM="${2:?Missing value for --confirm}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: deploy-quark-ops.sh [--environment production|staging] [--manifest path] [--verify] [--confirm token]" >&2
      exit 1
      ;;
  esac
done

case "$ENVIRONMENT" in
  production)
    APP_NAME="quark-ops"
    MANIFEST="${MANIFEST:-$REPO_ROOT/manifests/quark-ops.ophelia.yml}"
    ;;
  staging)
    APP_NAME="quark-ops-staging"
    MANIFEST="${MANIFEST:-$REPO_ROOT/manifests/quark-ops-staging.ophelia.yml}"
    ;;
  *)
    echo "Unsupported environment: $ENVIRONMENT" >&2
    exit 1
    ;;
esac

if [[ -n "${GHCR_TOKEN:-}" ]]; then
  ghcr_username="${GHCR_USERNAME:-${GITHUB_ACTOR:-mrbagels}}"
  printf '%s' "$GHCR_TOKEN" | docker login ghcr.io --username "$ghcr_username" --password-stdin >/dev/null
elif [[ "${QUARK_REQUIRE_GHCR_LOGIN:-0}" == "1" ]]; then
  echo "GHCR_TOKEN is required to pull the private Prism Quark image." >&2
  exit 1
fi

"$REPO_ROOT/cli/ship" deploy "$MANIFEST" --runtime-root "$RUNTIME_ROOT"
"$REPO_ROOT/platform/scripts/upsert-prism-surface-env.sh" "$APP_NAME"

deploy_args=(
  deploy "$MANIFEST"
  --runtime-root "$RUNTIME_ROOT"
  --ophelia-root "$REPO_ROOT"
  --apply
)

if [[ "$ENVIRONMENT" == "production" ]]; then
  if [[ -z "$CONFIRM" ]]; then
    echo "Production deploy requires --confirm. Run this plan command and pass its confirmation_token:" >&2
    "$REPO_ROOT/cli/ship" deploy "$MANIFEST" --runtime-root "$RUNTIME_ROOT" --plan --json >&2
    exit 1
  fi
  deploy_args+=(--confirm "$CONFIRM")
fi

"$REPO_ROOT/cli/ship" "${deploy_args[@]}"

if [[ "$VERIFY" == "1" ]]; then
  "$REPO_ROOT/cli/ship" verify "$MANIFEST"
fi
