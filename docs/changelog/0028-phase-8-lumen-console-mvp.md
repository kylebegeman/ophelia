---
id: 0028
title: Phase 8 Lumen console MVP
date: 2026-06-22
status: landed
areas: [lumen, console, api, cli, fixtures, docs, tests]
change_type: feature
commits: []
---

## Summary

Added a read-only Lumen operator console payload that composes apps, readiness,
approvals, workflows, plugins, quick actions, and source metadata into one
bounded contract.

## Why

Lumen needs one stable payload to render an operator cockpit without
reconstructing Ophelia state from many shell commands. Ophelia should remain the
executor, while Lumen owns presentation and approval UX.

## Changed Areas

- `src/ophelia/lumen_adapter.py`: adds `console_data`, compact app rows,
  approval queue metadata, workflow summaries, plugin summaries, and quick
  actions.
- `src/ophelia/commands/lumen.py`: adds `ship lumen console-data`.
- `src/ophelia/api.py` and `src/ophelia/api_routes.py`: expose
  `/lumen/console-data`.
- `Makefile`: adds `lumen-console-fixtures`.
- `tests/test_lumen_adapter.py`: verifies fixture console shape, custom
  manifest path handling, redaction, plugin visibility, quick actions, and
  approval metadata.
- `docs/lumen-operator-console.md`, README, fixture docs, roadmap, findings,
  and changelog index updated.

## Contract Impact

Additive contracts:

- `ship lumen console-data --json`
- `GET /lumen/console-data`
- `kind: "ophelia.lumen.console"`

The payload is read-only and does not execute commands, plugins, workflows, or
provider operations.

## Safety Impact

Net positive. The console exposes approval metadata without accepting
confirmation tokens or running mutations. App rows and workflow summaries are
bounded and redacted.

## Verification

- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest tests.test_lumen_adapter tests.test_command_catalog`
- `PYTHON=python3 make lumen-console-fixtures`
- `python3 -m compileall -q src/ophelia/lumen_adapter.py src/ophelia/commands/lumen.py src/ophelia/api.py src/ophelia/api_routes.py src/ophelia/command_catalog.py`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Render the console payload in Lumen UI.
- Add production hardening drills and migration guidance in Phase 9.
