#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUNTIME_ROOT="${1:-$HOME/ophelia-runtime}"
SOURCE_ROOT="$REPO_ROOT/platform/static"
TARGET_ROOT="$RUNTIME_ROOT/static"

mkdir -p "$TARGET_ROOT"

if [[ ! -d "$SOURCE_ROOT" ]]; then
  echo "No platform static assets to sync."
  exit 0
fi

if command -v rsync >/dev/null 2>&1; then
  rsync -az --delete "$SOURCE_ROOT/" "$TARGET_ROOT/"
else
  rm -rf "$TARGET_ROOT"
  mkdir -p "$TARGET_ROOT"
  cp -R "$SOURCE_ROOT"/. "$TARGET_ROOT"/
fi

echo "Synced platform static assets into $TARGET_ROOT"
