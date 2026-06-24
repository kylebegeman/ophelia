# 0070: Managed Addon Networking Release

Date: 2026-06-24

Status: landed

## Summary

Package metadata is bumped to `0.3.12`. This release makes per-app internal
networks compatible with Ophelia-managed shared Postgres and Redis, hardens
static asset publishing, and fixes remote production deploy confirmation so SSH
applies use confirmation tokens generated from the staged remote runtime bundle.

The immediate deployment need is a retained staging app: it can remove the
temporary `networking.internal: shared` workaround and return to
`networking.internal: per-app` while still reaching shared Postgres through
`ophelia-internal`.

## Changes

- `src/ophelia/templates.py`: attaches per-app services with managed Postgres or
  Redis to both the app-specific internal network and the shared
  `ophelia-internal` network.
- `src/ophelia/runtime.py`: prepares `ophelia-internal` for per-app apps that
  use managed shared addons.
- `platform/scripts/bootstrap-host.sh`: prepares the shared internal Docker
  network and preserves existing custom shared env keys.
- `src/ophelia/runtime.py` and `src/ophelia/planning.py`: block unsafe static
  asset symlinks before staging or publishing.
- `src/ophelia/commands/deploy.py` and `src/ophelia/remote.py`: route remote
  deploy plans through the VPS checkout and forward the remote confirmation
  token into apply.
- `README.md` and operator docs: document remote production confirmation tokens
  and bump public package metadata to `0.3.12`.

## Safety Notes

No production apply behavior is loosened. Local production applies still require
the matching local plan token, and remote production applies now require a token
from the matching remote plan. Per-app managed-addon networking only adds the
shared addon network attachment needed for declared managed Postgres or Redis.

## Verification

- `make test`
- `make docs-check`
- `make compile`
- `make open-source-audit-strict`
- `./cli/ship self-test --json`
- `git diff --check`
