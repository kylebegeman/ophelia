---
id: 0105
title: Release 0.6.14 daemon installer restart correction
date: 2026-08-08
status: landed
areas: [release, daemon, installer, systemd, reliability, docs, tests]
change_type: fix
commits: []
---

## Summary

Publish Ophelia 0.6.14 with a direct daemon installer correction. A daemon
upgrade promoted the new immutable release and ran `systemctl enable --now`,
but systemd does not restart a service that is already active. The service
could therefore continue executing the previous Ophelia process until a later
restart even though the `current` pointer named the new release.

## Changed Behavior

- The installer enables `opheliad.service` independently.
- It explicitly starts or restarts the service after promoting `current`.
- It verifies that the restarted service is active before returning a
  successful install receipt.
- Fresh installs retain the same enabled and active end state.

## Verification

- Regression coverage asserts the exact enable, restart, and active-check
  command sequence.
- `make test`
- `make compile`
- `make docs-check`
- `make open-source-audit-strict`
- `git diff --check`
