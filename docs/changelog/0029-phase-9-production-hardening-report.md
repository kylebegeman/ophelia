---
id: 0029
title: Phase 9 production hardening report
date: 2026-06-22
status: landed
areas: [hardening, cli, api, fixtures, command-catalog, docs, tests]
change_type: feature
commits: []
---

## Summary

Added a read-only production hardening report that composes live readiness,
Lumen console data, plugin validation, workflow availability, state status,
command catalog safety, and optional fixture-suite drills into one go/no-go
payload.

## Why

The live readiness and fixture lanes made real-value inspection possible, but
operators still needed one conservative hardening gate before moving toward
staging/prod migration rehearsals.

## Changed Areas

- `src/ophelia/hardening.py`: adds `production_hardening_report`.
- `src/ophelia/commands/hardening.py`: adds
  `ship hardening production-readiness`.
- `src/ophelia/api.py` and `src/ophelia/api_routes.py`: expose
  `/hardening/production-readiness`.
- `src/ophelia/command_catalog.py`: adds hardening examples.
- `Makefile`: adds `production-hardening-fixtures`.
- `tests/test_hardening.py`: verifies fixture expected state, redaction,
  catalog registration, route registration, and CLI JSON output.
- `docs/production-hardening.md`, README, fixture docs, roadmap, findings, and
  changelog index updated.

## Contract Impact

Additive contracts:

- `ship hardening production-readiness --json`
- `GET /hardening/production-readiness`
- `kind: "ophelia.production_hardening_report"`

The report is read-only. It does not execute workflows, plugin code, provider
mutations, shell commands, state refresh, HTTP probes, or Docker probes.

## Safety Impact

Net positive. Phase 9 adds a conservative go/no-go gate and fixture drill before
live mutation work. It accepts no confirmation tokens and emits no secret
values.

## Verification

- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest tests.test_hardening`
- `make production-hardening-fixtures`
- `python3 -m compileall -q src`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Add authenticated live provider probes behind explicit opt-in flags.
- Add production migration rehearsals that use existing plan/confirm/receipt
  contracts.
