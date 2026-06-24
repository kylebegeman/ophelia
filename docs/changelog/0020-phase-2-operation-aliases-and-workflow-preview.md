---
id: 0020
title: Phase 2 operation aliases and workflow preview
date: 2026-06-22
status: landed
areas: [operation-refs, workflows, receipts, state, traffic, docs, tests]
change_type: feature
commits: []
---

## Summary

Implemented Phase 2 of the strategic roadmap: shared operation-reference aliases
and a no-execution workflow run preview.

## Why

Operators should not need to copy full operation IDs for common continuation
flows. At the same time, workflow execution needs a final resolved preview
between graph planning and running non-mutating nodes.

## Changed Areas

- `src/ophelia/operation_refs.py`: new shared resolver for receipt and workflow
  references. Supports exact IDs, paths, `latest`, `latest:<app>`,
  `latest:<operation>`, unambiguous prefixes, and explicit ambiguity blockers.
- `src/ophelia/workflows.py`: added `preview_workflow`, alias-aware
  `show_workflow` / `run_workflow`, shared node-command preflight, executable
  path reporting, and digest-backed `ophelia.workflow_preview` output.
- `src/ophelia/commands/workflows.py`: added `ship workflow run --preview` and a
  command-catalog descriptor for `workflow.preview`.
- `src/ophelia/commands/receipts.py`, `src/ophelia/commands/restore.py`,
  `src/ophelia/commands/state.py`, and `src/ophelia/commands/app.py`: wired
  aliases into receipt show, restore-drill show, state receipt query, and traffic
  rollback plan/apply.
- `src/ophelia/portability.py` and `src/ophelia/restore_verification.py`: path
  passthrough support for receipt and restore-drill show helpers.
- `src/ophelia/command_catalog.py`: examples now advertise alias and preview
  workflows.
- Tests added/updated in `tests/test_operation_refs.py`,
  `tests/test_workflows.py`, and `tests/test_state_db.py`.

## Contract Impact

Additive CLI/API contract:

- `ship workflow run <workflow-ref> --preview --json` emits
  `kind: "ophelia.workflow_preview"`.
- `ship workflow show|run` accept workflow aliases and paths.
- `ship receipts show`, `ship restore-drills show`, and
  `ship app traffic rollback plan|apply --receipt` accept receipt aliases and
  paths.
- `ship state query receipts --ref <alias>` filters to one resolved receipt.

Existing exact IDs and paths keep working. Ambiguous prefixes fail with
`operation_ref_ambiguous` and candidate IDs.

## Safety Impact

Net positive. Workflow preview never executes a node and never writes a receipt.
Traffic rollback resolves aliases before generating or checking confirmation
tokens, so apply cannot silently target a different receipt if `latest` changes.

## Verification

- `python3 -m compileall -q src/ophelia/operation_refs.py src/ophelia/workflows.py src/ophelia/commands/workflows.py src/ophelia/commands/receipts.py src/ophelia/commands/state.py src/ophelia/commands/app.py src/ophelia/commands/restore.py src/ophelia/state_db.py src/ophelia/portability.py src/ophelia/restore_verification.py`
- `PYTHONPATH=src python3 -m unittest tests.test_operation_refs tests.test_workflows tests.test_state_db tests.test_command_catalog`
- `PYTHONPATH=src python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `./cli/ship workflow run latest:demo-service --preview --runtime-root <tmp> --json`
- `./cli/ship commands catalog --json | python3 -m json.tool`
- `PYTHON=python3 make validate-examples`
- `git diff --check`

## Follow-Ups

- Begin Phase 3 of `docs/ophelia-strategic-implementation-roadmap.md`: durable
  state service and drift engine foundation.
