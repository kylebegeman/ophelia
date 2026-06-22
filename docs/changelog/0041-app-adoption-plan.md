---
id: 0041
title: App adoption plan
date: 2026-06-22
status: landed
areas: [cli, adoption, command-catalog, docs, tests]
change_type: feature
---

## Summary

Adds a read-only app adoption planner for checking future app repositories
against the Ophelia contract before live hydration, provider setup, or
deployment work starts.

## Why

Ophelia is the source of truth for future app/runtime contracts. Operators and
agents need a first command that evaluates an app repo against that contract
without using legacy deployments as the model or collecting live values too
early.

## Changed Areas

- `src/ophelia/adoption.py`: adds `adoption_plan`.
- `src/ophelia/commands/app.py`: adds `ship app adoption plan` and a catalog
  descriptor.
- `src/ophelia/command_catalog.py`: adds adoption examples.
- `tests/test_adoption.py`: covers missing-manifest, valid-repo, CLI JSON, and
  catalog behavior.
- `docs/app-adoption.md`: documents the command, checks, output, and boundary.
- `docs/ophelia-source-of-truth.md`, `README.md`, and
  `docs/ophelia-strategic-implementation-roadmap.md`: route adoption work
  through the new read-only command.

## Contract Impact

New CLI command:

```bash
ship app adoption plan <app> --repo-path <repo> --environment <env> --json
```

The command emits an `ophelia.plan` with adoption gates, required artifacts,
embedded pack validation, and ordered next commands. It sets `read_only: true`,
`mutates_state: false`, and `live_values_collected: false`.

## Safety Impact

The command is read-only. It does not mutate app repos, runtime roots, VPS
state, providers, GitHub, env values, secret values, probe state, receipts, or
deployment state.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_adoption -v`
- `python3 -m py_compile src/ophelia/adoption.py src/ophelia/commands/app.py src/ophelia/command_catalog.py tests/test_adoption.py`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Use adoption plans for `stillup` and `clearedtorun` only when their explicit
  retained-product adoption or deployment phase begins.
