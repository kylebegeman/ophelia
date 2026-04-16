#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUNTIME_ROOT="${1:-$HOME/ophelia-runtime}"
MANIFEST_ROOT="${2:-$REPO_ROOT/manifests}"

"$REPO_ROOT/platform/scripts/bootstrap-host.sh" "$RUNTIME_ROOT"
"$REPO_ROOT/platform/scripts/sync-platform-static.sh" "$RUNTIME_ROOT"

shopt -s nullglob
manifests=("$MANIFEST_ROOT"/*.ophelia.yml)

if [[ ${#manifests[@]} -eq 0 ]]; then
  echo "No manifests found in $MANIFEST_ROOT"
  exit 0
fi

for manifest in "${manifests[@]}"; do
  echo "Applying $(basename "$manifest")"
  case "$(basename "$manifest")" in
    quark-ops.ophelia.yml)
      "$REPO_ROOT/platform/scripts/deploy-quark-ops.sh" --environment production
      ;;
    quark-ops-staging.ophelia.yml)
      "$REPO_ROOT/platform/scripts/deploy-quark-ops.sh" --environment staging
      ;;
    *)
      "$REPO_ROOT/cli/ship" deploy "$manifest" \
        --runtime-root "$RUNTIME_ROOT" \
        --ophelia-root "$REPO_ROOT" \
        --apply
      ;;
  esac
done
