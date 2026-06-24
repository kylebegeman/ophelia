---
id: 0010
title: Operator console adapter
date: 2026-06-21
status: landed
areas: [cli, api, operator-console, redaction, foundation, docs, changelog]
change_type: feature
commits: []
---

## Summary

Phase 9 adds a thin operator-console adapter: a small set of read-only translation
functions that frame Ophelia's existing contracts for private operator UI surfaces. A
new `ophelia.lumen_adapter` module exposes `capabilities` (what Ophelia can do),
`action_descriptors` (the shared command catalog), `dashboard_data` (one
aggregate health view across every known app/environment), and thin
`app_readiness` / `app_timeline` wrappers. A new `ship lumen` command group
(`capabilities` / `dashboard-data` / `action-descriptors`) and read-only
`/lumen/*` GET endpoints expose the same views over the local API.

## Why

Private operator UIs need a stable, secret-free read surface: a manifest of capabilities, a
single source of action descriptors, and one aggregate dashboard rather than a
dozen per-report calls. Ophelia already computes all of this (readiness,
receipts, route conflicts, backup freshness, the command catalog); the adapter
just translates and aggregates those existing outputs. Later phases will fill in
the reserved `traffic_status` and `observability` placeholders.

## Thin translation layer (no second implementation)

The adapter contains **no business logic**. It delegates to the existing,
already-redaction-safe contracts:

- `capabilities` / `action_descriptors` reuse `ophelia.command_catalog.catalog()`
  directly. `action_descriptors().descriptors` is equal to `catalog()` (the
  shared registry), never a copy, so it cannot drift.
- `dashboard_data` aggregates `ophelia.portability.app_readiness_report`
  (readiness level + portability score + blocker codes/counts),
  `ophelia.receipt_index.receipt_timeline` (recent receipts, capped at 10),
  `ophelia.conflicts.scan_conflicts` (route-conflict counts), and
  `ophelia.portability.backup_status_report` (backup freshness). The app/
  environment list comes from `ophelia.operator_reports.manifest_registry`.
- `app_readiness` / `app_timeline` are one-line wrappers over the readiness and
  timeline contracts.

## Secret safety

No raw env value, token, private key, DB URL, or full log is ever emitted. The
dashboard surfaces only counts, codes, statuses, and the scalar portability
score (not the full secret-redacted finding payloads or factor lists). Every
assembled payload is additionally swept through
`ophelia.redaction.deep_redact` as a final safety net. A test deploys an app
whose runtime env file contains `DATABASE_URL=postgres://u:LUMENLEAK@h/db` and
asserts neither `LUMENLEAK` nor `postgres://` appears anywhere in
`json.dumps(dashboard_data(...))`, while the readiness sub-report still runs over
that env file.

## Resilience

`dashboard_data` never crashes on a bad input. A registry-level failure or any
per-app sub-report failure (malformed manifest, missing runtime, raising report)
is converted into a structured `warnings` entry and the dashboard still returns;
the affected app row is emitted with whatever sub-reports succeeded.

## No circular import

`ophelia.lumen_adapter` imports only read-only contract modules
(`command_catalog`, `portability`, `conflicts`, `receipt_index`,
`operator_reports`, `redaction`, `operation_schema`, `config`). It never imports
any `ophelia.commands.*` module at top level. A test in a fresh interpreter
imports `ophelia.lumen_adapter` as its only Ophelia import and asserts no
`ophelia.commands.*` module is pulled in and the import succeeds.

## Changed Areas

- `src/ophelia/lumen_adapter.py`: new module. `capabilities` (kind
  `ophelia.lumen.capabilities`), `action_descriptors` (kind
  `ophelia.lumen.action_descriptors`), `dashboard_data` (kind
  `ophelia.lumen.dashboard`), and `app_readiness` / `app_timeline` wrappers.
  Pure JSON output, deep-redacted, no business logic.
- `src/ophelia/commands/lumen.py`: new `lumen` command group with
  `capabilities`, `dashboard-data`, and `action-descriptors` subcommands
  (`--json` emits pure JSON; optional `--runtime-root` / `--manifests-dir`).
  Registers three read-only CLI descriptors (`risk: low`,
  `mutates_state: false`).
- `src/ophelia/commands/__init__.py`: registers the new `lumen` group.
- `src/ophelia/api.py`: read-only GET endpoints `/lumen/capabilities`,
  `/lumen/dashboard-data`, `/lumen/action-descriptors`, `/lumen/apps`,
  `/lumen/apps/<app>/readiness` (optional `?environment=`), and
  `/lumen/apps/<app>/timeline`. All degrade gracefully (HTTP 200 with warnings/
  blockers, never 500). Existing endpoints unchanged.
- `tests/test_lumen_adapter.py`: new coverage (see Verification). No existing
  test file was modified.

## Contract Impact

Additive only. New `ophelia.lumen_adapter` module, three `ship lumen ...`
commands, and six read-only GET endpoints. No existing CLI, JSON, manifest, API,
receipt, runtime, or policy contract was changed or removed.

## Safety Impact

None to live infrastructure. All `lumen` views are read-only; nothing is
executed, no node mutates state, no VPS access, no SSH, and no raw env value is
read or printed. The adapter only reads files the runtime root already contains
and frames existing report outputs.

## Verification

- `PYTHON=python3 make compile`
- `PYTHONPATH=src python3 -m unittest discover -s tests`
- `./cli/ship lumen capabilities --json`
- `./cli/ship lumen action-descriptors --json`
- `./cli/ship lumen dashboard-data --json`
- `PYTHON=python3 make validate-examples`
- `PYTHON=python3 make docs-check`
- `git diff --check`

New test coverage: `ophelia.lumen_adapter` imports standalone in a fresh
interpreter with no `ophelia.commands.*` module pulled in (no circular import);
`capabilities`, `action_descriptors`, and `dashboard_data` round-trip through
`json.dumps`/`json.loads` and carry `schema_version` + `kind` + stable keys;
`action_descriptors().descriptors` equals `command_catalog.catalog()` (shared
registry, not a copy); a deployed app whose env file holds
`DATABASE_URL=postgres://u:LUMENLEAK@h/db` never leaks `LUMENLEAK` or
`postgres://` into the dashboard JSON; and `dashboard_data` surfaces a warning
(not a crash) when the manifest registry fails and when a per-app readiness
sub-report raises.

## Follow-Ups

- Later phases populate the reserved `traffic_status` and `observability`
  placeholder keys (both `null` now) once the traffic controller and telemetry
  layers land.
