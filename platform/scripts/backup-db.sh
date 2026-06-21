#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "$SCRIPT_DIR/../shared" && pwd)"
BACKUP_ROOT="${1:-$HOME/ophelia-runtime/backups/postgres}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_ROOT"

docker compose -f "$SHARED_DIR/compose.yml" exec -T postgres pg_dumpall -U postgres | gzip > "$BACKUP_ROOT/postgres-$STAMP.sql.gz"

echo "Wrote Postgres backup to $BACKUP_ROOT/postgres-$STAMP.sql.gz"
