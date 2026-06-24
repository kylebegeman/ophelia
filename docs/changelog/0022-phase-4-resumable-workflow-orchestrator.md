---
id: 0022
title: Phase 4 resumable workflow orchestrator
date: 2026-06-22
status: landed
areas: [workflows, state, command-catalog, docs, tests]
change_type: feature
commits: []
---

## Summary

Implemented Phase 4 of the strategic roadmap: resumable, receipt-backed
workflow orchestration with confirmation-gated mutating nodes.

## Why

Workflow graphs were useful for planning, preview, and read-only execution, but
they could not represent a real operator flow that pauses for approval, resumes
after a process restart, records per-node outputs, and stops dependents after a
failure. App moves, release checks, incident triage, GitHub provisioning, and
restore rehearsals need a durable workflow state model before later live
provider integrations can safely plug in.

## Changed Areas

- `src/ophelia/workflows.py`: added lifecycle statuses, workflow templates,
  per-node plan/receipt/artifact/blocker/warning/rollback fields,
  confirmation-gated mutating node execution, deterministic resume,
  pause/cancel controls, and policy checks before mutating nodes.
- `src/ophelia/commands/workflows.py`: added `ship workflow pause`,
  `ship workflow resume`, and `ship workflow cancel`; extended
  `workflow plan` inputs and `workflow run` with `--confirm-node`.
- `src/ophelia/state_db.py`: bumped the state schema to version 3 and indexed
  workflow graphs plus workflow nodes for the state summary read model.
- `src/ophelia/command_catalog.py`: added examples for workflow control and
  confirmation-gated resume flows.
- Docs updated in `README.md`, `docs/job-action-api.md`,
  `docs/ophelia-strategic-implementation-roadmap.md`, and
  `docs/product-improvement-findings.md`.
- Tests updated in `tests/test_workflows.py` and `tests/test_state_db.py`.

## Contract Impact

Additive CLI contract:

- `ship workflow plan <template> --app <app> ... --json` supports
  `move-app`, `incident-triage`, `release-readiness`, `github-provisioning`,
  and `restore-rehearsal`.
- `ship workflow run <workflow-ref> --preview --json` can preview the pause
  point for mutating nodes without executing anything.
- `ship workflow run <workflow-ref> --confirm-node NODE_ID=TOKEN --json`
  executes confirmed mutating nodes only for the supplied node ids.
- `ship workflow resume <workflow-ref> --confirm-node NODE_ID=TOKEN --json`
  skips succeeded nodes and continues from the stored graph.
- `ship workflow pause <workflow-ref> --json` and
  `ship workflow cancel <workflow-ref> --json` update local workflow state and
  write control receipts.
- `ship state summary --json` now includes workflow summaries and app-level
  workflow counts.

Workflow artifacts carry `workflow_status`, `node_counts`, lifecycle statuses,
parameters, and per-node status metadata. Workflow run receipts still use the
standard receipt envelope with bounded output summaries and redaction.

## Safety Impact

Net positive. Mutating workflow nodes do not run without an explicit
`--confirm-node NODE_ID=TOKEN` token. Tokens are supplied at execution time,
redacted through the existing deep-redaction path, and represented only as
applied node ids in receipts. Commands remain argv arrays with shell
metacharacter guards. Pause/cancel mutate only local workflow JSON and write
local receipts.

This phase does not add full live production provider integration. GitHub App,
secrets provider, and authenticated production value wiring remain later
phases.

## Verification

- `python3 -m compileall -q src/ophelia/workflows.py src/ophelia/commands/workflows.py src/ophelia/state_db.py tests/test_workflows.py tests/test_state_db.py`
- `PYTHONPATH=src python3 -m unittest tests.test_workflows tests.test_state_db tests.test_command_catalog`
- `PYTHONPATH=src python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `PYTHON=python3 make validate-examples`
- CLI smoke tests for `workflow plan`, `workflow run --preview`,
  `workflow pause`, `workflow cancel`, `state refresh`, and `state summary`.
- `git diff --check`

## Follow-Ups

- Begin Phase 5: GitHub App and secrets integrations.
- Later provider phases should replace remaining placeholder or local-only
  values with authenticated live observations and production-safe adapters.
