---
id: 0002
title: Command catalog and improvement foundation
date: 2026-06-21
status: landed
areas: [cli, api, foundation, docs, changelog]
change_type: feature
commits: []
---

## Summary

Adds the shared improvement foundation (centralized redaction, typed findings,
and a standard error envelope) and the Phase 1 command catalog: a single,
sorted, agent-facing command registry derived from the action registry, exposed
through `ship commands catalog --json` and `GET /commands`, plus an additive
`--json` output-consistency pass.

## Why

Agents (private operator UI, Legacy Console) need one machine-readable way to discover every command
with explicit risk, mutation, and confirmation metadata, and one consistent
error shape across CLI and HTTP. The foundation modules give every later phase a
single source of truth for redaction, findings/remediations, and error payloads
so each surface does not reinvent them.

## Changed Areas

Foundation (shared across all later phases):

- `src/ophelia/redaction.py`: centralized redaction. `REDACTED`,
  `SENSITIVE_KEY_PATTERNS`, `is_sensitive_key`, `looks_like_secret_value`,
  `redact_value`, `redact_mapping`, `redacted_cloudflare_record`, and
  `redacted_compose_text`. Redaction is decided by key name and value shape so
  no surface guesses; empty/`None` pass through so "present but empty" stays
  observable.
- `src/ophelia/findings.py`: typed `Finding` (severity blocker/warning/info,
  area, message, path, remediation) and `Remediation` (summary, typed commands,
  docs, manifest patch hint, human-approval flag). `Finding.to_dict()` is a
  strict superset of the legacy `{code, message, path}` issue shape;
  `finding_from_issue` and `attach_remediation` lift legacy issues upward
  without breaking existing consumers.
- `src/ophelia/operation_schema.py`: adds `error_envelope(message, code, ...)`
  emitting `{schema_version, kind: "ophelia.error", status: "failed", error,
  blockers, warnings}`, matching the shape already produced by `api.py:_error`.

Phase 1 command catalog and consistency pass:

- `src/ophelia/command_catalog.py`: new `CommandDescriptor` dataclass and one
  registry. `_descriptors_from_actions()` derives descriptors from
  `action_catalog()`; `CLI_ONLY_DESCRIPTORS` seeds CLI-only commands
  (`commands catalog`, `validate`, `render`, `receipts list`, `receipts show`);
  `command_registry()` dedupes by command and `catalog()` returns a sorted,
  JSON-serializable list. No raw-secret defaults in any args schema.
- `src/ophelia/commands/catalog.py`: new `ship commands catalog` command.
  `--json` (default) emits `{schema_version, kind: "ophelia.command_catalog",
  commands}`; `--human` prints an aligned table.
- `src/ophelia/commands/__init__.py`: registers the `commands` group.
- `src/ophelia/api.py`: adds `GET /commands` returning `{"commands": catalog()}`.
- `src/ophelia/commands/validate.py`, `operations.py`, `deploy.py`: additive
  `--json` consistency. Error paths now merge `error_envelope(...)` while
  keeping the existing `ok` key; the `deploy --json` (without `--plan`) guard
  now emits a pure-JSON error envelope instead of prose.
- `docs/job-action-api.md`, `docs/private-operator-vps-ophelia-2-handoff.md`: document the
  catalog command, `GET /commands`, and the `CommandDescriptor` fields.
- `tests/test_command_catalog.py`, `tests/test_json_output_consistency.py`: new
  coverage for catalog invariants and pure-JSON output.

## Contract Impact

Additive only. New CLI command `ship commands catalog` and new endpoint
`GET /commands`. `--json` error payloads for `validate`, `operations run`, and
`deploy` now additionally carry error-envelope keys (`schema_version`, `kind`,
`status`, `error`, `blockers`, `warnings`) while preserving the existing `ok`
key where it was present. No existing JSON key or CLI behavior was removed or
renamed.

## Safety Impact

None. All new surfaces are read-only discovery. The catalog describes input
shapes only and never carries secret values or raw-secret defaults. No VPS or
runtime state is mutated.

## Verification

- `PYTHON=python3 make compile`
- `PYTHONPATH=src python3 -m unittest discover -s tests` (143 tests, OK)
- `./cli/ship commands catalog --json | python3 -m json.tool` (CATALOG_OK)
- `./cli/ship actions --json | python3 -m json.tool` (ACTIONS_OK)
- `git diff --check`

## Follow-Ups

- Later phases append their own CLI-only descriptors via
  `register_cli_descriptor` and may populate `output_schema_ref` /
  `artifacts` as new envelopes land.
