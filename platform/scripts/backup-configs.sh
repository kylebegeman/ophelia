#!/usr/bin/env bash

set -euo pipefail

RUNTIME_ROOT="${1:-$HOME/ophelia-runtime}"
BACKUP_ROOT="${2:-$HOME/ophelia-runtime/backups/configs}"
STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="$BACKUP_ROOT/$STAMP"

mkdir -p "$TARGET"
cp -R "$RUNTIME_ROOT/apps" "$TARGET/apps"

echo "Wrote config backup to $TARGET"
