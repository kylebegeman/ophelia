---
id: 0017
title: Execution and observability roadmap items
date: 2026-06-21
status: landed
areas: [workflows, app-factory, github, lumen, observability, cli]
change_type: feature
commits: []
---

## Summary

Implemented the next roadmap slice: workflow graph execution for non-mutating
nodes, token-gated GitHub provisioning apply, populated Lumen traffic and
observability aggregates, and a cron-friendly observability schedule runner.

## Why

These surfaces were previously planned or placeholder-only. Agents and
operators can now execute safe workflow graph checks, provision GitHub resources
through an auditable apply path, inspect aggregate dashboard health, and run
observability sweeps from external schedulers.

## Changed Areas

- `src/ophelia/workflows.py`: added `run_workflow` with dependency ordering,
  placeholder substitution, non-mutating-node enforcement, and receipts.
- `src/ophelia/app_factory.py`: added GitHub provisioning plan/apply with typed
  `gh` argv arrays, confirmation tokens, stop-on-failure behavior, and receipts.
- `src/ophelia/lumen_adapter.py`: populated top-level traffic and observability
  aggregates from existing per-app contracts.
- `src/ophelia/observability.py`: added `observability_schedule_run` that writes
  timestamped run artifacts and `latest.json`.
- `src/ophelia/commands/*.py`: exposed the new CLI commands and catalog
  descriptors.
- `tests/`: added focused coverage for workflow execution, GitHub provisioning,
  Lumen aggregates, and scheduled observability.

## Contract Impact

New CLI commands:

- `ship workflow run <workflow_id> [--set TOKEN=VALUE ...]`
- `ship app github plan/apply`
- `ship observability schedule run`

`ophelia.lumen.dashboard` now returns populated `traffic_status` and
`observability` aggregate objects instead of `null`.

## Safety Impact

`workflow run` executes only stored nodes marked non-mutating, passes commands as
argv arrays, refuses unresolved placeholders, and stores no raw stdout/stderr.

`app github apply` mutates GitHub through `gh` only after a matching confirmation
token. It writes local receipts and stops on the first failed command, marking
remaining steps skipped.

`observability schedule run` writes only local run artifacts by default. HTTP and
Docker probes remain opt-in and timeout-bounded.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_workflows tests.test_app_factory`
- `PYTHONPATH=src python3 -m unittest tests.test_lumen_adapter tests.test_observability`
- `python3 -m compileall -q src`
- `PYTHONPATH=src python3 -m unittest discover -s tests`
- `git diff --check`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- CLI smoke: `ship app github plan --json`
- CLI smoke: `ship observability schedule run --json`
- CLI smoke: `ship workflow list --json`

## Follow-Ups

- None
