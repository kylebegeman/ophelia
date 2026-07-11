# Release hardening 0.4.0

```yaml
---
id: 0071
title: Release hardening 0.4.0
date: 2026-06-24
status: landed
areas: [release, image-lock, backup, offsite, cutover, cli, docs, tests]
change_type: minor-release
commits: []
---
```

## Summary

Ophelia 0.4.0 adds production image digest locking, stronger offsite rehearsal
evidence checks, clearer critical shared Postgres cutover evidence, and the
first-class `ship version` command.

## Why

The 0.3.x line made Ophelia usable for fixture-first deployment and SaaS
onboarding. The next retained-app deployments need sharper release safety:
production image refs should be immutable, offsite backup policy should point at
real evidence, shared database cutovers should produce explicit checkpoint
artifacts, and operators should have a reliable version command.

## Changed Areas

- `src/ophelia/image_lock.py`: new image digest resolution, plan, lock, and
  pinned-manifest helpers.
- `src/ophelia/commands/release.py`: new `ship release image-lock plan/apply`
  commands and command catalog descriptors.
- `src/ophelia/portability.py`: offsite evidence freshness checks and critical
  shared Postgres cutover evidence output.
- `src/ophelia/version.py`, `src/ophelia/commands/version.py`,
  `src/ophelia/main.py`: first-class `ship version` and global `ship
  --version`.
- `tests/test_image_lock.py`, `tests/test_portability.py`,
  `tests/test_self_test.py`: focused regression coverage.
- `README.md`, `docs/manifest-spec.md`, `docs/portable-app-pack-spec.md`,
  `docs/ROADMAP.md`: operator-facing documentation for the new workflows.

## Contract Impact

- New CLI surfaces:
  - `ship version [--json]`
  - `ship --version`
  - `ship release image-lock plan <manifest> [--output PATH]
    [--pinned-manifest PATH] [--json]`
  - `ship release image-lock apply <manifest> --confirm <token>
    [--output PATH] [--pinned-manifest PATH] [--json]`
- `ship pack validate` includes an `offsite_policy` summary and emits specific
  `offsite_rehearsal_evidence_*` warnings for missing, unreadable,
  unsuccessful, unstamped, or stale evidence.
- Critical apps with `data.postgres.mode: shared-postgres-database` include
  `shared_postgres_cutover` in cutover plans/receipts. Apply writes
  `shared-postgres-cutover-evidence.json`.
- Package metadata is bumped to `0.4.0`.

## Safety Impact

Image-lock plan is read-only. Image-lock apply is confirmation-gated and writes
only the requested image-lock artifact and optional pinned manifest copy. It
does not deploy, pull, restart, or mutate runtime state.

Offsite policy checks are read-only. Shared Postgres cutover evidence writes a
checkpoint artifact under the cutover directory; it does not dump, restore, or
mutate shared Postgres, Caddy, DNS, or active runtime state.

## Verification

- `PYTHONPATH=src .venv/bin/python -m unittest tests.test_image_lock tests.test_portability -v`
- `PYTHONPATH=src .venv/bin/python -m unittest tests.test_self_test tests.test_command_catalog -v`
- `PYTHONPATH=src .venv/bin/python -m unittest tests.test_self_test tests.test_command_catalog tests.test_fixture_app_suite -v`
- `make compile`
- `make docs-check`
- `make test`
- `make open-source-audit-strict`
- `git diff --check`
- `./cli/ship version --json`

## Follow-Ups

- None.
