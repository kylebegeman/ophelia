#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUNTIME_ROOT="${OPHELIA_RUNTIME_ROOT:-$HOME/ophelia-runtime}"
ENVIRONMENT="production"
VERIFY=0
MANIFEST=""

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
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: deploy-quark-ops.sh [--environment production|staging] [--manifest path] [--verify]" >&2
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

"$REPO_ROOT/cli/ship" deploy "$MANIFEST" --runtime-root "$RUNTIME_ROOT"
"$REPO_ROOT/platform/scripts/upsert-prism-surface-env.sh" "$APP_NAME"
"$REPO_ROOT/cli/ship" deploy "$MANIFEST" --runtime-root "$RUNTIME_ROOT" --ophelia-root "$REPO_ROOT" --apply

if [[ "$VERIFY" == "1" ]]; then
  "$REPO_ROOT/cli/ship" verify "$MANIFEST"
fi
