---
id: 0049
title: Remove private scratchpad notes
date: 2026-06-22
status: landed
areas: [open-source, docs, audit, tests]
change_type: cleanup
commits: []
---

## Summary

Removed tracked scratchpad notes that contained private live-readiness context
and updated durable docs to point to private operator notes instead.

## Why

Scratchpad files are temporary working material by convention. Keeping private
live evidence notes in the public repo conflicts with the open-source readiness
goal and made the public audit report avoidable blockers.

## Changed Areas

- `docs/scratchpad/*.md`: removed private temporary notes.
- `docs/live-hydration.md`: clarifies that private live snapshot records were
  removed and are not the forward model.
- `docs/ophelia-strategic-implementation-roadmap.md`: replaces scratchpad
  references with private operator-note language.
- `docs/changelog/0031-*`, `0038-*`, and `0039-*`: preserve the durable record
  while removing links to deleted scratchpad files.
- `src/ophelia/open_source_readiness.py`: skips tracked files that are missing
  from the current working tree, so deletion-in-progress does not crash audits.
- `tests/test_open_source_readiness.py`: covers staged-then-deleted tracked
  files in a temporary Git repo.

## Contract Impact

The open-source audit now reflects the current working tree when tracked files
have been deleted locally but not committed yet.

## Safety Impact

No runtime behavior changes. Removed docs were temporary notes only.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_open_source_readiness -v`
- `./cli/ship open-source audit --allow-blocked --max-findings 2000 --json`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Remaining open-source blockers are active private operational docs/manifests,
  private config, the private deploy workflow, governance files, and the final
  license decision.
