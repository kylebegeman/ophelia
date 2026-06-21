---
id: 0007
title: Runtime state SQLite read-model
date: 2026-06-21
status: landed
areas: [cli, api, state, redaction, foundation, docs, changelog]
change_type: feature
commits: []
---

## Summary

Phase 6 adds a local SQLite *read-model* over the runtime root. The new
`ophelia.state_db` module indexes what Ophelia already wrote to disk (apps,
environments, manifest locks, routes, releases, receipts, artifacts, checks,
backups, restore drills) into a single queryable index file. The index is a
**cache**, never a source of truth: it is rebuilt deterministically from the
files by `state rebuild`, and reads degrade gracefully when it is missing.

- New `ship state status`, `ship state rebuild`, and `ship state query receipts`
  CLI commands.
- New read-only API endpoints: `GET /state/status`, `/state/apps`,
  `/state/receipts`, `/state/routes`, `/state/backups`.
- Reuses the existing data sources (`receipt_index.receipt_timeline`,
  `portability._receipt_records`/`_backup_records`/`_restore_drill_receipts`,
  `operator_reports.manifest_registry`/`release_registry`) so the index matches
  the file-based read-models exactly.

## Why

Agents and operators could browse receipts and registries one file scan at a
time, but had no single indexed surface to query runtime state by app,
environment, operation, or status, and no way to ask the same question over HTTP
without re-scanning the runtime root. Phase 6 provides that index while keeping
the redaction contract intact and without adding any dependency (stdlib
`sqlite3` only).

## Local index vs VPS mutation

`state rebuild` writes **only** the local index file
(`<runtime_root>/state/ophelia-state.sqlite3`). This is a *local index
creation*, not a VPS/production mutation: it never touches the VPS, never
performs SSH, and never reads or prints raw env values. For that reason it
carries no production confirmation token; its command descriptor stays `risk:
low`, `mutates_state: false`, with the safety note "Writes only the local SQLite
index under the runtime root; never mutates VPS state." The database is a cache
rebuilt from files, so it can be deleted and regenerated at any time and is
never authoritative over the on-disk runtime files.

## Changed Areas

- `src/ophelia/state_db.py`: new. Stdlib `sqlite3` only, guarded by
  `SQLITE_AVAILABLE` (degrades gracefully if `sqlite3` is unavailable).
  `STATE_SCHEMA_VERSION = 1`. `state_db_path(runtime_root)` returns
  `<root>/state/ophelia-state.sqlite3`.
  - `rebuild_state(runtime_root, manifests_dir)`: idempotent (drops + recreates
    every table inside one transaction, then re-inserts, so reruns produce
    identical state with no duplicate-key errors). Tables: `apps`,
    `environments`, `manifests`, `routes`, `releases`, `receipts`, `artifacts`,
    `checks`, `backups`, `restore_drills`, `provider_configs`,
    `operation_plans`, `policy_results`, `schema_migrations`. Each payload table
    stores canonical, already-redacted `payload_json` plus typed query columns
    and (where a payload is stored) a `redacted` flag. Paths are stored relative
    to the runtime root where possible. Corrupt/unreadable files become
    `warnings` and are skipped; the rebuild never crashes. Returns
    `{schema_version, kind:"ophelia.state_rebuild", db_path, schema_version_db,
    counts, warnings, blockers, status}`.
  - `state_status(runtime_root)`: read-only (never creates the index). Returns
    `{available, db_path, exists, schema_version_code, schema_version_db,
    counts, needs_rebuild}` under `kind:"ophelia.state_status"`.
  - `query_receipts(runtime_root, *, app, environment, operation, status,
    limit)`: reads receipts from the index newest-first; results match
    `receipt_index.receipt_timeline` ordering and content for the same filters
    (`ORDER BY COALESCE(started_at,'') DESC, receipt_id DESC`). When the index is
    missing it returns a clear `needs_rebuild`/`missing` status telling the
    caller to run `ship state rebuild` (never auto-builds).
- `src/ophelia/commands/state.py`: new. `state` group with `status`, `rebuild`,
  and `query receipts` subcommands; `--json` emits pure JSON. The human-mode
  `rebuild` prints a one-line note that it writes only the local index under the
  runtime root. Registers three CLI descriptors (all `risk: low`,
  `mutates_state: false`); `state rebuild` carries the local-index safety note.
- `src/ophelia/commands/__init__.py`: registers the new `state` command group.
- `src/ophelia/api.py`: adds read-only `GET /state/status`, `/state/apps`,
  `/state/receipts`, `/state/routes`, `/state/backups`. Each reads the index
  read-only and returns a 200 JSON with `available:false`/`needs_rebuild:true`
  when the index is missing (never a 500). No GET rebuilds the index. Existing
  endpoints are unchanged.
- `tests/test_state_db.py`: new coverage (see Verification). No existing test
  file was modified.

## Contract Impact

Additive only. New `state_db` read-model, three `ship state ...` commands, and
five read-only `/state/...` endpoints. No existing CLI, JSON, manifest, API,
receipt, or runtime contract was changed or removed.

## Safety Impact

None to live infrastructure. All reads are read-only; the only write is the
local index file under the runtime root (local index creation, not a VPS
mutation), so no confirmation token is required. No SSH is performed. No raw env
value is ever stored: runtime `manifest.lock.json` payloads are routed through
`portability._redact_manifest_lock` (masking root `env` and per-service
`services.<name>.env` and setting `secret_values_redacted: True`) before
storage, and every other stored payload is additionally swept so any nested
`env` mapping is masked via `redaction.redact_mapping`. A test asserts no fixture
secret string appears in the raw SQLite bytes or in any `payload_json` column.

## Verification

- `PYTHON=python3 make compile`
- `PYTHONPATH=src python3 -m unittest discover -s tests` (203 tests, OK)
- `./cli/ship state rebuild --runtime-root <tmp> --json` (pure JSON)
- `./cli/ship state status --runtime-root <tmp> --json` (pure JSON)
- `./cli/ship state query receipts --runtime-root <tmp> --json` (pure JSON)
- `git diff --check`

New test coverage: tables populated; rebuild idempotent (run twice, identical
counts, no duplicate-key errors); corrupt receipt JSON surfaces as a
`receipt_unreadable` warning and is skipped (not a crash); `query_receipts`
matches `receipt_timeline` ids and order for the same filters; no secret fixture
value appears in the DB file bytes or in any stored `payload_json`;
`state_status` reports `exists/available:true` and correct code/db schema
versions after rebuild and does not create the index when none exists.

## Follow-Ups

- Phase 7 (policy engine) can populate `policy_results` and `operation_plans`
  through the same rebuild path.
