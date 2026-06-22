#!/usr/bin/env bash

set -euo pipefail

APP_NAME="${1:?Usage: upsert-prism-surface-env.sh <app-name> [env-path]}"
RUNTIME_ROOT="${OPHELIA_RUNTIME_ROOT:-$HOME/ophelia-runtime}"
ENV_PATH="${2:-$RUNTIME_ROOT/apps/$APP_NAME/env}"

mkdir -p "$(dirname "$ENV_PATH")"
touch "$ENV_PATH"

case "$APP_NAME" in
  quark-ops-staging)
    QUARK_ENV_PREFIX="QUARK_STAGING"
    ;;
  quark-ops)
    QUARK_ENV_PREFIX="QUARK_PRODUCTION"
    ;;
  *)
    QUARK_ENV_PREFIX=""
    ;;
esac

upsert_line() {
  local key="$1"
  local value="$2"

  if grep -q "^${key}=" "$ENV_PATH"; then
    python3 - "$ENV_PATH" "$key" "$value" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = path.read_text().splitlines()
updated = []
for line in lines:
    if line.startswith(f"{key}="):
        updated.append(f"{key}={value}")
    else:
        updated.append(line)
path.write_text("\n".join(updated) + "\n")
PY
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_PATH"
  fi
}

require_secret_or_existing() {
  local key="$1"
  local value="$2"

  if [[ -n "$value" ]]; then
    upsert_line "$key" "$value"
    return
  fi

  if ! grep -q "^${key}=" "$ENV_PATH"; then
    echo "Missing required ${key}. Export it before deploying ${APP_NAME}." >&2
    exit 1
  fi
}

env_value() {
  local key="$1"
  local value="${!key:-}"
  printf '%s' "$value"
}

specific_value() {
  local suffix="$1"
  if [[ -z "$QUARK_ENV_PREFIX" ]]; then
    printf ''
    return
  fi
  env_value "${QUARK_ENV_PREFIX}_${suffix}"
}

setup_token="${PRISM_CONSOLE_SETUP_TOKEN:-${QUARK_SETUP_TOKEN:-$(specific_value SETUP_TOKEN)}}"
mfa_key="${PRISM_MFA_ENCRYPTION_KEY:-${QUARK_MFA_ENCRYPTION_KEY:-$(specific_value MFA_ENCRYPTION_KEY)}}"
credential_key="${PRISM_CREDENTIAL_ENCRYPTION_KEY:-${QUARK_CREDENTIAL_ENCRYPTION_KEY:-$(specific_value CREDENTIAL_ENCRYPTION_KEY)}}"
if [[ -z "$credential_key" && -n "$mfa_key" ]]; then
  credential_key="$mfa_key"
fi
surface="${PRISM_CONSOLE_SURFACE:-quark}"
asset_path="${PRISM_CONSOLE_ASSET_PATH:-/opt/prism/console}"
dashboard_title="${QUARK_DASHBOARD_TITLE:-}"

upsert_line "PRISM_CONSOLE_SURFACE" "$surface"
upsert_line "PRISM_CONSOLE_ASSET_PATH" "$asset_path"
require_secret_or_existing "PRISM_CONSOLE_SETUP_TOKEN" "$setup_token"
require_secret_or_existing "PRISM_MFA_ENCRYPTION_KEY" "$mfa_key"
require_secret_or_existing "PRISM_CREDENTIAL_ENCRYPTION_KEY" "$credential_key"

if [[ -n "$dashboard_title" ]]; then
  upsert_line "QUARK_DASHBOARD_TITLE" "$dashboard_title"
fi

echo "Updated Prism surface env at $ENV_PATH"
