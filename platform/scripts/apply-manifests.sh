#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUNTIME_ROOT="${1:-$HOME/ophelia-runtime}"
MANIFEST_ROOT="${2:-$REPO_ROOT/manifests}"
APPLY_QUARK="${OPHELIA_APPLY_QUARK:-0}"

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
      if [[ "$APPLY_QUARK" == "1" ]]; then
        "$REPO_ROOT/platform/scripts/deploy-quark-ops.sh" --environment production
      else
        echo "Skipping quark-ops.ophelia.yml; run deploy-quark-ops.sh explicitly or set OPHELIA_APPLY_QUARK=1."
      fi
      ;;
    quark-ops-staging.ophelia.yml)
      if [[ "$APPLY_QUARK" == "1" ]]; then
        "$REPO_ROOT/platform/scripts/deploy-quark-ops.sh" --environment staging
      else
        echo "Skipping quark-ops-staging.ophelia.yml; run deploy-quark-ops.sh explicitly or set OPHELIA_APPLY_QUARK=1."
      fi
      ;;
    *)
      "$REPO_ROOT/cli/ship" deploy "$manifest" \
        --runtime-root "$RUNTIME_ROOT" \
        --ophelia-root "$REPO_ROOT" \
        --apply
      ;;
  esac
done
