---
id: 0106
title: Release 0.6.15 clean launcher shutdown handling
date: 2026-08-08
status: landed
areas: [release, daemon, launcher, systemd, upgrades, reliability, docs, tests]
change_type: fix
commits: []
---

## Summary

Publish Ophelia 0.6.15 with correct launcher semantics for an intentional
systemd stop or restart. The stable launcher forwarded SIGTERM or SIGINT to the
daemon child but returned the child's negative signal code. Systemd reported
the normal stop as exit code 241 and a failed service result.

The same path also invoked failed-start upgrade rollback during an intentional
service stop. A staged release could therefore be treated as a failed start
even though the operator or systemd initiated the shutdown.

## Changed Behavior

- A child that exits cleanly or from the exact signal forwarded by the launcher
  produces a clean launcher exit.
- An expected systemd stop does not run failed-start upgrade rollback.
- A spontaneous or unrelated nonzero child exit retains its failure status and
  existing rollback behavior.
- The daemon installer from 0.6.14 continues to restart and verify the promoted
  release explicitly.

## Verification

- Regression coverage includes SIGTERM, SIGINT, graceful exit, unrelated
  failure, and spontaneous termination cases.
- `make test`
- `make compile`
- `make docs-check`
- `make open-source-audit-strict`
- `git diff --check`
