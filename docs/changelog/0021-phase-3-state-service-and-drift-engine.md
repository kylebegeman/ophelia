---
id: 0021
title: Phase 3 state service and drift engine
date: 2026-06-22
status: landed
areas: [state, drift, api, lumen, command-catalog, docs, tests]
change_type: feature
commits: []
---

## Summary

Implemented Phase 3 of the strategic roadmap: a freshness-aware local state
service surface and structured drift reports.

## Why

Ophelia already had a SQLite runtime read-model and rendered-file drift checks,
but they were not yet product-level signals. Operators and agents need a clear
answer for whether local state is current, where drift exists, who owns it, and
which command should reconcile it.

## Changed Areas

- `src/ophelia/state_db.py`: bumped the state DB schema to version 2, added
  refresh metadata, freshness checks, `refresh_state`, `state_summary`, and
  index tables for observability runs, traffic state, provider snapshots, and
  GitHub provisioning receipts.
- `src/ophelia/commands/state.py`: added `ship state refresh` and
  `ship state summary`, with command-catalog descriptors.
- `src/ophelia/api.py` and `src/ophelia/api_routes.py`: added
  `GET /state/summary`.
- `src/ophelia/lumen_adapter.py`: exposed the state service in capabilities and
  dashboard payloads.
- `src/ophelia/drift.py`: added schema/kind/status/severity fields, bounded
  snapshots, severity-sorted findings, remediation commands, and plan
  candidates while preserving the existing rendered/release/env fields.
- `src/ophelia/command_catalog.py`: added examples for the new state-service
  commands.
- Tests updated in `tests/test_state_db.py`, `tests/test_drift.py`,
  `tests/test_lumen_adapter.py`, and `tests/test_json_output.py`.

## Contract Impact

Additive CLI/API contract:

- `ship state refresh --json` emits `kind: "ophelia.state_refresh"`.
- `ship state summary --json` emits `kind: "ophelia.state_summary"`.
- `GET /state/summary` returns the same read-only state summary from the local
  SQLite index.
- `ship drift <manifest> --json` now emits
  `kind: "ophelia.drift_report"` with `status`, `severity`, `snapshots`,
  `findings`, `remediation_commands`, and `plan_candidates`.
- `ship drift all --json` now emits `kind: "ophelia.drift_summary"` with
  aggregate severity and findings.

Existing drift callers can keep reading `drift`, `rendered`,
`release_metadata`, `env`, and `summary`.

## Safety Impact

Net positive. `state refresh` only writes the local SQLite index under the
runtime root. Drift reports are bounded and redact-safe; remediation commands
include paths and operation names, never secret values. Live GitHub/provider
API observations are not added in this phase. They are represented as
informational `not_observed` snapshots until later authenticated integration
work.

## Verification

- `python3 -m compileall -q src/ophelia tests/test_state_db.py tests/test_drift.py tests/test_lumen_adapter.py tests/test_json_output.py`
- `PYTHONPATH=src python3 -m unittest tests.test_state_db tests.test_drift tests.test_lumen_adapter tests.test_json_output tests.test_command_catalog`
- `PYTHONPATH=src python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `./cli/ship state refresh --runtime-root <tmp> --manifests-dir examples --json`
- `./cli/ship state summary --runtime-root <tmp> --json`
- `./cli/ship drift examples/portfolio.ophelia.yml --runtime-root <tmp> --json`
- `PYTHON=python3 make validate-examples`
- `git diff --check`

## Follow-Ups

- Begin Phase 4 of `docs/ophelia-strategic-implementation-roadmap.md`:
  resumable, receipt-backed workflow orchestration.
- Later GitHub App and provider phases should replace `not_observed` snapshot
  slots with authenticated live observations.
