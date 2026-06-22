---
id: 0035
title: No-probe live gate
date: 2026-06-22
status: landed
areas: [live-hydration, cli, command-catalog, docs, tests]
change_type: feature
commits: []
---

## Summary

Added `ship live-hydration probe-gate`, a read-only go/no-go report that
combines hydration and evidence validation before any opt-in probes are run.

## Why

The live path needs a final explicit stop point between file-based evidence and
live checks. The gate tells operators when probes are still blocked and only
emits exact follow-up probe commands when no file-based blockers remain.

## Changed Areas

- `src/ophelia/live_hydration.py`: adds no-probe gate composition, compact child
  summaries, and explicit probe command construction.
- `src/ophelia/commands/live_hydration.py`: adds
  `ship live-hydration probe-gate`.
- `src/ophelia/command_catalog.py`: adds probe-gate examples.
- `Makefile`: adds `live-hydration-probe-gate-legacy-console-staging`.
- `tests/test_live_hydration.py`: covers current no-go Legacy Console staging, fixture
  review probe command emission, CLI output, and catalog visibility.
- README, live-hydration docs, roadmap, findings, and scratchpad docs updated.

## Contract Impact

Additive CLI contract:

- `ship live-hydration probe-gate --json`
- JSON kind: `ophelia.live_hydration_probe_gate`
- Operation: `live_hydration.probe_gate`

The report includes `go_no_go`, compact child summaries, `probes_executed:
false`, disabled `probe_policy`, and `probe_commands` only when there are no
blockers.

## Safety Impact

Net positive. The gate never runs emitted commands. It performs no HTTP/Docker
probes, provider calls, workflow execution, state refresh, runtime writes, or
production mutations.

## Verification

- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest tests.test_live_hydration`
- `make live-hydration-probe-gate-legacy-console-staging`
- `python3 -m compileall -q src/ophelia/live_hydration.py src/ophelia/commands/live_hydration.py src/ophelia/command_catalog.py`
- `python3 -m compileall -q src`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Collect one app's real file-based evidence and re-run the gate.
- Run emitted probe commands only after the gate moves out of `no_go` and the
  operator accepts the remaining review warnings.
